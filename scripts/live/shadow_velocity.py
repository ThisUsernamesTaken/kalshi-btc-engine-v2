"""Shadow paper trader: BTC spot velocity entry signal.

Runs alongside the live v2 trader. Reads the same capture DB read-only,
makes paper-only decisions, logs to its own JSONL. No real orders.

Premise: when BTC spot moves >$20 in 15s (or >$30 in 30s) one direction,
that's a fast momentum burst the slower TA cycle misses. We paper-buy
ATM YES on an up-burst, NO on a down-burst, one entry per 15-min cycle.
Settlement reconciliation reuses the live_paper_ta.py logic.

Output JSONL kinds:
- velocity_signal: paper fill (entry took place)
- near_miss: |delta_15s| reached --near-miss-threshold but not --threshold-15s
- settle: market determined, outcome + P&L
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import signal
import sqlite3
import time
from collections import deque
from pathlib import Path
from typing import Any

from kalshi_btc_engine_v2.policy.edge import kalshi_taker_fee_cents
from kalshi_btc_engine_v2.storage.sqlite import connect

# 15-minute cycle in ms (matches live_ta.py / live_paper_ta.py).
CYCLE_MS = 15 * 60 * 1000


def cycle_floor_ms(ts_ms: int) -> int:
    return (ts_ms // CYCLE_MS) * CYCLE_MS


def find_atm_market(
    conn: sqlite3.Connection,
    cycle_close_ms: int,
) -> tuple[str, dict[str, Any]] | None:
    """Find the KXBTC15M market closing at cycle_close_ms whose latest
    yes_ask is closest to 50¢. Mirrors live_paper_ta.find_atm_market.
    """
    cycle_close_iso = dt.datetime.fromtimestamp(
        cycle_close_ms / 1000, tz=dt.UTC
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    candidates = [
        str(r[0])
        for r in conn.execute(
            "SELECT ticker FROM market_dim WHERE close_time = ? AND ticker LIKE 'KXBTC15M-%'",
            (cycle_close_iso,),
        ).fetchall()
    ]
    if not candidates:
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

    best: tuple[str, dict[str, Any], float] | None = None
    for ticker in candidates:
        row = conn.execute(
            """
            SELECT received_ts_ms, best_yes_bid, best_yes_ask
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
        payload = {
            "ticker": ticker,
            "received_ts_ms": int(row["received_ts_ms"]),
            "yes_bid": float(row["best_yes_bid"]) if row["best_yes_bid"] is not None else None,
            "yes_ask": yes_ask,
        }
        d = abs(yes_ask - 0.50)
        if best is None or d < best[2]:
            best = (ticker, payload, d)
    if best is None:
        return None
    return best[0], best[1]


def lookup_settlement(conn: sqlite3.Connection, ticker: str) -> str | None:
    """Return 'yes' or 'no' from a determined lifecycle event, else None."""
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


