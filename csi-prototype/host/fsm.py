"""Causal motion-index fall detector; all times use the same monotonic clock."""

from enum import Enum
import math

try:
    from .dsp import Thresholds
except ImportError:
    from dsp import Thresholds


class State(str, Enum):
    WARMUP = "WARMUP"
    NORMAL = "NORMAL"
    MOVING = "MOVING"
    CANDIDATE = "CANDIDATE"
    FALL_ALERT = "FALL_ALERT"
    UNKNOWN = "UNKNOWN"


class FallFSM:
    """Treat invalid samples and time gaps as interruptions, never as stillness."""

    def __init__(self, thresholds: Thresholds | None = None) -> None:
        self.thresholds = thresholds
        self.state = State.WARMUP
        self.last_sample_us: int | None = None
        self.last_update_us: int | None = None
        self.spike_start_us: int | None = None
        self.candidate_start_us: int | None = None
        self.still_start_us: int | None = None
        self.movement_start_us: int | None = None
        self.had_verified_stillness: bool = False
        self.candidate_burst: float | None = None
        self.last_note: str | None = None

    def _clear_timers(self) -> None:
        self.spike_start_us = None
        self.candidate_start_us = None
        self.still_start_us = None
        self.movement_start_us = None
        self.had_verified_stillness = False
        self.candidate_burst = None

    def reset(self) -> State:
        """Reset all candidate evidence, timers, streaks and return to quiescent state."""
        self._clear_timers()
        self.state = State.NORMAL if self.thresholds is not None else State.WARMUP
        return self.state

    def ack_alert(self) -> State:
        """Clear a latched alert only on explicit acknowledgment."""
        if self.state is State.FALL_ALERT:
            self.reset()
        return self.state

    def update(self, now_us: int, motion: float | None, burst_ratio: float | None = None,
               is_empty_room: bool | None = None) -> State:
        """Process a motion sample (None means invalid), or a timeout tick."""
        if (now_us < 0 or (motion is not None and (not math.isfinite(motion) or motion < 0))
                or (burst_ratio is not None and (not math.isfinite(burst_ratio) or not 0 <= burst_ratio <= 1))
                or (is_empty_room is not None and type(is_empty_room) is not bool)):
            raise ValueError("invalid time or motion")
        if self.last_update_us is not None and now_us <= self.last_update_us:
            if now_us == self.last_update_us:
                return self.state
            # Clock jump backward (reconnect, board reboot, timer wrap):
            self._clear_timers()
            self.last_sample_us = None
            self.last_update_us = now_us
            if self.state is not State.FALL_ALERT:
                self.state = State.UNKNOWN if motion is None else State.WARMUP
            return self.state
        self.last_update_us = now_us
        if self.state is State.FALL_ALERT:
            return self.state
        if self.last_sample_us is not None and now_us - self.last_sample_us > 500_000:
            self._clear_timers()
            self.state = State.UNKNOWN
            if motion is None:
                return self.state
        if motion is None:
            self.spike_start_us = None
            self.still_start_us = None
            return self.state
        self.last_sample_us = now_us
        if self.thresholds is None:
            self._clear_timers()
            self.state = State.WARMUP
            return self.state

        if self.state is State.CANDIDATE:
            assert self.candidate_start_us is not None
            if is_empty_room is True:
                self._clear_timers()
                self.last_note = "EXITED_ROOM"
                self.state = State.NORMAL
                return self.state

            # Thu nhận burst: chỉ cập nhật trong 800ms grace window, sau 800ms đóng băng
            if burst_ratio is not None and now_us - self.candidate_start_us <= 800_000:
                self.candidate_burst = max(self.candidate_burst or 0, burst_ratio)

            # Timeout: hủy sau 8s
            if now_us - self.candidate_start_us > 8_000_000:
                self.reset()
                self.state = State.MOVING if motion >= self.thresholds.move else State.NORMAL
                return self.state

            # Stillness tracking
            if motion < self.thresholds.still:
                if self.still_start_us is None:
                    self.still_start_us = now_us
                if now_us - self.still_start_us >= 500_000:
                    self.had_verified_stillness = True
                self.movement_start_us = None

                # Xác nhận Fall Alert (Fail-closed)
                if now_us - self.still_start_us >= 3_000_000:
                    if self.candidate_burst is not None and self.candidate_burst >= self.thresholds.burst:
                        self.state = State.FALL_ALERT
                    else:
                        self.reset()
                        self.state = State.NORMAL
                    return self.state
            else:
                self.still_start_us = None

            # Sustained Movement Cancellation
            movement_gate_open = (now_us - self.candidate_start_us >= 800_000) or self.had_verified_stillness
            if movement_gate_open and motion >= self.thresholds.move:
                if self.movement_start_us is None:
                    self.movement_start_us = now_us
                if now_us - self.movement_start_us >= 300_000:
                    self.reset()
                    self.state = State.MOVING
                    return self.state
            else:
                self.movement_start_us = None

            return self.state

        # Ngoài CANDIDATE: xét chuyển sang CANDIDATE hoặc MOVING/NORMAL
        cand_spike_thresh = (self.thresholds.candidate_spike if self.thresholds.candidate_spike is not None else self.thresholds.spike)
        if motion >= cand_spike_thresh:
            if self.spike_start_us is None:
                self.spike_start_us = now_us
            if now_us - self.spike_start_us >= 80_000:
                self.candidate_start_us = now_us
                self.still_start_us = None
                self.movement_start_us = None
                self.had_verified_stillness = False
                self.candidate_burst = burst_ratio
                self.last_note = None
                self.state = State.CANDIDATE
            else:
                self.state = State.MOVING
        else:
            self.spike_start_us = None
            self.state = State.MOVING if motion >= self.thresholds.move else State.NORMAL

        return self.state
