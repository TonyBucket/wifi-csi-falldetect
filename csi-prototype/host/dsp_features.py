"""Multi-stage CSI feature extraction, rolling event buffer, and sensitive trigger."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
from typing import Final, Sequence

import numpy as np
from scipy import stats

FEATURE_NAMES: Final[tuple[str, ...]] = (
    "peak_energy",
    "excess_energy",
    "rise_time",
    "decline_time",
    "pdr",
    "rise_decline_ratio",
    "pc1_variance_ratio",
    "pc2_variance_ratio",
    "effective_rank",
    "hler",
    "torso_energy_ratio",
    "spectral_entropy",
    "envelope_kurtosis",
    "post_pre_energy_ratio",
)


def extract_feature_vector(
    motions: Sequence[float],
    amps: Sequence[Sequence[float]],
    peak_idx: int | None = None,
    *,
    target_hz: float = 100.0,
) -> list[float]:
    """Extract a 14D normalized feature vector from a CSI event window.

    Window convention: approximately 250 samples (~2.5s @ 100 Hz),
    typically 100 samples pre-impact and 150 samples post-impact.
    """
    m = np.asarray(motions, dtype=np.float64)
    A = np.asarray(amps, dtype=np.float64)
    N = len(m)
    if N == 0:
        return [0.0] * len(FEATURE_NAMES)
    if A.ndim != 2 or A.shape[0] != N or A.shape[1] != 56:
        # Pad or reshape safely if needed
        if A.ndim == 2 and A.shape[0] == N:
            pass
        else:
            A = np.zeros((N, 56), dtype=np.float64)

    # 1. peak_energy
    if peak_idx is None or not (0 <= peak_idx < N):
        peak_idx = int(np.argmax(m))
    peak_energy = float(m[peak_idx])

    # Baseline: median of lowest 20% samples in the window
    sorted_m = np.sort(m)
    base_count = max(1, N // 5)
    m_base = float(np.median(sorted_m[:base_count]))

    # 2. excess_energy: integral above baseline
    excess_energy = float(np.sum(np.maximum(0.0, m - m_base)))

    # 3. rise_time (ms)
    rise_thresh = m_base + 0.10 * max(0.0, peak_energy - m_base)
    rise_start = 0
    for i in range(peak_idx, -1, -1):
        if m[i] <= rise_thresh:
            rise_start = i
            break
    dt_ms = 1000.0 / max(target_hz, 1.0)
    rise_time_ms = float((peak_idx - rise_start) * dt_ms)

    # 4. decline_time (ms)
    decline_thresh = m_base + 0.20 * max(0.0, peak_energy - m_base)
    decline_end = N - 1
    for i in range(peak_idx, N):
        if m[i] <= decline_thresh:
            decline_end = i
            break
    decline_time_ms = float((decline_end - peak_idx) * dt_ms)

    # 5. pdr (Power Decline Ratio in 100ms after peak)
    pdr_samples = int(round(0.100 * target_hz))
    pdr_idx = min(N - 1, peak_idx + max(1, pdr_samples))
    pdr = float((peak_energy - m[pdr_idx]) / max(peak_energy, 1e-6))

    # 6. rise_decline_ratio
    rise_decline_ratio = float(rise_time_ms / max(decline_time_ms, 1.0))

    # PCA on 56 subcarriers in impact window (+/- 0.5s around peak)
    pca_half = int(round(0.500 * target_hz))
    pca_start = max(0, peak_idx - pca_half)
    pca_end = min(N, peak_idx + pca_half + 1)
    sub_amps = A[pca_start:pca_end]

    if len(sub_amps) >= 2:
        cov = np.cov(sub_amps, rowvar=False)
        eigvals = np.linalg.eigvalsh(cov)
        eigvals = np.sort(np.maximum(0.0, eigvals))[::-1]
        sum_eig = np.sum(eigvals)
        if sum_eig > 1e-12:
            p_eig = eigvals / sum_eig
            pc1 = float(p_eig[0])
            pc2 = float(p_eig[1]) if len(p_eig) > 1 else 0.0
            # 9. effective_rank: exp(- sum p_i ln(p_i))
            nz = p_eig[p_eig > 1e-12]
            effective_rank = float(np.exp(-np.sum(nz * np.log(nz))))
        else:
            pc1, pc2, effective_rank = 1.0, 0.0, 1.0
    else:
        pc1, pc2, effective_rank = 1.0, 0.0, 1.0

    # FFT frequency domain features on motion envelope
    m_detrend = m - np.mean(m)
    fft_vals = np.abs(np.fft.rfft(m_detrend))
    freqs = np.fft.rfftfreq(N, d=1.0 / max(target_hz, 1.0))
    psd = fft_vals ** 2

    # 10. hler: High (15-35 Hz) to Low (1-10 Hz) Energy Ratio
    high_mask = (freqs >= 15.0) & (freqs <= 35.0)
    low_mask = (freqs >= 1.0) & (freqs <= 10.0)
    e_high = float(np.sum(psd[high_mask]))
    e_low = float(np.sum(psd[low_mask]))
    hler = float(e_high / max(e_low, 1e-9))

    # 11. torso_energy_ratio: (5-25 Hz band ratio / 1-45 Hz dynamic band)
    torso_mask = (freqs >= 5.0) & (freqs <= 25.0)
    dyn_mask = (freqs >= 1.0) & (freqs <= 45.0)
    e_torso = float(np.sum(psd[torso_mask]))
    e_dyn = float(np.sum(psd[dyn_mask]))
    torso_energy_ratio = float(e_torso / max(e_dyn, 1e-9))

    # 12. spectral_entropy
    psd_dyn = psd[dyn_mask]
    sum_dyn = np.sum(psd_dyn)
    if sum_dyn > 1e-12 and len(psd_dyn) > 1:
        p_dyn = psd_dyn / sum_dyn
        nz = p_dyn[p_dyn > 1e-12]
        spectral_entropy = float(-np.sum(nz * np.log2(nz)) / np.log2(len(psd_dyn)))
    else:
        spectral_entropy = 0.0

    # 13. envelope_kurtosis
    std_m = float(np.std(m))
    if std_m > 1e-6:
        envelope_kurtosis = float(np.mean(((m - np.mean(m)) / std_m) ** 4))
    else:
        envelope_kurtosis = 0.0

    # 14. post_pre_energy_ratio: 1.5s post-impact vs 1.0s pre-impact
    pre_samples = m[max(0, peak_idx - int(1.0 * target_hz)):peak_idx]
    post_samples = m[peak_idx:min(N, peak_idx + int(1.5 * target_hz))]
    e_pre = float(np.mean(pre_samples ** 2)) if len(pre_samples) > 0 else 1e-6
    e_post = float(np.mean(post_samples ** 2)) if len(post_samples) > 0 else 1e-6
    post_pre_energy_ratio = float(e_post / max(e_pre, 1e-6))

    vector = [
        peak_energy,
        excess_energy,
        rise_time_ms,
        decline_time_ms,
        pdr,
        rise_decline_ratio,
        pc1,
        pc2,
        effective_rank,
        hler,
        torso_energy_ratio,
        spectral_entropy,
        envelope_kurtosis,
        post_pre_energy_ratio,
    ]

    # Sanitize NaN/Inf
    return [0.0 if not math.isfinite(val) else float(val) for val in vector]


class RollingEventBuffer:
    """Rolling buffer storing CSI frames for feature extraction on event trigger."""

    def __init__(self, capacity: int = 350, *, event_samples: int = 250) -> None:
        self.capacity = capacity
        self.event_samples = event_samples
        self.timestamps: deque[int] = deque(maxlen=capacity)
        self.motions: deque[float] = deque(maxlen=capacity)
        self.amplitudes: deque[tuple[float, ...]] = deque(maxlen=capacity)
        self.last_us: int | None = None

    def clear(self) -> None:
        self.timestamps.clear()
        self.motions.clear()
        self.amplitudes.clear()
        self.last_us = None

    def push(self, capture_us: int, motion: float, amps: Sequence[float]) -> None:
        """Add sample; handles clock jumps and large gaps gracefully."""
        if self.last_us is not None:
            if capture_us <= self.last_us or capture_us - self.last_us > 500_000:
                self.clear()
        self.last_us = capture_us
        self.timestamps.append(capture_us)
        self.motions.append(motion)
        self.amplitudes.append(tuple(amps))

    def __len__(self) -> int:
        return len(self.timestamps)

    def get_event_window(
        self,
        center_us: int | None = None,
        *,
        pre_samples: int = 100,
        post_samples: int = 150,
    ) -> tuple[np.ndarray, np.ndarray, int] | None:
        """Extract an event window of approximately 250 samples around peak.

        Returns (motions_array, amps_matrix, peak_index_in_window) or None if insufficient.
        """
        total_len = pre_samples + post_samples
        if len(self.motions) < 30:
            return None

        m_arr = np.array(self.motions, dtype=np.float64)
        A_arr = np.array(self.amplitudes, dtype=np.float64)
        t_arr = np.array(self.timestamps, dtype=np.int64)

        if center_us is not None:
            center_idx = int(np.argmin(np.abs(t_arr - center_us)))
            search_start = max(0, center_idx - 50)
            search_end = min(len(m_arr), center_idx + 50)
            peak_idx = search_start + int(np.argmax(m_arr[search_start:search_end]))
        else:
            peak_idx = int(np.argmax(m_arr))

        start = max(0, peak_idx - pre_samples)
        end = min(len(m_arr), start + total_len)
        if end - start < total_len and len(m_arr) >= total_len:
            start = max(0, end - total_len)

        win_m = m_arr[start:end]
        win_A = A_arr[start:end]
        win_peak = peak_idx - start

        if len(win_m) < 20:
            return None
        return win_m, win_A, win_peak


@dataclass(slots=True)
class SensitiveTrigger:
    """Detects rapid energy change or sudden motion bursts without requiring sustained 80ms spike."""

    trigger_motion: float = 0.095
    is_triggered: bool = False
    trigger_us: int | None = None
    post_impact_needed_us: int = 1_500_000

    def check(self, capture_us: int, motion: float, motion_slope: float | None = None) -> bool:
        """Return True on newly fired trigger."""
        if self.is_triggered:
            return False

        fired = False
        if motion >= self.trigger_motion:
            fired = True
        elif motion_slope is not None and motion_slope > 0.40 and motion >= 0.075:
            fired = True

        if fired:
            self.is_triggered = True
            self.trigger_us = capture_us
            return True
        return False

    def is_window_ready(self, now_us: int) -> bool:
        """Check if post-impact collection (1.5s) is satisfied."""
        if not self.is_triggered or self.trigger_us is None:
            return False
        return (now_us - self.trigger_us) >= self.post_impact_needed_us

    def reset(self) -> None:
        self.is_triggered = False
        self.trigger_us = None
