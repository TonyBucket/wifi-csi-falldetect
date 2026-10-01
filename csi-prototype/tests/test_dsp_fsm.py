"""Time-based CSI DSP and fall-state regression checks."""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
from dsp import Calibrator, MotionDSP, Thresholds, amplitudes
from fsm import FallFSM, State


def test_amplitudes_mask_dc_guards_and_invalid_first_word():
    pairs = [(3, 4)] * 64
    pairs[4] = (6, 8)
    result = amplitudes(pairs)
    assert len(result) == 56
    assert result[0] == 10
    assert len(amplitudes(pairs, first_word_invalid=True)) == 56


def test_dsp_uses_real_time_window_and_rejects_missing_packets():
    dsp = MotionDSP([1.0] * 56, target_hz=100)
    shape = [5.0] * 28 + [10.0] * 28
    alternate = [10.0] * 28 + [5.0] * 28
    assert dsp.push(shape, 0) is None
    for t in range(10_000, 201_000, 10_000):
        result = dsp.push(shape if t % 20_000 else [v * 2 for v in alternate], t)
    assert result is not None and math.isclose(result, 1 / 3, abs_tol=0.02)
    assert dsp.push(shape, 502_000) is None
    assert dsp.push(shape, 1_000_000) is None

def test_calibration_normalizes_each_frame_and_keeps_timestamps():
    shape = [1.0] * 28 + [3.0] * 28
    base = Calibrator.baseline([(0, shape), (10_000, [v * 4 for v in shape])])
    assert base == (0.5,) * 28 + (1.5,) * 28
    dsp = MotionDSP(base, target_hz=100)
    assert dsp.push(shape, 0) is None
    for t in range(10_000, 101_000, 10_000):
        result = dsp.push([v * 10 for v in shape], t)
    assert result == 0


def test_calibration_mad_and_walk_percentile():
    thresholds = Calibrator.from_motion([0.01, 0.02, 0.03], [0.1, 0.2, 0.3])
    assert math.isclose(thresholds.still, 0.064478, abs_tol=1e-6)
    assert thresholds.still < thresholds.move < thresholds.spike
    assert thresholds.spike >= 1.3 * 0.29

def test_flat_still_data_retains_strict_threshold_order():
    t = Calibrator.from_motion([0] * 10, [0] * 10)
    assert math.isclose(t.still, 0.015)
    assert math.isclose(t.move, 0.03)
    assert math.isclose(t.spike, 0.16)


def engine():
    return FallFSM(Calibrator.from_motion([0.01, 0.02, 0.03], [0.1, 0.2, 0.3]))


def test_still_becomes_normal():
    fsm = engine()
    assert fsm.update(0, None) is State.WARMUP
    assert fsm.update(200_000, 0.02) is State.NORMAL


def test_walking_becomes_moving():
    fsm = engine()
    assert fsm.update(0, 0.13) is State.MOVING


def candidate(fsm, burst_ratio: float = 0.8):
    fsm.update(0, 0.5, burst_ratio=burst_ratio)
    assert fsm.update(79_000, 0.5, burst_ratio=burst_ratio) is not State.CANDIDATE
    assert fsm.update(80_000, 0.5, burst_ratio=burst_ratio) is State.CANDIDATE


def test_spike_then_three_seconds_still_alerts_until_ack():
    fsm = engine()
    candidate(fsm)
    assert fsm.update(100_000, 0.02) is State.CANDIDATE
    for t in range(200_000, 3_099_000, 100_000):
        assert fsm.update(t, 0.02) is State.CANDIDATE
    assert fsm.update(3_099_000, 0.02) is State.CANDIDATE
    assert fsm.update(3_100_000, 0.02) is State.FALL_ALERT
    assert fsm.update(4_000_000, 0.13) is State.FALL_ALERT
    assert fsm.ack_alert() is State.NORMAL


def test_spike_then_walking_cancels_candidate():
    fsm = engine()
    candidate(fsm)
    assert fsm.update(100_000, 0.02) is State.CANDIDATE
    # Within 800ms grace, walking does not cancel yet
    assert fsm.update(500_000, 0.13) is State.CANDIDATE
    # After 800ms grace, sustained walking for 300ms cancels candidate
    assert fsm.update(880_000, 0.13) is State.CANDIDATE
    assert fsm.update(1_080_000, 0.13) is State.CANDIDATE
    assert fsm.update(1_180_000, 0.13) is State.MOVING
    assert fsm.update(4_000_000, 0.13) is State.MOVING

def test_collision_tail_does_not_erase_candidate():
    fsm = engine()
    candidate(fsm)
    assert fsm.update(90_000, 0.5) is State.CANDIDATE
    assert fsm.update(200_000, 0.13) is State.CANDIDATE
    assert fsm.update(300_000, 0.02) is State.CANDIDATE
    for t in range(400_000, 3_300_000, 100_000):
        assert fsm.update(t, 0.02) is State.CANDIDATE
    assert fsm.update(3_300_000, 0.02) is State.FALL_ALERT

def test_candidate_sustained_movement_times_out():
    fsm = engine()
    candidate(fsm)
    for t in range(180_000, 8_080_000, 100_000):
        assert fsm.update(t, 0.08) is State.CANDIDATE
    assert fsm.update(8_180_000, 0.13) is State.MOVING

