"""HT20 CSI amplitude and causal short-window motion feature."""

from collections import deque
from dataclasses import dataclass
import math
import statistics
from typing import Final, Sequence

VALID_BINS: Final = tuple(range(4, 32)) + tuple(range(33, 61))


def normalize(values: Sequence[float], epsilon: float = 1e-6) -> tuple[float, ...]:
    if len(values) != 56 or not all(math.isfinite(v) and v >= 0 for v in values):
        raise ValueError("expected 56 finite nonnegative amplitudes")
    mean = sum(values) / 56
    return tuple(v / max(mean, epsilon) for v in values)


def amplitudes(pairs: Sequence[tuple[int, int]], *, first_word_invalid: bool = False) -> list[float]:
    """Extract the 56 occupied HT20 bins from 64 or 128 FFT-order imaginary/real pairs.

    For 128 pairs, HT-LTF (pairs 64..127) is selected for higher SNR.
    The first invalid word (bins 0 and 1) is outside the occupied band;
    never concatenate multiple LTFs as one vector.
    """
    if len(pairs) == 128:
        selected = pairs[64:128]
    elif len(pairs) == 64:
        selected = pairs[:64]
    else:
        raise ValueError(f"expected 64 or 128 pairs, got {len(pairs)}")
    return [math.hypot(*selected[index]) for index in VALID_BINS]


@dataclass(frozen=True, slots=True)
class Thresholds:
    still: float
    move: float
    spike: float
    candidate_spike: float = 0.160
    burst: float = 0.40
    empty: float = 3.0

    def __post_init__(self) -> None:
        if not (0 <= self.still < self.move < self.spike and
                all(math.isfinite(value) for value in (self.still, self.move, self.spike))
                and math.isfinite(self.candidate_spike) and self.move < self.candidate_spike <= self.spike
                and math.isfinite(self.burst) and 0 <= self.burst <= 1
                and math.isfinite(self.empty) and self.empty > 0):
            raise ValueError("thresholds must be finite, nonnegative and strictly ordered")


