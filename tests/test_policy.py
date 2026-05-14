from __future__ import annotations

import pytest

from kalshi_btc_engine_v2.policy.decision import (
    DecisionEngine,
    DecisionSnapshot,
    OpenPosition,
)
from kalshi_btc_engine_v2.policy.edge import (
    EdgeInputs,
    compute_edges,
    kalshi_maker_fee_cents,
    kalshi_taker_fee_cents,
)
from kalshi_btc_engine_v2.policy.exits import (
    ExitConfig,
    ExitInputs,
    evaluate_exit,
)
from kalshi_btc_engine_v2.policy.sizing import (
    SizingConfig,
    SizingInputs,
    size_position,
)
from kalshi_btc_engine_v2.policy.veto import (
    MarketHealth,
    check_veto,
)
from kalshi_btc_engine_v2.policy.windows import (
    classify_window,
    window_policy,
)
from kalshi_btc_engine_v2.risk.guards import RiskConfig, RiskGuard, WindowRiskState

# ---------- windows ----------


def test_window_classification_covers_all_phases():
    assert classify_window(0.0, 900.0) == "warmup"
    assert classify_window(15.0, 880.0) == "warmup"
    assert classify_window(31.0, 800.0) == "core"
    assert classify_window(500.0, 76.0) == "core"
    assert classify_window(500.0, 74.0) == "precision"
    assert classify_window(500.0, 16.0) == "precision"
    assert classify_window(500.0, 14.0) == "freeze"
    assert classify_window(500.0, 1.0) == "freeze"
    assert classify_window(900.0, 0.0) == "settlement_hold"
    assert classify_window(900.0, -30.0) == "settlement_hold"


def test_window_policy_thresholds_match_blueprint():
    assert window_policy("warmup").allow_new_entries is False
    assert window_policy("core").allow_new_entries is True
    assert window_policy("core").max_spread_cents == 4
    assert window_policy("core").min_edge_cents == pytest.approx(1.2)
    assert window_policy("precision").max_spread_cents == 3
    assert window_policy("precision").min_edge_cents == pytest.approx(1.8)
    assert window_policy("freeze").allow_new_entries is False


# ---------- veto ----------


def _healthy(**overrides):
    base = dict(
        exchange_active=True,
        trading_active=True,
        market_status="open",
        market_paused=False,
        max_staleness_ms=200,
        venue_quorum=3,
        venue_disagreement_bp=2.0,
        spread_cents=2,
        top5_depth=200.0,
        fragility_score=0.0,
        cooldown_active=False,
    )
    base.update(overrides)
    return MarketHealth(**base)


def test_veto_allows_healthy_market_in_core():
    decision = check_veto(_healthy(), "core", desired_size_contracts=5)
    assert decision.allowed is True


def test_veto_blocks_warmup_window():
    decision = check_veto(_healthy(), "warmup", desired_size_contracts=5)
    assert decision.allowed is False
    assert decision.code == "WINDOW_CLOSED"


def test_veto_blocks_paused_market():
    decision = check_veto(_healthy(market_paused=True), "core", desired_size_contracts=5)
    assert decision.code == "MARKET_PAUSED"


def test_veto_blocks_low_quorum():
    decision = check_veto(_healthy(venue_quorum=1), "core", desired_size_contracts=5)
    assert decision.code == "STALE_FEED"


def test_veto_blocks_stale_feed_per_window():
    # 700ms passes in core (1000ms limit) but fails in precision (500ms limit).
    assert check_veto(_healthy(max_staleness_ms=700), "core", 5).allowed is True
    assert check_veto(_healthy(max_staleness_ms=700), "precision", 5).code == "STALE_FEED"


def test_veto_blocks_wide_spread():
    decision = check_veto(_healthy(spread_cents=5), "core", desired_size_contracts=5)
    assert decision.code == "SPREAD_TOO_WIDE"


def test_veto_blocks_thin_depth():
    # need 5x5=25; depth 10 fails
    decision = check_veto(_healthy(top5_depth=10), "core", desired_size_contracts=5)
    assert decision.code == "INSUFFICIENT_DEPTH"


def test_veto_blocks_cooldown():
    decision = check_veto(
        _healthy(cooldown_active=True, cooldown_reason="stop_recently"),
        "core",
        desired_size_contracts=5,
    )
    assert decision.code == "COOLDOWN"


# ---------- edge ----------


def test_edge_picks_correct_side_for_fair_value_above_yes_ask():
    yes_edge, no_edge = compute_edges(EdgeInputs(q_cal=0.60, yes_ask_cents=55, no_ask_cents=42))
    assert yes_edge.edge_net_cents == pytest.approx(5.0)
    assert no_edge.edge_net_cents == pytest.approx(-2.0)


