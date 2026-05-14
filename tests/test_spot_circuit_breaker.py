from __future__ import annotations

from kalshi_btc_engine_v2.policy.exits import ExitConfig, ExitInputs, evaluate_exit


def _inputs(**overrides) -> ExitInputs:
    base = dict(
        side="yes",
        entry_price_cents=50,
        current_bid_cents=52,
        current_ask_cents=54,
        q_cal=0.55,
        seconds_to_close=300.0,
        forecast_edge_at_entry_cents=0.0,
        realized_edge_cents=0.0,
        fragility_score=0.0,
        venue_disagreement_bp=0.0,
        spot_at_entry=100_000.0,
        current_spot=100_000.0,
    )
    base.update(overrides)
    return ExitInputs(**base)


def test_spot_circuit_breaker_threshold_not_crossed() -> None:
    out = evaluate_exit(
        _inputs(current_spot=99_710.0),
        config=ExitConfig(spot_circuit_breaker_bp=30.0),
    )

    assert out.mode == "hold"


def test_spot_circuit_breaker_yes_fires_on_spot_drop() -> None:
    out = evaluate_exit(
        _inputs(side="yes", current_spot=99_600.0),
        config=ExitConfig(spot_circuit_breaker_bp=30.0),
    )

    assert out.mode == "spot_circuit_breaker"
    assert "spot_unfavorable=40.0bp" in out.reason


def test_spot_circuit_breaker_no_fires_on_spot_rise() -> None:
    out = evaluate_exit(
        _inputs(side="no", q_cal=0.45, current_spot=100_400.0),
        config=ExitConfig(spot_circuit_breaker_bp=30.0),
    )

    assert out.mode == "spot_circuit_breaker"
    assert "spot_unfavorable=40.0bp" in out.reason


def test_spot_circuit_breaker_yes_ignores_favorable_spot_rise() -> None:
    out = evaluate_exit(
        _inputs(side="yes", current_spot=100_400.0),
        config=ExitConfig(spot_circuit_breaker_bp=30.0),
    )

    assert out.mode == "hold"


def test_spot_circuit_breaker_default_config_never_fires() -> None:
    out = evaluate_exit(_inputs(side="yes", current_spot=99_600.0))

    assert out.mode == "hold"


def test_spot_circuit_breaker_keeps_adverse_revaluation_priority() -> None:
    out = evaluate_exit(
        _inputs(
            side="yes",
            entry_price_cents=60,
            q_cal=0.50,
            current_spot=99_600.0,
        ),
        config=ExitConfig(spot_circuit_breaker_bp=30.0),
    )

    assert out.mode == "adverse_revaluation"
