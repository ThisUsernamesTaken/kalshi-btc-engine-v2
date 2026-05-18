"""Live Pine Script directional trader v2 — with active position management.

Same as ``live_ta.py`` decision-for-decision (Pine Script TA score gates
entries, tier-scaled IOC buys, per-cycle dedupe, hard-cap safety guards).
Adds two early-exit mechanisms once a position is open:

  1. Trailing-stop on our bid: every poll tick we read the current best
     bid on our side from ``kalshi_l2_event``. We track an HWM (high
     water mark) starting at the entry price. If the bid drops 10c
     below HWM AND the bid is still >= 5c, we place a sell IOC at
     bid-3c. The bid>=5c floor prevents firing a sell into a totally
     evaporated book at deep-loss prices where holding to settle has
     similar or better EV than a forced taker exit.

  2. Score-inversion: if we entered CALL (yes) but the rolling Pine
     Script score drops below -15 — or entered PUT (no) but score
     rises above +15 — the directional thesis has flipped. Place a
     sell IOC at bid-3c.

After either exit fires we remove the position from open_positions and
log a `trail_exit` / `score_inversion_exit` record (analogous to the
existing `settle` record so downstream P&L tally still works). The
cycle stays in ``entered_cycles`` so we never re-enter the same cycle.

Same hard caps as live_ta.py — see module docstring there.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import signal
import sqlite3
import sys
import time
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

# Import the v1 KalshiClient (RSA-PSS auth, async). Must be on sys.path.
_V1_ROOT = Path(r"C:\Trading\btc-bias-engine")
if str(_V1_ROOT) not in sys.path:
    sys.path.insert(0, str(_V1_ROOT))
from kalshi_client import KalshiAPIError, KalshiClient  # noqa: E402

# ── Constants ─────────────────────────────────────────────────────────────

DEFAULT_SPOT_VENUE = "coinbase"
CYCLE_MS = 15 * 60 * 1000
MIN_MS = 60 * 1000

# Per-tier contract sizing — see live_ta.py docstring for rationale.
TIER_CONTRACTS: dict[str, int] = {
    "STRONG": 40,
    "MEDIUM": 20,
    "WEAK":   10,
    "MIMIC":   5,
}
MIN_TIER_CONTRACTS = 5

# v2 position-management thresholds.
TRAIL_CENTS = 10
SCORE_INVERSION_THRESH = 15

# Bid floor for trail exit — below this we'd rather hold to settle than
# take a deep loser at evaporated-book prices.
TRAIL_BID_FLOOR_CENTS = 5

# Slip when crossing to sell.
EXIT_SLIPPAGE_CENTS = 3

# Other hard safety caps. Do not parameterize.
DAILY_LOSS_CAP_CENTS = 999999
MIN_BALANCE_CENTS = 5 * 100
STALE_DATA_TIMEOUT_MS = 30_000
SLIPPAGE_CENTS = 3
LIMIT_CAP_CENTS = 99

KALSHI_CREDS_PATH = Path(r"C:\Trading\btc-bias-engine\credentials\kalshi.env")


def minute_floor_ms(ts_ms: int) -> int:
    return (ts_ms // MIN_MS) * MIN_MS


def cycle_floor_ms(ts_ms: int) -> int:
    return (ts_ms // CYCLE_MS) * CYCLE_MS


def bars_in_cycle_for_minute(minute_ms: int) -> int:
    return ((minute_ms - cycle_floor_ms(minute_ms)) // MIN_MS) + 1


class MinuteBarAggregator:
    """Build 1-min OHLC bars from spot quote events. Mirrors live_paper_ta."""

    def __init__(self) -> None:
        self._current_minute: int | None = None
        self._open: float | None = None
        self._high: float = -1.0
        self._low: float = 1e18
        self._close: float = 0.0
        self._count: int = 0

    def ingest(self, ts_ms: int, mid: float) -> OHLCBar | None:
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
        completed = OHLCBar(
            ts_minute_ms=self._current_minute,
            open=self._open or mid,
            high=self._high,
            low=self._low,
            close=self._close,
            volume=None,
            cycle_open_price=0.0,
            bars_in_cycle=0,
        )
        self._current_minute = m
        self._open = mid
        self._high = mid
        self._low = mid
        self._close = mid
        self._count = 1
        return completed


class CycleTracker:
    """Per-15-min-cycle state. Mirrors live_paper_ta."""

    def __init__(self) -> None:
        self._current_cycle_floor: int | None = None
        self._cycle_open_price: float | None = None
        self.score_state = TAScoreState()
        self.decided_side: str | None = None
        self.decided_at_bar: int | None = None
        self.decided_at_ts_ms: int | None = None
        self.consecutive_call_bars: int = 0
        self.consecutive_put_bars: int = 0

    def maybe_roll_cycle(self, minute_ms: int, open_price: float) -> bool:
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
    cycle_close_iso = dt.datetime.fromtimestamp(cycle_close_ms / 1000, tz=dt.UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
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


def latest_our_side_bid_cents(
    conn: sqlite3.Connection, ticker: str, side: str
) -> int | None:
    """Most recent best bid on the side we hold, in cents.

    side="yes" → our bid = best_yes_bid.
    side="no"  → our bid = 100 - best_yes_ask (selling no ≡ buyer buys
                 yes from us at 100-X; best price they pay for our no
                 is 100 - the lowest yes_ask).
    """
    row = conn.execute(
        """
        SELECT best_yes_bid, best_yes_ask
        FROM kalshi_l2_event
        WHERE market_ticker = ?
        ORDER BY event_id DESC
        LIMIT 1
        """,
        (ticker,),
    ).fetchone()
    if not row:
        return None
    if side == "yes":
        if row["best_yes_bid"] is None:
            return None
        return int(round(float(row["best_yes_bid"]) * 100))
    # side == "no"
    if row["best_yes_ask"] is None:
        return None
    yes_ask_c = int(round(float(row["best_yes_ask"]) * 100))
    return max(0, 100 - yes_ask_c)


def _spot_tail_sql() -> str:
    return """
    SELECT event_id, received_ts_ms, mid
    FROM spot_quote_event
    WHERE venue = ? AND event_id > ?
      AND mid IS NOT NULL
    ORDER BY event_id
    LIMIT ?
    """


# ── Credential loading ────────────────────────────────────────────────────

def load_kalshi_creds() -> tuple[str, str]:
    if not KALSHI_CREDS_PATH.exists():
        raise FileNotFoundError(f"Kalshi creds not found: {KALSHI_CREDS_PATH}")
    env: dict[str, str] = {}
    with KALSHI_CREDS_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    key_id = env.get("KALSHI_API_KEY") or os.environ.get("KALSHI_API_KEY")
    pem_path_raw = env.get("KALSHI_PRIVATE_KEY_PATH") or os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not key_id or not pem_path_raw:
        raise RuntimeError("kalshi.env missing KALSHI_API_KEY or KALSHI_PRIVATE_KEY_PATH")
    pem_path = Path(pem_path_raw)
    if not pem_path.exists():
        raise FileNotFoundError(f"Kalshi PEM not found: {pem_path}")
    return key_id, pem_path.read_text(encoding="utf-8")


# ── Trade-log replay (for daily loss + per-cycle dedupe) ──────────────────

def _utc_today_floor_ms() -> int:
    today = dt.datetime.now(dt.UTC).date()
    return int(
        dt.datetime(today.year, today.month, today.day, tzinfo=dt.UTC).timestamp() * 1000
    )


def replay_log_state(log_path: Path) -> tuple[int, set[int]]:
    if not log_path.exists():
        return 0, set()
    today_floor_ms = _utc_today_floor_ms()
    today_end_ms = today_floor_ms + 24 * 60 * 60 * 1000
    total_net = 0
    entered: set[int] = set()
    try:
        with log_path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = rec.get("kind")
                if kind in ("settle", "trail_exit", "score_inversion_exit"):
                    cc = rec.get("cycle_close_ms")
                    if cc is None:
                        continue
                    cc_i = int(cc)
                    if today_floor_ms <= cc_i < today_end_ms:
                        total_net += int(rec.get("net_cents", 0))
                elif kind in ("order_attempt", "fill"):
                    cf = rec.get("cycle_floor_ms")
                    if cf is not None:
                        entered.add(int(cf))
    except OSError:
        return 0, set()
    return max(0, -total_net), entered


# ── Main loop ─────────────────────────────────────────────────────────────

async def main_async() -> int:
    parser = argparse.ArgumentParser(
        prog="live-ta-v2",
        description="Pine Script directional LIVE trader v2 (trail-stop + score-inversion exits).",
    )
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--decision-log", required=True, type=Path,
                        help="JSONL log path. Conventionally data/live_ta_v2_trades.jsonl.")
    parser.add_argument("--venue", default=DEFAULT_SPOT_VENUE)
    parser.add_argument("--poll-interval-s", type=float, default=0.5)
    parser.add_argument("--status-every-s", type=float, default=30.0)
    parser.add_argument("--start-at-tail", action="store_true")
    parser.add_argument(
        "--stale-venue-timeout-s",
        type=float,
        default=600.0,
        help="Exit code 2 if no new spot events from --venue arrive within this many seconds.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the full decision pipeline and log a synthetic fill, but DO NOT "
        "call Kalshi place_order. Balance fetch still runs as a live API smoke check.",
    )
    args = parser.parse_args()

    if not args.db.exists():
        print(f"db not found: {args.db}", flush=True)
        return 1

    args.decision_log.parent.mkdir(parents=True, exist_ok=True)
    log_fp = args.decision_log.open("a", encoding="utf-8")

    key_id, pem = load_kalshi_creds()

    aggregator = MinuteBarAggregator()
    cycle = CycleTracker()
    cfg = TAScoreConfig()

    stop = {"flag": False}

    def _handle(signum, frame):  # noqa: ANN001
        stop["flag"] = True
    signal.signal(signal.SIGINT, _handle)
    try:
        signal.signal(signal.SIGTERM, _handle)
    except (AttributeError, ValueError):
        pass

    spot_event_id_watermark = -1
    if args.start_at_tail:
        with connect(args.db) as conn:
            row = conn.execute(
                "SELECT MAX(event_id) FROM spot_quote_event WHERE venue = ?",
                (args.venue,),
            ).fetchone()
            if row and row[0] is not None:
                spot_event_id_watermark = int(row[0])

    open_positions: dict[str, dict[str, Any]] = {}
    settled_trades: list[dict[str, Any]] = []
    decisions_made = 0
    fills = 0
    trail_exits = 0
    inversion_exits = 0
    last_status_t = time.time()
    last_dec = 0
    last_fills = 0
    last_new_event_t = time.time()
    last_received_ts_ms: int | None = None
    # Latest TA score from the current cycle, refreshed when a bar closes.
    # Used by score-inversion exit between bars; reset on cycle roll.
    score_latest: float | None = None

    daily_loss_cents, entered_cycles = replay_log_state(args.decision_log)
    halt_reason: str | None = None
    if daily_loss_cents >= DAILY_LOSS_CAP_CENTS:
        halt_reason = "DAILY-LOSS-CAP"
    daily_loss_day_floor = _utc_today_floor_ms()

    print(
        f"[live-ta-v2] starting tail={args.db} venue={args.venue} -> {args.decision_log} "
        f"dry_run={args.dry_run} daily_loss_loaded={daily_loss_cents}c "
        f"entered_cycles_loaded={len(entered_cycles)} halt={halt_reason} "
        f"trail_cents={TRAIL_CENTS} score_inversion_thresh={SCORE_INVERSION_THRESH}",
        flush=True,
    )

    startup_rec = {
        "kind": "startup",
        "ts_ms": int(time.time() * 1000),
        "venue": args.venue,
        "dry_run": args.dry_run,
        "daily_loss_cents_at_start": daily_loss_cents,
        "entered_cycles_at_start": len(entered_cycles),
        "halt_reason_at_start": halt_reason,
        "tier_contracts": TIER_CONTRACTS,
        "min_tier_contracts_fallback": MIN_TIER_CONTRACTS,
        "daily_loss_cap_cents": DAILY_LOSS_CAP_CENTS,
        "min_balance_cents": MIN_BALANCE_CENTS,
        "stale_data_timeout_ms": STALE_DATA_TIMEOUT_MS,
        "slippage_cents": SLIPPAGE_CENTS,
        "trail_cents": TRAIL_CENTS,
        "score_inversion_thresh": SCORE_INVERSION_THRESH,
        "trail_bid_floor_cents": TRAIL_BID_FLOOR_CENTS,
        "exit_slippage_cents": EXIT_SLIPPAGE_CENTS,
        "variant": "live_ta_v2",
    }
    log_fp.write(json.dumps(startup_rec, default=str) + "\n")
    log_fp.flush()

    async with KalshiClient(key_id=key_id, private_key_pem=pem, demo=False) as client:
        try:
            while not stop["flag"]:
                now_wall = time.time()
                now_ms = int(now_wall * 1000)

                today_floor = _utc_today_floor_ms()
                if today_floor != daily_loss_day_floor:
                    daily_loss_day_floor = today_floor
                    daily_loss_cents = 0
                    if halt_reason == "DAILY-LOSS-CAP":
                        halt_reason = None
                        print("[live-ta-v2] new UTC day — daily loss counter reset", flush=True)

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
                        last_received_ts_ms = ts_ms
                        completed_bar = aggregator.ingest(ts_ms, mid)
                        if completed_bar is None:
                            continue
                        cycle_rolled = cycle.maybe_roll_cycle(
                            completed_bar.ts_minute_ms, completed_bar.open
                        )
                        if cycle_rolled:
                            score_latest = None
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
                        score_latest = snap.score

                        bull_quals = snap.bull_tier >= 1
                        bear_quals = snap.bear_tier >= 1
                        cycle.consecutive_call_bars = (
                            cycle.consecutive_call_bars + 1 if bull_quals else 0
                        )
                        cycle.consecutive_put_bars = (
                            cycle.consecutive_put_bars + 1 if bear_quals else 0
                        )

                        hour_utc = dt.datetime.fromtimestamp(
                            bar.ts_minute_ms / 1000, tz=dt.UTC
                        ).hour
                        decisions_made += 1
                        decision = evaluate_entry(
                            snap,
                            config=cfg,
                            hour_utc=hour_utc,
                            already_decided=cycle.decided_side is not None,
                            consecutive_call_bars=cycle.consecutive_call_bars
                            - (1 if bull_quals else 0),
                            consecutive_put_bars=cycle.consecutive_put_bars
                            - (1 if bear_quals else 0),
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

                        if decision is None or cycle.decided_side is not None:
                            log_fp.write(json.dumps(log_record, default=str) + "\n")
                            log_fp.flush()
                            continue

                        cycle.decided_side = decision.side
                        cycle.decided_at_bar = decision.locked_at_bar
                        cycle.decided_at_ts_ms = decision.locked_at_ts_ms
                        cycle_close = cycle.cycle_close_ms
                        cycle_floor = cycle.cycle_floor_ms

                        decision_meta = {
                            "decided_side": decision.side,
                            "tier": decision.tier,
                            "tier_name": decision.tier_name,
                            "forced": decision.forced,
                            "confidence": decision.confidence,
                            "stake_multiplier": decision.stake_multiplier,
                        }

                        if halt_reason is not None:
                            log_record["kind"] = "decision_halt"
                            log_record["halt_reason"] = halt_reason
                            log_record["daily_loss_cents"] = daily_loss_cents
                            log_record.update(decision_meta)
                            log_fp.write(json.dumps(log_record, default=str) + "\n")
                            log_fp.flush()
                            continue

                        if cycle_floor is not None and cycle_floor in entered_cycles:
                            log_record["kind"] = "decision_skip_cycle_dup"
                            log_record.update(decision_meta)
                            log_fp.write(json.dumps(log_record, default=str) + "\n")
                            log_fp.flush()
                            continue

                        stale_age_ms = (
                            now_ms - last_received_ts_ms
                            if last_received_ts_ms is not None
                            else None
                        )
                        if stale_age_ms is None or stale_age_ms > STALE_DATA_TIMEOUT_MS:
                            log_record["kind"] = "decision_stale_data_skip"
                            log_record["stale_age_ms"] = stale_age_ms
                            log_record.update(decision_meta)
                            log_fp.write(json.dumps(log_record, default=str) + "\n")
                            log_fp.flush()
                            print(
                                f"[live-ta-v2] STALE DATA SKIP cycle={cycle_floor} "
                                f"age_ms={stale_age_ms}",
                                flush=True,
                            )
                            continue

                        market = find_atm_market(conn, cycle_close) if cycle_close else None
                        if market is None:
                            log_record["kind"] = "decision_no_market"
                            log_record.update(decision_meta)
                            log_fp.write(json.dumps(log_record, default=str) + "\n")
                            log_fp.flush()
                            continue

                        ticker, mkt = market
                        yes_ask_cents = int(round(mkt["yes_ask"] * 100))
                        no_ask_cents = 100 - yes_ask_cents
                        is_call = decision.side == "call"
                        entry_cents = yes_ask_cents if is_call else no_ask_cents
                        order_side = "yes" if is_call else "no"
                        limit_cents = min(LIMIT_CAP_CENTS, entry_cents + SLIPPAGE_CENTS)
                        contracts = TIER_CONTRACTS.get(
                            decision.tier_name, MIN_TIER_CONTRACTS
                        )

                        try:
                            bal = await client.get_balance()
                        except Exception as e:  # noqa: BLE001
                            log_record["kind"] = "decision_balance_error"
                            log_record["error"] = repr(e)
                            log_record.update(decision_meta)
                            log_fp.write(json.dumps(log_record, default=str) + "\n")
                            log_fp.flush()
                            continue

                        if bal.balance < MIN_BALANCE_CENTS:
                            halt_reason = "MIN-BALANCE"
                            log_record["kind"] = "decision_low_balance_halt"
                            log_record["balance_cents"] = bal.balance
                            log_record.update(decision_meta)
                            log_fp.write(json.dumps(log_record, default=str) + "\n")
                            log_fp.flush()
                            print(
                                f"[live-ta-v2] HALT MIN-BALANCE bal=${bal.balance/100:.2f}",
                                flush=True,
                            )
                            continue

                        if cycle_floor is not None:
                            entered_cycles.add(cycle_floor)

                        attempt_rec = {
                            "kind": "order_attempt",
                            "ts_ms": now_ms,
                            "cycle_floor_ms": cycle_floor,
                            "cycle_close_ms": cycle_close,
                            "ticker": ticker,
                            "side": order_side,
                            "contracts": contracts,
                            "ask_cents": entry_cents,
                            "limit_cents": limit_cents,
                            "yes_ask_cents": yes_ask_cents,
                            "no_ask_cents": no_ask_cents,
                            "balance_cents": bal.balance,
                            "dry_run": args.dry_run,
                            **decision_meta,
                        }
                        log_fp.write(json.dumps(attempt_rec, default=str) + "\n")
                        log_fp.flush()

                        if args.dry_run:
                            filled_count = contracts
                            avg_price_cents = entry_cents
                            order_id = "DRY-RUN"
                            order_status = "dry_run"
                            resp_record: dict[str, Any] = {
                                "kind": "order_dry_run",
                                "ticker": ticker,
                                "side": order_side,
                                "limit_cents": limit_cents,
                                "contracts": contracts,
                            }
                            log_fp.write(json.dumps(resp_record, default=str) + "\n")
                            log_fp.flush()
                        else:
                            try:
                                order = await client.place_order(
                                    ticker=ticker,
                                    side=order_side,
                                    count=contracts,
                                    price=limit_cents,
                                    order_type="limit",
                                    action="buy",
                                    time_in_force="immediate_or_cancel",
                                )
                            except KalshiAPIError as e:
                                rej = {
                                    "kind": "order_rejected",
                                    "ts_ms": int(time.time() * 1000),
                                    "ticker": ticker,
                                    "side": order_side,
                                    "limit_cents": limit_cents,
                                    "contracts": contracts,
                                    "status": e.status,
                                    "body": e.body,
                                    "path": e.path,
                                }
                                log_fp.write(json.dumps(rej, default=str) + "\n")
                                log_fp.flush()
                                print(
                                    f"[live-ta-v2] ORDER REJECTED ticker={ticker} "
                                    f"status={e.status} body={e.body}",
                                    flush=True,
                                )
                                continue
                            except Exception as e:  # noqa: BLE001
                                err = {
                                    "kind": "order_error",
                                    "ts_ms": int(time.time() * 1000),
                                    "ticker": ticker,
                                    "side": order_side,
                                    "limit_cents": limit_cents,
                                    "contracts": contracts,
                                    "error": repr(e),
                                }
                                log_fp.write(json.dumps(err, default=str) + "\n")
                                log_fp.flush()
                                print(f"[live-ta-v2] ORDER ERROR {e!r}", flush=True)
                                continue

                            filled_count = int(order.filled_count or 0)
                            avg_price_cents = (
                                int(order.average_price)
                                if order.average_price is not None
                                else entry_cents
                            )
                            order_id = order.order_id
                            order_status = order.status

                            resp_record = {
                                "kind": "order_response",
                                "ts_ms": int(time.time() * 1000),
                                "ticker": ticker,
                                "side": order_side,
                                "order_id": order_id,
                                "status": order_status,
                                "filled_count": filled_count,
                                "average_price_cents": avg_price_cents,
                                "limit_cents": limit_cents,
                                "contracts_requested": contracts,
                            }
                            log_fp.write(json.dumps(resp_record, default=str) + "\n")
                            log_fp.flush()

                        if filled_count == 0:
                            nofill = {
                                "kind": "order_no_fill",
                                "ts_ms": int(time.time() * 1000),
                                "ticker": ticker,
                                "side": order_side,
                                "limit_cents": limit_cents,
                                "ask_cents": entry_cents,
                                "order_id": order_id,
                                "status": order_status,
                            }
                            log_fp.write(json.dumps(nofill, default=str) + "\n")
                            log_fp.flush()
                            print(
                                f"[live-ta-v2] IOC NO-FILL ticker={ticker} side={order_side} "
                                f"limit={limit_cents}c",
                                flush=True,
                            )
                            continue

                        entry_fee = kalshi_taker_fee_cents(avg_price_cents, count=filled_count)
                        # HWM seeds at the entry price; bid management will lift it
                        # as the market trades up. Trail fires when current_bid drops
                        # ≥ TRAIL_CENTS below HWM AND current_bid ≥ TRAIL_BID_FLOOR_CENTS.
                        open_positions[ticker] = {
                            "ticker": ticker,
                            "side": order_side,
                            "contracts": filled_count,
                            "entry_price_cents": avg_price_cents,
                            "entry_fee_cents": entry_fee,
                            "decided_at_bar": decision.locked_at_bar,
                            "decided_at_ts_ms": decision.locked_at_ts_ms,
                            "tier": decision.tier,
                            "tier_name": decision.tier_name,
                            "confidence": decision.confidence,
                            "cycle_close_ms": cycle_close,
                            "cycle_floor_ms": cycle_floor,
                            "order_id": order_id,
                            "dry_run": args.dry_run,
                            "hwm_bid_cents": avg_price_cents,
                            "entered_at_ms": int(time.time() * 1000),
                        }
                        fills += 1

                        log_record["kind"] = "fill"
                        log_record["ticker"] = ticker
                        log_record["entry_price_cents"] = avg_price_cents
                        log_record["entry_fee_cents"] = entry_fee
                        log_record["contracts"] = filled_count
                        log_record["order_id"] = order_id
                        log_record["status"] = order_status
                        log_record["dry_run"] = args.dry_run
                        log_record["limit_cents"] = limit_cents
                        log_record.update(decision_meta)
                        log_fp.write(json.dumps(log_record, default=str) + "\n")
                        log_fp.flush()

                        print(
                            f"[live-ta-v2] FILL ticker={ticker} side={order_side} "
                            f"{filled_count}@{avg_price_cents}c "
                            f"order_id={order_id} dry_run={args.dry_run}",
                            flush=True,
                        )

                    # ── Position management (v2): trail-stop + score-inversion ──
                    # Runs every poll tick for open positions. Mutations are
                    # deferred via closed_tickers to keep the iteration safe.
                    closed_tickers: list[str] = []
                    if open_positions:
                        for ticker, pos in open_positions.items():
                            import time as _time
                            seconds_to_close = (pos.get("cycle_close_ms", 0) - int(_time.time() * 1000)) / 1000
                            if seconds_to_close <= 120:
                                continue  # final 2 minutes: hold to settlement, no trail or signal exit
                            if pos.get("dry_run"):
                                continue
                            side = pos["side"]
                            current_bid = latest_our_side_bid_cents(conn, ticker, side)
                            if current_bid is None:
                                continue
                            if current_bid > pos["hwm_bid_cents"]:
                                pos["hwm_bid_cents"] = current_bid

                            trail_trigger = (
                                current_bid <= pos["hwm_bid_cents"] - TRAIL_CENTS
                                and current_bid >= TRAIL_BID_FLOOR_CENTS
                            )
                            # Only check inversion when we have a fresh score from
                            # the position's cycle (cycle roll clears score_latest).
                            inversion_trigger = False
                            if score_latest is not None:
                                if side == "yes" and score_latest < -SCORE_INVERSION_THRESH:
                                    inversion_trigger = True
                                elif side == "no" and score_latest > SCORE_INVERSION_THRESH:
                                    inversion_trigger = True

                            if not (trail_trigger or inversion_trigger):
                                continue

                            exit_reason = (
                                "trail_exit" if trail_trigger else "score_inversion_exit"
                            )
                            sell_limit = max(1, current_bid - EXIT_SLIPPAGE_CENTS)
                            n = pos["contracts"]

                            sell_attempt_rec = {
                                "kind": "exit_attempt",
                                "exit_reason": exit_reason,
                                "ts_ms": int(time.time() * 1000),
                                "ticker": ticker,
                                "side": side,
                                "contracts": n,
                                "current_bid_cents": current_bid,
                                "hwm_bid_cents": pos["hwm_bid_cents"],
                                "trail_cents": TRAIL_CENTS,
                                "score_latest": score_latest,
                                "score_inversion_thresh": SCORE_INVERSION_THRESH,
                                "sell_limit_cents": sell_limit,
                                "entry_price_cents": pos["entry_price_cents"],
                                "cycle_floor_ms": pos["cycle_floor_ms"],
                                "cycle_close_ms": pos["cycle_close_ms"],
                            }
                            log_fp.write(json.dumps(sell_attempt_rec, default=str) + "\n")
                            log_fp.flush()

                            try:
                                sell_order = await client.place_order(
                                    ticker=ticker,
                                    side=side,
                                    count=n,
                                    price=sell_limit,
                                    action="sell",
                                    time_in_force="immediate_or_cancel",
                                )
                            except KalshiAPIError as e:
                                rej = {
                                    "kind": "exit_rejected",
                                    "exit_reason": exit_reason,
                                    "ts_ms": int(time.time() * 1000),
                                    "ticker": ticker,
                                    "side": side,
                                    "sell_limit_cents": sell_limit,
                                    "contracts": n,
                                    "status": e.status,
                                    "body": e.body,
                                    "path": e.path,
                                }
                                log_fp.write(json.dumps(rej, default=str) + "\n")
                                log_fp.flush()
                                print(
                                    f"[live-ta-v2] EXIT REJECTED ticker={ticker} "
                                    f"reason={exit_reason} status={e.status} body={e.body}",
                                    flush=True,
                                )
                                continue
                            except Exception as e:  # noqa: BLE001
                                err = {
                                    "kind": "exit_error",
                                    "exit_reason": exit_reason,
                                    "ts_ms": int(time.time() * 1000),
                                    "ticker": ticker,
                                    "side": side,
                                    "sell_limit_cents": sell_limit,
                                    "contracts": n,
                                    "error": repr(e),
                                }
                                log_fp.write(json.dumps(err, default=str) + "\n")
                                log_fp.flush()
                                print(f"[live-ta-v2] EXIT ERROR {e!r}", flush=True)
                                continue

                            sell_filled = int(sell_order.filled_count or 0)
                            sell_avg = (
                                int(sell_order.average_price)
                                if sell_order.average_price is not None
                                else sell_limit
                            )

                            sell_resp_rec = {
                                "kind": "exit_response",
                                "exit_reason": exit_reason,
                                "ts_ms": int(time.time() * 1000),
                                "ticker": ticker,
                                "side": side,
                                "order_id": sell_order.order_id,
                                "status": sell_order.status,
                                "filled_count": sell_filled,
                                "average_price_cents": sell_avg,
                                "sell_limit_cents": sell_limit,
                                "contracts_requested": n,
                            }
                            log_fp.write(json.dumps(sell_resp_rec, default=str) + "\n")
                            log_fp.flush()

                            if sell_filled == 0:
                                print(
                                    f"[live-ta-v2] EXIT NO-FILL ticker={ticker} "
                                    f"reason={exit_reason} limit={sell_limit}c bid={current_bid}c",
                                    flush=True,
                                )
                                continue

                            if sell_filled < n:
                                # Partial close: realize the filled portion,
                                # leave the remainder open for retry.
                                exit_fee = kalshi_taker_fee_cents(sell_avg, count=sell_filled)
                                partial_gross = sell_filled * (sell_avg - pos["entry_price_cents"])
                                entry_fee_partial = int(
                                    round(pos["entry_fee_cents"] * sell_filled / n)
                                )
                                partial_net = partial_gross - entry_fee_partial - exit_fee
                                partial_rec = {
                                    "kind": exit_reason,
                                    "ts_ms": int(time.time() * 1000),
                                    "ticker": ticker,
                                    "side": side,
                                    "contracts": sell_filled,
                                    "partial": True,
                                    "entry_price_cents": pos["entry_price_cents"],
                                    "exit_price_cents": sell_avg,
                                    "current_bid_cents": current_bid,
                                    "hwm_bid_cents": pos["hwm_bid_cents"],
                                    "score_latest": score_latest,
                                    "entry_fee_cents": entry_fee_partial,
                                    "exit_fee_cents": exit_fee,
                                    "gross_cents": partial_gross,
                                    "net_cents": partial_net,
                                    "cycle_floor_ms": pos["cycle_floor_ms"],
                                    "cycle_close_ms": pos["cycle_close_ms"],
                                    "settled_via": exit_reason,
                                }
                                log_fp.write(json.dumps(partial_rec, default=str) + "\n")
                                log_fp.flush()
                                settled_trades.append(partial_rec)
                                pos["contracts"] = n - sell_filled
                                pos["entry_fee_cents"] = pos["entry_fee_cents"] - entry_fee_partial
                                if partial_net < 0:
                                    daily_loss_cents += -partial_net
                                    if daily_loss_cents >= DAILY_LOSS_CAP_CENTS and halt_reason is None:
                                        halt_reason = "DAILY-LOSS-CAP"
                                if exit_reason == "trail_exit":
                                    trail_exits += 1
                                else:
                                    inversion_exits += 1
                                print(
                                    f"[live-ta-v2] PARTIAL EXIT ticker={ticker} "
                                    f"reason={exit_reason} {sell_filled}/{n}@{sell_avg}c "
                                    f"net={partial_net:+d}c remaining={n-sell_filled}",
                                    flush=True,
                                )
                                continue

                            # Full close.
                            exit_fee = kalshi_taker_fee_cents(sell_avg, count=sell_filled)
                            gross = sell_filled * (sell_avg - pos["entry_price_cents"])
                            net = gross - pos["entry_fee_cents"] - exit_fee
                            trade = {
                                **pos,
                                "exit_price_cents": sell_avg,
                                "exit_fee_cents": exit_fee,
                                "gross_cents": gross,
                                "net_cents": net,
                                "settled_via": exit_reason,
                                "current_bid_cents": current_bid,
                                "score_latest": score_latest,
                            }
                            settled_trades.append(trade)
                            log_fp.write(
                                json.dumps({"kind": exit_reason, **trade}, default=str) + "\n"
                            )
                            log_fp.flush()
                            closed_tickers.append(ticker)
                            if net < 0:
                                daily_loss_cents += -net
                                if daily_loss_cents >= DAILY_LOSS_CAP_CENTS and halt_reason is None:
                                    halt_reason = "DAILY-LOSS-CAP"
                                    print(
                                        f"[live-ta-v2] HALT DAILY-LOSS-CAP "
                                        f"loss=${daily_loss_cents/100:.2f}",
                                        flush=True,
                                    )
                            if exit_reason == "trail_exit":
                                trail_exits += 1
                            else:
                                inversion_exits += 1
                            print(
                                f"[live-ta-v2] {exit_reason.upper()} ticker={ticker} "
                                f"side={side} {sell_filled}@{sell_avg}c "
                                f"entry={pos['entry_price_cents']}c "
                                f"hwm={pos['hwm_bid_cents']}c bid={current_bid}c "
                                f"score={score_latest} net={net:+d}c",
                                flush=True,
                            )

                    for ticker in closed_tickers:
                        open_positions.pop(ticker, None)

                    # Settlement reconciliation for any position that survived
                    # to cycle close without trail/inversion firing.
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
                            trade = {
                                **pos,
                                "outcome": outcome,
                                "gross_cents": gross,
                                "net_cents": net,
                                "settled_via": "settlement",
                            }
                            settled_trades.append(trade)
                            log_fp.write(
                                json.dumps({"kind": "settle", **trade}, default=str) + "\n"
                            )
                            log_fp.flush()

                            if net < 0:
                                daily_loss_cents += -net
                                if daily_loss_cents >= DAILY_LOSS_CAP_CENTS and halt_reason is None:
                                    halt_reason = "DAILY-LOSS-CAP"
                                    print(
                                        f"[live-ta-v2] HALT DAILY-LOSS-CAP "
                                        f"loss=${daily_loss_cents/100:.2f}",
                                        flush=True,
                                    )

                    if eid_max > spot_event_id_watermark:
                        spot_event_id_watermark = eid_max
                        last_new_event_t = now_wall

                if now_wall - last_new_event_t > args.stale_venue_timeout_s:
                    print(
                        f"[live-ta-v2] STALE venue={args.venue} for "
                        f"{now_wall-last_new_event_t:.0f}s. Exiting for watchdog restart.",
                        flush=True,
                    )
                    log_fp.close()
                    return 2

                if now_wall - last_status_t >= args.status_every_s:
                    wr = sum(1 for t in settled_trades if t["net_cents"] > 0)
                    total_net = sum(t["net_cents"] for t in settled_trades)
                    print(
                        f"[live-ta-v2] decisions={decisions_made} (+{decisions_made-last_dec}) "
                        f"fills={fills} (+{fills-last_fills}) "
                        f"trail_ex={trail_exits} inv_ex={inversion_exits} "
                        f"open={len(open_positions)} settled={len(settled_trades)} "
                        f"wr={wr}/{len(settled_trades)} net={total_net:+d}c "
                        f"daily_loss=${daily_loss_cents/100:.2f} halt={halt_reason} "
                        f"watermark_eid={spot_event_id_watermark}",
                        flush=True,
                    )
                    last_status_t = now_wall
                    last_dec = decisions_made
                    last_fills = fills

                await asyncio.sleep(args.poll_interval_s)
        finally:
            log_fp.close()

    print(
        f"[live-ta-v2] stopped. decisions={decisions_made} fills={fills} "
        f"trail_exits={trail_exits} inversion_exits={inversion_exits} "
        f"settled={len(settled_trades)} halt={halt_reason}",
        flush=True,
    )
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