def test_edge_handles_fees_and_slippage():
    yes_edge, _ = compute_edges(
        EdgeInputs(
            q_cal=0.60,
            yes_ask_cents=55,
            no_ask_cents=42,
            fee_cents_yes=2.0,
            slippage_cents_buffer=1.0,
            model_haircut_cents=0.5,
        )
    )
    # gross = 5.0; net = 5.0 - 2.0 - 1.0 - 0.5
    assert yes_edge.edge_net_cents == pytest.approx(1.5)


def test_kalshi_fee_quadratic_zero_at_endpoints():
    assert kalshi_taker_fee_cents(0) == 0
    assert kalshi_taker_fee_cents(100) == 0
    assert kalshi_maker_fee_cents(50) < kalshi_taker_fee_cents(50)


def test_kalshi_fee_rounds_up_to_cent():
    # 0.07 * 1 * 0.50 * 0.50 * 100 = 1.75c → ceil 2c
    assert kalshi_taker_fee_cents(50, count=1) == 2


# ---------- sizing ----------


def test_sizing_zero_edge_returns_zero():
    out = size_position(
        SizingInputs(
            q_cal=0.5,
            cost_cents=50,
            edge_net_cents=0.0,
            bankroll_dollars=1000.0,
            top5_depth=100.0,
            window="core",
        )
    )
    assert out.contracts == 0
    assert out.capped_by == "no_edge"


def test_sizing_caps_by_market_basis_in_precision():
    out = size_position(
        SizingInputs(
            q_cal=0.65,
            cost_cents=50,
            edge_net_cents=5.0,
            bankroll_dollars=1000.0,
            top5_depth=1000.0,
            window="precision",
        ),
        config=SizingConfig(fractional_kelly=1.0, max_pos_basis_pct_precision=0.0075),
    )
    # max market basis = 0.0075 * 1000 = $7.50 → 15 contracts at 50c
    assert out.contracts <= 15
    assert out.capped_by in {"market_cap", "kelly", "depth_participation", "max_contracts"}


def test_sizing_caps_by_aggregate_exposure():
    out = size_position(
        SizingInputs(
            q_cal=0.80,
            cost_cents=20,
            edge_net_cents=20.0,
            bankroll_dollars=1000.0,
            top5_depth=10000.0,
            window="core",
            aggregate_btc_exposure_dollars=39.0,  # almost full of 4% cap
        ),
        config=SizingConfig(fractional_kelly=1.0),
    )
    # Aggregate cap remaining = $1; at 20c per contract = 5 contracts
    assert out.contracts <= 5


def test_sizing_fee_floor_blocks_small_off_center_low_edge():
    """Size 1-3 at off-center prices with edge below the fee-floor minimum
    should be vetoed. Mirrors the recent paper-run pattern where 1-contract
    entries at no_ask in [30, 62] with ~1.6c edge were fee-eaters."""
    out = size_position(
        SizingInputs(
            q_cal=0.55,
            cost_cents=30,  # off-center: |0.30 - 0.50| = 0.20 > 0.10 band
            edge_net_cents=1.6,  # below 4c min
            bankroll_dollars=20.0,
            top5_depth=100.0,
            window="core",
        ),
    )
    assert out.contracts == 0
    assert out.capped_by == "fee_floor_off_center"


def test_sizing_fee_floor_allows_near_center_low_edge():
    """Near-center small-size trades (within the off-center band) are allowed
    even at modest edge — the fee-per-contract impact is symmetric there."""
    out = size_position(
        SizingInputs(
            q_cal=0.55,
            cost_cents=45,  # |0.45 - 0.50| = 0.05 within 0.10 band
            edge_net_cents=1.6,
            bankroll_dollars=50.0,
            top5_depth=100.0,
            window="core",
        ),
    )
    assert out.contracts >= 1
    assert out.capped_by != "fee_floor_off_center"


def test_sizing_fee_floor_allows_high_edge_off_center():
    """Strong edge overrides the fee floor — conviction-based escape valve."""
    out = size_position(
        SizingInputs(
            q_cal=0.55,
            cost_cents=30,  # off-center
            edge_net_cents=10.0,  # >> 4c min — escape valve
            bankroll_dollars=20.0,
            top5_depth=100.0,
            window="core",
        ),
    )
    assert out.contracts >= 1
    assert out.capped_by != "fee_floor_off_center"


