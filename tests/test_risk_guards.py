from __future__ import annotations

from kalshi_btc_engine_v2.risk import (
    EntryIntent,
    PositionSnapshot,
    RiskConfig,
    RiskGuard,
    WindowRiskState,
)


def test_window_risk_cap_blocks_gross_cost_over_15_dollars() -> None:
    guard = RiskGuard(RiskConfig(max_risk_per_window_dollars=15.0))
    guard.record_fill(market_ticker="KXBTC15M-1", count=10, price_cents=100)

    decision = guard.check_entry(
        EntryIntent("KXBTC15M-2", side="yes", action="buy", count=6, price_cents=90)
    )

    assert not decision.allowed
    assert decision.code == "WINDOW_RISK_CAP"


def test_record_fill_tracks_window_state_and_ticker_lock() -> None:
    state = WindowRiskState(window_id="w1")
    guard = RiskGuard(state=state)
    guard.record_fill(market_ticker="KXBTC15M-1", count=5, price_cents=50)

    assert state.committed_cents == 250
    assert state.entry_count == 1
    assert "KXBTC15M-1" in state.entered_tickers

    decision = guard.check_entry(
        EntryIntent("KXBTC15M-1", side="yes", action="buy", count=1, price_cents=50)
    )
    assert not decision.allowed
    assert decision.code == "WINDOW_TICKER_LOCK"


def test_manual_fills_are_not_adopted_by_default() -> None:
    guard = RiskGuard()
    guard.record_fill(
        market_ticker="KXBTC15M-1",
        count=99,
        price_cents=99,
        source="manual",
    )

    assert guard.state.committed_cents == 0
    assert guard.state.entry_count == 0
    assert guard.state.entered_tickers == set()


def test_oversell_hardening_blocks_uncovered_sell() -> None:
    guard = RiskGuard()
    decision = guard.check_entry(
        EntryIntent("KXBTC15M-1", side="yes", action="sell", count=2, price_cents=60),
        position=PositionSnapshot("KXBTC15M-1", yes_count=1),
    )

    assert not decision.allowed
    assert decision.code == "SAFETY_OVERSELL"


def test_oversell_hardening_allows_visible_offset_or_inventory() -> None:
    guard = RiskGuard()
    covered = guard.check_entry(
        EntryIntent("KXBTC15M-1", side="yes", action="sell", count=2, price_cents=60),
        position=PositionSnapshot("KXBTC15M-1", yes_count=2),
    )
    offset = guard.check_entry(
        EntryIntent(
            "KXBTC15M-1",
            side="yes",
            action="sell",
            count=2,
            price_cents=60,
            visible_offsetting_buy=True,
        )
    )

    assert covered.allowed
    assert offset.allowed


def test_balance_drop_requires_second_fetch_before_confirming() -> None:
    guard = RiskGuard(
        RiskConfig(
            catastrophic_balance_drop_dollars=20.0,
            catastrophic_balance_drop_fraction=0.50,
        )
    )

    assert (
        guard.check_balance_drop(previous_balance_dollars=100.0, first_fetch_balance_dollars=45.0)
        == "needs_second_fetch"
    )
    assert (
        guard.check_balance_drop(
            previous_balance_dollars=100.0,
            first_fetch_balance_dollars=45.0,
            second_fetch_balance_dollars=98.0,
        )
        == "ok"
    )
    assert (
        guard.check_balance_drop(
            previous_balance_dollars=100.0,
            first_fetch_balance_dollars=45.0,
            second_fetch_balance_dollars=40.0,
        )
        == "confirmed_drop"
    )
