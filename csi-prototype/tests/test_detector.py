"""Unit tests for multi-stage FallDetector module."""

import pytest

from dsp import Thresholds
from detector import FallDetector
from classifier import FallClassifier, Prediction
from fsm import State


class MockClassifier(FallClassifier):
    def __init__(self, is_fall: bool = True, score: float = 1.0) -> None:
        super().__init__()
        self._mock_fall = is_fall
        self._mock_score = score

    def predict(self, features) -> Prediction:
        return Prediction(
            is_fall=self._mock_fall,
            score=self._mock_score,
            reason="Mock prediction",
        )


@pytest.fixture
def default_thresholds() -> Thresholds:
    return Thresholds(still=0.0459, move=0.0616, spike=0.2159)


def test_detector_normal_to_candidate_and_fall_alert(default_thresholds):
    clf = MockClassifier(is_fall=True, score=1.0)
    det = FallDetector(default_thresholds, clf, trigger_motion=0.095, stillness_duration_us=2_500_000)

    t = 0
    # 1. Quiescent frames (NORMAL)
    for _ in range(50):
        assert det.update(t, 0.03, [1.0] * 56) is State.NORMAL
        t += 10_000

    # 2. Impact burst: triggers CANDIDATE
    t += 10_000
    assert det.update(t, 0.15, [1.0] * 56) is State.CANDIDATE
    cand_t = t

    # 3. Post-impact collection (1.5s): still in CANDIDATE, decaying motion
    for _ in range(149):
        t += 10_000
        det.update(t, 0.035, [1.0] * 56)
        assert det.state is State.CANDIDATE

    # Frame at 1.5s post-impact triggers classifier evaluation
    t += 10_000
    det.update(t, 0.035, [1.0] * 56)
    assert det.classifier_evaluated is True
    assert det.classifier_confirmed is True
    assert "CLASSIFIER_CONFIRMED" in (det.last_note or "")

    # 4. Post-impact stillness confirmation (needs 2.5s continuous < thresholds.move)
    # Feed remaining stillness to reach 2.5s
    for _ in range(120):
        t += 10_000
        det.update(t, 0.035, [1.0] * 56)

    assert det.state is State.FALL_ALERT


def test_detector_classifier_veto_cancels_candidate(default_thresholds):
    # Classifier decides this is ADL (not fall)
    clf = MockClassifier(is_fall=False, score=0.0)
    det = FallDetector(default_thresholds, clf, trigger_motion=0.095)

    t = 0
    for _ in range(20):
        det.update(t, 0.03, [1.0] * 56)
        t += 10_000

    # Impact burst triggers CANDIDATE
    t += 10_000
    assert det.update(t, 0.18, [1.0] * 56) is State.CANDIDATE

    # Post-impact window (1.5s)
    for _ in range(155):
        t += 10_000
        det.update(t, 0.03, [1.0] * 56)

    # After classifier evaluation, candidate MUST be vetoed and canceled!
    assert det.state in (State.NORMAL, State.MOVING)
    assert "CLASSIFIER_VETO" in (det.last_note or "")
    assert det.classifier_confirmed is False


def test_detector_empty_room_fingerprint_veto(default_thresholds):
    clf = MockClassifier(is_fall=True)
    det = FallDetector(default_thresholds, clf)

    t = 0
    t += 10_000
    assert det.update(t, 0.15, [1.0] * 56) is State.CANDIDATE

    # User exits room -> is_empty_room=True triggers immediate veto
    t += 50_000
    st = det.update(t, 0.02, [1.0] * 56, is_empty_room=True)
    assert st is State.NORMAL
    assert det.last_note == "EXITED_ROOM"


def test_detector_sustained_movement_cancellation(default_thresholds):
    clf = MockClassifier(is_fall=True)
    det = FallDetector(default_thresholds, clf)

    t = 0
    t += 10_000
    assert det.update(t, 0.15, [1.0] * 56) is State.CANDIDATE

    # Post-impact evaluation (1.5s)
    for _ in range(160):
        t += 10_000
        det.update(t, 0.03, [1.0] * 56)

    assert det.classifier_confirmed is True

    # Victim gets back up and walks (motion >= move for 300ms)
    for _ in range(35):
        t += 10_000
        st = det.update(t, 0.08, [1.0] * 56)

    assert st is State.MOVING


def test_detector_clock_jump_and_timeout_safety(default_thresholds):
    det = FallDetector(default_thresholds)
    det.update(100_000, 0.03, [1.0] * 56)

    # Timeout > 500ms with missing sample (None)
    st = det.update(700_000, None)
    assert st is State.UNKNOWN

    # Clock jump backward
    st = det.update(500_000, 0.03, [1.0] * 56)
    assert st is State.WARMUP
