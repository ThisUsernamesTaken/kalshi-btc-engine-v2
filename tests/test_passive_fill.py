from __future__ import annotations

from kalshi_btc_engine_v2.execution.paper import PaperExecutor
from kalshi_btc_engine_v2.risk.guards import RiskConfig, RiskGuard, WindowRiskState


def _executor() -> PaperExecutor:
    return PaperExecutor(RiskGuard(RiskConfig(), WindowRiskState(window_id="W")))


def test_passive_buy_blocked_below_fill_score_one():
    ex = _executor()
    out = ex.submit_passive_buy(
        market_ticker="X",
        side="yes",
        contracts=5,
        post_price_cents=51,
        queue_ahead=200,
        expected_tape_consumption_30s=50,
    )
    assert out.accepted is False
    assert "fill_score" in out.rejection_reason


def test_passive_buy_filled_when_fill_score_ok():
    ex = _executor()
    out = ex.submit_passive_buy(
        market_ticker="X",
        side="yes",
        contracts=5,
        post_price_cents=51,
        queue_ahead=10,
        expected_tape_consumption_30s=200,
    )
    assert out.accepted is True
    assert len(out.fills) == 1
    fill = out.fills[0]
    assert fill.is_taker is False
    assert fill.contracts == 5


def test_passive_buy_uses_maker_fee():
    ex = _executor()
    out = ex.submit_passive_buy(
        market_ticker="X",
        side="yes",
        contracts=10,
        post_price_cents=50,
        queue_ahead=10,
        expected_tape_consumption_30s=200,
    )
    assert out.accepted is True
    fill = out.fills[0]
    # Maker fee at 50c is 0.0175 * 10 * 0.5 * 0.5 * 100 = 4.375 → ceil 5
    assert fill.fee_cents == 5


def test_passive_buy_rejects_bad_price():
    ex = _executor()
    out = ex.submit_passive_buy(
        market_ticker="X",
        side="yes",
        contracts=5,
        post_price_cents=0,
        queue_ahead=1,
        expected_tape_consumption_30s=100,
    )
    assert out.accepted is False


def test_passive_buy_records_adverse_selection_penalty():
    ex = _executor()
    out = ex.submit_passive_buy(
        market_ticker="X",
        side="yes",
        contracts=5,
        post_price_cents=50,
        queue_ahead=10,
        expected_tape_consumption_30s=200,
        adverse_selection_cents=1,
    )
    assert out.accepted is True
    fill = out.fills[0]
    assert fill.price_cents == 51  # 50 + 1c adverse selection
