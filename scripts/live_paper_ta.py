"""Standalone Pine Script paper trader.

Tails a growing burn-in SQLite (the one ``engine-v2 capture-burnin`` is
writing), aggregates BTC spot mids into 1-minute OHLC bars, computes the
Pine Script directional score, and "buys" Kalshi binaries (YES on CALL,
NO on PUT) at the current contract ask. Records decisions, fills, and
realized P&L to a JSONL log.

This is a pure directional predictor — it does NOT use the contract
fair-value model or the q_cal cascade. It is the BTC-up/down strategy
the user validated on TradingView, ported to interact with Kalshi
binaries.

Runs alongside the existing ``live_paper.py`` without conflict —
different decision-log file, different process, same source DB.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import signal
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from kalshi_btc_engine_v2.features.ta_score import (
    OHLCBar,
    TAScoreConfig,
    TAScoreState,
    evaluate_entry,
)
from kalshi_btc_engine_v2.policy.edge import kalshi_taker_fee_cents
from kalshi_btc_engine_v2.storage.sqlite import connect

# Default spot venue to derive 1-min OHLC bars from. Coinbase is the most
# liquid USD venue and matches the engine's default reference.
DEFAULT_SPOT_VENUE = "coinbase"

# 15-minute cycle in milliseconds.
CYCLE_MS = 15 * 60 * 1000
# Minute in milliseconds.
MIN_MS = 60 * 1000


def minute_floor_ms(ts_ms: int) -> int:
    return (ts_ms // MIN_MS) * MIN_MS


def cycle_floor_ms(ts_ms: int) -> int:
    return (ts_ms // CYCLE_MS) * CYCLE_MS


def bars_in_cycle_for_minute(minute_ms: int) -> int:
    """1-indexed position of this minute within its 15-min cycle. Range: 1..15."""
    return ((minute_ms - cycle_floor_ms(minute_ms)) // MIN_MS) + 1


class MinuteBarAggregator:
    """Build 1-min OHLC bars from spot quote events as they stream in.

    We treat each quote's ``mid`` as a tick. The bar for minute M is closed
    (emitted) when we see the first quote at minute M+1 or later. The
    aggregator yields completed bars in event-time order.
    """

    def __init__(self) -> None:
        self._current_minute: int | None = None
        self._open: float | None = None
        self._high: float = -1.0
        self._low: float = 1e18
        self._close: float = 0.0
        self._count: int = 0

    def ingest(self, ts_ms: int, mid: float) -> OHLCBar | None:
        """Ingest one mid tick. Returns a completed OHLCBar if this tick
        crossed a minute boundary, else None.

        cycle_open_price and bars_in_cycle on the returned bar must be set
        by the caller — the aggregator only knows raw OHLC.
        """
        m = minute_floor_ms(ts_ms)
        if self._current_minute is None:
            self._current_minute = m
            self._open = mid
            self._high = mid
            self._low = mid
            self._close = mid
            self._count = 1
            return None
        if m == self._current_minute:
            self._high = max(self._high, mid)
            self._low = min(self._low, mid)
            self._close = mid
            self._count += 1
            return None
        # m > current_minute: close out the current bar
        completed = OHLCBar(
            ts_minute_ms=self._current_minute,
            open=self._open or mid,
            high=self._high,
            low=self._low,
            close=self._close,
            volume=None,
            cycle_open_price=0.0,  # filled by caller
            bars_in_cycle=0,        # filled by caller
        )
        self._current_minute = m
        self._open = mid
        self._high = mid
        self._low = mid
        self._close = mid
        self._count = 1
        return completed


class CycleTracker:
    """Tracks per-15-min-cycle state: open price, score state, decision lock,
    consecutive-bar streaks. One instance per running process."""

    def __init__(self) -> None:
        self._current_cycle_floor: int | None = None
        self._cycle_open_price: float | None = None
        self.score_state = TAScoreState()
        self.decided_side: str | None = None  # 'call' / 'put' / None
        self.decided_at_bar: int | None = None
        self.decided_at_ts_ms: int | None = None
        self.consecutive_call_bars: int = 0
        self.consecutive_put_bars: int = 0

    def maybe_roll_cycle(self, minute_ms: int, open_price: float) -> bool:
        """If this minute_ms starts a new cycle, reset state. Returns True
        on a roll."""
        cf = cycle_floor_ms(minute_ms)
        if self._current_cycle_floor is None or cf != self._current_cycle_floor:
            self._current_cycle_floor = cf
            self._cycle_open_price = open_price
            self.score_state = TAScoreState(config=self.score_state.config)
            self.decided_side = None
            self.decided_at_bar = None
            self.decided_at_ts_ms = None
            self.consecutive_call_bars = 0
            self.consecutive_put_bars = 0
            return True
        return False

    @property
    def cycle_floor_ms(self) -> int | None:
        return self._current_cycle_floor

    @property
    def cycle_close_ms(self) -> int | None:
        if self._current_cycle_floor is None:
            return None
        return self._current_cycle_floor + CYCLE_MS

    @property
    def cycle_open_price(self) -> float | None:
        return self._cycle_open_price


def find_atm_market(
    conn: sqlite3.Connection,
    cycle_close_ms: int,
) -> tuple[str, dict[str, Any]] | None:
    """Find the KXBTC15M market whose close_time matches the current cycle's
    end. Among multiple strikes, pick the one whose latest L2 mid is closest
    to 50¢ (most balanced market).

    Returns (ticker, latest_l2_payload) or None if no market matches.
    """
    cycle_close_iso = dt.datetime.fromtimestamp(cycle_close_ms / 1000, tz=dt.UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    # Find candidate tickers via market_dim close_time
    candidates = [
        str(r[0])
        for r in conn.execute(
            "SELECT ticker FROM market_dim WHERE close_time = ? AND ticker LIKE 'KXBTC15M-%'",
            (cycle_close_iso,),
        ).fetchall()
    ]
    if not candidates:
        # Fallback: scan lifecycle for tickers with matching close_time
        rows = conn.execute(
            """
            SELECT DISTINCT market_ticker FROM kalshi_lifecycle_event
            WHERE close_time = ? AND market_ticker LIKE 'KXBTC15M-%'
            """,
            (cycle_close_iso,),
        ).fetchall()
        candidates = [str(r[0]) for r in rows]
    if not candidates:
        return None

    # For each candidate, fetch latest L2 to get current ask/bid
    best: tuple[str, dict[str, Any], float] | None = None
    for ticker in candidates:
        row = conn.execute(
            """
            SELECT received_ts_ms, best_yes_bid, best_yes_ask, raw_json
            FROM kalshi_l2_event
            WHERE market_ticker = ?
            ORDER BY event_id DESC
            LIMIT 1
            """,
            (ticker,),
        ).fetchone()
        if not row or row["best_yes_ask"] is None:
            continue
        yes_ask = float(row["best_yes_ask"])
        distance_to_atm = abs(yes_ask - 0.50)
        payload = {
            "ticker": ticker,
            "received_ts_ms": int(row["received_ts_ms"]),
            "yes_bid": float(row["best_yes_bid"]) if row["best_yes_bid"] is not None else None,
            "yes_ask": yes_ask,
        }
        if best is None or distance_to_atm < best[2]:
            best = (ticker, payload, distance_to_atm)
    if best is None:
        return None
    return best[0], best[1]


def lookup_settlement(conn: sqlite3.Connection, ticker: str) -> str | None:
    """Return 'yes' or 'no' if the market has a determined lifecycle event,
    else None."""
    row = conn.execute(
        """
        SELECT raw_json FROM kalshi_lifecycle_event
        WHERE market_ticker = ? AND status = 'determined'
        ORDER BY event_id DESC LIMIT 1
        """,
        (ticker,),
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        j = json.loads(row[0])
        msg = j.get("msg", j)
        return msg.get("result")
    except Exception:  # noqa: BLE001
        return None


def _spot_tail_sql() -> str:
    return """
    SELECT event_id, received_ts_ms, mid
    FROM spot_quote_event
    WHERE venue = ? AND event_id > ?
      AND mid IS NOT NULL
    ORDER BY event_id
    LIMIT ?
    """


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="live-paper-ta",
        description="Pine Script directional paper trader against Kalshi binaries.",
    )
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--decision-log", required=True, type=Path)
    parser.add_argument("--venue", default=DEFAULT_SPOT_VENUE,
                        help=f"Spot venue for OHLC construction (default {DEFAULT_SPOT_VENUE})")
    parser.add_argument("--base-stake", type=float, default=1.0,
                        help="Base contract stake multiplied by tier (default 1.0).")
    parser.add_argument("--poll-interval-s", type=float, default=0.5)
    parser.add_argument("--status-every-s", type=float, default=30.0)
    parser.add_argument("--start-at-tail", action="store_true",
                        help="Skip historical events; start fresh from the live tail.")
    parser.add_argument(
        "--stale-venue-timeout-s",
        type=float,
        default=600.0,
        help="Exit with code 2 if no new events from --venue arrive within this "
        "many seconds (default 600s). Watchdog should restart on stalls so that "
        "venue drop-outs don't silently freeze the trader.",
    )
    args = parser.parse_args()

    if not args.db.exists():
        print(f"db not found: {args.db}")
        return 1

    args.decision_log.parent.mkdir(parents=True, exist_ok=True)
    log_fp = args.decision_log.open("a", encoding="utf-8")

    aggregator = MinuteBarAggregator()
    cycle = CycleTracker()
    cfg = TAScoreConfig()

    stop = {"flag": False}

    def _handle(signum, frame):  # noqa: ANN001
        stop["flag"] = True
    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    # Watermark on spot_quote_event.event_id for the chosen venue.
    spot_event_id_watermark = -1
    if args.start_at_tail:
        with connect(args.db) as conn:
            row = conn.execute(
                "SELECT MAX(event_id) FROM spot_quote_event WHERE venue = ?",
                (args.venue,),
            ).fetchone()
            if row and row[0] is not None:
                spot_event_id_watermark = int(row[0])

    # Track open paper positions (per market) and counters.
    open_positions: dict[str, dict[str, Any]] = {}
    settled_trades: list[dict[str, Any]] = []
    decisions_made = 0
    fills = 0
    last_status_t = time.time()
    last_dec = 0
    last_fills = 0
    last_new_event_t = time.time()  # wall-clock of last new-event ingestion

    print(
        f"[live-paper-ta] starting tail of {args.db} venue={args.venue} -> {args.decision_log}",
        flush=True,
    )

    try:
        while not stop["flag"]:
            now_wall = time.time()
            with connect(args.db) as conn:
                rows = conn.execute(
                    _spot_tail_sql(), (args.venue, spot_event_id_watermark, 5000)
                ).fetchall()
                eid_max = spot_event_id_watermark
                for row in rows:
                    eid_max = max(eid_max, int(row["event_id"]))
                    ts_ms = int(row["received_ts_ms"])
                    mid_raw = row["mid"]
                    if mid_raw is None:
                        continue
                    mid = float(mid_raw)
                    completed_bar = aggregator.ingest(ts_ms, mid)
                    if completed_bar is None:
                        continue
                    # Roll cycle if needed; cycle_open_price is the FIRST bar's
                    # open price in the cycle. If we just rolled, use this bar's
                    # open as the cycle open.
                    rolled = cycle.maybe_roll_cycle(completed_bar.ts_minute_ms, completed_bar.open)
                    cycle_open = cycle.cycle_open_price
                    if cycle_open is None:
                        continue
                    bar_in_cycle = bars_in_cycle_for_minute(completed_bar.ts_minute_ms)
                    bar = OHLCBar(
                        ts_minute_ms=completed_bar.ts_minute_ms,
                        open=completed_bar.open,
                        high=completed_bar.high,
                        low=completed_bar.low,
                        close=completed_bar.close,
                        volume=completed_bar.volume,
                        cycle_open_price=cycle_open,
                        bars_in_cycle=bar_in_cycle,
                    )
                    snap = cycle.score_state.update(bar)

                    # Streaks: only count consecutive qualifying bars
                    bull_quals = snap.bull_tier >= 1
                    bear_quals = snap.bear_tier >= 1
                    cycle.consecutive_call_bars = (
                        cycle.consecutive_call_bars + 1 if bull_quals else 0
                    )
                    cycle.consecutive_put_bars = (
                        cycle.consecutive_put_bars + 1 if bear_quals else 0
                    )

                    hour_utc = dt.datetime.fromtimestamp(bar.ts_minute_ms / 1000, tz=dt.UTC).hour
                    decisions_made += 1
                    decision = evaluate_entry(
                        snap,
                        config=cfg,
                        hour_utc=hour_utc,
                        already_decided=cycle.decided_side is not None,
                        consecutive_call_bars=cycle.consecutive_call_bars - (1 if bull_quals else 0),
                        consecutive_put_bars=cycle.consecutive_put_bars - (1 if bear_quals else 0),
                    )
                    log_record: dict[str, Any] = {
                        "kind": "snapshot",
                        "ts_minute_ms": bar.ts_minute_ms,
                        "hour_utc": hour_utc,
                        "bar_in_cycle": bar_in_cycle,
                        "cycle_floor_ms": cycle.cycle_floor_ms,
                        "cycle_open_price": cycle_open,
                        "spot_close": bar.close,
                        "score": snap.score,
                        "score_velocity": snap.score_velocity,
                        "bull_conf": snap.bull_conf,
                        "bear_conf": snap.bear_conf,
                        "bull_tier": snap.bull_tier,
                        "bear_tier": snap.bear_tier,
                        "cycle_return_pct": snap.cycle_return_pct,
                        "ema_spread_pct": snap.ema_spread_pct,
                        "rsi": snap.rsi,
                        "candle_pressure": snap.candle_pressure,
                    }
                    if decision is not None and cycle.decided_side is None:
                        # Lock in the cycle's decision
                        cycle.decided_side = decision.side
                        cycle.decided_at_bar = decision.locked_at_bar
                        cycle.decided_at_ts_ms = decision.locked_at_ts_ms
                        cycle_close = cycle.cycle_close_ms
                        market = find_atm_market(conn, cycle_close) if cycle_close else None
                        if market is None:
                            log_record["kind"] = "decision_no_market"
                            log_record["decided_side"] = decision.side
                            log_record["tier"] = decision.tier
                            log_record["tier_name"] = decision.tier_name
                            log_record["forced"] = decision.forced
                        else:
                            ticker, mkt = market
                            yes_ask_cents = int(round(mkt["yes_ask"] * 100))
                            no_ask_cents = 100 - yes_ask_cents
                            is_call = decision.side == "call"
                            entry_cents = yes_ask_cents if is_call else no_ask_cents
                            contracts = max(1, int(round(args.base_stake * decision.stake_multiplier)))
                            entry_fee = kalshi_taker_fee_cents(entry_cents, count=contracts)
                            open_positions[ticker] = {
                                "ticker": ticker,
                                "side": "yes" if is_call else "no",
                                "contracts": contracts,
                                "entry_price_cents": entry_cents,
                                "entry_fee_cents": entry_fee,
                                "decided_at_bar": decision.locked_at_bar,
                                "decided_at_ts_ms": decision.locked_at_ts_ms,
                                "tier": decision.tier,
                                "tier_name": decision.tier_name,
                                "confidence": decision.confidence,
                                "cycle_close_ms": cycle_close,
                            }
                            fills += 1
                            log_record["kind"] = "fill"
                            log_record["decided_side"] = decision.side
                            log_record["tier"] = decision.tier
                            log_record["tier_name"] = decision.tier_name
                            log_record["forced"] = decision.forced
                            log_record["ticker"] = ticker
                            log_record["entry_price_cents"] = entry_cents
                            log_record["entry_fee_cents"] = entry_fee
                            log_record["contracts"] = contracts

                    log_fp.write(json.dumps(log_record, default=str) + "\n")
                    log_fp.flush()

                # Settlement reconciliation: walk open positions and check
                # whether their markets have determined.
                if open_positions:
                    for ticker in list(open_positions.keys()):
                        outcome = lookup_settlement(conn, ticker)
                        if outcome is None:
                            continue
                        pos = open_positions.pop(ticker)
                        n = pos["contracts"]
                        if pos["side"] == outcome:
                            gross = n * (100 - pos["entry_price_cents"])
                        else:
                            gross = -n * pos["entry_price_cents"]
                        # Settlement carries no exit fee
                        net = gross - pos["entry_fee_cents"]
                        trade = {**pos, "outcome": outcome, "gross_cents": gross,
                                 "net_cents": net, "settled_via": "settlement"}
                        settled_trades.append(trade)
                        log_fp.write(json.dumps({"kind": "settle", **trade}, default=str) + "\n")
                        log_fp.flush()

                if eid_max > spot_event_id_watermark:
                    spot_event_id_watermark = eid_max
                    last_new_event_t = now_wall

            # Stale-venue self-exit: if no new events for the configured
            # timeout, exit with code 2 so the watchdog can restart us
            # (potentially switching to a fresher venue).
            if now_wall - last_new_event_t > args.stale_venue_timeout_s:
                print(
                    f"[live-paper-ta] STALE venue={args.venue} for {now_wall-last_new_event_t:.0f}s. "
                    f"Exiting for watchdog restart.",
                    flush=True,
                )
                log_fp.close()
                return 2

            if now_wall - last_status_t >= args.status_every_s:
                wr = sum(1 for t in settled_trades if t["net_cents"] > 0)
                total_net = sum(t["net_cents"] for t in settled_trades)
                print(
                    f"[live-paper-ta] decisions={decisions_made} (+{decisions_made-last_dec}) "
                    f"fills={fills} (+{fills-last_fills}) "
                    f"open={len(open_positions)} settled={len(settled_trades)} "
                    f"wr={wr}/{len(settled_trades)} net={total_net:+d}c "
                    f"watermark_eid={spot_event_id_watermark}",
                    flush=True,
                )
                last_status_t = now_wall
                last_dec = decisions_made
                last_fills = fills
            time.sleep(args.poll_interval_s)
    finally:
        log_fp.close()
    print(f"[live-paper-ta] stopped. decisions={decisions_made} fills={fills} settled={len(settled_trades)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
