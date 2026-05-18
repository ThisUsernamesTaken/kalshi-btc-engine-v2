"""Live HYBRID trader — velocity entry + v2 position management.

Combines the fast-acting BTC spot velocity entry signal from
``shadow_velocity.py`` with the active position-management mechanics
(trailing stop on our bid, late-cycle hold-to-settle, signal-flip exit,
settlement reconciliation, hard-cap safeties) from ``live_ta_v2.py``.

Differences vs live_ta_v2.py:

1. ENTRY is velocity-based rather than TA-bar-based. We maintain a
   rolling deque of (ts_ms, mid) spot ticks covering the last ~35s and
   fire on |delta_15s| >= 20 or |delta_30s| >= 30. Tier (and contract
   count) is determined by the magnitude of the burst:
       |delta| >= 40 -> STRONG  (40 ct)
       |delta| >= 30 -> MEDIUM  (20 ct)
       |delta| >= 20 -> WEAK    (10 ct)
   YES is bought on an up-burst, NO on a down-burst, at ask + 3c IOC.

2. TA CONFIRMATION: the Pine Script TA score is computed on the side
   from the minute bars. If the current cycle's latest score strongly
   disagrees with the velocity direction (score > +20 while velocity is
   down, or score < -20 while velocity is up) we skip the entry. This
   filters noise-burst false positives.

3. SIGNAL-FLIP EXIT replaces score-inversion exit. If a SECOND velocity
   burst fires in the OPPOSITE direction to our open position, exit
   immediately at bid - 3c IOC. A short hold-grace prevents the entry
   burst from immediately tripping its own opposite check.

Trail-stop (10c off HWM with bid >= 5c floor), late-cycle hold gate
(no trail / no signal-flip in the final 2 minutes — let it settle), and
settlement reconciliation are unchanged from live_ta_v2.py.

JSONL kinds: startup, velocity_trigger, velocity_skip_*, order_attempt,
order_response, order_rejected, order_error, order_no_fill, fill,
exit_attempt, exit_response, exit_rejected, exit_error, trail_exit,
signal_flip_exit, settle.
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
from collections import deque
from pathlib import Path
from typing import Any

from kalshi_btc_engine_v2.features.ta_score import (
    OHLCBar,
    TAScoreConfig,
    TAScoreState,
)
from kalshi_btc_engine_v2.policy.edge import kalshi_taker_fee_cents
from kalshi_btc_engine_v2.storage.sqlite import connect

_V1_ROOT = Path(r"C:\Trading\btc-bias-engine")
if str(_V1_ROOT) not in sys.path:
    sys.path.insert(0, str(_V1_ROOT))
from kalshi_client import KalshiAPIError, KalshiClient  # noqa: E402

# ── Constants ─────────────────────────────────────────────────────────────

DEFAULT_SPOT_VENUE = "bitstamp"  # bitstamp has been more reliable than coinbase
CYCLE_MS = 15 * 60 * 1000
MIN_MS = 60 * 1000

# Velocity thresholds and rolling window.
THRESHOLD_15S_USD = 20.0
THRESHOLD_30S_USD = 30.0
VELOCITY_WINDOW_MS = 35_000

# Tier cutoffs on max(|delta_15s|, |delta_30s|).
TIER_STRONG_USD = 40.0
TIER_MEDIUM_USD = 30.0

TIER_CONTRACTS: dict[str, int] = {
    "STRONG": 40,
    "MEDIUM": 20,
    "WEAK":   10,
    "MIMIC":   5,
}
MIN_TIER_CONTRACTS = 5

# v2 position-management thresholds.
TRAIL_CENTS = 10
TRAIL_BID_FLOOR_CENTS = 5
LATE_CYCLE_HOLD_SECONDS = 120  # final 2 minutes: no trail, no signal-flip

# TA confirmation gate: |score| this far in the OPPOSITE direction blocks entry.
TA_DISAGREE_THRESHOLD = 20.0

# Min seconds we hold a position before the opposite-direction velocity check
# is allowed to fire. Prevents the entry burst from immediately tripping flip.
SIGNAL_FLIP_GRACE_S = 20.0

# Slip & limit caps.
SLIPPAGE_CENTS = 3
EXIT_SLIPPAGE_CENTS = 3
LIMIT_CAP_CENTS = 99

# Hard safety caps.
DAILY_LOSS_CAP_CENTS = 999999
MIN_BALANCE_CENTS = 5 * 100
STALE_DATA_TIMEOUT_MS = 30_000

KALSHI_CREDS_PATH = Path(r"C:\Trading\btc-bias-engine\credentials\kalshi.env")


def minute_floor_ms(ts_ms: int) -> int:
    return (ts_ms // MIN_MS) * MIN_MS


def cycle_floor_ms(ts_ms: int) -> int:
    return (ts_ms // CYCLE_MS) * CYCLE_MS


def bars_in_cycle_for_minute(minute_ms: int) -> int:
    return ((minute_ms - cycle_floor_ms(minute_ms)) // MIN_MS) + 1


class MinuteBarAggregator:
    """Build 1-min OHLC bars from spot quote events."""

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
    """Per-15-min-cycle state for TA score (used only for confirmation gate)."""

    def __init__(self) -> None:
        self._current_cycle_floor: int | None = None
        self._cycle_open_price: float | None = None
        self.score_state = TAScoreState()

    def maybe_roll_cycle(self, minute_ms: int, open_price: float) -> bool:
        cf = cycle_floor_ms(minute_ms)
        if self._current_cycle_floor is None or cf != self._current_cycle_floor:
            self._current_cycle_floor = cf
            self._cycle_open_price = open_price
            self.score_state = TAScoreState(config=self.score_state.config)
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
    conn: sqlite3.Connection, cycle_close_ms: int
) -> tuple[str, dict[str, Any]] | None:
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
    """Most recent best bid on the side we hold, in cents."""
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
    if row["best_yes_ask"] is None:
        return None
    yes_ask_c = int(round(float(row["best_yes_ask"]) * 100))
    return max(0, 100 - yes_ask_c)


def mid_at_or_before(window: deque, target_ts_ms: int) -> tuple[int, float] | None:
    best: tuple[int, float] | None = None
    for ts_ms, mid in window:
        if ts_ms <= target_ts_ms:
            best = (ts_ms, mid)
        else:
            break
    return best


def tier_for_magnitude(mag_usd: float) -> str:
    if mag_usd >= TIER_STRONG_USD:
        return "STRONG"
    if mag_usd >= TIER_MEDIUM_USD:
        return "MEDIUM"
    return "WEAK"


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
    pem_path_raw = env.get("KALSHI_PRIVATE_KEY_PATH") or os.environ.get(
        "KALSHI_PRIVATE_KEY_PATH"
    )
    if not key_id or not pem_path_raw:
        raise RuntimeError("kalshi.env missing KALSHI_API_KEY or KALSHI_PRIVATE_KEY_PATH")
    pem_path = Path(pem_path_raw)
    if not pem_path.exists():
        raise FileNotFoundError(f"Kalshi PEM not found: {pem_path}")
    return key_id, pem_path.read_text(encoding="utf-8")


def _utc_today_floor_ms() -> int:
    today = dt.datetime.now(dt.UTC).date()
    return int(
        dt.datetime(today.year, today.month, today.day, tzinfo=dt.UTC).timestamp() * 1000
    )


def replay_log_state(log_path: Path) -> tuple[int, set[int]]:
    """Reload today's net loss + set of cycle_floor_ms we've already entered."""
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
                if kind in ("settle", "trail_exit", "signal_flip_exit"):
                    cc = rec.get("cycle_close_ms")
                    if cc is None:
                        continue
                    cc_i = int(cc)
                    if today_floor_ms <= cc_i < today_end_ms:
                        total_net += int(rec.get("net_cents", 0))
                elif kind in ("velocity_trigger", "order_attempt", "fill"):
                    cf = rec.get("cycle_floor_ms")
                    if cf is not None:
                        entered.add(int(cf))
    except OSError:
        return 0, set()
    return max(0, -total_net), entered


async def main_async() -> int:
    parser = argparse.ArgumentParser(
        prog="live-hybrid",
        description="Velocity entry + v2 position-management LIVE trader.",
    )
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--decision-log", required=True, type=Path)
    parser.add_argument("--venue", default=DEFAULT_SPOT_VENUE)
    parser.add_argument("--poll-interval-s", type=float, default=0.5)
    parser.add_argument("--status-every-s", type=float, default=30.0)
    parser.add_argument("--start-at-tail", action="store_true")
    parser.add_argument(
        "--stale-venue-timeout-s", type=float, default=600.0,
        help="Exit code 2 if no new spot events from --venue within this many seconds.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run the full decision pipeline and log a synthetic fill, "
        "but DO NOT call Kalshi place_order. Balance fetch still runs.",
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

    window: deque = deque()  # (ts_ms, mid)
    open_positions: dict[str, dict[str, Any]] = {}
    settled_trades: list[dict[str, Any]] = []

    triggers_fired = 0
    fills = 0
    trail_exits = 0
    flip_exits = 0
    last_status_t = time.time()
    last_triggers = 0
    last_fills = 0
    last_new_event_t = time.time()
    last_received_ts_ms: int | None = None
    score_latest: float | None = None  # cleared on cycle roll

    daily_loss_cents, entered_cycles = replay_log_state(args.decision_log)
    halt_reason: str | None = None
    if daily_loss_cents >= DAILY_LOSS_CAP_CENTS:
        halt_reason = "DAILY-LOSS-CAP"
    daily_loss_day_floor = _utc_today_floor_ms()

    print(
        f"[live-hybrid] starting tail={args.db} venue={args.venue} -> {args.decision_log} "
        f"dry_run={args.dry_run} daily_loss_loaded={daily_loss_cents}c "
        f"entered_cycles_loaded={len(entered_cycles)} halt={halt_reason} "
        f"thr15={THRESHOLD_15S_USD} thr30={THRESHOLD_30S_USD} "
        f"trail={TRAIL_CENTS}c ta_disagree>={TA_DISAGREE_THRESHOLD}",
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
        "threshold_15s_usd": THRESHOLD_15S_USD,
        "threshold_30s_usd": THRESHOLD_30S_USD,
        "tier_strong_usd": TIER_STRONG_USD,
        "tier_medium_usd": TIER_MEDIUM_USD,
        "trail_cents": TRAIL_CENTS,
        "trail_bid_floor_cents": TRAIL_BID_FLOOR_CENTS,
        "late_cycle_hold_seconds": LATE_CYCLE_HOLD_SECONDS,
        "ta_disagree_threshold": TA_DISAGREE_THRESHOLD,
        "signal_flip_grace_s": SIGNAL_FLIP_GRACE_S,
        "daily_loss_cap_cents": DAILY_LOSS_CAP_CENTS,
        "min_balance_cents": MIN_BALANCE_CENTS,
        "stale_data_timeout_ms": STALE_DATA_TIMEOUT_MS,
        "slippage_cents": SLIPPAGE_CENTS,
        "exit_slippage_cents": EXIT_SLIPPAGE_CENTS,
        "variant": "live_hybrid",
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
                        print("[live-hybrid] new UTC day — daily loss reset", flush=True)

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
                        mid_raw = row["mid"]
                        if mid_raw is None:
                            continue
                        mid = float(mid_raw)
                        last_received_ts_ms = ts_ms
                        latest_ts_ms = ts_ms
                        latest_mid = mid

                        # Velocity window.
                        window.append((ts_ms, mid))

                        # TA score sidecar (for confirmation gate).
                        completed_bar = aggregator.ingest(ts_ms, mid)
                        if completed_bar is not None:
                            cycle_rolled = cycle.maybe_roll_cycle(
                                completed_bar.ts_minute_ms, completed_bar.open
                            )
                            if cycle_rolled:
                                score_latest = None
                            cycle_open = cycle.cycle_open_price
                            if cycle_open is not None:
                                bar_in_cycle = bars_in_cycle_for_minute(
                                    completed_bar.ts_minute_ms
                                )
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

                    # Trim velocity window.
                    if latest_ts_ms is not None:
                        cutoff = latest_ts_ms - VELOCITY_WINDOW_MS
                        while window and window[0][0] < cutoff:
                            window.popleft()

                    # ── Velocity entry/flip evaluation ─────────────────
                    delta_15s = 0.0
                    delta_30s = 0.0
                    have_15s = False
                    have_30s = False
                    trig_up = False
                    trig_down = False
                    if latest_ts_ms is not None and latest_mid is not None and len(window) >= 2:
                        ref_15s = mid_at_or_before(window, latest_ts_ms - 15_000)
                        ref_30s = mid_at_or_before(window, latest_ts_ms - 30_000)
                        delta_15s = (latest_mid - ref_15s[1]) if ref_15s else 0.0
                        delta_30s = (latest_mid - ref_30s[1]) if ref_30s else 0.0
                        have_15s = ref_15s is not None and (latest_ts_ms - ref_15s[0]) >= 14_000
                        have_30s = ref_30s is not None and (latest_ts_ms - ref_30s[0]) >= 28_000
                        trig_up = (have_15s and delta_15s >= THRESHOLD_15S_USD) or (
                            have_30s and delta_30s >= THRESHOLD_30S_USD
                        )
                        trig_down = (have_15s and delta_15s <= -THRESHOLD_15S_USD) or (
                            have_30s and delta_30s <= -THRESHOLD_30S_USD
                        )

                    # Signal-flip exit: check FIRST so a reversal that
                    # would otherwise be a fresh entry on a new cycle
                    # closes the existing position first.
                    flip_targets: list[tuple[str, str]] = []
                    if (trig_up or trig_down) and open_positions:
                        burst_dir = "up" if trig_up else "down"
                        for tkr, p in open_positions.items():
                            if p.get("dry_run"):
                                continue
                            entered_at_ms = p.get("entered_at_ms", 0)
                            if latest_ts_ms is None:
                                continue
                            if (latest_ts_ms - entered_at_ms) / 1000.0 < SIGNAL_FLIP_GRACE_S:
                                continue
                            seconds_to_close = (
                                p.get("cycle_close_ms", 0) - now_ms
                            ) / 1000.0
                            if seconds_to_close <= LATE_CYCLE_HOLD_SECONDS:
                                continue
                            if p["side"] == "yes" and burst_dir == "down":
                                flip_targets.append((tkr, burst_dir))
                            elif p["side"] == "no" and burst_dir == "up":
                                flip_targets.append((tkr, burst_dir))

                    closed_tickers: list[str] = []
                    for tkr, burst_dir in flip_targets:
                        pos = open_positions[tkr]
                        side = pos["side"]
                        current_bid = latest_our_side_bid_cents(conn, tkr, side)
                        if current_bid is None:
                            continue
                        sell_limit = max(1, current_bid - EXIT_SLIPPAGE_CENTS)
                        n = pos["contracts"]
                        exit_reason = "signal_flip_exit"

                        attempt = {
                            "kind": "exit_attempt",
                            "exit_reason": exit_reason,
                            "ts_ms": now_ms,
                            "ticker": tkr,
                            "side": side,
                            "contracts": n,
                            "current_bid_cents": current_bid,
                            "sell_limit_cents": sell_limit,
                            "flip_direction": burst_dir,
                            "velocity_15s": round(delta_15s, 2),
                            "velocity_30s": round(delta_30s, 2),
                            "entry_price_cents": pos["entry_price_cents"],
                            "cycle_floor_ms": pos["cycle_floor_ms"],
                            "cycle_close_ms": pos["cycle_close_ms"],
                        }
                        log_fp.write(json.dumps(attempt, default=str) + "\n")
                        log_fp.flush()

                        try:
                            sell_order = await client.place_order(
                                ticker=tkr,
                                side=side,
                                count=n,
                                price=sell_limit,
                                action="sell",
                                time_in_force="immediate_or_cancel",
                            )
                        except KalshiAPIError as e:
                            log_fp.write(json.dumps({
                                "kind": "exit_rejected",
                                "exit_reason": exit_reason,
                                "ts_ms": int(time.time() * 1000),
                                "ticker": tkr,
                                "side": side,
                                "sell_limit_cents": sell_limit,
                                "contracts": n,
                                "status": e.status,
                                "body": e.body,
                                "path": e.path,
                            }, default=str) + "\n")
                            log_fp.flush()
                            print(
                                f"[live-hybrid] FLIP EXIT REJECTED ticker={tkr} "
                                f"status={e.status} body={e.body}", flush=True,
                            )
                            continue
                        except Exception as e:  # noqa: BLE001
                            log_fp.write(json.dumps({
                                "kind": "exit_error",
                                "exit_reason": exit_reason,
                                "ts_ms": int(time.time() * 1000),
                                "ticker": tkr,
                                "side": side,
                                "sell_limit_cents": sell_limit,
                                "contracts": n,
                                "error": repr(e),
                            }, default=str) + "\n")
                            log_fp.flush()
                            print(f"[live-hybrid] FLIP EXIT ERROR {e!r}", flush=True)
                            continue

                        sell_filled = int(sell_order.filled_count or 0)
                        sell_avg = (
                            int(sell_order.average_price)
                            if sell_order.average_price is not None
                            else sell_limit
                        )
                        log_fp.write(json.dumps({
                            "kind": "exit_response",
                            "exit_reason": exit_reason,
                            "ts_ms": int(time.time() * 1000),
                            "ticker": tkr,
                            "side": side,
                            "order_id": sell_order.order_id,
                            "status": sell_order.status,
                            "filled_count": sell_filled,
                            "average_price_cents": sell_avg,
                            "sell_limit_cents": sell_limit,
                            "contracts_requested": n,
                        }, default=str) + "\n")
                        log_fp.flush()

                        if sell_filled == 0:
                            print(
                                f"[live-hybrid] FLIP NO-FILL ticker={tkr} "
                                f"limit={sell_limit}c bid={current_bid}c", flush=True,
                            )
                            continue

                        if sell_filled < n:
                            # Partial — realize filled, keep remainder open.
                            exit_fee = kalshi_taker_fee_cents(sell_avg, count=sell_filled)
                            partial_gross = sell_filled * (sell_avg - pos["entry_price_cents"])
                            entry_fee_partial = int(round(
                                pos["entry_fee_cents"] * sell_filled / n
                            ))
                            partial_net = partial_gross - entry_fee_partial - exit_fee
                            partial_rec = {
                                "kind": exit_reason,
                                "ts_ms": int(time.time() * 1000),
                                "ticker": tkr,
                                "side": side,
                                "contracts": sell_filled,
                                "partial": True,
                                "entry_price_cents": pos["entry_price_cents"],
                                "exit_price_cents": sell_avg,
                                "current_bid_cents": current_bid,
                                "flip_direction": burst_dir,
                                "velocity_15s": round(delta_15s, 2),
                                "velocity_30s": round(delta_30s, 2),
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
                            flip_exits += 1
                            print(
                                f"[live-hybrid] FLIP PARTIAL ticker={tkr} "
                                f"{sell_filled}/{n}@{sell_avg}c net={partial_net:+d}c",
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
                            "flip_direction": burst_dir,
                            "velocity_15s": round(delta_15s, 2),
                            "velocity_30s": round(delta_30s, 2),
                        }
                        settled_trades.append(trade)
                        log_fp.write(json.dumps({"kind": exit_reason, **trade},
                                                default=str) + "\n")
                        log_fp.flush()
                        closed_tickers.append(tkr)
                        if net < 0:
                            daily_loss_cents += -net
                            if daily_loss_cents >= DAILY_LOSS_CAP_CENTS and halt_reason is None:
                                halt_reason = "DAILY-LOSS-CAP"
                        flip_exits += 1
                        print(
                            f"[live-hybrid] SIGNAL_FLIP_EXIT ticker={tkr} side={side} "
                            f"{sell_filled}@{sell_avg}c entry={pos['entry_price_cents']}c "
                            f"net={net:+d}c", flush=True,
                        )

                    for tkr in closed_tickers:
                        open_positions.pop(tkr, None)

                    # ── New entry (if a trigger fired and cycle not used) ──
                    if (trig_up or trig_down) and latest_ts_ms is not None:
                        cf = cycle_floor_ms(latest_ts_ms)
                        cycle_close = cf + CYCLE_MS
                        direction = "up" if trig_up else "down"
                        side = "yes" if trig_up else "no"
                        mag = max(abs(delta_15s), abs(delta_30s))
                        tier_name = tier_for_magnitude(mag)

                        base_trigger_rec = {
                            "kind": "velocity_trigger",
                            "ts_ms": now_ms,
                            "timestamp": latest_ts_ms,
                            "direction": direction,
                            "side": side,
                            "tier_name": tier_name,
                            "velocity_15s": round(delta_15s, 2),
                            "velocity_30s": round(delta_30s, 2),
                            "btc_price": round(latest_mid, 2) if latest_mid is not None else None,
                            "cycle_floor_ms": cf,
                            "cycle_close_ms": cycle_close,
                            "score_latest": score_latest,
                        }
                        triggers_fired += 1
                        log_fp.write(json.dumps(base_trigger_rec, default=str) + "\n")
                        log_fp.flush()

                        skip_reason: str | None = None
                        if halt_reason is not None:
                            skip_reason = "halt"
                        elif cf in entered_cycles:
                            skip_reason = "cycle_already_entered"
                        elif cf in {p["cycle_floor_ms"] for p in open_positions.values()}:
                            skip_reason = "cycle_position_open"
                        else:
                            if score_latest is not None:
                                if side == "yes" and score_latest < -TA_DISAGREE_THRESHOLD:
                                    skip_reason = "ta_disagree"
                                elif side == "no" and score_latest > TA_DISAGREE_THRESHOLD:
                                    skip_reason = "ta_disagree"

                        if skip_reason is not None:
                            log_fp.write(json.dumps({
                                **base_trigger_rec,
                                "kind": f"velocity_skip_{skip_reason}",
                            }, default=str) + "\n")
                            log_fp.flush()
                        else:
                            stale_age_ms = (
                                now_ms - last_received_ts_ms
                                if last_received_ts_ms is not None
                                else None
                            )
                            if stale_age_ms is None or stale_age_ms > STALE_DATA_TIMEOUT_MS:
                                log_fp.write(json.dumps({
                                    **base_trigger_rec,
                                    "kind": "velocity_skip_stale_data",
                                    "stale_age_ms": stale_age_ms,
                                }, default=str) + "\n")
                                log_fp.flush()
                            else:
                                market = find_atm_market(conn, cycle_close)
                                if market is None:
                                    log_fp.write(json.dumps({
                                        **base_trigger_rec,
                                        "kind": "velocity_skip_no_market",
                                    }, default=str) + "\n")
                                    log_fp.flush()
                                else:
                                    ticker, mkt = market
                                    yes_ask_cents = int(round(mkt["yes_ask"] * 100))
                                    no_ask_cents = 100 - yes_ask_cents
                                    entry_cents = yes_ask_cents if side == "yes" else no_ask_cents
                                    limit_cents = min(LIMIT_CAP_CENTS, entry_cents + SLIPPAGE_CENTS)
                                    contracts = TIER_CONTRACTS.get(tier_name, MIN_TIER_CONTRACTS)

                                    try:
                                        bal = await client.get_balance()
                                    except Exception as e:  # noqa: BLE001
                                        log_fp.write(json.dumps({
                                            **base_trigger_rec,
                                            "kind": "velocity_skip_balance_error",
                                            "error": repr(e),
                                        }, default=str) + "\n")
                                        log_fp.flush()
                                        continue

                                    if bal.balance < MIN_BALANCE_CENTS:
                                        halt_reason = "MIN-BALANCE"
                                        log_fp.write(json.dumps({
                                            **base_trigger_rec,
                                            "kind": "velocity_skip_low_balance",
                                            "balance_cents": bal.balance,
                                        }, default=str) + "\n")
                                        log_fp.flush()
                                        print(
                                            f"[live-hybrid] HALT MIN-BALANCE "
                                            f"bal=${bal.balance/100:.2f}", flush=True,
                                        )
                                        continue

                                    entered_cycles.add(cf)

                                    attempt_rec = {
                                        "kind": "order_attempt",
                                        "ts_ms": now_ms,
                                        "ticker": ticker,
                                        "side": side,
                                        "contracts": contracts,
                                        "ask_cents": entry_cents,
                                        "limit_cents": limit_cents,
                                        "yes_ask_cents": yes_ask_cents,
                                        "no_ask_cents": no_ask_cents,
                                        "balance_cents": bal.balance,
                                        "dry_run": args.dry_run,
                                        "tier_name": tier_name,
                                        "velocity_15s": round(delta_15s, 2),
                                        "velocity_30s": round(delta_30s, 2),
                                        "direction": direction,
                                        "cycle_floor_ms": cf,
                                        "cycle_close_ms": cycle_close,
                                    }
                                    log_fp.write(json.dumps(attempt_rec, default=str) + "\n")
                                    log_fp.flush()

                                    if args.dry_run:
                                        filled_count = contracts
                                        avg_price_cents = entry_cents
                                        order_id = "DRY-RUN"
                                        order_status = "dry_run"
                                        log_fp.write(json.dumps({
                                            "kind": "order_dry_run",
                                            "ticker": ticker,
                                            "side": side,
                                            "limit_cents": limit_cents,
                                            "contracts": contracts,
                                        }, default=str) + "\n")
                                        log_fp.flush()
                                    else:
                                        try:
                                            order = await client.place_order(
                                                ticker=ticker,
                                                side=side,
                                                count=contracts,
                                                price=limit_cents,
                                                order_type="limit",
                                                action="buy",
                                                time_in_force="immediate_or_cancel",
                                            )
                                        except KalshiAPIError as e:
                                            log_fp.write(json.dumps({
                                                "kind": "order_rejected",
                                                "ts_ms": int(time.time() * 1000),
                                                "ticker": ticker,
                                                "side": side,
                                                "limit_cents": limit_cents,
                                                "contracts": contracts,
                                                "status": e.status,
                                                "body": e.body,
                                                "path": e.path,
                                            }, default=str) + "\n")
                                            log_fp.flush()
                                            print(
                                                f"[live-hybrid] ORDER REJECTED ticker={ticker} "
                                                f"status={e.status} body={e.body}", flush=True,
                                            )
                                            continue
                                        except Exception as e:  # noqa: BLE001
                                            log_fp.write(json.dumps({
                                                "kind": "order_error",
                                                "ts_ms": int(time.time() * 1000),
                                                "ticker": ticker,
                                                "side": side,
                                                "limit_cents": limit_cents,
                                                "contracts": contracts,
                                                "error": repr(e),
                                            }, default=str) + "\n")
                                            log_fp.flush()
                                            print(f"[live-hybrid] ORDER ERROR {e!r}", flush=True)
                                            continue

                                        filled_count = int(order.filled_count or 0)
                                        avg_price_cents = (
                                            int(order.average_price)
                                            if order.average_price is not None
                                            else entry_cents
                                        )
                                        order_id = order.order_id
                                        order_status = order.status

                                        log_fp.write(json.dumps({
                                            "kind": "order_response",
                                            "ts_ms": int(time.time() * 1000),
                                            "ticker": ticker,
                                            "side": side,
                                            "order_id": order_id,
                                            "status": order_status,
                                            "filled_count": filled_count,
                                            "average_price_cents": avg_price_cents,
                                            "limit_cents": limit_cents,
                                            "contracts_requested": contracts,
                                        }, default=str) + "\n")
                                        log_fp.flush()

                                    if filled_count == 0:
                                        log_fp.write(json.dumps({
                                            "kind": "order_no_fill",
                                            "ts_ms": int(time.time() * 1000),
                                            "ticker": ticker,
                                            "side": side,
                                            "limit_cents": limit_cents,
                                            "ask_cents": entry_cents,
                                            "order_id": order_id,
                                            "status": order_status,
                                        }, default=str) + "\n")
                                        log_fp.flush()
                                        print(
                                            f"[live-hybrid] IOC NO-FILL ticker={ticker} "
                                            f"side={side} limit={limit_cents}c", flush=True,
                                        )
                                    else:
                                        entry_fee = kalshi_taker_fee_cents(
                                            avg_price_cents, count=filled_count
                                        )
                                        open_positions[ticker] = {
                                            "ticker": ticker,
                                            "side": side,
                                            "contracts": filled_count,
                                            "entry_price_cents": avg_price_cents,
                                            "entry_fee_cents": entry_fee,
                                            "tier_name": tier_name,
                                            "direction": direction,
                                            "velocity_15s_at_entry": round(delta_15s, 2),
                                            "velocity_30s_at_entry": round(delta_30s, 2),
                                            "cycle_close_ms": cycle_close,
                                            "cycle_floor_ms": cf,
                                            "order_id": order_id,
                                            "dry_run": args.dry_run,
                                            "hwm_bid_cents": avg_price_cents,
                                            "entered_at_ms": int(time.time() * 1000),
                                        }
                                        fills += 1

                                        fill_rec = {
                                            "kind": "fill",
                                            "ts_ms": int(time.time() * 1000),
                                            "ticker": ticker,
                                            "side": side,
                                            "entry_price_cents": avg_price_cents,
                                            "entry_fee_cents": entry_fee,
                                            "contracts": filled_count,
                                            "order_id": order_id,
                                            "status": order_status,
                                            "dry_run": args.dry_run,
                                            "limit_cents": limit_cents,
                                            "tier_name": tier_name,
                                            "direction": direction,
                                            "velocity_15s": round(delta_15s, 2),
                                            "velocity_30s": round(delta_30s, 2),
                                            "cycle_floor_ms": cf,
                                            "cycle_close_ms": cycle_close,
                                            "score_latest": score_latest,
                                        }
                                        log_fp.write(json.dumps(fill_rec, default=str) + "\n")
                                        log_fp.flush()
                                        print(
                                            f"[live-hybrid] FILL ticker={ticker} side={side} "
                                            f"{filled_count}@{avg_price_cents}c tier={tier_name} "
                                            f"v15={delta_15s:+.1f} v30={delta_30s:+.1f} "
                                            f"score={score_latest} dry_run={args.dry_run}",
                                            flush=True,
                                        )

                    # ── Trail-stop loop ───────────────────────────────
                    trail_closed: list[str] = []
                    if open_positions:
                        for ticker, pos in open_positions.items():
                            seconds_to_close = (
                                pos.get("cycle_close_ms", 0) - int(time.time() * 1000)
                            ) / 1000
                            if seconds_to_close <= LATE_CYCLE_HOLD_SECONDS:
                                continue
                            if pos.get("dry_run"):
                                continue
                            side = pos["side"]
                            current_bid = latest_our_side_bid_cents(conn, ticker, side)
                            if current_bid is None:
                                continue
                            if current_bid > pos["hwm_bid_cents"]:
                                pos["hwm_bid_cents"] = current_bid

                            if not (
                                current_bid <= pos["hwm_bid_cents"] - TRAIL_CENTS
                                and current_bid >= TRAIL_BID_FLOOR_CENTS
                            ):
                                continue

                            exit_reason = "trail_exit"
                            sell_limit = max(1, current_bid - EXIT_SLIPPAGE_CENTS)
                            n = pos["contracts"]

                            log_fp.write(json.dumps({
                                "kind": "exit_attempt",
                                "exit_reason": exit_reason,
                                "ts_ms": int(time.time() * 1000),
                                "ticker": ticker,
                                "side": side,
                                "contracts": n,
                                "current_bid_cents": current_bid,
                                "hwm_bid_cents": pos["hwm_bid_cents"],
                                "trail_cents": TRAIL_CENTS,
                                "sell_limit_cents": sell_limit,
                                "entry_price_cents": pos["entry_price_cents"],
                                "cycle_floor_ms": pos["cycle_floor_ms"],
                                "cycle_close_ms": pos["cycle_close_ms"],
                            }, default=str) + "\n")
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
                                log_fp.write(json.dumps({
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
                                }, default=str) + "\n")
                                log_fp.flush()
                                print(
                                    f"[live-hybrid] TRAIL EXIT REJECTED ticker={ticker} "
                                    f"status={e.status} body={e.body}", flush=True,
                                )
                                continue
                            except Exception as e:  # noqa: BLE001
                                log_fp.write(json.dumps({
                                    "kind": "exit_error",
                                    "exit_reason": exit_reason,
                                    "ts_ms": int(time.time() * 1000),
                                    "ticker": ticker,
                                    "side": side,
                                    "sell_limit_cents": sell_limit,
                                    "contracts": n,
                                    "error": repr(e),
                                }, default=str) + "\n")
                                log_fp.flush()
                                print(f"[live-hybrid] TRAIL EXIT ERROR {e!r}", flush=True)
                                continue

                            sell_filled = int(sell_order.filled_count or 0)
                            sell_avg = (
                                int(sell_order.average_price)
                                if sell_order.average_price is not None
                                else sell_limit
                            )

                            log_fp.write(json.dumps({
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
                            }, default=str) + "\n")
                            log_fp.flush()

                            if sell_filled == 0:
                                print(
                                    f"[live-hybrid] TRAIL NO-FILL ticker={ticker} "
                                    f"limit={sell_limit}c bid={current_bid}c", flush=True,
                                )
                                continue

                            if sell_filled < n:
                                exit_fee = kalshi_taker_fee_cents(sell_avg, count=sell_filled)
                                partial_gross = sell_filled * (sell_avg - pos["entry_price_cents"])
                                entry_fee_partial = int(round(
                                    pos["entry_fee_cents"] * sell_filled / n
                                ))
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
                                trail_exits += 1
                                print(
                                    f"[live-hybrid] TRAIL PARTIAL ticker={ticker} "
                                    f"{sell_filled}/{n}@{sell_avg}c net={partial_net:+d}c",
                                    flush=True,
                                )
                                continue

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
                            }
                            settled_trades.append(trade)
                            log_fp.write(json.dumps({"kind": exit_reason, **trade},
                                                    default=str) + "\n")
                            log_fp.flush()
                            trail_closed.append(ticker)
                            if net < 0:
                                daily_loss_cents += -net
                                if daily_loss_cents >= DAILY_LOSS_CAP_CENTS and halt_reason is None:
                                    halt_reason = "DAILY-LOSS-CAP"
                                    print(
                                        f"[live-hybrid] HALT DAILY-LOSS-CAP "
                                        f"loss=${daily_loss_cents/100:.2f}", flush=True,
                                    )
                            trail_exits += 1
                            print(
                                f"[live-hybrid] TRAIL_EXIT ticker={ticker} side={side} "
                                f"{sell_filled}@{sell_avg}c entry={pos['entry_price_cents']}c "
                                f"hwm={pos['hwm_bid_cents']}c bid={current_bid}c "
                                f"net={net:+d}c", flush=True,
                            )

                    for ticker in trail_closed:
                        open_positions.pop(ticker, None)

                    # ── Settlement reconciliation ─────────────────────
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
                            log_fp.write(json.dumps({"kind": "settle", **trade},
                                                    default=str) + "\n")
                            log_fp.flush()
                            if net < 0:
                                daily_loss_cents += -net
                                if daily_loss_cents >= DAILY_LOSS_CAP_CENTS and halt_reason is None:
                                    halt_reason = "DAILY-LOSS-CAP"
                                    print(
                                        f"[live-hybrid] HALT DAILY-LOSS-CAP "
                                        f"loss=${daily_loss_cents/100:.2f}", flush=True,
                                    )

                    if eid_max > spot_event_id_watermark:
                        spot_event_id_watermark = eid_max
                        last_new_event_t = now_wall

                if now_wall - last_new_event_t > args.stale_venue_timeout_s:
                    print(
                        f"[live-hybrid] STALE venue={args.venue} for "
                        f"{now_wall-last_new_event_t:.0f}s. Exiting for watchdog restart.",
                        flush=True,
                    )
                    log_fp.close()
                    return 2

                if now_wall - last_status_t >= args.status_every_s:
                    wr = sum(1 for t in settled_trades if t["net_cents"] > 0)
                    total_net = sum(t["net_cents"] for t in settled_trades)
                    print(
                        f"[live-hybrid] triggers={triggers_fired} (+{triggers_fired-last_triggers}) "
                        f"fills={fills} (+{fills-last_fills}) trail_ex={trail_exits} "
                        f"flip_ex={flip_exits} open={len(open_positions)} "
                        f"settled={len(settled_trades)} wr={wr}/{len(settled_trades)} "
                        f"net={total_net:+d}c daily_loss=${daily_loss_cents/100:.2f} "
                        f"halt={halt_reason} window_ticks={len(window)} "
                        f"watermark_eid={spot_event_id_watermark}",
                        flush=True,
                    )
                    last_status_t = now_wall
                    last_triggers = triggers_fired
                    last_fills = fills

                await asyncio.sleep(args.poll_interval_s)
        finally:
            log_fp.close()

    print(
        f"[live-hybrid] stopped. triggers={triggers_fired} fills={fills} "
        f"trail_exits={trail_exits} flip_exits={flip_exits} "
        f"settled={len(settled_trades)} halt={halt_reason}",
        flush=True,
    )
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