def test_sizing_fee_floor_disabled_via_zero_min_edge():
    """Setting fee_floor_min_edge_cents=0 disables the veto entirely."""
    out = size_position(
        SizingInputs(
            q_cal=0.55,
            cost_cents=30,
            edge_net_cents=1.6,
            bankroll_dollars=20.0,
            top5_depth=100.0,
            window="core",
        ),
        config=SizingConfig(fee_floor_min_edge_cents=0.0),
    )
    assert out.contracts >= 1


def test_sizing_min_floor_kicks_in_for_dust_edges():
    out = size_position(
        SizingInputs(
            q_cal=0.51,
            cost_cents=50,
            edge_net_cents=0.05,
            bankroll_dollars=100.0,
            top5_depth=10.0,
            window="core",
        ),
        config=SizingConfig(min_contracts=10),
    )
    assert out.contracts == 0
    assert out.capped_by in {"min_contracts", "kelly_target_zero", "liquidity_squeezed"}


# ---------- exits ----------


def test_exit_holds_when_in_profit_and_not_late():
    out = evaluate_exit(
        ExitInputs(
            side="yes",
            entry_price_cents=50,
            current_bid_cents=52,
            current_ask_cents=54,
            q_cal=0.60,
            seconds_to_close=300.0,
            forecast_edge_at_entry_cents=4.0,
            realized_edge_cents=2.0,
            fragility_score=0.0,
            venue_disagreement_bp=2.0,
        )
    )
    assert out.mode == "hold"


def test_exit_adverse_revaluation_fires():
    out = evaluate_exit(
        ExitInputs(
            side="yes",
            entry_price_cents=55,
            current_bid_cents=40,
            current_ask_cents=42,
            q_cal=0.45,  # ev = 45 - 55 = -10c
            seconds_to_close=300.0,
            forecast_edge_at_entry_cents=4.0,
            realized_edge_cents=0.0,
            fragility_score=0.0,
            venue_disagreement_bp=2.0,
        )
    )
    assert out.mode == "adverse_revaluation"


def test_exit_profit_capture_fires():
    out = evaluate_exit(
        ExitInputs(
            side="yes",
            entry_price_cents=50,
            current_bid_cents=58,
            current_ask_cents=60,
            q_cal=0.60,
            seconds_to_close=300.0,
            forecast_edge_at_entry_cents=4.0,
            realized_edge_cents=3.0,  # 75% of forecast
            fragility_score=0.0,
            venue_disagreement_bp=2.0,
        )
    )
    assert out.mode == "profit_capture"


def test_exit_hold_to_settlement_only_when_extreme_q():
    base = dict(
        side="yes",
        entry_price_cents=50,
        current_bid_cents=85,
        current_ask_cents=87,
        seconds_to_close=5.0,
        forecast_edge_at_entry_cents=4.0,
        realized_edge_cents=2.5,
        fragility_score=0.0,
        venue_disagreement_bp=2.0,
    )
    extreme_q = evaluate_exit(ExitInputs(**{**base, "q_cal": 0.90}))
    assert extreme_q.mode in {"hold_to_settlement", "profit_capture"}
    middling_q = evaluate_exit(ExitInputs(**{**base, "q_cal": 0.70}))
    assert middling_q.mode != "hold_to_settlement"


def test_exit_profit_capture_disabled_holds():
    """When profit_capture_enabled=False, capture-ratio >= threshold no longer
    triggers an exit. This is the hold-to-settle-pure preset semantics."""
    inputs = ExitInputs(
        side="yes",
        entry_price_cents=50,
        current_bid_cents=58,
        current_ask_cents=60,
        q_cal=0.60,
        seconds_to_close=300.0,
        forecast_edge_at_entry_cents=4.0,
        realized_edge_cents=3.0,  # 75% of forecast — would fire by default
        fragility_score=0.0,
        venue_disagreement_bp=2.0,
    )
    fires = evaluate_exit(inputs)
    assert fires.mode == "profit_capture"
    holds = evaluate_exit(inputs, config=ExitConfig(profit_capture_enabled=False))
    assert holds.mode == "hold"


def test_exit_hold_to_settle_pure_still_bails_on_feed_degraded():
    """Hold-to-settle-pure disables EV-flip and profit_capture but feed_degraded
    must remain — it is an operational rare-bail that cannot be disabled."""
    pure_cfg = ExitConfig(
        adverse_ev_cents=-1e9,
        profit_capture_enabled=False,
        spot_circuit_breaker_bp=30.0,
    )
    out = evaluate_exit(
        ExitInputs(
            side="yes",
            entry_price_cents=50,
            current_bid_cents=52,
            current_ask_cents=54,
            q_cal=0.55,
            seconds_to_close=300.0,
            forecast_edge_at_entry_cents=4.0,
            realized_edge_cents=0.0,
            fragility_score=0.0,
            venue_disagreement_bp=2.0,
            feed_healthy=False,
        ),
        config=pure_cfg,
    )
    assert out.mode == "adverse_revaluation"
    assert out.reason == "feed_degraded"