class MotionDSP:
    """Return a normalized, population-stddev motion index, or None if invalid."""

    def __init__(self, baseline: Sequence[float], *, target_hz: float = 100,
                 window_us: int = 200_000, epsilon: float = 1e-6) -> None:
        if (len(baseline) != 56 or not all(math.isfinite(v) and v >= 0 for v in baseline)
                or not math.isfinite(target_hz) or target_hz <= 0 or window_us <= 0 or epsilon <= 0):
            raise ValueError("invalid baseline or sampling settings")
        self.baseline = tuple(max(v, epsilon) for v in baseline)
        self.epsilon = epsilon
        self.window_us = window_us
        self.min_samples = math.ceil(target_hz * window_us / 1_000_000 * 0.5)
        self.samples: deque[tuple[int, tuple[float, ...]]] = deque()
        self.last_us: int | None = None
        self.energy: deque[tuple[int, float]] = deque()
        self.motion_history: deque[tuple[int, float]] = deque()
        self.burst_ratio: float | None = None
        self.motion_slope: float | None = None
        self.empty_baseline: tuple[tuple[float, ...], tuple[float, ...]] | None = None

    def push_motion_energy(self, capture_us: int, motion: float, activity: float | None = None, *, noise_floor: float = 0.0) -> None:
        """Rolling 1 s concentration of mean-square activity in any 200 ms interval."""
        if capture_us < 0 or not math.isfinite(motion) or motion < 0:
            raise ValueError("invalid motion energy sample")
        if not math.isfinite(noise_floor) or noise_floor < 0:
            raise ValueError("invalid noise floor")
        if self.energy and capture_us <= self.energy[-1][0]:
            self.energy.clear(); self.motion_history.clear()
        if self.energy and capture_us - self.energy[-1][0] > 300_000:
            self.energy.clear(); self.motion_history.clear()
        self.energy.append((capture_us, motion))
        self.motion_history.append((capture_us, motion if activity is None else activity))
        while self.energy and self.energy[0][0] <= capture_us - 1_000_000:
            self.energy.popleft()
        while len(self.motion_history) > 2 and self.motion_history[1][0] <= capture_us - 100_000:
            self.motion_history.popleft()
        prior = self.motion_history[0]
        self.motion_slope = ((motion if activity is None else activity) - prior[1]) / ((capture_us - prior[0]) / 1e6) if prior[0] < capture_us else None
        energies = list(self.energy)
        total = sum(value for _, value in energies)
        if (capture_us - self.energy[0][0] >= 800_000) and (total > max(self.epsilon, len(self.energy) * noise_floor)):
            best = 0.0
            for index, (start, _) in enumerate(energies):
                best = max(best, sum(value for t, value in energies[index:] if t < start + 200_000))
            self.burst_ratio = best / (total + self.epsilon)
        else:
            self.burst_ratio = None

    def set_empty_baseline(self, rows: Sequence[Sequence[float]]) -> None:
        """Only explicit empty-room samples may set the fingerprint."""
        if len(rows) < 2:
            raise ValueError("need at least two empty-room frames")
        shaped = [tuple(math.log(max(v, self.epsilon)) for v in normalize(row, self.epsilon)) for row in rows]
        center = tuple(statistics.median(row[i] for row in shaped) for i in range(56))
        # ponytail: 0.05 log-amplitude floor limits noise amplification; tune on measured empty-room captures.
        scale = tuple(max(1.4826 * statistics.median(abs(row[i] - center[i]) for row in shaped), 0.05) for i in range(56))
        self.empty_baseline = center, scale

    def get_empty_baseline(self) -> tuple[tuple[float, ...], tuple[float, ...]] | None:
        return self.empty_baseline

    def spectral_distance(self, current_amps: Sequence[float]) -> float | None:
        shaped = tuple(math.log(max(v, self.epsilon)) for v in normalize(current_amps, self.epsilon))
        if self.empty_baseline is None:
            return None
        center, scale = self.empty_baseline
        return statistics.median(abs(v - c) / s for v, c, s in zip(shaped, center, scale))

    def push(self, values: Sequence[float], capture_us: int) -> float | None:
        """Consume 56 occupied-bin amplitudes with monotonically increasing timestamps."""
        if len(values) != 56 or not all(math.isfinite(v) and v >= 0 for v in values):
            raise ValueError("expected 56 finite nonnegative amplitudes")
        if capture_us < 0:
            raise ValueError("negative timestamp")
        if self.last_us is not None and capture_us <= self.last_us:
            self.samples.clear()
            self.last_us = capture_us
            return None
        if self.last_us is not None and capture_us - self.last_us > 300_000:
            self.samples.clear()
        self.last_us = capture_us
        self.samples.append((capture_us, tuple(v / b - 1.0 for v, b in zip(normalize(values, self.epsilon), self.baseline))))
        while self.samples and self.samples[0][0] < capture_us - self.window_us:
            self.samples.popleft()
        if len(self.samples) < self.min_samples:
            return None
        return statistics.median(statistics.pstdev(row[k] for _, row in self.samples)
                                 for k in range(56))

    def current(self, now_us: int) -> float | None:
        """Return None on stale CSI; no cached motion value implies stillness."""
        if self.last_us is None or now_us - self.last_us > 300_000:
            return None
        if len(self.samples) < self.min_samples:
            return None
        return statistics.median(statistics.pstdev(row[k] for _, row in self.samples)
                                 for k in range(56))


class Calibrator:
    """Calibrate baseline from still CSI, then thresholds from still/walk motion."""

    @staticmethod
    def baseline(still_amplitudes: Sequence) -> tuple[float, ...]:
        if not still_amplitudes:
            raise ValueError("need nonempty valid still CSI")
        rows = [normalize(row[1] if isinstance(row, tuple) and len(row) == 2 and isinstance(row[0], int) else row)
                for row in still_amplitudes]
        return tuple(statistics.median(row[k] for row in rows) for k in range(56))

    @staticmethod
    def from_motion(still: Sequence[float], walk: Sequence[float], *, previous: Thresholds | None = None) -> Thresholds:
        if not still or not walk or not all(math.isfinite(v) and v >= 0 for v in (*still, *walk)):
            raise ValueError("need nonempty finite still and walk motion samples")
        mu = statistics.median(still)
        sigma = max(1.4826 * statistics.median(abs(v - mu) for v in still), 0.005)
        ordered = sorted(walk)
        index = 0.95 * (len(ordered) - 1)
        lower = math.floor(index)
        p95 = ordered[lower] + (index - lower) * (ordered[math.ceil(index)] - ordered[lower])
        still_limit = mu + 3 * sigma
        move_limit = mu + 6 * sigma

        candidate_spike = 0.160 if (previous is None or previous.candidate_spike is None) else previous.candidate_spike
        burst = 0.40 if previous is None else previous.burst
        empty = 3.0 if previous is None else previous.empty

        if move_limit >= candidate_spike:
            raise ValueError(f"move threshold ({move_limit:.4f}) must be less than candidate_spike ({candidate_spike:.4f})")
        spike_limit = max(move_limit + 0.02, mu + 12 * sigma, 1.3 * p95, candidate_spike)
        return Thresholds(still_limit, move_limit, spike_limit, candidate_spike=candidate_spike, burst=burst, empty=empty)
