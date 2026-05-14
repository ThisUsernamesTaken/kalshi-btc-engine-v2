from __future__ import annotations

import pytest

from kalshi_btc_engine_v2.models.calibration import (
    CalibrationSample,
    IsotonicCalibrator,
    TimeBucketIsotonicCalibrator,
    power_logit_recalibrate,
)
from kalshi_btc_engine_v2.models.fair_prob import (
    SettlementProbabilityConfig,
    SettlementProbabilityInput,
    settlement_fair_probability,
)


def test_pre_window_atm_probability_is_near_half() -> None:
    result = settlement_fair_probability(
        SettlementProbabilityInput(
            spot=100_000.0,
            strike=100_000.0,
            seconds_to_close=900.0,
            realized_vol_annualized=0.60,
        )
    )

    assert result.case == "pre_window"
    assert result.k_required is None
    assert result.probability_yes == pytest.approx(0.5)


def test_pre_window_positive_drift_shrinkage_raises_probability() -> None:
    base = SettlementProbabilityInput(
        spot=100_000.0,
        strike=100_000.0,
        seconds_to_close=600.0,
        realized_vol_annualized=0.50,
        drift_annualized=8.0,
    )
    no_drift = settlement_fair_probability(
        base,
        SettlementProbabilityConfig(drift_shrinkage=0.0),
    )
    with_drift = settlement_fair_probability(
        base,
        SettlementProbabilityConfig(drift_shrinkage=0.5),
    )

    assert with_drift.effective_drift_annualized == pytest.approx(4.0)
    assert with_drift.probability_yes > no_drift.probability_yes


def test_inside_window_uses_required_remaining_average() -> None:
    result = settlement_fair_probability(
        SettlementProbabilityInput(
            spot=100.0,
            strike=100.0,
            seconds_to_close=30.0,
            realized_vol_annualized=0.50,
            observed_settlement_average=101.0,
            observed_settlement_seconds=30.0,
        )
    )

    assert result.case == "inside_window"
    assert result.k_required == pytest.approx(99.0)
    assert result.probability_yes > 0.5


def test_inside_window_already_mathematically_locked_yes() -> None:
    result = settlement_fair_probability(
        SettlementProbabilityInput(
            spot=100.0,
            strike=100.0,
            seconds_to_close=1.0,
            realized_vol_annualized=0.50,
            observed_settlement_average=102.0,
            observed_settlement_seconds=59.0,
        )
    )

    assert result.case == "inside_window"
    assert result.k_required < 0.0
    assert result.probability_yes == pytest.approx(0.999999)


def test_sigma_floor_is_applied() -> None:
    result = settlement_fair_probability(
        SettlementProbabilityInput(
            spot=101.0,
            strike=100.0,
            seconds_to_close=120.0,
            realized_vol_annualized=0.01,
            implied_vol_annualized=0.02,
        ),
        SettlementProbabilityConfig(sigma_floor_annualized=0.25),
    )

    assert result.effective_sigma_annualized == pytest.approx(0.25)


def test_missing_inside_window_observation_warns_and_assumes_spot() -> None:
    result = settlement_fair_probability(
        SettlementProbabilityInput(
            spot=100.0,
            strike=100.0,
            seconds_to_close=30.0,
            realized_vol_annualized=0.50,
        )
    )

    assert "observed_seconds_missing_inferred_from_clock" in result.warnings
    assert "observed_average_missing_assumed_spot" in result.warnings
    assert result.k_required == pytest.approx(100.0)


def test_isotonic_calibrator_enforces_monotonicity() -> None:
    calibrator = IsotonicCalibrator.fit(
        predicted_probabilities=[0.1, 0.2, 0.3, 0.8],
        outcomes=[1, 0, 0, 1],
    )

    predictions = [calibrator.predict(value) for value in [0.1, 0.2, 0.3, 0.8]]
    assert predictions == sorted(predictions)
    assert predictions[-1] == pytest.approx(1.0)


def test_time_bucket_calibrator_uses_bucket_when_enough_samples() -> None:
    samples = [
        CalibrationSample(0.2, 0, 30.0),
        CalibrationSample(0.8, 0, 40.0),
        CalibrationSample(0.2, 1, 90.0),
        CalibrationSample(0.8, 1, 100.0),
    ]
    calibrator = TimeBucketIsotonicCalibrator.fit(
        samples,
        bucket_seconds=60,
        min_bucket_samples=2,
    )

    assert calibrator.predict(0.8, seconds_to_close=30.0) == pytest.approx(0.0)
    assert calibrator.predict(0.2, seconds_to_close=90.0) == pytest.approx(1.0)


def test_power_logit_recalibration_compresses_when_theta_below_one() -> None:
    assert power_logit_recalibrate(0.8, theta=0.5) < 0.8
    assert power_logit_recalibrate(0.2, theta=0.5) > 0.2
