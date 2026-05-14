from __future__ import annotations

import bisect
import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CalibrationSample:
    predicted_probability: float
    outcome: int
    seconds_to_close: float
    weight: float = 1.0


@dataclass(frozen=True, slots=True)
class IsotonicCalibrator:
    thresholds: tuple[float, ...]
    values: tuple[float, ...]

    @classmethod
    def fit(
        cls,
        predicted_probabilities: list[float],
        outcomes: list[int],
        weights: list[float] | None = None,
    ) -> IsotonicCalibrator:
        if len(predicted_probabilities) != len(outcomes):
            raise ValueError("predicted_probabilities and outcomes must have equal length")
        if not predicted_probabilities:
            raise ValueError("cannot fit isotonic calibrator with no samples")
        if weights is not None and len(weights) != len(predicted_probabilities):
            raise ValueError("weights must have equal length")

        raw_weights = weights or [1.0] * len(predicted_probabilities)
        rows = sorted(
            (
                _clip_unit(float(probability)),
                float(outcome),
                max(float(weight), 0.0),
            )
            for probability, outcome, weight in zip(
                predicted_probabilities, outcomes, raw_weights, strict=True
            )
            if weight > 0.0
        )
        if not rows:
            raise ValueError("cannot fit isotonic calibrator with zero total weight")

        blocks: list[dict[str, float]] = []
        for probability, outcome, weight in rows:
            blocks.append(
                {
                    "x_max": probability,
                    "weight": weight,
                    "sum_y": outcome * weight,
                    "value": outcome,
                }
            )
            while len(blocks) >= 2 and blocks[-2]["value"] > blocks[-1]["value"]:
                right = blocks.pop()
                left = blocks.pop()
                merged_weight = left["weight"] + right["weight"]
                merged_sum = left["sum_y"] + right["sum_y"]
                blocks.append(
                    {
                        "x_max": right["x_max"],
                        "weight": merged_weight,
                        "sum_y": merged_sum,
                        "value": merged_sum / merged_weight,
                    }
                )

        return cls(
            thresholds=tuple(block["x_max"] for block in blocks),
            values=tuple(_clip_unit(block["value"]) for block in blocks),
        )

    def predict(self, predicted_probability: float) -> float:
        if not self.thresholds:
            raise ValueError("calibrator has no thresholds")
        probability = _clip_unit(float(predicted_probability))
        index = bisect.bisect_left(self.thresholds, probability)
        if index >= len(self.values):
            index = len(self.values) - 1
        return self.values[index]


@dataclass(frozen=True, slots=True)
class TimeBucketIsotonicCalibrator:
    bucket_seconds: int
    bucket_models: dict[int, IsotonicCalibrator]
    global_model: IsotonicCalibrator
    min_bucket_samples: int

    @classmethod
    def fit(
        cls,
        samples: list[CalibrationSample],
        *,
        bucket_seconds: int = 60,
        min_bucket_samples: int = 30,
    ) -> TimeBucketIsotonicCalibrator:
        if bucket_seconds <= 0:
            raise ValueError("bucket_seconds must be positive")
        if not samples:
            raise ValueError("cannot fit time-bucket calibrator with no samples")

        global_model = IsotonicCalibrator.fit(
            [sample.predicted_probability for sample in samples],
            [sample.outcome for sample in samples],
            [sample.weight for sample in samples],
        )

        grouped: dict[int, list[CalibrationSample]] = {}
        for sample in samples:
            bucket = bucket_for_seconds(sample.seconds_to_close, bucket_seconds)
            grouped.setdefault(bucket, []).append(sample)

        bucket_models: dict[int, IsotonicCalibrator] = {}
        for bucket, bucket_samples in grouped.items():
            if len(bucket_samples) < min_bucket_samples:
                continue
            bucket_models[bucket] = IsotonicCalibrator.fit(
                [sample.predicted_probability for sample in bucket_samples],
                [sample.outcome for sample in bucket_samples],
                [sample.weight for sample in bucket_samples],
            )

        return cls(
            bucket_seconds=bucket_seconds,
            bucket_models=bucket_models,
            global_model=global_model,
            min_bucket_samples=min_bucket_samples,
        )

    def predict(self, predicted_probability: float, *, seconds_to_close: float) -> float:
        bucket = bucket_for_seconds(seconds_to_close, self.bucket_seconds)
        model = self.bucket_models.get(bucket, self.global_model)
        return model.predict(predicted_probability)


def power_logit_recalibrate(probability: float, theta: float) -> float:
    """Prediction-market logit-slope correction.

    theta > 1 sharpens probabilities away from 0.5; theta < 1 compresses them.
    """

    p = _clip_open_unit(probability)
    if theta <= 0.0 or not math.isfinite(theta):
        raise ValueError("theta must be positive and finite")
    yes = p**theta
    no = (1.0 - p) ** theta
    return yes / (yes + no)


def bucket_for_seconds(seconds_to_close: float, bucket_seconds: int) -> int:
    if bucket_seconds <= 0:
        raise ValueError("bucket_seconds must be positive")
    seconds = max(float(seconds_to_close), 0.0)
    return int(seconds // bucket_seconds) * bucket_seconds


def _clip_unit(value: float) -> float:
    return min(max(float(value), 0.0), 1.0)


def _clip_open_unit(value: float) -> float:
    return min(max(float(value), 1.0e-12), 1.0 - 1.0e-12)
