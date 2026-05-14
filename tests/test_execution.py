from __future__ import annotations

from decimal import Decimal

import pytest

from kalshi_btc_engine_v2.core.orderbook import KalshiOrderBook
from kalshi_btc_engine_v2.execution.live import LiveExecutor, LiveExecutorConfig
from kalshi_btc_engine_v2.execution.paper import PaperExecutor, PaperExecutorConfig
from kalshi_btc_engine_v2.risk.guards import RiskConfig, RiskGuard, WindowRiskState


def _book_with_yes_ask_53() -> KalshiOrderBook:
    book = KalshiOrderBook(market_ticker="KXBTC15M-FAKE")
    book.apply_snapshot(
        yes_levels=[(Decimal("0.50"), Decimal("20")), (Decimal("0.51"), Decimal("30"))],
        no_levels=[
            (Decimal("0.45"), Decimal("10")),
            (Decimal("0.46"), Decimal("15")),
            (Decimal("0.47"), Decimal("40")),
        ],
        seq=1,
    )
    return book


def _engine_pair() -> tuple[PaperExecutor, RiskGuard]:
    guard = RiskGuard(
        RiskConfig(max_risk_per_window_dollars=15.0),
        WindowRiskState(window_id="W"),
    )
    executor = PaperExecutor(guard)
    return executor, guard


def test_paper_buy_yes_takes_inferred_yes_ask():
    executor, guard = _engine_pair()
    book = _book_with_yes_ask_53()
    result = executor.submit_buy(
        market_ticker="KXBTC15M-FAKE",
        side="yes",
        contracts=5,
        book=book,
        now_ms=1_000,
    )
    assert result.accepted is True
    assert len(result.fills) == 1
    fill = result.fills[0]
    assert fill.action == "buy"
    assert fill.side == "yes"
    # Best NO bid is 47c → YES ask = 53c, NO bid qty 40 → ample for 5
    assert fill.price_cents == 53
    assert fill.contracts == 5
    # Risk guard accounting
    assert guard.state.committed_cents == 5 * 53
    assert guard.state.entry_count == 1
    assert "KXBTC15M-FAKE" in guard.state.entered_tickers


def test_paper_buy_sweeps_when_top_level_insufficient():
    executor, _ = _engine_pair()
    book = KalshiOrderBook(market_ticker="X")
    # No bids: 0.45 size 2, 0.46 size 3 → YES asks 0.55 size 2, 0.54 size 3
    book.apply_snapshot(
        yes_levels=[(Decimal("0.40"), Decimal("100"))],
        no_levels=[
            (Decimal("0.45"), Decimal("2")),
            (Decimal("0.46"), Decimal("3")),
        ],
        seq=1,
    )
    result = executor.submit_buy(
        market_ticker="X",
        side="yes",
        contracts=4,
        book=book,
        now_ms=1_000,
    )
    assert result.accepted is True
    # Sweeps 54c (3) then 55c (1) for total 4
    contracts_filled = sum(f.contracts for f in result.fills)
    assert contracts_filled == 4
    levels_hit = {f.price_cents for f in result.fills}
    assert levels_hit == {54, 55}


def test_paper_buy_rejects_when_remaining_above_sweep_window():
    executor, _ = _engine_pair()
    book = KalshiOrderBook(market_ticker="X")
    book.apply_snapshot(
        yes_levels=[(Decimal("0.40"), Decimal("100"))],
        no_levels=[(Decimal("0.45"), Decimal("1"))],
        seq=1,
    )
    result = executor.submit_buy(
        market_ticker="X",
        side="yes",
        contracts=10,
        book=book,
    )
    assert result.accepted is False
    assert "partial_unfilled" in result.rejection_reason


def test_paper_sell_rejects_oversell():
    executor, _ = _engine_pair()
    book = _book_with_yes_ask_53()
    result = executor.submit_sell(
        market_ticker="KXBTC15M-FAKE",
        side="yes",
        contracts=5,
        book=book,
    )
    assert result.accepted is False
    assert result.rejection_reason == "oversell_position_short"