def test_exit_feed_degraded_forces_revaluation():
    out = evaluate_exit(
        ExitInputs(
            side="yes",
            entry_price_cents=50,
            current_bid_cents=52,
            current_ask_cents=54,
            q_cal=0.55,
            seconds_to_close=300.0,
            forecast_edge_at_entry_cents=4.0,
            realized_edge_cents=0.0,
            fragility_score=0.0,
            venue_disagreement_bp=2.0,
            feed_healthy=False,
        )
    )
    assert out.mode == "adverse_revaluation"


# ---------- decision orchestrator ----------


def _engine() -> DecisionEngine:
    return DecisionEngine(
        risk_guard=RiskGuard(
            RiskConfig(max_risk_per_window_dollars=15.0),
            WindowRiskState(window_id="W"),
        ),
    )


def _entry_snapshot(**overrides) -> DecisionSnapshot:
    base = dict(
        market_ticker="KXBTC15M-FAKE",
        seconds_since_open=120.0,
        seconds_to_close=400.0,
        health=_healthy(),
        edge=EdgeInputs(
            q_cal=0.60,
            yes_ask_cents=53,
            no_ask_cents=44,
            yes_bid_cents=51,
            no_bid_cents=42,
        ),
        bankroll_dollars=200.0,
    )
    base.update(overrides)
    return DecisionSnapshot(**base)


def test_decision_warmup_blocks_entry():
    snap = _entry_snapshot(seconds_since_open=5.0, seconds_to_close=895.0)
    out = _engine().decide(snap)
    assert out.action == "FLAT"
    assert out.window == "warmup"


def test_decision_kill_switch_short_circuits():
    snap = _entry_snapshot(kill_switch_engaged=True)
    out = _engine().decide(snap)
    assert out.action == "KILL_SWITCH"


def test_decision_buy_yes_when_yes_edge_high():
    snap = _entry_snapshot()
    out = _engine().decide(snap)
    assert out.action == "BUY_YES"
    assert out.side == "yes"
    assert out.contracts >= 1


def test_decision_flat_when_edge_below_window_min():
    snap = _entry_snapshot(
        edge=EdgeInputs(
            q_cal=0.52, yes_ask_cents=52, no_ask_cents=49, yes_bid_cents=50, no_bid_cents=47
        ),
    )
    out = _engine().decide(snap)
    assert out.action == "FLAT"


def test_decision_blocks_when_spread_too_wide_in_precision():
    snap = _entry_snapshot(
        seconds_to_close=50.0,
        health=_healthy(spread_cents=5),
    )
    out = _engine().decide(snap)
    assert out.action == "FLAT"
    assert out.veto_code == "SPREAD_TOO_WIDE"


def test_decision_exits_when_position_open_and_adverse():
    snap = _entry_snapshot(
        edge=EdgeInputs(
            q_cal=0.40, yes_ask_cents=42, no_ask_cents=55, yes_bid_cents=40, no_bid_cents=53
        ),
        open_position=OpenPosition(
            side="yes",
            contracts=5,
            entry_price_cents=58,
            forecast_edge_at_entry_cents=4.0,
            q_cal_at_entry=0.62,
        ),
    )
    out = _engine().decide(snap)
    assert out.action == "EXIT"
    assert out.exit_mode == "adverse_revaluation"


def test_decision_holds_position_when_in_band():
    snap = _entry_snapshot(
        edge=EdgeInputs(
            q_cal=0.58, yes_ask_cents=55, no_ask_cents=43, yes_bid_cents=53, no_bid_cents=41
        ),
        open_position=OpenPosition(
            side="yes",
            contracts=5,
            entry_price_cents=52,
            forecast_edge_at_entry_cents=4.0,
            q_cal_at_entry=0.60,
        ),
        realized_edge_cents=1.0,
    )
    out = _engine().decide(snap)
    assert out.action == "HOLD"


def test_decision_respects_window_cap_via_risk_guard():
    guard = RiskGuard(
        RiskConfig(max_risk_per_window_dollars=0.05),  # 5c cap = unreachable
        WindowRiskState(window_id="W"),
    )
    engine = DecisionEngine(risk_guard=guard)
    out = engine.decide(_entry_snapshot())
    assert out.action == "FLAT"
    assert out.veto_code in {"WINDOW_RISK_CAP", "WINDOW_ENTRY_CAP"}
