"""Tests for the post-hoc analysis modules built in cron cycles.

Covers ``counterfactual``, ``per_market_report``, ``trade_patterns``,
``divergence_stats``, and ``backfill`` — modules added to inspect captured
data without explicit unit coverage at the time of authoring.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kalshi_btc_engine_v2.backtest.backfill import (
    backfill_from_lifecycle,
    parse_ticker_close_time,
)
from kalshi_btc_engine_v2.backtest.counterfactual import (
    _hold_pnl,
    hold_to_settlement,
)
from kalshi_btc_engine_v2.backtest.divergence_stats import divergence_stats
from kalshi_btc_engine_v2.backtest.per_market_report import per_market_report
from kalshi_btc_engine_v2.backtest.trade_patterns import (
    TradePatternConfig,
    detect_patterns,
)
from kalshi_btc_engine_v2.storage.sqlite import connect, init_db


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    db = tmp_path / "test.sqlite"
    init_db(db)
    return db


@pytest.fixture
def decision_log(tmp_path: Path) -> Path:
    """A synthetic JSONL with two trades on one market."""
    log = tmp_path / "decisions.jsonl"
    rows = [
        # Trade 1: BUY_NO 5x at 30c, EXIT at no_bid 40c (won 50c)
        {
            "ts_ms": 1_000_000,
            "market_ticker": "MKT-A",
            "action": "BUY_NO",
            "side": "no",
            "contracts": 5,
            "q_cal": 0.45,
            "yes_ask_cents": 71,
            "no_ask_cents": 30,
            "yes_bid_cents": 70,
            "no_bid_cents": 29,
            "exit_mode": None,
            "diag": {"regime_reason": "divergence_logit=2.5"},
        },
        {
            "ts_ms": 1_005_000,  # 5s hold
            "market_ticker": "MKT-A",
            "action": "EXIT",
            "side": "no",
            "contracts": 5,
            "q_cal": 0.40,
            "yes_ask_cents": 61,
            "no_ask_cents": 40,
            "yes_bid_cents": 60,
            "no_bid_cents": 39,
            "exit_mode": "profit_capture",
            "diag": {"regime_reason": "divergence_logit=3.0"},
        },
        # Trade 2: BUY_YES 1x at 50c, EXIT at yes_bid 45c
        {
            "ts_ms": 2_000_000,
            "market_ticker": "MKT-B",
            "action": "BUY_YES",
            "side": "yes",
            "contracts": 1,
            "q_cal": 0.60,
            "yes_ask_cents": 50,
            "no_ask_cents": 51,
            "yes_bid_cents": 49,
            "no_bid_cents": 50,
            "exit_mode": None,
            "diag": {"regime_reason": "divergence_logit=-1.0"},
        },
        {
            "ts_ms": 2_002_000,  # 2s hold — quick flip
            "market_ticker": "MKT-B",
            "action": "EXIT",
            "side": "yes",
            "contracts": 1,
            "q_cal": 0.55,
            "yes_ask_cents": 46,
            "no_ask_cents": 55,
            "yes_bid_cents": 45,
            "no_bid_cents": 54,
            "exit_mode": "adverse_revaluation",
            "diag": {"regime_reason": "divergence_logit=-0.5"},
        },
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return log


# ---------- counterfactual ----------


def test_hold_pnl_correct_side_wins():
    # BUY YES at 30c, YES won → (100-30) * 2 = 140
    assert _hold_pnl("yes", 2, 30, yes_won=1) == 140.0
    # BUY NO at 30c, NO won → (100-30) * 2 = 140
    assert _hold_pnl("no", 2, 30, yes_won=0) == 140.0


def test_hold_pnl_wrong_side_loses():
    # BUY YES at 30c, YES lost → (0-30) * 2 = -60
    assert _hold_pnl("yes", 2, 30, yes_won=0) == -60.0
    assert _hold_pnl("no", 2, 30, yes_won=1) == -60.0


def test_counterfactual_no_settled_markets(decision_log: Path, tmp_db: Path):
    report = hold_to_settlement(decision_log, tmp_db)
    assert report.entries == 2
    assert report.settled_entries == 0


def test_counterfactual_with_settled_market(decision_log: Path, tmp_db: Path):
    # Mark MKT-A as settled NO won (BUY_NO trade was directionally correct)
    with connect(tmp_db) as conn:
        conn.execute(
            "INSERT INTO market_dim(ticker, series_ticker, raw_json, "
            "created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, ?)",
            ("MKT-A", "TEST", json.dumps({"result": "no"}), 0, 0),
        )
        conn.commit()
    report = hold_to_settlement(decision_log, tmp_db)
    assert report.settled_entries == 1
    assert report.directionally_correct == 1
    # NO win → hold P&L = (100-30) * 5 = 350
    assert report.hold_to_settlement_gross_cents == pytest.approx(350.0)


# ---------- per_market_report ----------


def test_per_market_report_pairs_entries_to_exits(decision_log: Path, tmp_db: Path):
    stats = per_market_report(decision_log, tmp_db)
    by_ticker = {s.market_ticker: s for s in stats}
    a = by_ticker["MKT-A"]
    b = by_ticker["MKT-B"]
    assert a.entries == 1
    assert a.exits == 1
    # MKT-A: BUY NO @ 30c, exit no_bid 39c → P&L = (39-30) * 5 = 45c
    assert a.realized_pnl_cents == pytest.approx(45.0)
    # MKT-B: BUY YES @ 50c, exit yes_bid 45c → P&L = (45-50) * 1 = -5c
    assert b.realized_pnl_cents == pytest.approx(-5.0)


def test_per_market_report_avg_hold_time(decision_log: Path, tmp_db: Path):
    stats = per_market_report(decision_log, tmp_db)
    by_ticker = {s.market_ticker: s for s in stats}
    assert by_ticker["MKT-A"].avg_hold_seconds == pytest.approx(5.0)
    assert by_ticker["MKT-B"].avg_hold_seconds == pytest.approx(2.0)


def test_per_market_report_exit_modes(decision_log: Path, tmp_db: Path):
    stats = per_market_report(decision_log, tmp_db)
    by_ticker = {s.market_ticker: s for s in stats}
    assert by_ticker["MKT-A"].exit_modes == {"profit_capture": 1}
    assert by_ticker["MKT-B"].exit_modes == {"adverse_revaluation": 1}


# ---------- trade_patterns ----------


def test_quick_flip_detected_below_threshold(decision_log: Path):
    report = detect_patterns(decision_log, config=TradePatternConfig(quick_flip_max_s=10.0))
    # Both trades held <10s
    assert report.quick_flips == 2


def test_quick_flip_not_detected_above_threshold(decision_log: Path):
    report = detect_patterns(decision_log, config=TradePatternConfig(quick_flip_max_s=1.0))
    # Both held >1s
    assert report.quick_flips == 0


def test_chase_pattern_detected(tmp_path: Path):
    log = tmp_path / "chase.jsonl"
    rows = [
        {
            "ts_ms": 1_000,
            "market_ticker": "M",
            "action": "BUY_NO",
            "side": "no",
            "contracts": 1,
            "q_cal": 0.3,
            "yes_ask_cents": 71,
            "no_ask_cents": 30,
            "yes_bid_cents": 70,
            "no_bid_cents": 29,
            "diag": {},
        },
        {
            "ts_ms": 11_000,
            "market_ticker": "M",
            "action": "BUY_NO",
            "side": "no",
            "contracts": 1,
            "q_cal": 0.25,
            "yes_ask_cents": 76,
            "no_ask_cents": 25,
            "yes_bid_cents": 75,
            "no_bid_cents": 24,
            "diag": {},
        },
        {
            "ts_ms": 21_000,
            "market_ticker": "M",
            "action": "BUY_NO",
            "side": "no",
            "contracts": 1,
            "q_cal": 0.20,
            "yes_ask_cents": 81,
            "no_ask_cents": 20,
            "yes_bid_cents": 80,
            "no_bid_cents": 19,
            "diag": {},
        },
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    report = detect_patterns(
        log, config=TradePatternConfig(chase_window_s=60.0, chase_min_entries=2)
    )
    assert report.chases >= 1


# ---------- divergence_stats ----------


def test_divergence_stats_parses_diag(decision_log: Path):
    stats = divergence_stats(decision_log)
    # 4 decisions; only 4 have diag.regime_reason with divergence_logit
    assert stats.sample_count == 4
    assert stats.positive_count == 2
    assert stats.negative_count == 2


def test_divergence_stats_percentiles(decision_log: Path):
    stats = divergence_stats(decision_log)
    assert "p50" in stats.abs_percentiles
    assert "p99" in stats.abs_percentiles
    assert stats.max_abs == pytest.approx(3.0)


# ---------- backfill ----------


def test_parse_ticker_close_time_known_format():
    dt = parse_ticker_close_time("KXBTC15M-26MAY120815-15")
    assert dt is not None
    # 08:15 EDT = 12:15 UTC
    assert dt.year == 2026
    assert dt.month == 5
    assert dt.day == 12
    assert dt.hour == 12
    assert dt.minute == 15


def test_parse_ticker_close_time_returns_none_for_bad_format():
    assert parse_ticker_close_time("not-a-real-ticker") is None
    assert parse_ticker_close_time("KXBTC15M-NOTADATE-99") is None


def test_backfill_idempotent_on_empty_db(tmp_db: Path):
    result = backfill_from_lifecycle(tmp_db)
    assert result["lifecycle_tickers_seen"] == 0
    assert result["upserted"] == 0


def test_backfill_inserts_from_lifecycle_event(tmp_db: Path):
    # Insert a lifecycle event for a KXBTC15M market
    with connect(tmp_db) as conn:
        conn.execute(
            "INSERT INTO kalshi_lifecycle_event(received_ts_ms, market_ticker, raw_json) "
            "VALUES (?, ?, ?)",
            (
                1_000_000,
                "KXBTC15M-26MAY120815-15",
                json.dumps(
                    {
                        "msg": {
                            "event_type": "metadata_updated",
                            "market_ticker": "KXBTC15M-26MAY120815-15",
                            "floor_strike": 80756.58,
                        }
                    }
                ),
            ),
        )
        conn.commit()
    result = backfill_from_lifecycle(tmp_db)
    assert result["lifecycle_tickers_seen"] == 1
    assert result["upserted"] == 1
    with connect(tmp_db) as conn:
        row = conn.execute(
            "SELECT raw_json FROM market_dim WHERE ticker=?",
            ("KXBTC15M-26MAY120815-15",),
        ).fetchone()
    assert row is not None
    payload = json.loads(row["raw_json"])
    assert payload["floor_strike"] == 80756.58