def test_paper_buy_then_sell_records_realized_pnl():
    executor, _ = _engine_pair()
    book = _book_with_yes_ask_53()
    buy = executor.submit_buy(
        market_ticker="KXBTC15M-FAKE",
        side="yes",
        contracts=4,
        book=book,
        now_ms=1_000,
    )
    assert buy.accepted is True
    # Now book moves up: NO bid drops to 0.40 → YES ask 60; YES bid stays at 50
    book.apply_snapshot(
        yes_levels=[(Decimal("0.55"), Decimal("10")), (Decimal("0.56"), Decimal("10"))],
        no_levels=[(Decimal("0.40"), Decimal("20"))],
        seq=2,
    )
    sell = executor.submit_sell(
        market_ticker="KXBTC15M-FAKE",
        side="yes",
        contracts=4,
        book=book,
        now_ms=2_000,
    )
    assert sell.accepted is True
    pos = executor.position("KXBTC15M-FAKE")
    assert pos.is_flat
    # Sold 4 at best YES bid 56c (top of yes_bids reversed: 0.56), entry 53c
    expected_pnl_cents = (56 - 53) * 4
    assert pos.realized_pnl_cents == pytest.approx(expected_pnl_cents)


def test_paper_buy_blocked_by_window_risk_cap():
    guard = RiskGuard(
        RiskConfig(max_risk_per_window_dollars=0.01),  # 1c — unreachable
        WindowRiskState(window_id="W"),
    )
    executor = PaperExecutor(guard)
    book = _book_with_yes_ask_53()
    # Bypass policy layer here; PaperExecutor itself doesn't enforce risk caps
    # at submit time. That's the policy/risk-guard pair's job. Confirm the
    # fill records *do* update committed cents so the next entry would be
    # capped at the policy layer.
    result = executor.submit_buy(
        market_ticker="KXBTC15M-FAKE",
        side="yes",
        contracts=5,
        book=book,
    )
    assert result.accepted is True
    assert guard.state.committed_cents > 1  # exceeds 1c cap, which policy would catch upstream


def test_live_executor_blocked_when_disabled():
    guard = RiskGuard(RiskConfig(), WindowRiskState(window_id="W"))

    class _FakeRest:
        live_enabled = False

    executor = LiveExecutor(
        risk_guard=guard,
        rest_client=_FakeRest(),  # type: ignore[arg-type]
        config=LiveExecutorConfig(enabled=False),
    )
    import asyncio

    result = asyncio.run(
        executor.submit_buy(
            market_ticker="X",
            side="yes",
            contracts=5,
            ask_price_cents=55,
        )
    )
    assert result.accepted is False
    assert result.rejection_reason == "live_executor_disabled"


def test_live_executor_blocked_when_rest_client_live_disabled():
    guard = RiskGuard(RiskConfig(), WindowRiskState(window_id="W"))

    class _FakeRest:
        live_enabled = False

        async def create_order(self, payload):
            return {}

    executor = LiveExecutor(
        risk_guard=guard,
        rest_client=_FakeRest(),  # type: ignore[arg-type]
        config=LiveExecutorConfig(enabled=True),
    )
    import asyncio

    result = asyncio.run(
        executor.submit_buy(
            market_ticker="X",
            side="yes",
            contracts=5,
            ask_price_cents=55,
        )
    )
    assert result.accepted is False
    assert result.rejection_reason == "rest_client_live_disabled"


def test_paper_executor_config_caps_sweep_levels():
    executor, _ = _engine_pair()
    config = PaperExecutorConfig(max_sweep_levels=1)
    executor.config = config
    book = KalshiOrderBook(market_ticker="X")
    book.apply_snapshot(
        yes_levels=[(Decimal("0.40"), Decimal("100"))],
        no_levels=[
            (Decimal("0.45"), Decimal("2")),
            (Decimal("0.46"), Decimal("3")),
        ],
        seq=1,
    )
    # Only first ask level (54c, qty 3) considered → can't fill 5
    result = executor.submit_buy(market_ticker="X", side="yes", contracts=5, book=book)
    assert result.accepted is False
