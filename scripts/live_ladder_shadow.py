"""Shadow ladder-DCA observer that runs alongside KalshiLiveTA.

For every open live position, tracks the contract's price tick-by-tick
(from the capture DB) and simulates what a confirmation-driven add ladder
WOULD have done. Never places real orders — has no Kalshi client wired.

Ladder logic (user-specified):
    1. Entry phase 1 is the live trader's original fill. We observe it.
    2. While the position is open and adverse (contract trading below
       entry), check the four conditions every poll:
         - Thin liquidity: top5_depth < THIN_DEPTH_CAP
         - Near strike:    |spot - strike| / strike < NEAR_STRIKE_FRAC
         - Stabilized:     contract-price std over last 10s < STABILIZED_STD_CENTS
         - (Session-bias-reversal is logged but not gating yet — needs
            more data on its predictiveness.)
    3. When all four fire AND we're not currently in a ladder for this
       ticker, "place" a rung at ask + LADDER_LIFT_CENTS. Log a would_add.
    4. Wait LADDER_HOLD_SECONDS. Check if the current ask is >= the rung's
       fill price.
         - If yes: rung held. If conditions still fire, place next rung.
           Otherwise return to idle.
         - If no:  rung failed. Stop the ladder for this position.
    5. At settlement (lifecycle determined), compute counterfactual P&L:
         - actual:   live trader's net (entry + fee, settled outcome)
         - +ladder:  same outcome but with each shadow rung as an extra fill
                     at its simulated price. Outcome side determines whether
                     each rung is a winner or loser.

Outputs ``data/ladder_shadow.jsonl`` with one record per event. Kinds:
    - ``startup``        - one per process start
    - ``track_open``     - new position from live trader picked up
    - ``conditions``     - periodic snapshot of the four conditions per open position
    - ``would_add``      - shadow rung placed
    - ``rung_held``      - 10s check showed price held
    - ``rung_failed``    - 10s check showed price did not hold; ladder stopped
    - ``settle_with_ladder`` - final counterfactual P&L when position settles

To run as a service, install via scripts/install_services.ps1.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import signal
import sqlite3
import statistics
import sys
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

# ── Configurable thresholds (hard-coded — adjust by editing this file) ──
THIN_DEPTH_CAP = 200            # top5_depth < this counts as "thin"
NEAR_STRIKE_FRAC = 0.0005       # |spot-strike|/strike < this = "near strike" (0.05%)
STABILIZED_STD_CENTS = 2.0      # contract mid stddev over 10s < this = "stabilized"
LADDER_LIFT_CENTS = 2           # rung price = current ask + this
LADDER_HOLD_SECONDS = 10        # wait this long before checking if rung held
MAX_RUNGS_PER_POSITION = 3      # safety cap — at most N adds per open position
ADVERSE_TRIGGER_CENTS = 5       # only ladder when price is at least N cents below entry

# Ticker → spot venue: which spot stream's mid to use for the "near strike" test
SPOT_VENUE_FALLBACK = ("bitstamp", "coinbase", "fusion:median2of3")

CYCLE_MS = 15 * 60 * 1000


def kalshi_taker_fee_cents(price_cents: int, count: int = 1) -> int:
    if count <= 0 or price_cents <= 0 or price_cents >= 100:
        return 0
    p = price_cents / 100.0
    return int(math.ceil(0.07 * count * p * (1 - p) * 100 - 1e-12))


def cycle_floor_ms(ts_ms: int) -> int:
    return (ts_ms // CYCLE_MS) * CYCLE_MS


# ── Position tracking ─────────────────────────────────────────────────────

class TrackedPosition:
    """One open live position we're observing.

    Handles both paper- and live-trader fill record schemas:
      - paper_ta_2026_05_12.jsonl: `ts_ms`, `side` ("yes"/"no")
      - live_ta_trades.jsonl:      `ts_minute_ms`, `decided_side` ("call"/"put")
    """

    def __init__(self, fill_record: dict):
        self.ticker: str = fill_record["ticker"]

        # Side may be "yes"/"no" (paper) or "call"/"put" (live, decided_side field)
        side_raw = (
            fill_record.get("side")
            or fill_record.get("decided_side")
            or ""
        )
        if side_raw == "call":
            self.side: str = "yes"
        elif side_raw == "put":
            self.side = "no"
        else:
            self.side = side_raw  # already "yes" or "no"

        self.contracts: int = int(fill_record["contracts"])
        self.entry_price_cents: int = int(fill_record["entry_price_cents"])
        self.entry_fee_cents: int = int(fill_record.get("entry_fee_cents", 0))

        # Timestamp field varies by source — try all known keys
        ts_raw = (
            fill_record.get("ts_ms")
            or fill_record.get("ts_minute_ms")
            or fill_record.get("entry_ts_ms")
            or 0
        )
        self.entry_ts_ms: int = int(ts_raw)
        self.cycle_floor_ms: int = int(
            fill_record.get("cycle_floor_ms") or cycle_floor_ms(self.entry_ts_ms)
        )
        self.cycle_close_ms: int = int(
            fill_record.get("cycle_close_ms") or (self.cycle_floor_ms + CYCLE_MS)
        )
        self.order_id: str = fill_record.get("order_id", "?")
        # Ladder state
        self.rungs: list[dict[str, Any]] = []  # each: {ts_ms, price_cents, contracts, fee_cents}
        self.ladder_state: str = "idle"  # idle|waiting|stopped
        self.last_rung_ts_ms: int = 0
        self.settled: bool = False
        # Local price history for stabilization checks
        self.recent_prices: deque[tuple[int, float]] = deque(maxlen=120)  # (ts_ms, mid_cents)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker, "side": self.side, "contracts": self.contracts,
            "entry_price_cents": self.entry_price_cents, "entry_fee_cents": self.entry_fee_cents,
            "entry_ts_ms": self.entry_ts_ms, "cycle_floor_ms": self.cycle_floor_ms,
            "cycle_close_ms": self.cycle_close_ms, "order_id": self.order_id,
            "rungs": self.rungs, "ladder_state": self.ladder_state,
        }


# ── DB readers ────────────────────────────────────────────────────────────

def latest_l2(conn: sqlite3.Connection, ticker: str) -> dict | None:
    row = conn.execute(
        """
        SELECT received_ts_ms, best_yes_bid, best_yes_ask, spread, raw_json
        FROM kalshi_l2_event
        WHERE market_ticker = ?
        ORDER BY event_id DESC
        LIMIT 1
        """,
        (ticker,),
    ).fetchone()
    if not row:
        return None
    return {
        "received_ts_ms": int(row[0]),
        "yes_bid": float(row[1]) if row[1] is not None else None,
        "yes_ask": float(row[2]) if row[2] is not None else None,
        "spread_dollars": float(row[3]) if row[3] is not None else None,
    }


def latest_spot_mid(conn: sqlite3.Connection) -> tuple[int, float] | None:
    for venue in SPOT_VENUE_FALLBACK:
        row = conn.execute(
            """
            SELECT received_ts_ms, mid FROM spot_quote_event
            WHERE venue = ? AND mid IS NOT NULL
            ORDER BY event_id DESC LIMIT 1
            """,
            (venue,),
        ).fetchone()
        if row and row[1] is not None:
            return int(row[0]), float(row[1])
    return None


def strike_for_ticker(conn: sqlite3.Connection, ticker: str) -> float | None:
    """Parse the strike from the ticker name (e.g. KXBTC15M-26MAY131115-15
    closes at 11:15 with strike-suffix 15 meaning $103,015 etc). Falls back
    to looking at market_dim's raw_json floor_strike if present."""
    row = conn.execute(
        "SELECT raw_json FROM market_dim WHERE ticker = ? LIMIT 1", (ticker,)
    ).fetchone()
    if row and row[0]:
        try:
            j = json.loads(row[0])
            fs = j.get("floor_strike")
            if fs is not None:
                return float(fs)
        except Exception:
            pass
    # Fallback: from any l2 raw_json
    row = conn.execute(
        "SELECT raw_json FROM kalshi_l2_event WHERE market_ticker = ? ORDER BY event_id LIMIT 1",
        (ticker,),
    ).fetchone()
    if row and row[0]:
        try:
            j = json.loads(row[0])
            msg = j.get("msg", j)
            fs = msg.get("floor_strike")
            if fs is not None:
                return float(fs)
        except Exception:
            pass
    return None


