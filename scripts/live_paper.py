"""Continuous paper-trading decision streamer.

Tails a growing burn-in SQLite (written by ``engine-v2 capture-burnin``) and
feeds new events into the Backtester to produce a continuous JSONL decision
log with full factor / feature breakdowns.

Reuses the existing ``Backtester`` event-driven pipeline; the only thing this
script adds is a tail loop that polls the DB for new rows past a watermark
and re-fetches ``market_dim`` so rolling 15-minute markets are picked up.
"""

from __future__ import annotations

import argparse
import json
import signal
import sqlite3
import time
from pathlib import Path

from kalshi_btc_engine_v2.backtest.runner import (
    DEFAULT_DECISION_INTERVAL_MS,
    BacktestConfig,
    Backtester,
)
from kalshi_btc_engine_v2.cli import _BACKTEST_PRESETS, _apply_preset
from kalshi_btc_engine_v2.core.events import ReplayEvent
from kalshi_btc_engine_v2.policy.exits import ExitConfig
from kalshi_btc_engine_v2.policy.sizing import SizingConfig
from kalshi_btc_engine_v2.risk.guards import RiskConfig
from kalshi_btc_engine_v2.storage.sqlite import connect, fetch_all

# Same shape as replay.engine.REPLAY_SQL, but split per table and watermarked
# by each table's integer primary key. The previous UNION query watermarked on
# (COALESCE(exchange_ts_ms, received_ts_ms), event_id), which forced SQLite to
# repeatedly evaluate/sort a cross-table frontier. Primary-key tailing is the
# low-latency path; we sort the small fetched batch in Python before ingest.
TAIL_TABLES = ("kalshi_l2_event", "kalshi_trade_event", "spot_quote_event")

TAIL_SQL_BY_TABLE = {
    "kalshi_l2_event": """
SELECT
    COALESCE(exchange_ts_ms, received_ts_ms) AS event_time_ms,
    'kalshi_l2_event' AS table_name,
    event_id,
    received_ts_ms,
    exchange_ts_ms,
    market_ticker,
    event_type,
    seq,
    side,
    price,
    size,
    delta,
    yes_levels_json,
    no_levels_json,
    best_yes_bid,
    best_yes_ask,
    spread,
    source_channel,
    raw_json
FROM kalshi_l2_event
WHERE event_id > ?
ORDER BY event_id
LIMIT ?
""",
    "kalshi_trade_event": """
SELECT
    COALESCE(exchange_ts_ms, received_ts_ms) AS event_time_ms,
    'kalshi_trade_event' AS table_name,
    event_id,
    received_ts_ms,
    exchange_ts_ms,
    market_ticker,
    NULL AS event_type,
    NULL AS seq,
    side,
    price,
    count AS size,
    NULL AS delta,
    NULL AS yes_levels_json,
    NULL AS no_levels_json,
    NULL AS best_yes_bid,
    NULL AS best_yes_ask,
    NULL AS spread,
    NULL AS source_channel,
    raw_json
FROM kalshi_trade_event
WHERE event_id > ?
ORDER BY event_id
LIMIT ?
""",
    "spot_quote_event": """
SELECT
    COALESCE(exchange_ts_ms, received_ts_ms) AS event_time_ms,
    'spot_quote_event' AS table_name,
    event_id,
    received_ts_ms,
    exchange_ts_ms,
    NULL AS market_ticker,
    NULL AS event_type,
    NULL AS seq,
    NULL AS side,
    mid AS price,
    NULL AS size,
    NULL AS delta,
    NULL AS yes_levels_json,
    NULL AS no_levels_json,
    NULL AS best_yes_bid,
    NULL AS best_yes_ask,
    NULL AS spread,
    venue AS source_channel,
    raw_json
FROM spot_quote_event
WHERE event_id > ?
ORDER BY event_id
LIMIT ?
""",
}


def _row_to_event(row) -> ReplayEvent:
    payload = dict(row)
    table = str(payload.pop("table_name"))
    event_time_ms = int(payload.pop("event_time_ms"))
    event_id = int(payload["event_id"])
    return ReplayEvent(
        event_time_ms=event_time_ms,
        table=table,
        event_id=event_id,
        payload=payload,
    )


def _row_sort_key(row: sqlite3.Row) -> tuple[int, str, int]:
    return int(row["event_time_ms"] or 0), str(row["table_name"]), int(row["event_id"])


