"""Multi-stage CSI Fall Detector integrating Sensitive Trigger, Rolling Event Buffer, 14D Feature Extractor, Pure-Python Classifier, and Post-impact Stillness Verification."""

from __future__ import annotations

import math
from typing import Any, Sequence

try:
    from .dsp import Thresholds
    from .dsp_features import RollingEventBuffer, SensitiveTrigger, extract_feature_vector
    from .classifier import FallClassifier, Prediction
    from .fsm import FallFSM, State
except ImportError:
    from dsp import Thresholds
    from dsp_features import RollingEventBuffer, SensitiveTrigger, extract_feature_vector
    from classifier import FallClassifier, Prediction
    from fsm import FallFSM, State


class FallDetector:
    """Multi-stage fall detector replacing 1D scalar burst with 14D classifier verification."""

    def __init__(
        self,
        thresholds: Thresholds,
        classifier: FallClassifier | None = None,
        *,
        target_hz: float = 100.0,
        trigger_motion: float = 0.095,
        stillness_duration_us: int = 2_500_000,
    ) -> None:
        self.thresholds = thresholds
        self.classifier = classifier or FallClassifier()
        self.target_hz = target_hz
        self.stillness_duration_us = stillness_duration_us

        self.fsm = FallFSM(thresholds)
        self.buffer = RollingEventBuffer(capacity=350, event_samples=250)
        self.trigger = SensitiveTrigger(trigger_motion=trigger_motion)

        self.last_update_us: int | None = None
        self.last_sample_us: int | None = None
        self.candidate_start_us: int | None = None
        self.peak_motion: float = 0.0
        self.peak_us: int | None = None

        self.classifier_evaluated: bool = False
        self.classifier_confirmed: bool = False
        self.last_prediction: Prediction | None = None
        self.last_features: list[float] | None = None

        self.still_start_us: int | None = None
        self.had_verified_stillness: bool = False
        self.movement_start_us: int | None = None

    @property
    def state(self) -> State:
        return self.fsm.state

    @property
    def last_note(self) -> str | None:
        return self.fsm.last_note

    def _clear_candidate_timers(self) -> None:
        self.candidate_start_us = None
        self.peak_motion = 0.0
        self.peak_us = None
        self.classifier_evaluated = False
        self.classifier_confirmed = False
        self.still_start_us = None
        self.had_verified_stillness = False
        self.movement_start_us = None
        self.trigger.reset()

    def reset(self) -> State:
        self._clear_candidate_timers()
        self.buffer.clear()
        self.fsm.reset()
        self.last_prediction = None
        self.last_features = None
        return self.fsm.state

    def ack_alert(self) -> State:
        if self.fsm.state is State.FALL_ALERT:
            self.reset()
        return self.fsm.state

    def update(
        self,
        now_us: int,
        motion: float | None,
        amps: Sequence[float] | None = None,
        *,
        burst_ratio: float | None = None,
        is_empty_room: bool | None = None,
        motion_slope: float | None = None,
    ) -> State:
        """Process incoming frame through the multi-stage detection pipeline."""
        if (
            now_us < 0
            or (motion is not None and (not math.isfinite(motion) or motion < 0))
            or (burst_ratio is not None and (not math.isfinite(burst_ratio) or not 0 <= burst_ratio <= 1))
            or (is_empty_room is not None and type(is_empty_room) is not bool)
        ):
            raise ValueError("invalid time or motion")

        # Clock jump backward check
        if self.last_update_us is not None and now_us <= self.last_update_us:
            if now_us == self.last_update_us:
                return self.fsm.state
            self._clear_candidate_timers()
            self.buffer.clear()
            self.last_sample_us = None
            self.last_update_us = now_us
            if self.fsm.state is not State.FALL_ALERT:
                self.fsm.state = State.UNKNOWN if motion is None else State.WARMUP
            return self.fsm.state

        self.last_update_us = now_us

        if self.fsm.state is State.FALL_ALERT:
            return self.fsm.state

        # CSI timeout (> 500ms)
        if self.last_sample_us is not None and now_us - self.last_sample_us > 500_000:
            self._clear_candidate_timers()
            self.buffer.clear()
            self.fsm.state = State.UNKNOWN
            if motion is None:
                return self.fsm.state

        if motion is None:
            self.still_start_us = None
            return self.fsm.state

        self.last_sample_us = now_us

        # Append to rolling event buffer
        if amps is not None and len(amps) == 56:
            self.buffer.push(now_us, motion, amps)

        # FSM Pipeline
        if self.fsm.state is State.CANDIDATE:
            assert self.candidate_start_us is not None

            # 1. Empty room fingerprint veto
            if is_empty_room is True:
                self._clear_candidate_timers()
                self.fsm.last_note = "EXITED_ROOM"
                self.fsm.state = State.NORMAL
                return self.fsm.state

            # 2. Track peak motion during event
            if motion > self.peak_motion:
                self.peak_motion = motion
                self.peak_us = now_us

            # 3. Classifier evaluation once 1.5s post-impact gathered
            if not self.classifier_evaluated and self.peak_us is not None:
                elapsed_peak = now_us - self.peak_us
                is_ready = elapsed_peak >= 1_500_000

                if is_ready:
                    self.classifier_evaluated = True
                    window = self.buffer.get_event_window(center_us=self.peak_us, pre_samples=100, post_samples=150)
                    if window is not None:
                        win_m, win_A, win_pk = window
                        feats = extract_feature_vector(win_m, win_A, win_pk, target_hz=self.target_hz)
                        self.last_features = feats
                        pred = self.classifier.predict(feats)
                        self.last_prediction = pred

                        if pred.is_fall:
                            self.classifier_confirmed = True
                            self.fsm.last_note = f"CLASSIFIER_CONFIRMED: {pred.reason}"
                        else:
                            # CLASSIFIER VETO: Immediate rejection!
                            self.fsm.last_note = f"CLASSIFIER_VETO: {pred.reason}"
                            self._clear_candidate_timers()
                            self.fsm.state = State.MOVING if motion >= self.thresholds.move else State.NORMAL
                            return self.fsm.state

            # 4. Timeout after 8s
            if now_us - self.candidate_start_us > 8_000_000:
                self._clear_candidate_timers()
                self.fsm.state = State.MOVING if motion >= self.thresholds.move else State.NORMAL
                return self.fsm.state

            # 5. Stillness tracking:
            # When classifier has confirmed a fall, stillness requires staying below move threshold
            still_limit = self.thresholds.move if self.classifier_confirmed else self.thresholds.still
            if motion < still_limit:
                if self.still_start_us is None:
                    self.still_start_us = now_us
                if now_us - self.still_start_us >= 500_000:
                    self.had_verified_stillness = True
                self.movement_start_us = None

                # Fall Alert confirmation: Requires stillness >= 2.5s AND classifier confirmation!
                if now_us - self.still_start_us >= self.stillness_duration_us:
                    if self.classifier_confirmed:
                        self.fsm.state = State.FALL_ALERT
                        return self.fsm.state
                    elif self.classifier_evaluated and not self.classifier_confirmed:
                        self._clear_candidate_timers()
                        self.fsm.state = State.NORMAL
                        return self.fsm.state
            else:
                self.still_start_us = None

            # 6. Sustained Movement Cancellation: active only after classifier evaluation or verified stillness
            movement_gate_open = self.classifier_evaluated or self.had_verified_stillness
            if movement_gate_open and motion >= self.thresholds.move:
                if self.movement_start_us is None:
                    self.movement_start_us = now_us
                if now_us - self.movement_start_us >= 300_000:
                    self._clear_candidate_timers()
                    self.fsm.state = State.MOVING
                    return self.fsm.state
            else:
                self.movement_start_us = None

            return self.fsm.state

        # Outside CANDIDATE: sensitive trigger
        if self.trigger.check(now_us, motion, motion_slope):
            self.fsm.state = State.CANDIDATE
            self.candidate_start_us = now_us
            self.peak_motion = motion
            self.peak_us = now_us
            self.classifier_evaluated = False
            self.classifier_confirmed = False
            self.still_start_us = None
            self.had_verified_stillness = False
            self.movement_start_us = None
            self.fsm.last_note = None
        else:
            self.fsm.state = State.MOVING if motion >= self.thresholds.move else State.NORMAL

        return self.fsm.state
