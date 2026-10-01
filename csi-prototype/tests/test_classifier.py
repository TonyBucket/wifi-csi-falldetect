"""Unit tests for Pure-Python FallClassifier module."""

from pathlib import Path
import time
import pytest

from classifier import FallClassifier, Prediction
from dsp_features import FEATURE_NAMES


def test_classifier_loads_artifact():
    model_path = Path(__file__).resolve().parents[1] / "host" / "model.json"
    assert model_path.is_file()
    clf = FallClassifier(model_path)
    assert clf.model_type == "decision_tree"
    assert clf.threshold == 0.50
    assert len(clf.feature_names) == 14


def test_classifier_inference_speed():
    model_path = Path(__file__).resolve().parents[1] / "host" / "model.json"
    clf = FallClassifier(model_path)

    # 14D sample vector
    sample_vec = [0.18, 7.5, 500.0, 300.0, 0.35, 1.6, 0.75, 0.20, 2.1, 0.015, 0.12, 0.50, 3.8, 0.70]

    # Benchmark 1000 inferences
    t0 = time.perf_counter()
    for _ in range(1000):
        pred = clf.predict(sample_vec)
    t1 = time.perf_counter()
    duration_per_call_ms = ((t1 - t0) / 1000) * 1000.0

    assert duration_per_call_ms < 0.10  # Far below the 2ms requirement
    assert isinstance(pred, Prediction)
    assert isinstance(pred.is_fall, bool)
    assert 0.0 <= pred.score <= 1.0
    assert len(pred.reason) > 0


def test_classifier_fall_vs_adl_predictions():
    model_path = Path(__file__).resolve().parents[1] / "host" / "model.json"
    clf = FallClassifier(model_path)

    # Clear fall pattern
    fall_feats = [0.181, 7.7, 510.0, 320.0, 0.264, 1.59, 0.74, 0.208, 2.30, 0.016, 0.121, 0.49, 3.78, 0.858]
    fall_pred = clf.predict(fall_feats)
    assert fall_pred.is_fall is True
    assert fall_pred.score == 1.0
    assert "FALL" in fall_pred.reason

    # Clear ADL walk pattern (post_pre > 0.875)
    walk_feats = [0.288, 19.6, 820.0, 860.0, 0.212, 0.95, 0.67, 0.282, 2.41, 0.015, 0.147, 0.47, 2.34, 1.196]
    walk_pred = clf.predict(walk_feats)
    assert walk_pred.is_fall is False
    assert walk_pred.score == 0.0
    assert "ADL" in walk_pred.reason

    # Sit pattern (torso <= 0.080)
    sit_feats = [0.270, 14.1, 1000.0, 160.0, 0.292, 6.25, 0.79, 0.118, 2.08, 0.004, 0.033, 0.35, 4.26, 0.481]
    sit_pred = clf.predict(sit_feats)
    assert sit_pred.is_fall is False
    assert sit_pred.score == 0.0


def test_classifier_fallback_without_artifact(tmp_path):
    # Non-existent file raises FileNotFoundError
    with pytest.raises(FileNotFoundError):
        FallClassifier(tmp_path / "non_existent.json")

    # Heuristic fallback
    clf = FallClassifier()
    clf.model_data = None
    clf.model_type = "heuristic"
    pred = clf.predict([0.15, 5.0, 100.0, 200.0, 0.40, 0.5, 0.8, 0.1, 2.0, 0.01, 0.10, 0.5, 3.0, 0.50])
    assert pred.is_fall is True
