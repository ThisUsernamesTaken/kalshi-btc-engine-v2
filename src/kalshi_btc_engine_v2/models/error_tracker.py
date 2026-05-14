# HANDOFF: owned by Claude (models/). Edit only via HANDOFF.md Open Request.
"""Rolling calibration-error tracker.

Records (predicted probability, realized outcome) pairs as markets settle.
Reports mean absolute error and the derived ``model_haircut`` (in cents) that
the policy edge calculation should subtract — per the blueprint:

    model_haircut = 0.4 * rolling_abs_calibration_error

This keeps live edge math honest about how miscalibrated the fair-prob model
has been recently. The window is intentionally short (default 200 samples) so
the haircut adapts to regime shifts rather than averaging away seasonality.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ErrorTrackerConfig:
    window_size: int = 200
    haircut_factor: float = 0.4
    min_samples_for_haircut: int = 20


@dataclass(slots=True)
class CalibrationErrorTracker:
    config: ErrorTrackerConfig = field(default_factory=ErrorTrackerConfig)
    samples: deque[tuple[float, float]] = field(default_factory=deque)

    def __post_init__(self) -> None:
        if self.samples.maxlen != self.config.window_size:
            self.samples = deque(self.samples, maxlen=self.config.window_size)

    def record(self, predicted_probability: float, realized_outcome: int) -> None:
        p = max(0.0, min(1.0, float(predicted_probability)))
        outcome = 1.0 if int(realized_outcome) >= 1 else 0.0
        self.samples.append((p, outcome))

    def sample_count(self) -> int:
        return len(self.samples)

    def mean_abs_error(self) -> float:
        if not self.samples:
            return 0.0
        return sum(abs(p - o) for p, o in self.samples) / len(self.samples)

    def brier_score(self) -> float:
        if not self.samples:
            return 0.0
        return sum((p - o) ** 2 for p, o in self.samples) / len(self.samples)

    def model_haircut_cents(self) -> float:
        if self.sample_count() < self.config.min_samples_for_haircut:
            return 0.0
        return 100.0 * self.config.haircut_factor * self.mean_abs_error()

    def reset(self) -> None:
        self.samples.clear()