def top5_depth(conn: sqlite3.Connection, ticker: str, side: str) -> float | None:
    """Sum size of top-5 levels on the relevant side. Returns None if no L2 yet."""
    row = conn.execute(
        """
        SELECT yes_levels_json, no_levels_json FROM kalshi_l2_event
        WHERE market_ticker = ?
        ORDER BY event_id DESC LIMIT 1
        """,
        (ticker,),
    ).fetchone()
    if not row:
        return None
    levels_json = row[0] if side == "yes" else row[1]
    if not levels_json:
        return None
    try:
        levels = json.loads(levels_json)
        sizes = [float(lv[1]) for lv in levels[:5] if len(lv) >= 2]
        return sum(sizes)
    except Exception:
        return None


def lookup_settlement(conn: sqlite3.Connection, ticker: str) -> str | None:
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
        msg = json.loads(row[0]).get("msg", {})
        return msg.get("result")
    except Exception:
        return None


# ── Main loop ─────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="live-ladder-shadow",
        description="Shadow ladder-DCA observer running alongside KalshiLiveTA.",
    )
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--live-trade-log", required=True, type=Path,
                        help="Path to data/live_ta_trades.jsonl (read-only).")
    parser.add_argument("--shadow-log", required=True, type=Path,
                        help="Path to write ladder-shadow JSONL events.")
    parser.add_argument("--poll-interval-s", type=float, default=2.0,
                        help="Wall-clock interval between polling for new fills and price ticks.")
    parser.add_argument("--status-every-s", type=float, default=60.0)
    args = parser.parse_args()

    if not args.db.exists():
        print(f"db not found: {args.db}", flush=True)
        return 1

    args.shadow_log.parent.mkdir(parents=True, exist_ok=True)
    shadow_fp = args.shadow_log.open("a", encoding="utf-8")

    stop = {"flag": False}

    def _handle(signum, frame):  # noqa: ANN001
        stop["flag"] = True
    signal.signal(signal.SIGINT, _handle)
    try:
        signal.signal(signal.SIGTERM, _handle)
    except (AttributeError, ValueError):
        pass

    open_positions: dict[str, TrackedPosition] = {}
    seen_fill_order_ids: set[str] = set()
    last_trade_log_offset: int = 0
    last_status_t = time.time()
    last_poll_t = 0.0

    def emit(rec: dict[str, Any]) -> None:
        shadow_fp.write(json.dumps(rec, default=str) + "\n")
        shadow_fp.flush()

    emit({
        "kind": "startup",
        "ts_ms": int(time.time() * 1000),
        "thin_depth_cap": THIN_DEPTH_CAP,
        "near_strike_frac": NEAR_STRIKE_FRAC,
        "stabilized_std_cents": STABILIZED_STD_CENTS,
        "ladder_lift_cents": LADDER_LIFT_CENTS,
        "ladder_hold_seconds": LADDER_HOLD_SECONDS,
        "max_rungs_per_position": MAX_RUNGS_PER_POSITION,
        "adverse_trigger_cents": ADVERSE_TRIGGER_CENTS,
    })
    print("[ladder-shadow] starting", flush=True)

    # On startup, seek to the END of the live trade log so we only react
    # to future fills (replaying historical ones would create stale ladders).
    if args.live_trade_log.exists():
        last_trade_log_offset = args.live_trade_log.stat().st_size

    try:
        while not stop["flag"]:
            now_wall = time.time()
            if now_wall - last_poll_t < args.poll_interval_s:
                time.sleep(0.2)
                continue
            last_poll_t = now_wall
            now_ms = int(now_wall * 1000)

            # 1. Tail the live trade log for new fills
            try:
                with args.live_trade_log.open("r", encoding="utf-8") as f:
                    f.seek(last_trade_log_offset)
                    new_lines = f.readlines()
                    last_trade_log_offset = f.tell()
            except FileNotFoundError:
                new_lines = []
            for line in new_lines:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("kind") != "fill":
                    continue
                if rec.get("dry_run"):
                    continue
                order_id = rec.get("order_id")
                if not order_id or order_id in seen_fill_order_ids:
                    continue
                seen_fill_order_ids.add(order_id)
                ticker = rec.get("ticker")
                if not ticker:
                    continue
                # Guard against schema drift in the fill record so a bad row
                # never crash-loops the watchdog. Log the parse failure to
                # stderr but keep going.
                try:
                    pos = TrackedPosition(rec)
                except Exception as e:  # noqa: BLE001
                    emit({
                        "kind": "track_open_parse_error",
                        "ts_ms": now_ms,
                        "ticker": ticker,
                        "order_id": order_id,
                        "error": repr(e),
                        "record_keys": sorted(rec.keys()),
                    })
                    print(
                        f"[ladder-shadow] PARSE ERROR ticker={ticker} order_id={order_id} "
                        f"err={e!r}",
                        flush=True,
                    )
                    continue
                open_positions[ticker] = pos
                emit({"kind": "track_open", "ts_ms": now_ms, **pos.to_dict()})
                print(f"[ladder-shadow] tracking {ticker} entry={pos.entry_price_cents}c side={pos.side}", flush=True)

            # 2. For each open position, run the ladder logic
            with sqlite3.connect(f"file:{args.db}?mode=ro", uri=True) as conn:
                for ticker in list(open_positions.keys()):
                    pos = open_positions[ticker]

                    # 2a. Check for settlement first
                    outcome = lookup_settlement(conn, ticker)
                    if outcome is not None and not pos.settled:
                        pos.settled = True
                        _emit_settlement(emit, pos, outcome, now_ms)
                        del open_positions[ticker]
                        continue

                    # 2b. Cycle past close — clean up if not settled yet
                    if now_ms > pos.cycle_close_ms + 60_000 and pos.ladder_state != "stopped":
                        # Past cycle close, waiting for settlement
                        pass

                    # 2c. Read latest contract state
                    l2 = latest_l2(conn, ticker)
                    if not l2 or l2["yes_ask"] is None:
                        continue
                    yes_ask_c = int(round(l2["yes_ask"] * 100))
                    yes_bid_c = int(round(l2["yes_bid"] * 100)) if l2["yes_bid"] is not None else None
                    no_ask_c = 100 - yes_ask_c
                    no_bid_c = 100 - yes_bid_c if yes_bid_c is not None else None

                    side_ask_c = yes_ask_c if pos.side == "yes" else no_ask_c
                    side_bid_c = yes_bid_c if pos.side == "yes" else no_bid_c
                    side_mid_c = (side_ask_c + side_bid_c) / 2 if side_bid_c is not None else side_ask_c

                    pos.recent_prices.append((l2["received_ts_ms"], side_mid_c))

                    # 2d. Adverse-price gate: only consider laddering when underwater
                    if side_ask_c >= pos.entry_price_cents - ADVERSE_TRIGGER_CENTS:
                        # Position is at/above entry — no need to add
                        continue

                    # 2e. Four conditions
                    depth = top5_depth(conn, ticker, pos.side)
                    thin_liq = depth is not None and depth < THIN_DEPTH_CAP

                    spot_row = latest_spot_mid(conn)
                    strike = strike_for_ticker(conn, ticker)
                    near_strike = False
                    if spot_row is not None and strike is not None and strike > 0:
                        spot_mid = spot_row[1]
                        near_strike = abs(spot_mid - strike) / strike < NEAR_STRIKE_FRAC

                    # Stabilization: stddev of side-mid over last 10 seconds
                    cutoff_ms = now_ms - 10_000
                    window = [p for t, p in pos.recent_prices if t >= cutoff_ms]
                    stabilized = len(window) >= 3 and statistics.pstdev(window) < STABILIZED_STD_CENTS

                    cond = {
                        "thin_liquidity": thin_liq,
                        "near_strike": near_strike,
                        "stabilized": stabilized,
                        "depth_top5": depth,
                        "spot_mid": spot_row[1] if spot_row else None,
                        "strike": strike,
                        "side_ask_cents": side_ask_c,
                        "side_bid_cents": side_bid_c,
                        "window_n": len(window),
                        "window_std_cents": statistics.pstdev(window) if len(window) >= 2 else None,
                    }

                    all_fire = thin_liq and near_strike and stabilized

                    # 2f. Ladder state machine
                    if pos.ladder_state == "waiting":
                        # Check the previous rung's hold condition
                        elapsed = now_ms - pos.last_rung_ts_ms
                        if elapsed < LADDER_HOLD_SECONDS * 1000:
                            continue
                        last_rung = pos.rungs[-1]
                        if side_ask_c >= last_rung["price_cents"]:
                            # Held — log, transition to idle so we can add again if conditions persist
                            emit({
                                "kind": "rung_held", "ts_ms": now_ms, "ticker": ticker,
                                "rung_index": len(pos.rungs), "rung_price_cents": last_rung["price_cents"],
                                "current_ask_cents": side_ask_c,
                            })
                            pos.ladder_state = "idle"
                        else:
                            # Did not hold — stop the ladder for this position
                            emit({
                                "kind": "rung_failed", "ts_ms": now_ms, "ticker": ticker,
                                "rung_index": len(pos.rungs), "rung_price_cents": last_rung["price_cents"],
                                "current_ask_cents": side_ask_c,
                                "drop_cents": last_rung["price_cents"] - side_ask_c,
                            })
                            pos.ladder_state = "stopped"
                        continue

                    if pos.ladder_state == "stopped":
                        continue

                    # 2g. Idle: maybe trigger a new rung
                    if not all_fire:
                        # Periodically log condition snapshot for debugging
                        continue
                    if len(pos.rungs) >= MAX_RUNGS_PER_POSITION:
                        continue
                    rung_price = min(99, side_ask_c + LADDER_LIFT_CENTS)
                    rung_contracts = pos.contracts  # match original size per rung
                    rung_fee = kalshi_taker_fee_cents(rung_price, count=rung_contracts)
                    pos.rungs.append({
                        "ts_ms": now_ms, "price_cents": rung_price,
                        "contracts": rung_contracts, "fee_cents": rung_fee,
                    })
                    pos.ladder_state = "waiting"
                    pos.last_rung_ts_ms = now_ms
                    emit({
                        "kind": "would_add", "ts_ms": now_ms, "ticker": ticker,
                        "rung_index": len(pos.rungs), "rung_price_cents": rung_price,
                        "contracts": rung_contracts, "fee_cents": rung_fee,
                        **cond,
                    })
                    print(f"[ladder-shadow] WOULD ADD {ticker} rung#{len(pos.rungs)} {rung_contracts}@{rung_price}c", flush=True)

            # 3. Status line periodically
            if now_wall - last_status_t >= args.status_every_s:
                pending = len(open_positions)
                total_rungs = sum(len(p.rungs) for p in open_positions.values())
                print(f"[ladder-shadow] open_positions={pending} total_rungs_active={total_rungs}", flush=True)
                last_status_t = now_wall
    finally:
        shadow_fp.close()
    print("[ladder-shadow] stopped", flush=True)
    return 0


