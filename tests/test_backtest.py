from __future__ import annotations

import json
from decimal import Decimal

from kalshi_btc_engine_v2.backtest.runner import (
    BacktestConfig,
    Backtester,
    default_strike_provider,
)
from kalshi_btc_engine_v2.core.events import ReplayEvent
from kalshi_btc_engine_v2.policy.sizing import SizingConfig
from kalshi_btc_engine_v2.policy.veto import VetoConfig
from kalshi_btc_engine_v2.risk.guards import RiskConfig


def _market_dim(ticker: str, open_ms: int, close_ms: int, *, strike: float) -> dict:
    return {
        "ticker": ticker,
        "open_time": _iso_ms(open_ms),
        "close_time": _iso_ms(close_ms),
        "floor_strike": str(strike),
        "title": f"Will BTC be above ${int(strike):,} at close?",
    }


def _iso_ms(ms: int) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.UTC).isoformat().replace("+00:00", "Z")


def _l2_event(ticker: str, ts_ms: int, yes_levels, no_levels, seq: int) -> ReplayEvent:
    yes_str = [[str(p), str(q)] for p, q in yes_levels]
    no_str = [[str(p), str(q)] for p, q in no_levels]
    payload = {
        "market_ticker": ticker,
        "received_ts_ms": ts_ms,
        "exchange_ts_ms": ts_ms,
        "event_type": "snapshot",
        "seq": seq,
        "yes_levels_json": json.dumps(yes_str),
        "no_levels_json": json.dumps(no_str),
        "raw_json": "{}",
    }
    return ReplayEvent(
        event_time_ms=ts_ms,
        table="kalshi_l2_event",
        event_id=ts_ms,
        payload=payload,
    )


def _spot_event(ts_ms: int, mid: float, venue: str = "fusion:median2of3") -> ReplayEvent:
    return ReplayEvent(
        event_time_ms=ts_ms,
        table="spot_quote_event",
        event_id=ts_ms,
        payload={
            "price": str(mid),
            "received_ts_ms": ts_ms,
            "exchange_ts_ms": ts_ms,
            "source_channel": venue,
            "raw_json": "{}",
        },
    )


def test_strike_provider_reads_floor_strike():
    dim = {"floor_strike": "103000"}
    assert default_strike_provider("X", dim) == 103000.0


def test_strike_provider_falls_back_to_title():
    dim = {"title": "Will BTC be above $102,500 at close?"}
    assert default_strike_provider("X", dim) == 102500.0


def test_backtester_empty_run():
    bt = Backtester()
    out = bt.run_events(events=[])
    assert out.events_processed == 0
    assert out.decisions_made == 0
    assert out.fills == 0


def test_backtester_processes_events_and_can_buy_when_edge_present():
    open_ms = 1_000_000_000_000
    close_ms = open_ms + 15 * 60 * 1000
    ticker = "KXBTC15M-TEST"
    strike = 100_000.0

    bt = Backtester(
        config=BacktestConfig(
            bankroll_dollars=200.0,
            decision_interval_ms=1000,
            min_returns_for_decision=30,
            risk_config=RiskConfig(max_risk_per_window_dollars=15.0),
            sizing_config=SizingConfig(fractional_kelly=1.0, max_contracts=20),
            veto_config=VetoConfig(min_depth_multiplier=1.0),
        )
    )
    bt.upsert_market_dim(ticker, _market_dim(ticker, open_ms, close_ms, strike=strike))

    events: list[ReplayEvent] = []
    # Seed 60s of spot at $103k so the model thinks YES (above 100k) is very likely.
    for i in range(60):
        ts = open_ms + 30_000 + i * 1000
        events.append(_spot_event(ts, 103_000.0 + i * 0.5))
    # Kalshi book: YES asks 53c, NO bid 47c, plenty of depth
    for i in range(10):
        ts = open_ms + 90_000 + i * 1000
        events.append(
            _l2_event(
                ticker,
                ts,
                yes_levels=[[Decimal("0.50"), Decimal("500")]],
                no_levels=[[Decimal("0.47"), Decimal("500")]],
                seq=i + 1,
            )
        )

    summary = bt.run_events(events)
    assert summary.events_processed == len(events)
    assert summary.decisions_made >= 1
    assert summary.decisions_buy >= 1
    assert summary.fills >= 1


def test_backtester_skips_decisions_without_strike():
    open_ms = 1_000_000_000_000
    close_ms = open_ms + 15 * 60 * 1000
    ticker = "KXBTC15M-NOSTRIKE"
    bt = Backtester()
    bt.upsert_market_dim(
        ticker,
        {
            "ticker": ticker,
            "open_time": _iso_ms(open_ms),
            "close_time": _iso_ms(close_ms),
            "title": "No strike here",
        },
    )
    events = []
    for i in range(60):
        ts = open_ms + 30_000 + i * 1000
        events.append(_spot_event(ts, 103_000.0 + i))
    for i in range(5):
        ts = open_ms + 90_000 + i * 1000
        events.append(
            _l2_event(
                ticker,
                ts,
                yes_levels=[[Decimal("0.50"), Decimal("100")]],
                no_levels=[[Decimal("0.47"), Decimal("100")]],
                seq=i + 1,
            )
        )
    out = bt.run_events(events)
    assert out.decisions_made == 0


def test_backtester_throttles_decisions_by_interval():
    open_ms = 1_000_000_000_000
    close_ms = open_ms + 15 * 60 * 1000
    ticker = "KXBTC15M-RATE"
    bt = Backtester(config=BacktestConfig(decision_interval_ms=5000, min_returns_for_decision=10))
    bt.upsert_market_dim(ticker, _market_dim(ticker, open_ms, close_ms, strike=100_000.0))
    events = []
    for i in range(30):
        ts = open_ms + 10_000 + i * 1000
        events.append(_spot_event(ts, 100_000.0 + i))
    for i in range(20):
        ts = open_ms + 40_000 + i * 500  # twice per second
        events.append(
            _l2_event(
                ticker,
                ts,
                yes_levels=[[Decimal("0.50"), Decimal("50")]],
                no_levels=[[Decimal("0.47"), Decimal("50")]],
                seq=i + 1,
            )
        )
    out = bt.run_events(events)
    # 20 L2 events at 500ms cadence over 10s with 5s decision interval → ~2-3 decisions
    assert out.decisions_made <= 3
