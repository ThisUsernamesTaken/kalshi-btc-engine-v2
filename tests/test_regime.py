from __future__ import annotations

from kalshi_btc_engine_v2.models.regime import (
    RegimeInputs,
    classify_regime,
    is_tradeable,
)


def _healthy(**overrides) -> RegimeInputs:
    base = dict(
        seconds_to_close=400.0,
        fresh_venues=3,
        venue_disagreement_bp=2.0,
        market_status_open=True,
        market_paused=False,
        spread_cents=2,
        top5_depth=200.0,
        fragility_score=0.0,
    )
    base.update(overrides)
    return RegimeInputs(**base)


def test_default_is_info_absorption_trend():
    out = classify_regime(_healthy())
    assert out.label == "info_absorption_trend"
    assert is_tradeable(out.label)


def test_settlement_hazard_near_close():
    out = classify_regime(_healthy(seconds_to_close=20.0))
    assert out.label == "settlement_hazard"
    assert not is_tradeable(out.label)


def test_data_fault_low_quorum():
    out = classify_regime(_healthy(fresh_venues=1))
    assert out.label == "data_fault"


def test_data_fault_paused():
    out = classify_regime(_healthy(market_paused=True))
    assert out.label == "data_fault"


def test_data_fault_high_venue_disagreement():
    out = classify_regime(_healthy(venue_disagreement_bp=25.0))
    assert out.label == "data_fault"


def test_illiquid_wide_spread():
    out = classify_regime(_healthy(spread_cents=10))
    assert out.label == "illiquid_no_trade"


def test_illiquid_thin_depth():
    out = classify_regime(_healthy(top5_depth=10.0))
    assert out.label == "illiquid_no_trade"


def test_reflexive_squeeze_when_reflex_and_ecr_high():
    out = classify_regime(_healthy(reflexivity=2.0, entropy_compression_rate=1.5))
    assert out.label == "reflexive_squeeze"


def test_mean_revert_dislocation_on_divergence():
    out = classify_regime(_healthy(divergence_logit=0.8))
    assert out.label == "mean_revert_dislocation"


def test_is_tradeable_helper():
    assert is_tradeable("info_absorption_trend")
    assert is_tradeable("reflexive_squeeze")
    assert is_tradeable("mean_revert_dislocation")
    assert not is_tradeable("settlement_hazard")
    assert not is_tradeable("data_fault")
    assert not is_tradeable("illiquid_no_trade")
