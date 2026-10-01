"""Pure-Python Fall Classifier runtime (<2ms, 0 runtime dependencies on scikit-learn)."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any, Sequence

try:
    from .dsp_features import FEATURE_NAMES
except ImportError:
    from dsp_features import FEATURE_NAMES


@dataclass(frozen=True, slots=True)
class Prediction:
    is_fall: bool
    score: float
    reason: str


class FallClassifier:
    """Zero-dependency runtime inference for CSI fall detection."""

    def __init__(self, model_path: str | Path | None = None) -> None:
        self.model_data: dict[str, Any] | None = None
        self.model_type: str = "heuristic"
        self.threshold: float = 0.50
        self.feature_names: tuple[str, ...] = FEATURE_NAMES
        self.feature_index: dict[str, int] = {name: i for i, name in enumerate(FEATURE_NAMES)}

        if model_path:
            self.load(model_path)
        else:
            default_path = Path(__file__).resolve().parent / "model.json"
            if default_path.is_file():
                self.load(default_path)

    def load(self, model_path: str | Path) -> None:
        path = Path(model_path).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Model file not found: {path}")
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        self.model_data = data
        self.model_type = data.get("model_type", "heuristic")
        self.threshold = float(data.get("threshold", 0.50))
        if "feature_names" in data:
            self.feature_names = tuple(data["feature_names"])
            self.feature_index = {name: i for i, name in enumerate(self.feature_names)}

    def predict(self, features: Sequence[float]) -> Prediction:
        """Evaluate a 14D feature vector and return a Prediction."""
        if len(features) < len(self.feature_names):
            # Pad with 0.0 if incomplete
            padded = list(features) + [0.0] * (len(self.feature_names) - len(features))
            features = padded

        if self.model_type == "decision_tree" and self.model_data:
            return self._predict_tree(features)
        elif self.model_type == "logistic_regression" and self.model_data:
            return self._predict_logistic(features)
        else:
            return self._predict_heuristic(features)

    def _predict_tree(self, features: Sequence[float]) -> Prediction:
        assert self.model_data is not None
        node = self.model_data["tree"]
        reasons: list[str] = []

        while not node.get("is_leaf", False):
            f_idx = node["feature_idx"]
            f_name = self.feature_names[f_idx] if f_idx < len(self.feature_names) else f"feat_{f_idx}"
            val = float(features[f_idx])
            thresh = float(node["threshold"])

            if val <= thresh:
                reasons.append(f"{f_name}={val:.3f} <= {thresh:.3f}")
                node = node["left"]
            else:
                reasons.append(f"{f_name}={val:.3f} > {thresh:.3f}")
                node = node["right"]

        prob = float(node.get("prob", 1.0 if node.get("value") == 1 else 0.0))
        is_fall = prob >= self.threshold

        summary = "FALL" if is_fall else "ADL"
        path_str = ", ".join(reasons)
        reason = f"{summary} (tree_score={prob:.2f} via {path_str})"
        return Prediction(is_fall=is_fall, score=prob, reason=reason)

    def _predict_logistic(self, features: Sequence[float]) -> Prediction:
        assert self.model_data is not None
        scaler = self.model_data.get("scaler", {})
        means = scaler.get("mean", [0.0] * len(features))
        scales = scaler.get("scale", [1.0] * len(features))
        weights = self.model_data.get("weights", [0.0] * len(features))
        intercept = float(self.model_data.get("intercept", 0.0))

        z = intercept
        contributions: list[tuple[str, float]] = []
        for i, val in enumerate(features[: len(weights)]):
            mean = float(means[i]) if i < len(means) else 0.0
            scale = float(scales[i]) if i < len(scales) and scales[i] != 0 else 1.0
            scaled_val = (val - mean) / scale
            w = float(weights[i])
            contrib = w * scaled_val
            z += contrib
            fname = self.feature_names[i] if i < len(self.feature_names) else f"f_{i}"
            contributions.append((fname, contrib))

        # Sigmoid clipping to prevent overflow
        z = max(-20.0, min(20.0, z))
        prob = 1.0 / (1.0 + math.exp(-z))
        is_fall = prob >= self.threshold

        # Sort top positive and negative contributors
        contributions.sort(key=lambda item: abs(item[1]), reverse=True)
        top = [f"{name}:{c:+.2f}" for name, c in contributions[:3]]
        reason = f"{'FALL' if is_fall else 'ADL'} (logistic_score={prob:.2f}, z={z:.2f}, top={', '.join(top)})"
        return Prediction(is_fall=is_fall, score=prob, reason=reason)

    def _predict_heuristic(self, features: Sequence[float]) -> Prediction:
        """Physical rule-based fallback if no artifact is loaded."""
        idx = self.feature_index
        peak = features[idx.get("peak_energy", 0)]
        post_pre = features[idx.get("post_pre_energy_ratio", 13)]
        torso = features[idx.get("torso_energy_ratio", 10)]
        pdr = features[idx.get("pdr", 4)]

        # Physical fall signature:
        # Significant impact (peak >= 0.09)
        # Followed by relative stillness (post_pre <= 0.88)
        # Substantial torso velocity band energy (torso >= 0.075)
        # Rapid deceleration (pdr >= 0.20)
        is_fall = (peak >= 0.09) and (post_pre <= 0.88) and (torso >= 0.075) and (pdr >= 0.20)
        score = 0.90 if is_fall else 0.10
        reason = f"Heuristic: peak={peak:.3f}, post_pre={post_pre:.3f}, torso={torso:.3f}, pdr={pdr:.3f}"
        return Prediction(is_fall=is_fall, score=score, reason=reason)