def _fetch_tail_rows(
    conn: sqlite3.Connection,
    watermarks: dict[str, int],
    *,
    limit_per_table: int,
) -> list[sqlite3.Row]:
    rows: list[sqlite3.Row] = []
    limit = max(1, int(limit_per_table))
    for table in TAIL_TABLES:
        rows.extend(conn.execute(TAIL_SQL_BY_TABLE[table], (watermarks[table], limit)).fetchall())
    rows.sort(key=_row_sort_key)
    return rows


def _max_event_time_ms(conn: sqlite3.Connection) -> int | None:
    values: list[int] = []
    for table in TAIL_TABLES:
        row = conn.execute(f"""
            SELECT MAX(COALESCE(exchange_ts_ms, received_ts_ms))
            FROM {table}
            """).fetchone()
        if row is not None and row[0] is not None:
            values.append(int(row[0]))
    return max(values) if values else None


def _tail_watermarks(
    conn: sqlite3.Connection,
    *,
    start_at_tail: bool,
    warmup_lookback_s: float,
) -> dict[str, int]:
    watermarks = dict.fromkeys(TAIL_TABLES, 0)
    if not start_at_tail:
        return watermarks

    max_ts = _max_event_time_ms(conn)
    if max_ts is None:
        return watermarks
    cutoff_ms = max_ts - int(max(0.0, warmup_lookback_s) * 1000.0)
    for table in TAIL_TABLES:
        row = conn.execute(
            f"""
            SELECT COALESCE(MAX(event_id), 0)
            FROM {table}
            WHERE COALESCE(exchange_ts_ms, received_ts_ms) < ?
            """,
            (cutoff_ms,),
        ).fetchone()
        watermarks[table] = int(row[0] or 0)
    return watermarks


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="live-paper",
        description="Tail a growing burn-in SQLite and stream paper decisions.",
    )
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--decision-log", required=True, type=Path)
    parser.add_argument("--bankroll", type=float, default=200.0)
    parser.add_argument(
        "--decision-interval-ms",
        type=int,
        default=DEFAULT_DECISION_INTERVAL_MS,
        help="Decision cadence in event-time milliseconds (default 250ms).",
    )
    parser.add_argument("--min-returns", type=int, default=30)
    parser.add_argument("--window-cap-dollars", type=float, default=15.0)
    parser.add_argument("--fractional-kelly", type=float, default=0.20)
    parser.add_argument("--max-contracts", type=int, default=100)
    parser.add_argument(
        "--poll-interval-s",
        type=float,
        default=0.25,
        help="Wall-clock DB polling interval in seconds (default 0.25s).",
    )
    parser.add_argument(
        "--status-every-s",
        type=float,
        default=15.0,
        help="Print a one-line status to stderr every N seconds.",
    )
    parser.add_argument(
        "--adverse-ev-cents",
        type=float,
        default=-0.6,
        help="Adverse-revaluation exit threshold in cents (default -0.6). "
        "Set to -100.0 for never-bail (hold-through-wiggles).",
    )
    parser.add_argument(
        "--spot-circuit-breaker-bp",
        type=float,
        default=0.0,
        help="Exit when spot moves against entry by this many bp (default 0 disables).",
    )
    parser.add_argument(
        "--profit-capture-enabled",
        dest="profit_capture_enabled",
        action="store_true",
        default=True,
        help="Enable the profit_capture early-exit branch (default ON).",
    )
    parser.add_argument(
        "--no-profit-capture",
        dest="profit_capture_enabled",
        action="store_false",
        help="Disable the profit_capture branch (hold-to-settlement-pure).",
    )
    parser.add_argument(
        "--q-cal-min",
        type=float,
        default=0.0,
        help="Minimum calibrated probability to allow an entry (0.10 vetoes "
        "extreme-confidence entries the model gets wrong).",
    )
    parser.add_argument(
        "--q-cal-max",
        type=float,
        default=1.0,
        help="Maximum calibrated probability to allow an entry (0.90 vetoes "
        "extreme-confidence entries the model gets wrong).",
    )
    parser.add_argument(
        "--fee-floor-max-contracts",
        type=int,
        default=3,
        help="Fee-floor veto threshold; see backtest --help.",
    )
    parser.add_argument(
        "--fee-floor-off-center-band",
        type=float,
        default=0.10,
        help="Near-center band where fee-floor veto does not apply.",
    )
    parser.add_argument(
        "--fee-floor-min-edge-cents",
        type=float,
        default=4.0,
        help="Min edge for small-size off-center entries.",
    )
    parser.add_argument(
        "--preset",
        type=str,
        default=None,
        choices=sorted(_BACKTEST_PRESETS),
        help="Apply a named parameter preset (same names as the backtest "
        "command). CLI args override preset.",
    )
    parser.add_argument(
        "--metrics-log",
        type=Path,
        default=None,
        help="Optional JSONL path for live-paper loop telemetry: lag, query "
        "time, ingest time, loop time, duty cycle, decisions, fills.",
    )
    parser.add_argument(
        "--tail-batch-limit",
        type=int,
        default=5000,
        help="Maximum new rows to fetch per source table per poll (default 5000).",
    )
    parser.add_argument(
        "--start-at-tail",
        action="store_true",
        help="Start from the live tail instead of replaying the full DB. Uses "
        "--warmup-lookback-s to seed enough recent book/spot state.",
    )
    parser.add_argument(
        "--warmup-lookback-s",
        type=float,
        default=1200.0,
        help="When --start-at-tail is set, replay this many seconds of recent "
        "history before tailing live rows (default 1200s, enough to include "
        "the active 15-minute market's initial book snapshot).",
    )
    # Fields the preset can override (must exist for _apply_preset to read).
    parser.set_defaults(tradeable_regimes=None)
    args = parser.parse_args()
    _apply_preset(args)

    if not args.db.exists():
        print(f"db not found: {args.db}")
        return 1

    tradeable_regimes_override = (
        tuple(r.strip() for r in args.tradeable_regimes.split(",") if r.strip())
        if args.tradeable_regimes
        else None
    )
    config = BacktestConfig(
        bankroll_dollars=args.bankroll,
        decision_interval_ms=args.decision_interval_ms,
        min_returns_for_decision=args.min_returns,
        risk_config=RiskConfig(max_risk_per_window_dollars=args.window_cap_dollars),
        sizing_config=SizingConfig(
            fractional_kelly=args.fractional_kelly,
            max_contracts=args.max_contracts,
            fee_floor_max_contracts=args.fee_floor_max_contracts,
            fee_floor_off_center_band=args.fee_floor_off_center_band,
            fee_floor_min_edge_cents=args.fee_floor_min_edge_cents,
        ),
        exit_config=ExitConfig(
            adverse_ev_cents=args.adverse_ev_cents,
            spot_circuit_breaker_bp=args.spot_circuit_breaker_bp,
            profit_capture_enabled=args.profit_capture_enabled,
        ),
        q_cal_min=args.q_cal_min,
        q_cal_max=args.q_cal_max,
        tradeable_regimes_override=tradeable_regimes_override,
    )
    bt = Backtester(config=config)

    # Open decision log in append mode so restarts don't truncate.
    args.decision_log.parent.mkdir(parents=True, exist_ok=True)
    bt.decision_log_path = args.decision_log
    bt._decision_log_fp = args.decision_log.open("a", encoding="utf-8")
    metrics_fp = None
    if args.metrics_log is not None:
        args.metrics_log.parent.mkdir(parents=True, exist_ok=True)
        metrics_fp = args.metrics_log.open("a", encoding="utf-8")

    stop = {"flag": False}

    def _handle(signum, frame):  # noqa: ANN001
        stop["flag"] = True

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    with connect(args.db) as conn:
        table_watermarks = _tail_watermarks(
            conn,
            start_at_tail=args.start_at_tail,
            warmup_lookback_s=args.warmup_lookback_s,
        )
    watermark_ms = -1
    last_status_t = time.time()
    last_decisions = 0
    last_fills = 0
    last_event_count = 0
    last_market_dim_refresh = 0.0
    last_loop_metrics: dict[str, float | int | None] = {}

    print(
        f"[live-paper] starting tail of {args.db} -> {args.decision_log}; "
        f"table_watermarks={table_watermarks}",
        flush=True,
    )

    try:
        while not stop["flag"]:
            loop_start = time.perf_counter()
            now = time.time()

            # Refresh market_dim every 30s so rolling 15-min markets show up.
            market_dim_refresh_ms = 0.0
            if now - last_market_dim_refresh > 30.0:
                refresh_start = time.perf_counter()
                with connect(args.db) as conn:
                    for row in fetch_all(conn, "SELECT * FROM market_dim"):
                        bt.upsert_market_dim(str(row["ticker"]), dict(row))
                last_market_dim_refresh = now
                market_dim_refresh_ms = (time.perf_counter() - refresh_start) * 1000.0

            # Pull new rows by table-primary-key watermarks. This avoids the
            # cross-table COALESCE/ORDER BY scan that dominated the 250ms loop.
            query_start = time.perf_counter()
            with connect(args.db) as conn:
                rows = _fetch_tail_rows(
                    conn,
                    table_watermarks,
                    limit_per_table=args.tail_batch_limit,
                )
            query_ms = (time.perf_counter() - query_start) * 1000.0

            ingest_start = time.perf_counter()
            for row in rows:
                event = _row_to_event(row)
                bt._ingest(event)
                table = str(row["table_name"])
                table_watermarks[table] = max(table_watermarks[table], event.event_id)
                if event.event_time_ms > watermark_ms:
                    watermark_ms = event.event_time_ms
            ingest_ms = (time.perf_counter() - ingest_start) * 1000.0
            loop_ms = (time.perf_counter() - loop_start) * 1000.0
            event_lag_ms = int(time.time() * 1000) - watermark_ms if watermark_ms > 0 else None
            rows_per_s = len(rows) / (loop_ms / 1000.0) if loop_ms > 0 else 0.0
            duty_cycle = loop_ms / max(args.poll_interval_s * 1000.0, 1.0)
            last_loop_metrics = {
                "rows": len(rows),
                "query_ms": round(query_ms, 3),
                "ingest_ms": round(ingest_ms, 3),
                "market_dim_refresh_ms": round(market_dim_refresh_ms, 3),
                "loop_ms": round(loop_ms, 3),
                "event_lag_ms": event_lag_ms,
                "rows_per_s": round(rows_per_s, 1),
                "duty_cycle": round(duty_cycle, 4),
                "active_watermark_tables": sum(
                    1 for table in TAIL_TABLES if table_watermarks[table] > 0
                ),
            }

            if now - last_status_t >= args.status_every_s:
                decisions = len(bt._decisions)
                fills = len(bt.executor.fills)
                events_total = bt._events_processed
                delta_dec = decisions - last_decisions
                delta_fills = fills - last_fills
                delta_events = events_total - last_event_count
                last_decisions = decisions
                last_fills = fills
                last_event_count = events_total
                last_status_t = now
                positions = sum(1 for p in bt.executor.positions.values() if not p.is_flat)
                status_record = {
                    "ts_wall_ms": int(time.time() * 1000),
                    "events": events_total,
                    "events_delta": delta_events,
                    "decisions": decisions,
                    "decisions_delta": delta_dec,
                    "fills": fills,
                    "fills_delta": delta_fills,
                    "open_positions": positions,
                    "watermark_ms": watermark_ms,
                    "table_watermarks": dict(table_watermarks),
                    "decision_interval_ms": args.decision_interval_ms,
                    "poll_interval_s": args.poll_interval_s,
                    **last_loop_metrics,
                }
                print(
                    f"[live-paper] events={events_total} (+{delta_events}) "
                    f"decisions={decisions} (+{delta_dec}) "
                    f"fills={fills} (+{delta_fills}) "
                    f"open_positions={positions} watermark_ms={watermark_ms} "
                    f"lag_ms={last_loop_metrics.get('event_lag_ms')} "
                    f"loop_ms={last_loop_metrics.get('loop_ms')} "
                    f"query_ms={last_loop_metrics.get('query_ms')} "
                    f"ingest_ms={last_loop_metrics.get('ingest_ms')} "
                    f"duty={last_loop_metrics.get('duty_cycle')}",
                    flush=True,
                )
                if metrics_fp is not None:
                    metrics_fp.write(json.dumps(status_record, separators=(",", ":")) + "\n")
                    metrics_fp.flush()

            time.sleep(args.poll_interval_s)
    finally:
        bt.close()
        if metrics_fp is not None:
            metrics_fp.close()

    print(
        f"[live-paper] stopped. final events={bt._events_processed} "
        f"decisions={len(bt._decisions)} fills={len(bt.executor.fills)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
