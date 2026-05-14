from __future__ import annotations

import math

import pytest

from kalshi_btc_engine_v2.models.fair_prob import (
    SettlementProbabilityConfig,
    SettlementProbabilityInput,
    settlement_fair_probability,
)
from kalshi_btc_engine_v2.models.vol_estimator import (
    bipower_variance_per_sec,
    estimate_vol_drift,
    log_returns_from_prices,
    realized_variance_per_sec,
    rolling_mean,
)


def test_rolling_mean_basic():
    assert rolling_mean([], 60) == 0.0
    assert rolling_mean([1.0, 2.0, 3.0], 2) == pytest.approx(2.5)
    assert rolling_mean([1.0, 2.0, 3.0], 10) == pytest.approx(2.0)


def test_realized_and_bipower_variance_per_sec():
    rs = [0.001, -0.001, 0.001, -0.001]
    rv = realized_variance_per_sec(rs, 4)
    assert rv == pytest.approx(1e-6)
    bv = bipower_variance_per_sec(rs, 4)
    assert bv > 0.0


def test_log_returns_from_prices_roundtrip():
    prices = [100.0, 101.0, 100.0, 102.0]
    rs = log_returns_from_prices(prices)
    assert len(rs) == 3
    assert rs[0] == pytest.approx(math.log(101.0 / 100.0))
    assert rs[2] == pytest.approx(math.log(102.0 / 100.0))


def test_log_returns_rejects_non_positive():
    with pytest.raises(ValueError):
        log_returns_from_prices([100.0, 0.0])


def test_estimate_drift_clip_bound_against_blueprint():
    # Huge positive returns; clip must hold drift below 0.25 * sigma / sqrt(max(h,30)).
    rs = [0.01] * 600
    estimate = estimate_vol_drift(rs, seconds_to_close=600.0)
    bound = 0.25 * estimate.sigma_per_second / math.sqrt(600.0)
    assert estimate.drift_per_second_clipped <= bound + 1e-15
    assert estimate.drift_per_second_clipped >= -bound - 1e-15
    # Pre-clip drift was much larger than the bound.
    assert estimate.drift_per_second > bound


def test_estimate_zero_drift_when_no_vol():
    rs = [0.0] * 600
    estimate = estimate_vol_drift(rs, seconds_to_close=600.0)
    assert estimate.sigma_per_second == 0.0
    assert estimate.drift_per_second_clipped == 0.0


def test_estimator_to_fair_prob_pipeline_atm_balanced():
    rs = [0.0] * 600
    estimate = estimate_vol_drift(rs, seconds_to_close=600.0)
    # Floor sigma if estimator says zero (calm market path); model has its own floor too.
    result = settlement_fair_probability(
        SettlementProbabilityInput(
            spot=100_000.0,
            strike=100_000.0,
            seconds_to_close=600.0,
            realized_vol_annualized=max(estimate.sigma_annualized, 1e-6),
            drift_annualized=estimate.drift_annualized,
        ),
        SettlementProbabilityConfig(drift_shrinkage=1.0),
    )
    assert result.case == "pre_window"
    assert result.probability_yes == pytest.approx(0.5, abs=1e-6)


def test_estimator_feeds_fair_prob_with_upside_drift():
    rs = [0.0001] * 600  # steady positive drift
    estimate = estimate_vol_drift(rs, seconds_to_close=600.0)
    # drift_annualized must be positive after clipping
    assert estimate.drift_annualized > 0.0
    result = settlement_fair_probability(
        SettlementProbabilityInput(
            spot=100_000.0,
            strike=100_000.0,
            seconds_to_close=600.0,
            realized_vol_annualized=max(estimate.sigma_annualized, 1e-6),
            drift_annualized=estimate.drift_annualized,
        ),
        SettlementProbabilityConfig(drift_shrinkage=1.0),
    )
    assert result.probability_yes > 0.5


def test_sample_counts_reflect_input_length():
    rs = [0.0] * 30
    estimate = estimate_vol_drift(rs, seconds_to_close=120.0)
    assert estimate.samples_used_mu_fast == 30
    assert estimate.samples_used_mu_slow == 30
    assert estimate.samples_used_rv == 30
    assert estimate.samples_used_bv == 29
