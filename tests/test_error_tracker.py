from __future__ import annotations

from kalshi_btc_engine_v2.models.error_tracker import (
    CalibrationErrorTracker,
    ErrorTrackerConfig,
)


def test_empty_tracker_zero_haircut():
    t = CalibrationErrorTracker()
    assert t.mean_abs_error() == 0.0
    assert t.model_haircut_cents() == 0.0


def test_haircut_only_after_min_samples():
    t = CalibrationErrorTracker(ErrorTrackerConfig(min_samples_for_haircut=10))
    for _ in range(5):
        t.record(0.6, 0)  # error = 0.6
    assert t.sample_count() == 5
    # below min: haircut should be 0
    assert t.model_haircut_cents() == 0.0
    for _ in range(5):
        t.record(0.6, 0)
    assert t.model_haircut_cents() > 0.0


def test_perfect_calibration_yields_zero_haircut():
    t = CalibrationErrorTracker(ErrorTrackerConfig(min_samples_for_haircut=1))
    t.record(1.0, 1)
    t.record(0.0, 0)
    assert t.mean_abs_error() == 0.0
    assert t.model_haircut_cents() == 0.0


def test_window_drops_oldest():
    cfg = ErrorTrackerConfig(window_size=3, min_samples_for_haircut=1)
    t = CalibrationErrorTracker(cfg)
    t.record(0.9, 0)
    t.record(0.9, 0)
    t.record(0.9, 0)
    assert t.sample_count() == 3
    t.record(0.5, 1)  # error = 0.5, drops one of the 0.9 errors
    assert t.sample_count() == 3
    # 2× 0.9-error and 1× 0.5-error → mean = (0.9+0.9+0.5)/3 ≈ 0.767
    assert abs(t.mean_abs_error() - (0.9 + 0.9 + 0.5) / 3) < 1e-9


def test_brier_score():
    t = CalibrationErrorTracker(ErrorTrackerConfig(min_samples_for_haircut=1))
    t.record(0.7, 1)
    t.record(0.3, 0)
    # 0.09 + 0.09 = 0.18 / 2 = 0.09
    assert abs(t.brier_score() - 0.09) < 1e-9