def test_candidate_strong_movement_after_impact_is_cancelled():
    fsm = engine()
    candidate(fsm)
    for t in range(180_000, 880_000, 100_000):
        assert fsm.update(t, 0.5) is State.CANDIDATE
    assert fsm.update(880_000, 0.5) is State.CANDIDATE
    assert fsm.update(980_000, 0.5) is State.CANDIDATE
    assert fsm.update(1_080_000, 0.5) is State.CANDIDATE
    assert fsm.update(1_180_000, 0.5) is State.MOVING


def test_walking_after_collision_tail_cancels_candidate_and_prevents_false_alert():
    # move threshold in engine is ~0.109, candidate_spike is 0.160, spike is ~0.377
    fsm = engine()
    candidate(fsm)
    # Within 800ms collision tail (80ms to 880ms), walking motion 0.13 doesn't cancel
    assert fsm.update(500_000, 0.13) is State.CANDIDATE
    # Beyond 800ms collision tail, sustained walking motion >= 300ms cancels candidate
    assert fsm.update(880_000, 0.13) is State.CANDIDATE
    assert fsm.update(980_000, 0.13) is State.CANDIDATE
    assert fsm.update(1_080_000, 0.13) is State.CANDIDATE
    assert fsm.update(1_180_000, 0.13) is State.MOVING
    # When person stops walking later and stands still for 3s, it must NOT fire FALL_ALERT
    for t in range(1_280_000, 4_480_000, 100_000):
        assert fsm.update(t, 0.02) is State.NORMAL



def test_spike_then_signal_loss_never_becomes_still():
    fsm = engine()
    candidate(fsm)
    assert fsm.update(600_001, None) is State.UNKNOWN
    assert fsm.update(4_000_000, None) is State.UNKNOWN
    assert fsm.update(4_010_000, 0.02) is State.NORMAL


def test_short_noise_spike_never_becomes_candidate():
    fsm = engine()
    fsm.update(0, 0.5)
    assert fsm.update(70_000, 0.02) is State.NORMAL
    assert fsm.update(3_200_000, 0.02) is State.NORMAL


def test_invalid_motion_breaks_stillness_and_long_gap_clears_candidate():
    fsm = engine()
    candidate(fsm)
    fsm.update(100_000, 0.02)
    assert fsm.update(400_000, None) is State.CANDIDATE
    assert fsm.update(450_000, 0.02) is State.CANDIDATE
    assert fsm.update(700_000, None) is State.CANDIDATE
    assert fsm.update(1_000_001, None) is State.UNKNOWN


def test_clock_backward_jump_and_duplicate_timestamp_do_not_crash():
    fsm = engine()
    assert fsm.update(1_000_000, 0.02) is State.NORMAL
    # Duplicate timestamp
    assert fsm.update(1_000_000, 0.02) is State.NORMAL
    # Backward jump (e.g. board reset or reconnect)
    assert fsm.update(50_000, None) is State.UNKNOWN
    # Resume normal progression after backward jump
    assert fsm.update(60_000, 0.02) is State.NORMAL

def test_burst_concentration_and_slope_on_sample_timestamps():
    dsp = MotionDSP([1.0] * 56, target_hz=10)
    for t in range(0, 1_100_000, 100_000):
        dsp.push([1.0] * 56, t)
        dsp.push_motion_energy(t, 1.0 if 400_000 <= t < 600_000 else 0.0)
    assert math.isclose(dsp.burst_ratio, 1.0, abs_tol=1e-6)
    assert math.isclose(dsp.motion_slope, 0.0)
    for t in range(1_100_000, 2_200_000, 100_000):
        dsp.push_motion_energy(t, 1.0)
    assert 0.19 <= dsp.burst_ratio <= 0.31

def test_empty_fingerprint_uses_log_shape_and_explicit_capture():
    dsp = MotionDSP([1.0] * 56)
    empty = [1.0] * 28 + [3.0] * 28
    changed = [3.0] * 28 + [1.0] * 28
    assert dsp.spectral_distance(empty) is None
    dsp.set_empty_baseline([empty, [v * 2 for v in empty]])
    assert dsp.spectral_distance([v * 4 for v in empty]) == 0
    assert dsp.spectral_distance(changed) > 1
    assert dsp.get_empty_baseline() is not None

def test_candidate_exits_room_without_alert():
    fsm = FallFSM(Thresholds(0.048, 0.065, 0.220, candidate_spike=0.160))
    assert fsm.update(0, 0.17, burst_ratio=0.8) is State.MOVING
    assert fsm.update(80_000, 0.17, burst_ratio=0.8) is State.CANDIDATE
    assert fsm.update(100_000, 0.01, is_empty_room=True) is State.NORMAL
    assert fsm.last_note == "EXITED_ROOM"
    assert fsm.update(3_200_000, 0.01) is State.NORMAL

def test_candidate_requires_burst_when_available_and_stillness():
    for burst, expected in ((0.8, State.FALL_ALERT), (0.1, State.NORMAL)):
        fsm = FallFSM(Thresholds(0.048, 0.065, 0.220, candidate_spike=0.160))
        fsm.update(0, 0.18, burst_ratio=burst)
        assert fsm.update(80_000, 0.18, burst_ratio=burst) is State.CANDIDATE
        for t in range(100_000, 3_200_000, 100_000):
            fsm.update(t, 0.01, burst_ratio=burst, is_empty_room=False)
        assert fsm.update(3_200_000, 0.01, burst_ratio=burst, is_empty_room=False) is expected
