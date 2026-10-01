"""Unit tests for dsp_features module."""

import math
import numpy as np
import pytest

from dsp_features import (
    FEATURE_NAMES,
    RollingEventBuffer,
    SensitiveTrigger,
    extract_feature_vector,
)


def test_feature_names_length_and_order():
    assert len(FEATURE_NAMES) == 14
    expected = (
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
    assert FEATURE_NAMES == expected


def test_extract_feature_vector_finite_values():
    np.random.seed(42)
    m = np.abs(np.sin(np.linspace(0, 4 * np.pi, 250)) * 0.15)
    A = np.random.uniform(0.5, 2.0, (250, 56))
    features = extract_feature_vector(m, A, peak_idx=60)
    assert len(features) == 14
    for val in features:
        assert math.isfinite(val)
        assert not math.isnan(val)


def test_extract_feature_vector_edge_cases():
    # Empty
    empty_feats = extract_feature_vector([], [])
    assert len(empty_feats) == 14
    assert empty_feats == [0.0] * 14

    # All zeros
    m_zero = np.zeros(100)
    A_zero = np.zeros((100, 56))
    feats_zero = extract_feature_vector(m_zero, A_zero, peak_idx=10)
    assert len(feats_zero) == 14
    for val in feats_zero:
        assert math.isfinite(val)

    # Constant signal
    m_const = np.full(250, 0.05)
    A_const = np.ones((250, 56))
    feats_const = extract_feature_vector(m_const, A_const, peak_idx=50)
    assert len(feats_const) == 14
    assert feats_const[0] == 0.05


def test_rolling_event_buffer_push_and_window():
    buf = RollingEventBuffer(capacity=350, event_samples=250)
    assert len(buf) == 0

    # Push 300 samples with peak at index 120
    for i in range(300):
        t_us = i * 10_000
        m = 0.03 + (0.15 if i == 120 else 0.0)
        amps = [1.0] * 56
        buf.push(t_us, m, amps)

    assert len(buf) == 300
    res = buf.get_event_window(center_us=120 * 10_000, pre_samples=100, post_samples=150)
    assert res is not None
    win_m, win_A, win_pk = res
    assert len(win_m) == 250
    assert win_A.shape == (250, 56)
    assert win_pk == 100


def test_rolling_event_buffer_clock_gap_clears():
    buf = RollingEventBuffer(capacity=350)
    buf.push(100_000, 0.05, [1.0] * 56)
    buf.push(200_000, 0.05, [1.0] * 56)
    assert len(buf) == 2

    # Clock jump backward
    buf.push(150_000, 0.05, [1.0] * 56)
    assert len(buf) == 1

    # Large gap (> 500ms)
    buf.push(1_000_000, 0.05, [1.0] * 56)
    assert len(buf) == 1


def test_sensitive_trigger_behavior():
    trig = SensitiveTrigger(trigger_motion=0.095)
    assert not trig.is_triggered

    # Low motion does not trigger
    assert not trig.check(100_000, 0.05)
    assert not trig.is_triggered

    # High motion triggers immediately without requiring 80ms streak
    assert trig.check(110_000, 0.10)
    assert trig.is_triggered
    assert trig.trigger_us == 110_000

    # Ready check
    assert not trig.is_window_ready(110_000 + 1_000_000)
    assert trig.is_window_ready(110_000 + 1_500_000)

    trig.reset()
    assert not trig.is_triggered
    assert trig.trigger_us is None