def _emit_settlement(emit_fn, pos: TrackedPosition, outcome: str, now_ms: int) -> None:
    """Compute counterfactual P&L at settlement and emit settle_with_ladder."""
    n_orig = pos.contracts
    cost_orig = pos.entry_price_cents
    fee_orig = pos.entry_fee_cents
    if pos.side == outcome:
        actual_gross = n_orig * (100 - cost_orig)
    else:
        actual_gross = -n_orig * cost_orig
    actual_net = actual_gross - fee_orig

    ladder_gross = 0
    ladder_fees = 0
    ladder_n = 0
    for r in pos.rungs:
        n = r["contracts"]
        c = r["price_cents"]
        f = r["fee_cents"]
        ladder_n += n
        ladder_fees += f
        if pos.side == outcome:
            ladder_gross += n * (100 - c)
        else:
            ladder_gross += -n * c
    ladder_net = ladder_gross - ladder_fees
    combined_net = actual_net + ladder_net

    emit_fn({
        "kind": "settle_with_ladder", "ts_ms": now_ms, "ticker": pos.ticker,
        "side": pos.side, "outcome": outcome,
        "entry_contracts": n_orig, "entry_price_cents": cost_orig,
        "entry_fee_cents": fee_orig,
        "actual_gross_cents": actual_gross, "actual_net_cents": actual_net,
        "ladder_rungs": len(pos.rungs), "ladder_contracts": ladder_n,
        "ladder_gross_cents": ladder_gross, "ladder_fees_cents": ladder_fees,
        "ladder_net_cents": ladder_net,
        "combined_net_cents": combined_net,
        "delta_vs_actual_cents": ladder_net,  # how much the ladder improved (+) or worsened (-)
    })


if __name__ == "__main__":
    sys.exit(main())