def mid_at_or_before(window: deque, target_ts_ms: int) -> tuple[int, float] | None:
    """Find the latest tick in window with ts_ms <= target_ts_ms.

    window holds (ts_ms, mid) sorted by ts_ms ascending. Linear scan from
    the right — windows are <= 30s of ticks, small.
    """
    best: tuple[int, float] | None = None
    for ts_ms, mid in window:
        if ts_ms <= target_ts_ms:
            best = (ts_ms, mid)
        else:
            break
    return best


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="shadow-velocity",
        description="Paper-only BTC spot velocity entry signal vs Kalshi 15M binaries.",
    )
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--decision-log", required=True, type=Path)
    parser.add_argument("--venue", default="bitstamp",
                        help="Spot venue for velocity (default bitstamp — coinbase "
                             "has stalled silently in past).")
    parser.add_argument("--threshold-15s", type=float, default=20.0,
                        help="|delta| in USD over 15s to trigger entry (default 20).")
    parser.add_argument("--threshold-30s", type=float, default=30.0,
                        help="|delta| in USD over 30s to trigger entry (default 30).")
    parser.add_argument("--near-miss-threshold", type=float, default=15.0,
                        help="|delta_15s| floor for near_miss logs (default 15).")
    parser.add_argument("--base-stake", type=int, default=1,
                        help="Contracts per paper entry (default 1).")
    parser.add_argument("--poll-interval-s", type=float, default=1.0)
    parser.add_argument("--status-every-s", type=float, default=30.0)
    parser.add_argument("--near-miss-cooldown-s", type=float, default=5.0,
                        help="Min seconds between near_miss logs per direction.")
    parser.add_argument("--start-at-tail", action="store_true",
                        help="Skip historical events; start from live tail.")
    parser.add_argument(
        "--stale-venue-timeout-s", type=float, default=600.0,
        help="Exit 2 if no new events from --venue for this long.",
    )
    args = parser.parse_args()

    if not args.db.exists():
        print(f"db not found: {args.db}")
        return 1

    args.decision_log.parent.mkdir(parents=True, exist_ok=True)
    log_fp = args.decision_log.open("a", encoding="utf-8")

    stop = {"flag": False}

    def _handle(signum, frame):  # noqa: ANN001
        stop["flag"] = True
    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    spot_event_id_watermark = -1
    if args.start_at_tail:
        with connect(args.db) as conn:
            row = conn.execute(
                "SELECT MAX(event_id) FROM spot_quote_event WHERE venue = ?",
                (args.venue,),
            ).fetchone()
            if row and row[0] is not None:
                spot_event_id_watermark = int(row[0])

    # Rolling window of (ts_ms, mid) covering last ~35s (buffer over 30s).
    window: deque = deque()
    WINDOW_MS = 35_000

    # Track entered cycles (one entry per 15-min cycle floor).
    entered_cycle_floors: set[int] = set()
    open_positions: dict[str, dict[str, Any]] = {}
    settled_trades: list[dict[str, Any]] = []

    last_near_miss_t = {"up": 0.0, "down": 0.0}
    signals_fired = 0
    near_misses = 0
    last_status_t = time.time()
    last_new_event_t = time.time()

    print(
        f"[shadow-velocity] starting venue={args.venue} thr15={args.threshold_15s} "
        f"thr30={args.threshold_30s} near={args.near_miss_threshold} -> {args.decision_log}",
        flush=True,
    )

    try:
        while not stop["flag"]:
            now_wall = time.time()
            with connect(args.db) as conn:
                rows = conn.execute(
                    """
                    SELECT event_id, received_ts_ms, mid
                    FROM spot_quote_event
                    WHERE venue = ? AND event_id > ? AND mid IS NOT NULL
                    ORDER BY event_id
                    LIMIT 5000
                    """,
                    (args.venue, spot_event_id_watermark),
                ).fetchall()
                eid_max = spot_event_id_watermark
                latest_ts_ms: int | None = None
                latest_mid: float | None = None
                for row in rows:
                    eid_max = max(eid_max, int(row["event_id"]))
                    ts_ms = int(row["received_ts_ms"])
                    mid = float(row["mid"])
                    window.append((ts_ms, mid))
                    latest_ts_ms = ts_ms
                    latest_mid = mid

                # Trim old entries beyond WINDOW_MS.
                if latest_ts_ms is not None:
                    cutoff = latest_ts_ms - WINDOW_MS
                    while window and window[0][0] < cutoff:
                        window.popleft()

                # Velocity check only if we have a fresh tick.
                if latest_ts_ms is not None and latest_mid is not None and len(window) >= 2:
                    ref_15s = mid_at_or_before(window, latest_ts_ms - 15_000)
                    ref_30s = mid_at_or_before(window, latest_ts_ms - 30_000)
                    delta_15s = (latest_mid - ref_15s[1]) if ref_15s else 0.0
                    delta_30s = (latest_mid - ref_30s[1]) if ref_30s else 0.0
                    have_15s = ref_15s is not None and (latest_ts_ms - ref_15s[0]) >= 14_000
                    have_30s = ref_30s is not None and (latest_ts_ms - ref_30s[0]) >= 28_000

                    cf = cycle_floor_ms(latest_ts_ms)
                    cycle_close = cf + CYCLE_MS

                    trig_up = (have_15s and delta_15s >= args.threshold_15s) or (
                        have_30s and delta_30s >= args.threshold_30s
                    )
                    trig_down = (have_15s and delta_15s <= -args.threshold_15s) or (
                        have_30s and delta_30s <= -args.threshold_30s
                    )

                    if (trig_up or trig_down) and cf not in entered_cycle_floors:
                        direction = "up" if trig_up else "down"
                        side = "yes" if trig_up else "no"  # YES on up, NO on down
                        market = find_atm_market(conn, cycle_close)
                        if market is None:
                            log_fp.write(json.dumps({
                                "kind": "velocity_signal_no_market",
                                "timestamp": latest_ts_ms,
                                "direction": direction,
                                "velocity_15s": round(delta_15s, 2),
                                "velocity_30s": round(delta_30s, 2),
                                "btc_price": round(latest_mid, 2),
                                "cycle_floor_ms": cf,
                                "cycle_close_ms": cycle_close,
                            }) + "\n")
                            log_fp.flush()
                        else:
                            ticker, mkt = market
                            yes_ask_cents = int(round(mkt["yes_ask"] * 100))
                            no_ask_cents = 100 - yes_ask_cents
                            entry_cents = yes_ask_cents if side == "yes" else no_ask_cents
                            contracts = max(1, int(args.base_stake))
                            entry_fee = kalshi_taker_fee_cents(entry_cents, count=contracts)
                            open_positions[ticker] = {
                                "ticker": ticker,
                                "side": side,
                                "contracts": contracts,
                                "entry_price_cents": entry_cents,
                                "entry_fee_cents": entry_fee,
                                "decided_at_ts_ms": latest_ts_ms,
                                "cycle_floor_ms": cf,
                                "cycle_close_ms": cycle_close,
                                "direction": direction,
                                "velocity_15s": round(delta_15s, 2),
                                "velocity_30s": round(delta_30s, 2),
                                "btc_price_at_entry": round(latest_mid, 2),
                            }
                            entered_cycle_floors.add(cf)
                            signals_fired += 1
                            log_fp.write(json.dumps({
                                "kind": "velocity_signal",
                                "timestamp": latest_ts_ms,
                                "direction": direction,
                                "velocity_15s": round(delta_15s, 2),
                                "velocity_30s": round(delta_30s, 2),
                                "btc_price": round(latest_mid, 2),
                                "ticker": ticker,
                                "side": side,
                                "entry_price_cents": entry_cents,
                                "entry_fee_cents": entry_fee,
                                "contracts": contracts,
                                "cycle_floor_ms": cf,
                                "cycle_close_ms": cycle_close,
                                "yes_ask_at_entry": mkt["yes_ask"],
                            }) + "\n")
                            log_fp.flush()
                    elif have_15s and not (trig_up or trig_down):
                        # Near-miss: |delta_15s| in [near_miss_threshold, threshold_15s)
                        abs15 = abs(delta_15s)
                        if abs15 >= args.near_miss_threshold:
                            d = "up" if delta_15s > 0 else "down"
                            if now_wall - last_near_miss_t[d] >= args.near_miss_cooldown_s:
                                last_near_miss_t[d] = now_wall
                                near_misses += 1
                                log_fp.write(json.dumps({
                                    "kind": "near_miss",
                                    "timestamp": latest_ts_ms,
                                    "direction": d,
                                    "velocity_15s": round(delta_15s, 2),
                                    "velocity_30s": round(delta_30s, 2),
                                    "btc_price": round(latest_mid, 2),
                                    "cycle_floor_ms": cf,
                                    "cycle_already_entered": cf in entered_cycle_floors,
                                }) + "\n")
                                log_fp.flush()

                # Settlement reconciliation.
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
                        net = gross - pos["entry_fee_cents"]
                        trade = {**pos, "outcome": outcome, "gross_cents": gross,
                                 "net_cents": net, "settled_via": "settlement"}
                        settled_trades.append(trade)
                        log_fp.write(json.dumps({"kind": "settle", **trade}) + "\n")
                        log_fp.flush()

                if eid_max > spot_event_id_watermark:
                    spot_event_id_watermark = eid_max
                    last_new_event_t = now_wall

            if now_wall - last_new_event_t > args.stale_venue_timeout_s:
                print(
                    f"[shadow-velocity] STALE venue={args.venue} for "
                    f"{now_wall - last_new_event_t:.0f}s. Exiting for watchdog restart.",
                    flush=True,
                )
                log_fp.close()
                return 2

            if now_wall - last_status_t >= args.status_every_s:
                wins = sum(1 for t in settled_trades if t["net_cents"] > 0)
                total_net = sum(t["net_cents"] for t in settled_trades)
                print(
                    f"[shadow-velocity] signals={signals_fired} near_misses={near_misses} "
                    f"open={len(open_positions)} settled={len(settled_trades)} "
                    f"wins={wins} net={total_net:+d}c window_ticks={len(window)} "
                    f"watermark_eid={spot_event_id_watermark}",
                    flush=True,
                )
                last_status_t = now_wall

            time.sleep(args.poll_interval_s)
    finally:
        log_fp.close()
    print(
        f"[shadow-velocity] stopped. signals={signals_fired} near_misses={near_misses} "
        f"settled={len(settled_trades)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
