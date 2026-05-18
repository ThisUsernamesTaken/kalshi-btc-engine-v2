"""Live V5 UNIFIED trader — late-favorite (V5 enhanced w/ velocity+cushion
sizing) + early-contrarian (Variant F) + earlier-moderate favorite,
hold-to-settle.

Three legs run per market (at most one entry per cycle):

  EARLIER-MODERATE LEG (minutes 5-12, i.e. close − 600s to close − 180s):
    Once a poll lands in the window, evaluate: favorite-side ask (price-
    derived) >= 60c AND |BTC − strike|/strike * 1e4 >= 10 bps AND BTC is on
    the favorite's side of the strike (direction agrees). If all hold: IOC
    buy the favorite at min(99c, ask + tier-adaptive slippage), 20 contracts
    (slippage tiers match the late leg: +2/+3/+5c above the ask for 80c+/
    65-79c/55-64c). One entry per window. If the signal does not fire by
    minute 12, the LATE leg takes over. Can be disabled with
    --disable-earlier-moderate.

  EARLY LEG (first 3 minutes of cycle):
    On market discovery, immediately start watching the book each poll.
    Track if either side has *traded* at >= 40c (contested flag).
    If contested AND either side hits 80c in first 3 minutes
    → IOC buy the OTHER (cheap) side at its current ask, 2ct.
    One early entry per window max. If the early leg fires, the late
    leg skips on this ticker (no double exposure).

  LATE LEG (last 3 minutes, minute_idx >= 12) — POC "leader_at_min12":
    On the FIRST poll of the late window per ticker, read the book and
    pick the leader (side with higher ask). Tier by leader's ask price
    (small sizing across the board for proof of concept). IOC limit is
    leader_ask + tier-adaptive slippage so thin books still fill:
      Leader ask >= 80c  → LEADER_80PLUS, 5ct, IOC @ min(ask+2, 99)
      Leader ask 65-79c  → LEADER_65_79,  5ct, IOC @ min(ask+3, 99)
      Leader ask 55-64c  → LEADER_55_64,  2ct, IOC @ min(ask+5, 99)
      Leader ask <  55c  → LEADER_BELOW_55, skip (too close to call)
    Velocity (v60) + cushion are still computed and logged but no longer
    drive sizing. Hold to settlement. One entry per window; if any
    earlier leg already filled, late is skipped.

BTC price source: REST poll Bitstamp `/api/v2/ticker/btcusd/` ~every poll.
Maintains a (ts, price) deque covering the last ~70s. Velocity = newest −
oldest-within-window-near-60s. If the buffer hasn't filled (startup or
network), the late leg falls back to MOVING_SMALL sizing (5ct favorite).

Safety:
  - DAILY_LOSS_CAP_CENTS = 4000 ($40/day). Replay log on startup to recompute.
  - MIN_BALANCE_CENTS = 500 ($5). Re-checked before every IOC.
  - Per-ticker dedupe: once attempted (either leg), don't re-enter.
  - replay_log_state rebuilds attempted set + open positions from JSONL.

JSONL kinds: startup, discover, btc_poll_error, early_watch, early_contested,
early_trigger, early_fill, early_skip, early_no_fill,
earlier_moderate_trigger, earlier_moderate_fill,
earlier_moderate_no_fill, earlier_moderate_skip,
late_trigger, late_fill, late_no_fill, late_skip,
order_attempt, order_rejected, order_error, settle, balance_halt,
poll_skip_halt, poll_skip_closed.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import datetime as dt
import json
import math
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp

_V1_ROOT = Path(r"D:\Trading\btc-bias-engine")
if str(_V1_ROOT) not in sys.path:
    sys.path.insert(0, str(_V1_ROOT))
from kalshi_client import (  # noqa: E402
    KalshiAPIError,
    KalshiClient,
    KalshiContract,
    KalshiOrderBook,
)
from kalshi_ws import KalshiWebSocket  # noqa: E402

# ── Strategy params (mirror backtest) ─────────────────────────────────────

# LATE leg
LATE_WINDOW_S = 180                # last 3 min of 15-min cycle (= minute 12+)
LATE_TRIGGER_CENTS = 80            # favorite trigger
LATE_CAP_CENTS = 99                # raise cap from 95 → 99 (per spec)
RESTING_LIMIT_CENTS = 80           # resting-mode buy limit (single side, favorite only)
RESTING_PLACE_MIN_CENTS = 60       # only place resting if favorite's ask is >= this
VELOCITY_THRESHOLD_USD = 10.0      # |v60| > $10 = "moving"
CUSHION_THRESHOLD_USD = 40.0       # cushion >= $40 = "big"
N_MOVING_BIG = 20
N_MOVING_SMALL = 5
N_FLAT_BIG = 5
N_FLAT_SMALL = 3                   # contrarian flip ct (spec says 2-3, picked 3)
CONTRARIAN_FLIP_SLIP_C = 3         # slippage above cheap-side ask
CONTRARIAN_FLIP_MAX_CENTS = 30     # don't pay >30c for the "cheap" side

# EARLY leg (Variant F contrarian: minutes 0-3)
EARLY_WINDOW_S = 180               # first 3 min of cycle
EARLY_TRIGGER_CENTS = 80
EARLY_CONTESTED_CENTS = 40         # other side must have traded >= 40c earlier
N_EARLY = 2
EARLY_SLIP_C = 3                   # slip above cheap-side ask
EARLY_MAX_CENTS = 30

# EARLIER-MODERATE leg (minutes 5-12): IOC buy the favorite when its ask
# is moderately strong AND BTC is on the favorite's side of the strike.
# Distinct from both the EARLY contrarian leg (minutes 0-3) and the LATE
# leg (minute 12+). Only fires if no EARLY/RESTING entry has been taken
# already. If it does not fire by minute 12, the LATE leg takes over.
EARLIER_MODERATE_MIN_S = 600       # window opens at close − 600s (= minute 5)
EARLIER_MODERATE_MIN_ASK = 60      # favorite-side ask floor
EARLIER_MODERATE_BPS_THRESHOLD = 10.0  # |btc − strike| / strike * 1e4 (basis pts)
EARLIER_MODERATE_CONTRACTS = 20
EARLIER_MODERATE_CAP_CENTS = 99    # IOC cap

# Adaptive IOC slippage by leader/favorite ask tier. Crossing 0..+5c above
# the displayed ask lets the IOC sweep thin books instead of dying at the
# top-of-book quote. Caller still clamps the resulting limit at the leg cap.
LATE_LEADER_SLIPPAGE_C = {
    "LEADER_80PLUS": 2,    # >=80c
    "LEADER_65_79":  3,    # 65-79c
    "LEADER_55_64":  5,    # 55-64c (and <55c, though late skips that)
}


def adaptive_ioc_slippage_cents(ask_cents: int) -> int:
    """Slippage (cents) to add above the ask for an IOC limit."""
    if ask_cents >= 80:
        return LATE_LEADER_SLIPPAGE_C["LEADER_80PLUS"]
    if ask_cents >= 65:
        return LATE_LEADER_SLIPPAGE_C["LEADER_65_79"]
    return LATE_LEADER_SLIPPAGE_C["LEADER_55_64"]


# Common
DISCOVERY_INTERVAL_S = 30.0
STALE_BOOK_TIMEOUT_MS = 5_000
SETTLE_POLL_INTERVAL_S = 15.0
BTC_POLL_INTERVAL_S = 2.0
BTC_BUFFER_WINDOW_S = 320          # keep ~5min of BTC prices (was 75, extended for RV5m calc)
BTC_VELOCITY_LOOKBACK_S = 60       # compute v60 across ~60s

# ── Smart-V5 enhancements (additive; gated by CLI flags) ─────────────────
# Defaults derived from full backtest verification in _v5_verify_and_explore.out
# (305 markets / 309 entries) cross-validated against live trade analysis
# (38 settles in live_v5_unified_trades.jsonl). See _LIVE_INSIGHTS_2026_05_16.md.
#
# Backtest-best single rule: RV-regime FLIP per leg at optimal cutoffs.
#   EM p80 (RV >= 0.039)  → flip to underdog: +$57.12 vs baseline -$14.08
#   LATE p50 (RV >= 0.023) → flip to underdog: +$46.85 vs baseline -$35.85
# Combined projected swing: ~+$104 net on 309 backtest entries.
RV_REGIME_CUTOFF_EM_DEFAULT = 0.039   # backtest p80 of RV5m at EM decision time
RV_REGIME_CUTOFF_LATE_DEFAULT = 0.023 # backtest p50 of RV5m at LATE decision time
RV_HIGH_CONF_CUTOFF = 0.025           # below this, calm regime — eligible for upsize
EM_HIGH_CONF_GAP_BPS = 18.0           # gap_bps above which EM is high-confidence
EM_BIG_CONTRACTS = 30                 # high-conviction EM size (vs default 20)
EM_REGIME_FLIP_CONTRACTS = 5          # flipped-side bet on high-vol EM (small probe)
LATE_REGIME_FLIP_CONTRACTS = 5        # flipped-side bet on high-vol LATE
# Velocity-alignment filter for LATE leg (backtest: AGAINST -$22.41, FLAT -$15.25,
# ALIGNED +$1.81 — sense check: when BTC is moving the wrong way for the leader,
# the leader is more likely to lose). EM is already aligned via direction_agrees.
LATE_VEL_ALIGN_FLAT_THRESHOLD_USD = 1.0  # |v60| below this is treated as FLAT

# ── T-30 SNIPER (additive; --enable-t30-sniper) ──
# DATA-VALIDATED EDGE (2026-05-17 analysis _fast_edge_scan2.out):
#   "at T-30s, if favorite_bid >= 85c, buy favorite at favorite_ask"
#   TRAIN: 29/29 = 100% WR (29 markets, first 70% of 331)
#   TEST:  15/15 = 100% WR (15 markets, last 30% — pure out-of-sample)
#   COMBINED: 44/44 = 100% WR, net +$5.69 at 5ct, +$11.67 at 10ct
#   EV/trade: +$0.27 at 10ct in test set
#   Trade pace: ~14/day expected
# Rationale: settlement window for Kalshi BTC 15m is the last 60s VWAP. At T-30,
# half the settlement has already happened. If the favorite is bid up to 85c,
# the market has converged on the outcome with near-certainty. Buy and hold.
T30_SNIPER_WINDOW_OPEN_S = 40       # window starts at T-40s
T30_SNIPER_WINDOW_CLOSE_S = 20      # window ends at T-20s (10s tolerance per side of T-30)
T30_SNIPER_FAV_BID_MIN = 85         # favorite-side bid floor; backtest 100% WR at this threshold
T30_SNIPER_CONTRACTS = 5            # 2026-05-17 v2: lowered 10 -> 5 per literature review.
                                    # Quarter-Kelly at Bayesian-skeptical 88% WR (overfit
                                    # haircut + Wilson lower bound). Will scale to 10ct after
                                    # N=10 live with <=1 loss, 20ct after N=25 with <=2.
                                    # See STRATEGY_PARTICIPATION.md sec 11.
T30_SNIPER_CAP_CENTS = 99           # IOC limit cap
T30_SNIPER_SLIP_C = 2               # +2c above ask (LEADER_80PLUS tier slippage)

# WebSocket book freshness — fall back to REST if WS book older than this.
WS_BOOK_FRESH_S = 2.0
# Active-window poll cadence when WS is connected and a ticker is in
# late window (book reads via WS are free, so poll tighter).
ACTIVE_WS_SLEEP_S = 0.25

# Safety caps
DAILY_LOSS_CAP_CENTS = 4000        # $40/day stop (raised from $25 to accommodate 20ct MOVING_BIG)
MIN_BALANCE_CENTS = 500            # $5
KALSHI_CREDS_PATH = Path(r"D:\Trading\btc-bias-engine\credentials\kalshi.env")

BITSTAMP_TICKER_URL = "https://www.bitstamp.net/api/v2/ticker/btcusd/"


# ── Credentials + log replay helpers ──────────────────────────────────────


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


def kalshi_taker_fee_cents(price_cents: int, count: int) -> int:
    """Kalshi taker fee: ceil(0.07 * n * P * (1-P) * 100) / 100. Returns cents."""
    p = price_cents / 100.0
    fee_dollars = math.ceil(0.07 * count * p * (1.0 - p) * 100.0) / 100.0
    return int(round(fee_dollars * 100))


def replay_log_state(
    log_path: Path,
) -> tuple[int, set[str], dict[str, dict[str, Any]]]:
    """Rebuild state from JSONL: daily loss (today UTC), attempted tickers,
    open (unsettled) positions."""
    if not log_path.exists():
        return 0, set(), {}
    today_floor_ms = _utc_today_floor_ms()
    today_end_ms = today_floor_ms + 24 * 60 * 60 * 1000

    total_net = 0
    attempted: set[str] = set()
    fills: dict[str, dict[str, Any]] = {}
    settled: set[str] = set()

    try:
        with log_path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = rec.get("kind")
                tkr = rec.get("ticker")
                if kind in (
                    "order_attempt", "early_fill", "late_fill",
                    "earlier_moderate_fill",
                    "order_rejected", "order_error", "early_no_fill",
                    "late_no_fill", "earlier_moderate_no_fill",
                    "early_trigger", "late_trigger",
                    "earlier_moderate_trigger",
                    "resting_place", "resting_fill", "resting_expire",
                    "resting_skip", "resting_place_error",
                ):
                    if tkr:
                        attempted.add(tkr)
                if (
                    kind in ("early_fill", "late_fill", "resting_fill",
                             "earlier_moderate_fill")
                    and tkr
                ):
                    if kind == "early_fill":
                        leg_default = "EARLY"
                    elif kind == "earlier_moderate_fill":
                        leg_default = "EARLIER_MODERATE"
                    else:
                        leg_default = "LATE"
                    fills[tkr] = {
                        "ticker": tkr,
                        "leg": rec.get("leg") or leg_default,
                        "tier": rec.get("tier"),
                        "side": rec.get("side"),
                        "contracts": int(rec.get("contracts", 0) or 0),
                        "entry_price_cents": int(rec.get("entry_price_cents", 0) or 0),
                        "entry_fee_cents": int(rec.get("entry_fee_cents", 0) or 0),
                        "close_ts_ms": int(rec.get("close_ts_ms", 0) or 0),
                        "order_id": rec.get("order_id"),
                    }
                if kind == "settle" and tkr:
                    settled.add(tkr)
                    close_ts_ms = int(rec.get("close_ts_ms", 0) or 0)
                    if today_floor_ms <= close_ts_ms < today_end_ms:
                        total_net += int(rec.get("net_cents", 0) or 0)
    except OSError:
        return 0, set(), {}

    open_positions = {t: v for t, v in fills.items() if t not in settled}
    return max(0, -total_net), attempted, open_positions


# ── BTC price source ──────────────────────────────────────────────────────


class BTCPriceBuffer:
    """Rolling (ts_ms, price) deque covering the last ~75s. Refreshed by
    a background poll task hitting Bitstamp REST."""

    def __init__(self, window_s: float = BTC_BUFFER_WINDOW_S):
        self.window_ms = int(window_s * 1000)
        self._buf: collections.deque[tuple[int, float]] = collections.deque(maxlen=200)
        self.poll_errors = 0
        self.last_poll_ok_ms = 0

    def add(self, ts_ms: int, price: float) -> None:
        self._buf.append((ts_ms, price))
        cutoff = ts_ms - self.window_ms
        while self._buf and self._buf[0][0] < cutoff:
            self._buf.popleft()

    def latest(self) -> tuple[int, float] | None:
        return self._buf[-1] if self._buf else None

    def velocity_60s(self, now_ms: int) -> float | None:
        """Return (latest_price - price ~60s ago). None if buffer doesn't
        cover ~60s yet. Allows a 10s slack on either side of the 60s mark."""
        if len(self._buf) < 2:
            return None
        latest_ts, latest_price = self._buf[-1]
        target_ts = latest_ts - BTC_VELOCITY_LOOKBACK_S * 1000
        # Find buffered point closest to target_ts.
        best: tuple[int, float] | None = None
        best_diff = float("inf")
        for ts, price in self._buf:
            diff = abs(ts - target_ts)
            if diff < best_diff:
                best_diff = diff
                best = (ts, price)
        if best is None:
            return None
        # Require the lookback point to be at least 30s old (otherwise
        # buffer hasn't accumulated enough history yet).
        age_s = (latest_ts - best[0]) / 1000.0
        if age_s < 30.0:
            return None
        return latest_price - best[1]

    def realized_vol_5m(self, now_ms: int, window_s: int = 300) -> float | None:
        """5-min realized volatility (% per √min, ~annualized basis but
        reported in raw %). Backtest "RV5m" feature from _v5_combinations.py.
        Returns None if buffer hasn't accumulated enough history yet."""
        if len(self._buf) < 30:
            return None
        latest_ts, _ = self._buf[-1]
        cutoff_ts = latest_ts - window_s * 1000
        # Only use prices within the window.
        prices = [p for ts, p in self._buf if ts >= cutoff_ts and p > 0]
        if len(prices) < 30:
            return None
        # Require the oldest sample to be at least ~half the window old.
        oldest_ts = next((ts for ts, p in self._buf if ts >= cutoff_ts and p > 0), latest_ts)
        if (latest_ts - oldest_ts) / 1000.0 < window_s * 0.5:
            return None
        rets: list[float] = []
        for i in range(1, len(prices)):
            if prices[i-1] > 0 and prices[i] > 0:
                rets.append(math.log(prices[i] / prices[i-1]))
        if len(rets) < 20:
            return None
        # pstdev * sqrt(60) * 100 — matches backtest formula
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        return math.sqrt(var) * math.sqrt(60) * 100


async def btc_poller(
    buf: BTCPriceBuffer, session: aiohttp.ClientSession, log_fp, stop: dict,
) -> None:
    """Background loop: poll Bitstamp every BTC_POLL_INTERVAL_S, append to buffer."""
    consecutive_errors = 0
    while not stop["flag"]:
        try:
            async with session.get(BITSTAMP_TICKER_URL, timeout=aiohttp.ClientTimeout(total=5)) as r:
                r.raise_for_status()
                data = await r.json()
            price = float(data.get("last") or 0.0)
            if price > 0:
                buf.add(int(time.time() * 1000), price)
                buf.last_poll_ok_ms = int(time.time() * 1000)
                consecutive_errors = 0
            else:
                consecutive_errors += 1
        except Exception as e:  # noqa: BLE001
            consecutive_errors += 1
            buf.poll_errors += 1
            if consecutive_errors == 1 or consecutive_errors % 10 == 0:
                log_fp.write(json.dumps({
                    "kind": "btc_poll_error",
                    "ts_ms": int(time.time() * 1000),
                    "error": repr(e),
                    "consecutive": consecutive_errors,
                }) + "\n")
                log_fp.flush()
        await asyncio.sleep(BTC_POLL_INTERVAL_S)


# ── Kalshi helpers ────────────────────────────────────────────────────────


async def discover_markets(client: KalshiClient) -> list[KalshiContract]:
    try:
        return await client.find_btc_contracts(min_minutes_remaining=0.0)
    except Exception:  # noqa: BLE001
        return []


async def fetch_book(client: KalshiClient, ticker: str) -> KalshiOrderBook | None:
    try:
        return await client.get_orderbook(ticker)
    except Exception:  # noqa: BLE001
        return None


async def fetch_contract(client: KalshiClient, ticker: str) -> KalshiContract | None:
    try:
        return await client.get_contract(ticker)
    except Exception:  # noqa: BLE001
        return None


async def fetch_floor_strike(client: KalshiClient, ticker: str) -> float | None:
    """Hit /markets/{ticker} raw and pull floor_strike. KalshiContract
    dataclass doesn't expose it, and CLAUDE.md says don't modify
    kalshi_client.py — so we reach for the underscore-prefixed _request
    method directly. Returns None on error or missing field."""
    try:
        data = await client._request("GET", f"/markets/{ticker}")
    except Exception:  # noqa: BLE001
        return None
    market = (data or {}).get("market") or {}
    strike = market.get("floor_strike")
    if strike is None:
        return None
    try:
        return float(strike)
    except (TypeError, ValueError):
        return None


# ── IOC order helper ──────────────────────────────────────────────────────


async def place_ioc(
    client: KalshiClient, ticker: str, side: str, count: int, limit_cents: int,
    log_fp, base_rec: dict, dry_run: bool,
) -> tuple[int, int, str | None]:
    """Place an IOC buy. Returns (filled_count, avg_price_cents, order_id).
    Returns (0, 0, None) on error or no-fill. Logs order_attempt before send
    and order_rejected/order_error on failure."""
    log_fp.write(json.dumps({**base_rec, "kind": "order_attempt"}, default=str) + "\n")
    log_fp.flush()

    if dry_run:
        return count, limit_cents, "DRY-RUN"

    try:
        order = await client.place_order(
            ticker=ticker, side=side, count=count, price=limit_cents,
            order_type="limit", action="buy", time_in_force="immediate_or_cancel",
        )
    except KalshiAPIError as e:
        log_fp.write(json.dumps({
            **base_rec, "kind": "order_rejected",
            "status": e.status, "body": e.body, "path": e.path,
        }, default=str) + "\n")
        log_fp.flush()
        return 0, 0, None
    except Exception as e:  # noqa: BLE001
        log_fp.write(json.dumps({
            **base_rec, "kind": "order_error", "error": repr(e),
        }, default=str) + "\n")
        log_fp.flush()
        return 0, 0, None

    filled = int(order.filled_count or 0)
    avg = (
        int(order.average_price) if order.average_price is not None else limit_cents
    )
    return filled, avg, order.order_id


# ── Main loop ─────────────────────────────────────────────────────────────


async def main_async() -> int:
    parser = argparse.ArgumentParser(
        prog="live-v5-unified",
        description="Unified late-favorite (sized) + early-contrarian LIVE Kalshi trader.",
    )
    parser.add_argument("--decision-log", required=True, type=Path)
    parser.add_argument("--poll-interval-s", type=float, default=1.5)
    parser.add_argument("--status-every-s", type=float, default=30.0)
    parser.add_argument("--dry-run", action="store_true",
                        help="Log decisions but don't call place_order.")
    parser.add_argument("--disable-early", action="store_true",
                        help="Disable the early-contrarian leg (Variant F, "
                        "minutes 0-3).")
    parser.add_argument("--disable-earlier-moderate", action="store_true",
                        help="Disable the earlier-moderate favorite leg "
                        "(minutes 5-12). Default: enabled.")
    parser.add_argument("--disable-flat-small-flip", action="store_true",
                        help="Disable the FLAT_SMALL contrarian flip "
                        "(falls back to no late entry in that tier).")
    parser.add_argument("--no-websocket", action="store_true",
                        help="Disable Kalshi WebSocket orderbook feed; "
                        "use REST polling only. Default: WS enabled.")
    parser.add_argument("--no-resting", action="store_true",
                        help="Disable single-side resting-limit late-window "
                        "entries (favorite@80c when fav_ask in [60,79]); "
                        "fall back to legacy IOC polling that fires when an "
                        "ask hits 80c. Default: resting mode enabled.")
    # ── Smart-V5 additive enhancements (off by default; opt-in) ──
    # See _LIVE_INSIGHTS_2026_05_16.md + _v5_verify_and_explore.out for the
    # backtest evidence behind these defaults.
    parser.add_argument("--enable-rv-regime-gate", action="store_true",
                        help="Apply 5-min realized-vol regime gate to EM and "
                        "LATE entries. Mode controlled by --rv-regime-mode. "
                        "Backtest: +$104 net swing on 309 entries vs baseline.")
    parser.add_argument("--rv-regime-mode", choices=("skip", "flip"), default="flip",
                        help="On high-RV regime: 'skip' the entry (defensive) "
                        "or 'flip' to underdog at (100-leader_ask) with "
                        "EM_REGIME_FLIP_CONTRACTS / LATE_REGIME_FLIP_CONTRACTS "
                        "(5ct each). 'flip' has higher EV in backtest.")
    parser.add_argument("--rv-cutoff-em", type=float,
                        default=RV_REGIME_CUTOFF_EM_DEFAULT,
                        help=f"5-min realized-vol cutoff for EM leg "
                        f"(default {RV_REGIME_CUTOFF_EM_DEFAULT}, "
                        "backtest p80; flip EM trades above this RV).")
    parser.add_argument("--rv-cutoff-late", type=float,
                        default=RV_REGIME_CUTOFF_LATE_DEFAULT,
                        help=f"5-min realized-vol cutoff for LATE leg "
                        f"(default {RV_REGIME_CUTOFF_LATE_DEFAULT}, "
                        "backtest p50; flip LATE trades above this RV).")
    parser.add_argument("--enable-em-upsize", action="store_true",
                        help="Upsize EM from 20 -> EM_BIG_CONTRACTS (30) when "
                        "gap_bps>=EM_HIGH_CONF_GAP_BPS (18) AND entry>=90c AND "
                        "RV5m<RV_HIGH_CONF_CUTOFF (0.025).")
    parser.add_argument("--enable-late-vel-align", action="store_true",
                        help="Skip LATE entries when BTC velocity is AGAINST "
                        "(opposite direction of leader) or FLAT (|v60|<$1). "
                        "Backtest: ALIGNED +$1.81 / AGAINST -$22.41 / FLAT -$15.25.")
    parser.add_argument("--enable-t30-sniper", action="store_true",
                        help="T-30 SNIPER: at T-30s window, if favorite_bid >= 85c, "
                        "IOC buy favorite. Backtest 100pct WR on 44/44 entries. "
                        "Mutex with EM/LATE.")
    parser.add_argument("--disable-late", action="store_true",
                        help="Disable the LATE leg (leader_at_min12 at T-180s). "
                        "Backtest: structurally -EV in test set. Recommended "
                        "to combine with --enable-t30-sniper which replaces it.")
    args = parser.parse_args()

    args.decision_log.parent.mkdir(parents=True, exist_ok=True)
    log_fp = args.decision_log.open("a", encoding="utf-8")

    key_id, pem = load_kalshi_creds()

    stop = {"flag": False}

    def _handle(signum, frame):  # noqa: ANN001
        stop["flag"] = True
    signal.signal(signal.SIGINT, _handle)
    try:
        signal.signal(signal.SIGTERM, _handle)
    except (AttributeError, ValueError):
        pass

    daily_loss_cents, attempted_tickers, open_positions = replay_log_state(
        args.decision_log
    )
    daily_loss_day_floor = _utc_today_floor_ms()
    halt_reason: str | None = None
    if daily_loss_cents >= DAILY_LOSS_CAP_CENTS:
        halt_reason = "DAILY-LOSS-CAP"

    # Per-market state.
    # status: watching → polling → entered → settled (or skipped_*)
    # leg_taken: None | "EARLY" | "LATE"
    # early_state: watching → contested → entered/skipped/done
    tracked: dict[str, dict[str, Any]] = {}
    for tkr, p in open_positions.items():
        tracked[tkr] = {
            "close_ts_ms": int(p.get("close_ts_ms", 0) or 0),
            "open_ts_ms": int(p.get("close_ts_ms", 0) or 0) - 900_000,
            "status": "entered",
            "leg_taken": p.get("leg"),
            "side": p.get("side"),
            "contracts": int(p.get("contracts", 0) or 0),
            "entry_price_cents": int(p.get("entry_price_cents", 0) or 0),
            "entry_fee_cents": int(p.get("entry_fee_cents", 0) or 0),
            "order_id": p.get("order_id"),
            "tier": p.get("tier"),
            "strike": 0.0,
            "early_state": "done",
            "early_max_yes_cents": 0,
            "early_max_no_cents": 0,
            "earlier_moderate_state": "done",
        }

    btc_buf = BTCPriceBuffer()

    early_triggers = 0
    early_fills = 0
    late_triggers = 0
    late_fills = 0
    settles = 0
    last_discover_t = 0.0
    last_settle_poll_t = 0.0
    last_status_t = time.time()

    startup_rec = {
        "kind": "startup",
        "ts_ms": int(time.time() * 1000),
        "variant": "live_v5_unified_v2_leader_at_min12_poc_adaptive_slip",
        "late_entry_mode": "leader_at_min12",
        "late_poc_sizing": {
            "leader_80plus_ct": 5,
            "leader_65_79_ct": 5,
            "leader_55_64_ct": 2,
            "leader_below_55": "skip",
        },
        "ioc_slippage_cents": LATE_LEADER_SLIPPAGE_C,
        "dry_run": args.dry_run,
        "disable_early": args.disable_early,
        "disable_earlier_moderate": args.disable_earlier_moderate,
        "disable_flat_small_flip": args.disable_flat_small_flip,
        "late_window_s": LATE_WINDOW_S,
        "late_trigger_cents_legacy_unused": LATE_TRIGGER_CENTS,
        "late_cap_cents": LATE_CAP_CENTS,
        "velocity_threshold_usd_logging_only": VELOCITY_THRESHOLD_USD,
        "cushion_threshold_usd_logging_only": CUSHION_THRESHOLD_USD,
        "n_moving_big_legacy_unused": N_MOVING_BIG,
        "n_moving_small_legacy_unused": N_MOVING_SMALL,
        "n_flat_big_legacy_unused": N_FLAT_BIG,
        "n_flat_small_legacy_unused": N_FLAT_SMALL,
        "early_window_s": EARLY_WINDOW_S,
        "early_trigger_cents": EARLY_TRIGGER_CENTS,
        "early_contested_cents": EARLY_CONTESTED_CENTS,
        "n_early": N_EARLY,
        "earlier_moderate_min_s": EARLIER_MODERATE_MIN_S,
        "earlier_moderate_min_ask": EARLIER_MODERATE_MIN_ASK,
        "earlier_moderate_bps_threshold": EARLIER_MODERATE_BPS_THRESHOLD,
        "earlier_moderate_contracts": EARLIER_MODERATE_CONTRACTS,
        "earlier_moderate_cap_cents": EARLIER_MODERATE_CAP_CENTS,
        "daily_loss_cap_cents": DAILY_LOSS_CAP_CENTS,
        "min_balance_cents": MIN_BALANCE_CENTS,
        "poll_interval_s": args.poll_interval_s,
        "daily_loss_cents_at_start": daily_loss_cents,
        "attempted_at_start": len(attempted_tickers),
        "open_positions_at_start": len(open_positions),
        "halt_reason_at_start": halt_reason,
        "websocket_enabled": (not args.no_websocket),
        "ws_book_fresh_s": WS_BOOK_FRESH_S,
        "active_ws_sleep_s": ACTIVE_WS_SLEEP_S,
        "resting_mode": (not args.no_resting),
        "resting_mode_variant": "single_side_favorite_v1",
        "resting_limit_cents": RESTING_LIMIT_CENTS,
        "resting_place_min_cents": RESTING_PLACE_MIN_CENTS,
        # Smart-V5 additive features (per CLI flags)
        "smart_v5_enable_rv_regime_gate": args.enable_rv_regime_gate,
        "smart_v5_rv_regime_mode": args.rv_regime_mode,
        "smart_v5_rv_cutoff_em": args.rv_cutoff_em,
        "smart_v5_rv_cutoff_late": args.rv_cutoff_late,
        "smart_v5_enable_em_upsize": args.enable_em_upsize,
        "smart_v5_em_big_contracts": EM_BIG_CONTRACTS,
        "smart_v5_em_high_conf_gap_bps": EM_HIGH_CONF_GAP_BPS,
        "smart_v5_rv_high_conf_cutoff": RV_HIGH_CONF_CUTOFF,
        "smart_v5_em_regime_flip_contracts": EM_REGIME_FLIP_CONTRACTS,
        "smart_v5_late_regime_flip_contracts": LATE_REGIME_FLIP_CONTRACTS,
        "smart_v5_enable_late_vel_align": args.enable_late_vel_align,
        "smart_v5_late_vel_flat_threshold_usd": LATE_VEL_ALIGN_FLAT_THRESHOLD_USD,
        "btc_buffer_window_s": BTC_BUFFER_WINDOW_S,
        # T-30 SNIPER (data-validated edge from 331-market backtest 100% WR)
        "t30_sniper_enabled": args.enable_t30_sniper,
        "t30_sniper_window_open_s": T30_SNIPER_WINDOW_OPEN_S,
        "t30_sniper_window_close_s": T30_SNIPER_WINDOW_CLOSE_S,
        "t30_sniper_fav_bid_min": T30_SNIPER_FAV_BID_MIN,
        "t30_sniper_contracts": T30_SNIPER_CONTRACTS,
        "t30_sniper_slip_c": T30_SNIPER_SLIP_C,
        "disable_late": args.disable_late,
    }
    log_fp.write(json.dumps(startup_rec, default=str) + "\n")
    log_fp.flush()
    print(
        f"[live-unified] starting dry_run={args.dry_run} "
        f"resting_mode={not args.no_resting} "
        f"daily_loss={daily_loss_cents}c attempted={len(attempted_tickers)} "
        f"open={len(open_positions)} halt={halt_reason} "
        f"disable_early={args.disable_early} "
        f"disable_earlier_moderate={args.disable_earlier_moderate} "
        f"disable_flip={args.disable_flat_small_flip} "
        f"smart_v5_regime={args.enable_rv_regime_gate}/"
        f"{args.rv_regime_mode}(em={args.rv_cutoff_em},late={args.rv_cutoff_late}) "
        f"em_upsize={args.enable_em_upsize} "
        f"late_vel_align={args.enable_late_vel_align} "
        f"T30_SNIPER={args.enable_t30_sniper} "
        f"disable_late={args.disable_late}",
        flush=True,
    )
    print(
        "[live-unified] LATE entry_mode=leader_at_min12 POC sizing: "
        "leader>=80c->5ct, leader 65-79c->5ct, leader 55-64c->2ct, "
        "leader<55c->skip (no 80c threshold; v60/cushion logged only)",
        flush=True,
    )
    print(
        "[live-unified] adaptive IOC slippage (late + earlier-moderate): "
        "ask>=80c +2c, 65-79c +3c, 55-64c +5c (clamped at leg cap 99c)",
        flush=True,
    )
    print(
        f"[live-unified] earlier_moderate: min_s={EARLIER_MODERATE_MIN_S} "
        f"min_ask={EARLIER_MODERATE_MIN_ASK}c "
        f"bps_threshold={EARLIER_MODERATE_BPS_THRESHOLD} "
        f"contracts={EARLIER_MODERATE_CONTRACTS} "
        f"cap={EARLIER_MODERATE_CAP_CENTS}c "
        f"enabled={not args.disable_earlier_moderate}",
        flush=True,
    )

    # WebSocket setup (graceful failure → REST-only mode).
    ws: KalshiWebSocket | None = None
    ws_task: asyncio.Task | None = None
    ws_subscribed_ticker: str | None = None
    ws_book_reads = 0
    ws_rest_fallbacks = 0
    if not args.no_websocket:
        try:
            ws = KalshiWebSocket(key_id=key_id, private_key_pem=pem, demo=False)
            ws_task = asyncio.create_task(ws.connect())
            log_fp.write(json.dumps({
                "kind": "ws_init",
                "ts_ms": int(time.time() * 1000),
                "ok": True,
            }, default=str) + "\n")
            log_fp.flush()
            print("[live-unified] WS task started (background)", flush=True)
        except Exception as e:  # noqa: BLE001
            log_fp.write(json.dumps({
                "kind": "ws_init",
                "ts_ms": int(time.time() * 1000),
                "ok": False, "error": repr(e),
            }, default=str) + "\n")
            log_fp.flush()
            print(f"[live-unified] WS init FAILED: {e} — REST-only mode", flush=True)
            ws = None
            ws_task = None

    async with aiohttp.ClientSession() as btc_session:
        btc_task = asyncio.create_task(btc_poller(btc_buf, btc_session, log_fp, stop))

        async with KalshiClient(key_id=key_id, private_key_pem=pem, demo=False) as client:
            try:
                while not stop["flag"]:
                    now_wall = time.time()
                    now_ms = int(now_wall * 1000)

                    # Daily reset.
                    today_floor = _utc_today_floor_ms()
                    if today_floor != daily_loss_day_floor:
                        daily_loss_day_floor = today_floor
                        daily_loss_cents = 0
                        if halt_reason == "DAILY-LOSS-CAP":
                            halt_reason = None
                            print(
                                "[live-unified] new UTC day — daily loss reset",
                                flush=True,
                            )

                    # ── Discovery ─────────────────────────────────────
                    if now_wall - last_discover_t >= DISCOVERY_INTERVAL_S:
                        last_discover_t = now_wall
                        markets = await discover_markets(client)
                        log_fp.write(json.dumps({
                            "kind": "discover",
                            "ts_ms": now_ms,
                            "n_markets": len(markets),
                            "tickers": [m.ticker for m in markets],
                        }, default=str) + "\n")
                        log_fp.flush()
                        for m in markets:
                            if m.ticker in tracked:
                                continue
                            if m.ticker in attempted_tickers:
                                tracked[m.ticker] = {
                                    "close_ts_ms": m.expiry_ts,
                                    "open_ts_ms": m.expiry_ts - 900_000,
                                    "status": "skipped_attempted_prior",
                                    "leg_taken": None,
                                    "early_state": "done",
                                    "early_max_yes_cents": 0,
                                    "early_max_no_cents": 0,
                                    "earlier_moderate_state": "done",
                                    "strike": 0.0,
                                }
                                continue
                            strike = await fetch_floor_strike(client, m.ticker) or 0.0
                            tracked[m.ticker] = {
                                "close_ts_ms": m.expiry_ts,
                                "open_ts_ms": m.expiry_ts - 900_000,
                                "status": "watching",
                                "leg_taken": None,
                                "early_state": "watching",
                                "early_max_yes_cents": 0,
                                "early_max_no_cents": 0,
                                "earlier_moderate_state": "watching",
                                "strike": strike,
                            }

                    # ── Per-market processing ─────────────────────────
                    for ticker in list(tracked.keys()):
                        state = tracked[ticker]
                        if state["status"] not in ("watching", "polling"):
                            continue

                        close_ts_ms = int(state["close_ts_ms"])
                        open_ts_ms = int(state.get("open_ts_ms", close_ts_ms - 900_000))
                        secs_to_close = (close_ts_ms - now_ms) / 1000.0
                        secs_from_open = (now_ms - open_ts_ms) / 1000.0

                        if secs_to_close <= 0:
                            if state["status"] == "watching":
                                state["status"] = "skipped_closed"
                                log_fp.write(json.dumps({
                                    "kind": "poll_skip_closed",
                                    "ts_ms": now_ms,
                                    "ticker": ticker,
                                    "close_ts_ms": close_ts_ms,
                                }, default=str) + "\n")
                                log_fp.flush()
                            continue

                        in_early_window = (
                            not args.disable_early
                            and 0 <= secs_from_open <= EARLY_WINDOW_S
                            and state["leg_taken"] is None
                            and state.get("early_state") != "done"
                        )
                        in_earlier_moderate_window = (
                            not args.disable_earlier_moderate
                            and secs_to_close <= EARLIER_MODERATE_MIN_S
                            and secs_to_close > LATE_WINDOW_S
                            and state["leg_taken"] is None
                            and state.get("earlier_moderate_state") != "done"
                        )
                        in_late_window = (
                            secs_to_close <= LATE_WINDOW_S
                            and state["leg_taken"] is None
                        )

                        if (not in_early_window
                                and not in_earlier_moderate_window
                                and not in_late_window):
                            continue

                        if halt_reason is not None:
                            if state["status"] != "skipped_halt":
                                state["status"] = "skipped_halt"
                                log_fp.write(json.dumps({
                                    "kind": "poll_skip_halt",
                                    "ts_ms": now_ms,
                                    "ticker": ticker,
                                    "halt_reason": halt_reason,
                                }, default=str) + "\n")
                                log_fp.flush()
                            continue

                        state["status"] = "polling"

                        # ── WS subscription: keep exactly one active ticker.
                        # When this ticker first enters its active window,
                        # switch the WS subscription to it (unsubscribing
                        # from any prior ticker). Trading logic is unchanged.
                        if ws is not None and ws_subscribed_ticker != ticker:
                            try:
                                if ws_subscribed_ticker:
                                    await ws.unsubscribe(ws_subscribed_ticker)
                                await ws.subscribe(ticker)
                                prev = ws_subscribed_ticker
                                ws_subscribed_ticker = ticker
                                log_fp.write(json.dumps({
                                    "kind": "ws_subscribe",
                                    "ts_ms": now_ms, "ticker": ticker,
                                    "prev_ticker": prev,
                                }, default=str) + "\n")
                                log_fp.flush()
                            except Exception as e:  # noqa: BLE001
                                log_fp.write(json.dumps({
                                    "kind": "ws_subscribe_error",
                                    "ts_ms": now_ms, "ticker": ticker,
                                    "error": repr(e),
                                }, default=str) + "\n")
                                log_fp.flush()

                        # ── Book read: prefer WS local book when fresh,
                        # else fall back to REST. yes_ask/no_ask/yes_bid/
                        # no_bid semantics are identical on both paths.
                        yes_ask = no_ask = yes_bid = no_bid = 0
                        book_source: str = "rest"
                        ws_book_ok = False
                        if ws is not None and ws_subscribed_ticker == ticker:
                            ws_book = ws.get_book(ticker)
                            if ws_book.is_ready and ws_book.is_fresh(WS_BOOK_FRESH_S):
                                yes_ask = ws_book.best_yes_ask
                                no_ask = ws_book.best_no_ask
                                yes_bid = ws_book.best_yes_bid
                                no_bid = ws_book.best_no_bid
                                if yes_bid or no_bid or yes_ask or no_ask:
                                    book_source = "ws"
                                    ws_book_ok = True
                                    ws_book_reads += 1

                        if not ws_book_ok:
                            book = await fetch_book(client, ticker)
                            if book is None or (not book.yes_bids and not book.no_bids):
                                continue
                            if book.book_age_ms > STALE_BOOK_TIMEOUT_MS:
                                continue
                            yes_ask = book.best_yes_ask
                            no_ask = book.best_no_ask
                            yes_bid = book.best_yes_bid
                            no_bid = book.best_no_bid
                            book_source = "rest_fallback" if ws is not None else "rest"
                            if ws is not None:
                                ws_rest_fallbacks += 1

                        # Trade-price proxies: best bid = highest someone will
                        # pay = "traded recently at" approximation. Use this
                        # to update contested tracker (not the ask, which can
                        # be a phantom 99c offer).
                        if yes_bid > state["early_max_yes_cents"]:
                            state["early_max_yes_cents"] = yes_bid
                        if no_bid > state["early_max_no_cents"]:
                            state["early_max_no_cents"] = no_bid

                        # ── EARLY leg ─────────────────────────────────
                        if in_early_window:
                            yes_trig = yes_ask <= 99 and yes_ask >= EARLY_TRIGGER_CENTS
                            no_trig = no_ask <= 99 and no_ask >= EARLY_TRIGGER_CENTS

                            # contested status (other side must have hit >= 40c earlier)
                            if (state["early_max_yes_cents"] >= EARLY_CONTESTED_CENTS
                                    or state["early_max_no_cents"] >= EARLY_CONTESTED_CENTS):
                                if state["early_state"] == "watching":
                                    state["early_state"] = "contested"
                                    log_fp.write(json.dumps({
                                        "kind": "early_contested",
                                        "ts_ms": now_ms, "ticker": ticker,
                                        "max_yes_cents": state["early_max_yes_cents"],
                                        "max_no_cents": state["early_max_no_cents"],
                                    }, default=str) + "\n")
                                    log_fp.flush()

                            if state["early_state"] == "contested" and (yes_trig or no_trig):
                                # Buy the OTHER (cheap) side. Determine favorite first.
                                if yes_trig and no_trig:
                                    fav_side = "yes" if yes_ask >= no_ask else "no"
                                elif yes_trig:
                                    fav_side = "yes"
                                else:
                                    fav_side = "no"

                                # Contested check: the OTHER side must have
                                # actually traded >= 40c earlier in window.
                                if fav_side == "yes":
                                    other_max = state["early_max_no_cents"]
                                else:
                                    other_max = state["early_max_yes_cents"]
                                if other_max < EARLY_CONTESTED_CENTS:
                                    # not actually contested yet (favorite side
                                    # was the one that hit 40c). skip but stay
                                    # watching.
                                    continue

                                cheap_side = "no" if fav_side == "yes" else "yes"
                                cheap_ask = no_ask if cheap_side == "no" else yes_ask
                                limit_cents = min(EARLY_MAX_CENTS, cheap_ask + EARLY_SLIP_C)

                                early_triggers += 1
                                attempted_tickers.add(ticker)

                                # Balance check.
                                try:
                                    bal = await client.get_balance()
                                except Exception as e:  # noqa: BLE001
                                    log_fp.write(json.dumps({
                                        "kind": "balance_error",
                                        "ts_ms": now_ms, "ticker": ticker,
                                        "stage": "early", "error": repr(e),
                                    }, default=str) + "\n")
                                    log_fp.flush()
                                    state["early_state"] = "done"
                                    continue
                                if bal.balance < MIN_BALANCE_CENTS:
                                    halt_reason = "MIN-BALANCE"
                                    log_fp.write(json.dumps({
                                        "kind": "balance_halt",
                                        "ts_ms": now_ms, "ticker": ticker,
                                        "balance_cents": bal.balance, "stage": "early",
                                    }, default=str) + "\n")
                                    log_fp.flush()
                                    state["early_state"] = "done"
                                    continue

                                trigger_rec = {
                                    "kind": "early_trigger",
                                    "ts_ms": now_ms, "ticker": ticker,
                                    "fav_side": fav_side, "cheap_side": cheap_side,
                                    "yes_ask_cents": yes_ask, "no_ask_cents": no_ask,
                                    "cheap_ask_cents": cheap_ask,
                                    "limit_cents": limit_cents,
                                    "max_yes_cents_observed": state["early_max_yes_cents"],
                                    "max_no_cents_observed": state["early_max_no_cents"],
                                    "secs_from_open": round(secs_from_open, 1),
                                    "balance_cents": bal.balance,
                                    "book_source": book_source,
                                }
                                log_fp.write(json.dumps(trigger_rec, default=str) + "\n")
                                log_fp.flush()

                                base = {
                                    "ts_ms": now_ms, "ticker": ticker,
                                    "leg": "EARLY", "tier": "EARLY",
                                    "side": cheap_side, "contracts": N_EARLY,
                                    "limit_cents": limit_cents,
                                    "yes_ask_cents": yes_ask, "no_ask_cents": no_ask,
                                    "yes_bid_cents": yes_bid, "no_bid_cents": no_bid,
                                    "close_ts_ms": close_ts_ms,
                                    "secs_to_close": round(secs_to_close, 1),
                                    "secs_from_open": round(secs_from_open, 1),
                                    "balance_cents": bal.balance,
                                    "dry_run": args.dry_run,
                                }
                                filled, avg_cents, oid = await place_ioc(
                                    client, ticker, cheap_side, N_EARLY,
                                    limit_cents, log_fp, base, args.dry_run,
                                )
                                if filled == 0:
                                    log_fp.write(json.dumps({
                                        "kind": "early_no_fill",
                                        "ts_ms": int(time.time() * 1000),
                                        "ticker": ticker, "side": cheap_side,
                                        "limit_cents": limit_cents,
                                        "cheap_ask_cents": cheap_ask,
                                        "order_id": oid,
                                    }, default=str) + "\n")
                                    log_fp.flush()
                                    state["early_state"] = "done"
                                    print(
                                        f"[live-unified] EARLY NO-FILL {ticker} "
                                        f"{cheap_side} limit={limit_cents}c",
                                        flush=True,
                                    )
                                    continue

                                entry_fee = kalshi_taker_fee_cents(avg_cents, filled)
                                early_fills += 1
                                state.update({
                                    "status": "entered",
                                    "leg_taken": "EARLY",
                                    "side": cheap_side,
                                    "contracts": filled,
                                    "entry_price_cents": avg_cents,
                                    "entry_fee_cents": entry_fee,
                                    "order_id": oid,
                                    "tier": "EARLY",
                                    "early_state": "done",
                                })
                                log_fp.write(json.dumps({
                                    "kind": "early_fill",
                                    "ts_ms": int(time.time() * 1000),
                                    "ticker": ticker, "leg": "EARLY", "tier": "EARLY",
                                    "side": cheap_side, "contracts": filled,
                                    "entry_price_cents": avg_cents,
                                    "entry_fee_cents": entry_fee,
                                    "order_id": oid,
                                    "close_ts_ms": close_ts_ms,
                                    "secs_from_open": round(secs_from_open, 1),
                                    "yes_ask_cents": yes_ask, "no_ask_cents": no_ask,
                                    "max_yes_cents_observed": state["early_max_yes_cents"],
                                    "max_no_cents_observed": state["early_max_no_cents"],
                                }, default=str) + "\n")
                                log_fp.flush()
                                print(
                                    f"[live-unified] EARLY FILL {ticker} {cheap_side} "
                                    f"{filled}@{avg_cents}c (cheap_ask={cheap_ask}c)",
                                    flush=True,
                                )
                                continue  # done with this market this cycle

                            # Past EARLY window? Mark done so we stop checking.
                            if secs_from_open > EARLY_WINDOW_S and state["early_state"] != "done":
                                state["early_state"] = "done"

                        # ── EARLIER-MODERATE leg ──────────────────────
                        # Minutes 5-12. Fires if the favorite ask is >= 60c
                        # AND BTC is >= 10 bps from strike in the same
                        # direction as the favorite. IOC the favorite at
                        # current ask, 20ct. One entry per window; if not
                        # taken by minute 12, falls through to LATE.
                        if in_earlier_moderate_window:
                            # Determine favorite by ask price.
                            if yes_ask <= 0 and no_ask <= 0:
                                # No book — keep watching.
                                pass
                            else:
                                if yes_ask >= no_ask:
                                    fav_side_em = "yes"
                                    fav_ask_em = yes_ask
                                else:
                                    fav_side_em = "no"
                                    fav_ask_em = no_ask

                                # Need BTC + strike to evaluate the gap.
                                latest_em = btc_buf.latest()
                                btc_now_em = latest_em[1] if latest_em else None
                                strike_em = float(state.get("strike", 0.0) or 0.0)
                                if strike_em <= 0.0:
                                    s_em = await fetch_floor_strike(client, ticker)
                                    if s_em and s_em > 0:
                                        strike_em = s_em
                                        state["strike"] = s_em

                                if (btc_now_em is None
                                        or strike_em <= 0.0
                                        or fav_ask_em < EARLIER_MODERATE_MIN_ASK):
                                    # Either missing data or ask not yet at
                                    # threshold — keep watching.
                                    pass
                                else:
                                    gap_bps_em = (
                                        abs(btc_now_em - strike_em) / strike_em
                                        * 10_000.0
                                    )
                                    btc_fav_em = (
                                        "yes" if btc_now_em > strike_em else "no"
                                    )
                                    direction_agrees = (btc_fav_em == fav_side_em)
                                    gap_ok = gap_bps_em >= EARLIER_MODERATE_BPS_THRESHOLD

                                    # ── Smart-V5 EM gate ──
                                    # When the base EM signal fires, apply RV
                                    # regime + conviction-tier logic. By
                                    # default (no flags) behavior is unchanged.
                                    em_rv5 = btc_buf.realized_vol_5m(now_ms)
                                    em_skip_reason: str | None = None
                                    em_side = fav_side_em
                                    em_entry_ask = fav_ask_em
                                    em_contracts = EARLIER_MODERATE_CONTRACTS
                                    em_size_tag = "STANDARD_20"
                                    em_regime_flipped = False
                                    if direction_agrees and gap_ok:
                                        # Gate A: RV regime
                                        if (args.enable_rv_regime_gate
                                                and em_rv5 is not None
                                                and em_rv5 >= args.rv_cutoff_em):
                                            if args.rv_regime_mode == "skip":
                                                em_skip_reason = (
                                                    f"RV_REGIME_SKIP rv5={em_rv5:.4f}"
                                                    f" >= cutoff={args.rv_cutoff_em:.4f}"
                                                )
                                            else:  # flip
                                                other_side_em = (
                                                    "no" if fav_side_em == "yes" else "yes"
                                                )
                                                other_ask_em = (
                                                    no_ask if other_side_em == "no" else yes_ask
                                                )
                                                if 0 < other_ask_em < 100:
                                                    em_side = other_side_em
                                                    em_entry_ask = other_ask_em
                                                    em_contracts = EM_REGIME_FLIP_CONTRACTS
                                                    em_size_tag = "REGIME_FLIP_5"
                                                    em_regime_flipped = True
                                                else:
                                                    em_skip_reason = (
                                                        "RV_REGIME_FLIP_NO_UNDERDOG_LIQ "
                                                        f"rv5={em_rv5:.4f}"
                                                    )
                                        # Gate B: high-conviction upsize
                                        elif (args.enable_em_upsize
                                                and gap_bps_em >= EM_HIGH_CONF_GAP_BPS
                                                and fav_ask_em >= 90
                                                and em_rv5 is not None
                                                and em_rv5 < RV_HIGH_CONF_CUTOFF):
                                            em_contracts = EM_BIG_CONTRACTS
                                            em_size_tag = "HIGH_CONF_BIG_30"

                                    if (direction_agrees and gap_ok
                                            and em_skip_reason is not None):
                                        log_fp.write(json.dumps({
                                            "kind": "earlier_moderate_skip",
                                            "ts_ms": now_ms, "ticker": ticker,
                                            "reason": em_skip_reason,
                                            "fav_ask_cents": fav_ask_em,
                                            "fav_side": fav_side_em,
                                            "gap_bps": round(gap_bps_em, 3),
                                            "rv_5m": (round(em_rv5, 4)
                                                      if em_rv5 is not None else None),
                                            "rv_cutoff_em": args.rv_cutoff_em,
                                            "yes_ask_cents": yes_ask,
                                            "no_ask_cents": no_ask,
                                            "secs_to_close": round(secs_to_close, 1),
                                        }, default=str) + "\n")
                                        log_fp.flush()
                                        state["earlier_moderate_state"] = "done"
                                        attempted_tickers.add(ticker)
                                        print(
                                            f"[live-unified] EM SMART-SKIP "
                                            f"{ticker} {em_skip_reason}",
                                            flush=True,
                                        )
                                        continue

                                    if direction_agrees and gap_ok:
                                        # Signal fires — IOC at em_side/em_entry_ask/em_contracts.
                                        slippage_c_em = adaptive_ioc_slippage_cents(
                                            em_entry_ask,
                                        )
                                        limit_cents_em = min(
                                            EARLIER_MODERATE_CAP_CENTS,
                                            em_entry_ask + slippage_c_em,
                                        )
                                        attempted_tickers.add(ticker)

                                        # Balance check.
                                        try:
                                            bal = await client.get_balance()
                                        except Exception as e:  # noqa: BLE001
                                            log_fp.write(json.dumps({
                                                "kind": "balance_error",
                                                "ts_ms": now_ms, "ticker": ticker,
                                                "stage": "earlier_moderate",
                                                "error": repr(e),
                                            }, default=str) + "\n")
                                            log_fp.flush()
                                            state["earlier_moderate_state"] = "done"
                                            continue
                                        if bal.balance < MIN_BALANCE_CENTS:
                                            halt_reason = "MIN-BALANCE"
                                            log_fp.write(json.dumps({
                                                "kind": "balance_halt",
                                                "ts_ms": now_ms, "ticker": ticker,
                                                "balance_cents": bal.balance,
                                                "stage": "earlier_moderate",
                                            }, default=str) + "\n")
                                            log_fp.flush()
                                            state["earlier_moderate_state"] = "done"
                                            continue

                                        trig_em = {
                                            "kind": "earlier_moderate_trigger",
                                            "ts_ms": now_ms, "ticker": ticker,
                                            "fav_side": fav_side_em,
                                            "fav_ask_cents": fav_ask_em,
                                            "entry_side": em_side,
                                            "entry_ask_cents": em_entry_ask,
                                            "size_tag": em_size_tag,
                                            "regime_flipped": em_regime_flipped,
                                            "rv_5m": (round(em_rv5, 4)
                                                      if em_rv5 is not None else None),
                                            "yes_ask_cents": yes_ask,
                                            "no_ask_cents": no_ask,
                                            "yes_bid_cents": yes_bid,
                                            "no_bid_cents": no_bid,
                                            "btc_price": btc_now_em,
                                            "strike": strike_em,
                                            "gap_bps": round(gap_bps_em, 3),
                                            "btc_fav_side": btc_fav_em,
                                            "limit_cents": limit_cents_em,
                                            "slippage_cents": slippage_c_em,
                                            "contracts": em_contracts,
                                            "secs_to_close": round(secs_to_close, 1),
                                            "secs_from_open": round(secs_from_open, 1),
                                            "balance_cents": bal.balance,
                                            "book_source": book_source,
                                        }
                                        log_fp.write(
                                            json.dumps(trig_em, default=str) + "\n"
                                        )
                                        log_fp.flush()

                                        base_em = {
                                            "ts_ms": now_ms, "ticker": ticker,
                                            "leg": "EARLIER_MODERATE",
                                            "tier": "EARLIER_MODERATE",
                                            "side": em_side,
                                            "contracts": em_contracts,
                                            "size_tag": em_size_tag,
                                            "regime_flipped": em_regime_flipped,
                                            "rv_5m": (round(em_rv5, 4)
                                                      if em_rv5 is not None else None),
                                            "limit_cents": limit_cents_em,
                                            "slippage_cents": slippage_c_em,
                                            "fav_ask_cents": fav_ask_em,
                                            "fav_side": fav_side_em,
                                            "yes_ask_cents": yes_ask,
                                            "no_ask_cents": no_ask,
                                            "yes_bid_cents": yes_bid,
                                            "no_bid_cents": no_bid,
                                            "btc_price": btc_now_em,
                                            "strike": strike_em,
                                            "gap_bps": round(gap_bps_em, 3),
                                            "close_ts_ms": close_ts_ms,
                                            "secs_to_close": round(secs_to_close, 1),
                                            "secs_from_open": round(secs_from_open, 1),
                                            "balance_cents": bal.balance,
                                            "dry_run": args.dry_run,
                                        }
                                        f_em, avg_em, oid_em = await place_ioc(
                                            client, ticker, em_side,
                                            em_contracts,
                                            limit_cents_em, log_fp, base_em,
                                            args.dry_run,
                                        )
                                        if f_em == 0:
                                            log_fp.write(json.dumps({
                                                "kind": "earlier_moderate_no_fill",
                                                "ts_ms": int(time.time() * 1000),
                                                "ticker": ticker,
                                                "side": em_side,
                                                "size_tag": em_size_tag,
                                                "regime_flipped": em_regime_flipped,
                                                "limit_cents": limit_cents_em,
                                                "slippage_cents": slippage_c_em,
                                                "entry_ask_cents": em_entry_ask,
                                                "fav_ask_cents": fav_ask_em,
                                                "order_id": oid_em,
                                            }, default=str) + "\n")
                                            log_fp.flush()
                                            state["earlier_moderate_state"] = "done"
                                            print(
                                                f"[live-unified] EARLIER-MOD NO-FILL "
                                                f"{ticker} {em_side} "
                                                f"({em_size_tag}) "
                                                f"entry_ask={em_entry_ask}c "
                                                f"slip=+{slippage_c_em}c "
                                                f"limit={limit_cents_em}c "
                                                f"gap={gap_bps_em:.2f}bps",
                                                flush=True,
                                            )
                                            continue

                                        entry_fee_em = kalshi_taker_fee_cents(
                                            avg_em, f_em,
                                        )
                                        state.update({
                                            "status": "entered",
                                            "leg_taken": "EARLIER_MODERATE",
                                            "side": em_side,
                                            "contracts": f_em,
                                            "entry_price_cents": avg_em,
                                            "entry_fee_cents": entry_fee_em,
                                            "order_id": oid_em,
                                            "tier": "EARLIER_MODERATE",
                                            "size_tag": em_size_tag,
                                            "regime_flipped": em_regime_flipped,
                                            "earlier_moderate_state": "done",
                                        })
                                        log_fp.write(json.dumps({
                                            "kind": "earlier_moderate_fill",
                                            "ts_ms": int(time.time() * 1000),
                                            "ticker": ticker,
                                            "leg": "EARLIER_MODERATE",
                                            "tier": "EARLIER_MODERATE",
                                            "side": em_side,
                                            "contracts": f_em,
                                            "size_tag": em_size_tag,
                                            "regime_flipped": em_regime_flipped,
                                            "rv_5m": (round(em_rv5, 4)
                                                      if em_rv5 is not None else None),
                                            "entry_price_cents": avg_em,
                                            "entry_fee_cents": entry_fee_em,
                                            "order_id": oid_em,
                                            "limit_cents": limit_cents_em,
                                            "slippage_cents": slippage_c_em,
                                            "fav_ask_cents": fav_ask_em,
                                            "fav_side": fav_side_em,
                                            "close_ts_ms": close_ts_ms,
                                            "secs_to_close": round(secs_to_close, 1),
                                            "secs_from_open": round(secs_from_open, 1),
                                            "yes_ask_cents": yes_ask,
                                            "no_ask_cents": no_ask,
                                            "btc_price": btc_now_em,
                                            "strike": strike_em,
                                            "gap_bps": round(gap_bps_em, 3),
                                        }, default=str) + "\n")
                                        log_fp.flush()
                                        print(
                                            f"[live-unified] EARLIER-MOD FILL "
                                            f"{ticker} {fav_side_em} "
                                            f"{f_em}@{avg_em}c "
                                            f"fav_ask={fav_ask_em}c "
                                            f"slip=+{slippage_c_em}c "
                                            f"limit={limit_cents_em}c "
                                            f"gap={gap_bps_em:.2f}bps "
                                            f"btc=${btc_now_em:.0f} "
                                            f"strike=${strike_em:.0f} "
                                            f"secs_left={secs_to_close:.0f}",
                                            flush=True,
                                        )
                                        continue  # done with this market this cycle

                        # Past earlier-moderate window without firing? Log + mark done.
                        if (not args.disable_earlier_moderate
                                and secs_to_close <= LATE_WINDOW_S
                                and state.get("earlier_moderate_state") == "watching"
                                and state["leg_taken"] is None):
                            log_fp.write(json.dumps({
                                "kind": "earlier_moderate_skip",
                                "ts_ms": now_ms, "ticker": ticker,
                                "reason": "WINDOW_PASSED_NO_SIGNAL",
                                "yes_ask_cents": yes_ask,
                                "no_ask_cents": no_ask,
                                "secs_to_close": round(secs_to_close, 1),
                            }, default=str) + "\n")
                            log_fp.flush()
                            state["earlier_moderate_state"] = "done"

                        # ── LATE leg ──────────────────────────────────
                        if not in_late_window:
                            continue

                        # ── T-30 SNIPER LEG (additive; --enable-t30-sniper) ──
                        # DATA-VALIDATED: 100% WR on 44/44 train+test entries (331-market
                        # backtest, _fast_edge_scan2.out 2026-05-17).
                        # At T-30s, with half the 60s settlement VWAP window already
                        # elapsed, the favorite-bid >= 85c market has converged on the
                        # outcome. Buy and hold.
                        in_t30_window = (
                            args.enable_t30_sniper
                            and T30_SNIPER_WINDOW_CLOSE_S <= secs_to_close <= T30_SNIPER_WINDOW_OPEN_S
                            and state["leg_taken"] is None
                            and state.get("t30_sniper_state") != "done"
                        )
                        if in_t30_window:
                            # Determine favorite by BID (most recent decisive trade).
                            if yes_bid >= no_bid:
                                fav_side_t30 = "yes"
                                fav_bid_t30 = yes_bid
                                fav_ask_t30 = yes_ask
                            else:
                                fav_side_t30 = "no"
                                fav_bid_t30 = no_bid
                                fav_ask_t30 = no_ask
                            if (fav_bid_t30 >= T30_SNIPER_FAV_BID_MIN
                                    and 0 < fav_ask_t30 < 100):
                                slip_t30 = T30_SNIPER_SLIP_C
                                limit_t30 = min(T30_SNIPER_CAP_CENTS,
                                                fav_ask_t30 + slip_t30)
                                attempted_tickers.add(ticker)
                                # Balance check
                                try:
                                    bal = await client.get_balance()
                                except Exception as e:  # noqa: BLE001
                                    log_fp.write(json.dumps({
                                        "kind": "balance_error",
                                        "ts_ms": now_ms, "ticker": ticker,
                                        "stage": "t30_sniper", "error": repr(e),
                                    }, default=str) + "\n")
                                    log_fp.flush()
                                    state["t30_sniper_state"] = "done"
                                    continue
                                if bal.balance < MIN_BALANCE_CENTS:
                                    halt_reason = "MIN-BALANCE"
                                    log_fp.write(json.dumps({
                                        "kind": "balance_halt",
                                        "ts_ms": now_ms, "ticker": ticker,
                                        "balance_cents": bal.balance,
                                        "stage": "t30_sniper",
                                    }, default=str) + "\n")
                                    log_fp.flush()
                                    state["t30_sniper_state"] = "done"
                                    continue
                                trig_t30 = {
                                    "kind": "t30_sniper_trigger",
                                    "ts_ms": now_ms, "ticker": ticker,
                                    "fav_side": fav_side_t30,
                                    "fav_bid_cents": fav_bid_t30,
                                    "fav_ask_cents": fav_ask_t30,
                                    "yes_bid_cents": yes_bid,
                                    "no_bid_cents": no_bid,
                                    "yes_ask_cents": yes_ask,
                                    "no_ask_cents": no_ask,
                                    "limit_cents": limit_t30,
                                    "slippage_cents": slip_t30,
                                    "contracts": T30_SNIPER_CONTRACTS,
                                    "secs_to_close": round(secs_to_close, 1),
                                    "balance_cents": bal.balance,
                                    "book_source": book_source,
                                }
                                log_fp.write(json.dumps(trig_t30, default=str) + "\n")
                                log_fp.flush()
                                base_t30 = {
                                    "ts_ms": now_ms, "ticker": ticker,
                                    "leg": "T30_SNIPER",
                                    "tier": "T30_SNIPER",
                                    "side": fav_side_t30,
                                    "contracts": T30_SNIPER_CONTRACTS,
                                    "limit_cents": limit_t30,
                                    "slippage_cents": slip_t30,
                                    "fav_bid_cents": fav_bid_t30,
                                    "fav_ask_cents": fav_ask_t30,
                                    "yes_bid_cents": yes_bid,
                                    "no_bid_cents": no_bid,
                                    "yes_ask_cents": yes_ask,
                                    "no_ask_cents": no_ask,
                                    "close_ts_ms": close_ts_ms,
                                    "secs_to_close": round(secs_to_close, 1),
                                    "balance_cents": bal.balance,
                                    "dry_run": args.dry_run,
                                }
                                f_t30, avg_t30, oid_t30 = await place_ioc(
                                    client, ticker, fav_side_t30,
                                    T30_SNIPER_CONTRACTS, limit_t30,
                                    log_fp, base_t30, args.dry_run,
                                )
                                if f_t30 == 0:
                                    log_fp.write(json.dumps({
                                        "kind": "t30_sniper_no_fill",
                                        "ts_ms": int(time.time() * 1000),
                                        "ticker": ticker,
                                        "side": fav_side_t30,
                                        "limit_cents": limit_t30,
                                        "fav_ask_cents": fav_ask_t30,
                                        "order_id": oid_t30,
                                    }, default=str) + "\n")
                                    log_fp.flush()
                                    state["t30_sniper_state"] = "done"
                                    print(
                                        f"[live-unified] T30-SNIPER NO-FILL "
                                        f"{ticker} {fav_side_t30} "
                                        f"fav_bid={fav_bid_t30}c "
                                        f"fav_ask={fav_ask_t30}c "
                                        f"limit={limit_t30}c",
                                        flush=True,
                                    )
                                    continue
                                entry_fee_t30 = kalshi_taker_fee_cents(avg_t30, f_t30)
                                state.update({
                                    "status": "entered",
                                    "leg_taken": "T30_SNIPER",
                                    "side": fav_side_t30,
                                    "contracts": f_t30,
                                    "entry_price_cents": avg_t30,
                                    "entry_fee_cents": entry_fee_t30,
                                    "order_id": oid_t30,
                                    "tier": "T30_SNIPER",
                                    "t30_sniper_state": "done",
                                })
                                log_fp.write(json.dumps({
                                    "kind": "t30_sniper_fill",
                                    "ts_ms": int(time.time() * 1000),
                                    "ticker": ticker, "leg": "T30_SNIPER",
                                    "tier": "T30_SNIPER",
                                    "side": fav_side_t30, "contracts": f_t30,
                                    "entry_price_cents": avg_t30,
                                    "entry_fee_cents": entry_fee_t30,
                                    "order_id": oid_t30,
                                    "limit_cents": limit_t30,
                                    "fav_bid_cents": fav_bid_t30,
                                    "fav_ask_cents": fav_ask_t30,
                                    "yes_bid_cents": yes_bid,
                                    "no_bid_cents": no_bid,
                                    "close_ts_ms": close_ts_ms,
                                    "secs_to_close": round(secs_to_close, 1),
                                }, default=str) + "\n")
                                log_fp.flush()
                                print(
                                    f"[live-unified] T30-SNIPER FILL "
                                    f"{ticker} {fav_side_t30} "
                                    f"{f_t30}@{avg_t30}c "
                                    f"fav_bid={fav_bid_t30}c "
                                    f"fav_ask={fav_ask_t30}c "
                                    f"secs_left={secs_to_close:.0f}",
                                    flush=True,
                                )
                                continue  # done with this market

                        # ── Disable LATE leg if requested ──
                        if args.disable_late:
                            if state.get("leg_taken") is None and state.get("late_disabled_logged") != True:
                                log_fp.write(json.dumps({
                                    "kind": "late_skip",
                                    "ts_ms": now_ms, "ticker": ticker,
                                    "reason": "LATE_DISABLED_BY_FLAG",
                                    "secs_to_close": round(secs_to_close, 1),
                                }, default=str) + "\n")
                                log_fp.flush()
                                state["late_disabled_logged"] = True
                            # Only mark leg_taken after T-30 sniper window has passed
                            # (so the sniper has a chance to fire).
                            if secs_to_close < T30_SNIPER_WINDOW_CLOSE_S:
                                state["leg_taken"] = "LATE"
                                state["status"] = "skipped_late_disabled"
                            continue

                        # ── Resting-order path (single-side favorite) ──
                        # Only ever ONE resting limit buy at 80c at a time,
                        # placed on the side that's trending toward 80
                        # (i.e., whose ask is in [60, 79]). If the favorite
                        # is already >= 80, fall through to legacy IOC. If
                        # neither side is above 60, keep polling (IOC will
                        # catch a side that crosses 80 directly). If filled
                        # → hold to settle. If the market flips (other side
                        # becomes favorite) → cancel and re-evaluate.
                        if not args.no_resting:
                            rstate = state.get("resting_state")

                            # ── Phase 2: monitor active resting order ──
                            if rstate == "active":
                                rest_side = state.get("resting_side")
                                rest_oid = state.get("resting_oid")
                                tier_r = state.get("resting_tier")

                                # Past close → cancel + expire.
                                if now_ms >= close_ts_ms:
                                    if not args.dry_run and rest_oid:
                                        try:
                                            await client.cancel_order(rest_oid)
                                        except Exception:  # noqa: BLE001
                                            pass
                                    state["resting_state"] = "expired"
                                    state["status"] = "late_no_fill"
                                    state["leg_taken"] = "LATE"
                                    log_fp.write(json.dumps({
                                        "kind": "resting_expire",
                                        "ts_ms": now_ms, "ticker": ticker,
                                        "tier": tier_r, "side": rest_side,
                                        "order_id": rest_oid,
                                    }, default=str) + "\n")
                                    log_fp.flush()
                                    print(
                                        f"[live-unified] RESTING EXPIRE {ticker} "
                                        f"tier={tier_r} side={rest_side} (no fill)",
                                        flush=True,
                                    )
                                    continue

                                # Poll fill status. Kalshi's average_price
                                # can be 0 briefly after a fresh fill —
                                # treat 0 as "not yet populated" and fall
                                # back to the limit (worst-case for P&L).
                                r_filled = 0
                                r_avg = RESTING_LIMIT_CENTS
                                if not args.dry_run and rest_oid:
                                    try:
                                        o_chk = await client.get_order(rest_oid)
                                        r_filled = int(o_chk.filled_count or 0)
                                        if (o_chk.average_price is not None
                                                and int(o_chk.average_price) > 0):
                                            r_avg = int(o_chk.average_price)
                                    except Exception:  # noqa: BLE001
                                        pass

                                if r_filled > 0:
                                    # Filled. Cancel remaining so silent fills
                                    # can't grow our position past what we've
                                    # accounted for; then hold to settle.
                                    if not args.dry_run and rest_oid:
                                        try:
                                            await client.cancel_order(rest_oid)
                                        except Exception:  # noqa: BLE001
                                            pass
                                        try:
                                            w_ord = await client.get_order(rest_oid)
                                            f2 = int(w_ord.filled_count or 0)
                                            if f2 > r_filled:
                                                r_filled = f2
                                            if (w_ord.average_price is not None
                                                    and int(w_ord.average_price) > 0):
                                                r_avg = int(w_ord.average_price)
                                        except Exception:  # noqa: BLE001
                                            pass

                                    entry_fee = kalshi_taker_fee_cents(r_avg, r_filled)
                                    late_fills += 1
                                    state.update({
                                        "status": "entered",
                                        "leg_taken": "LATE",
                                        "side": rest_side,
                                        "contracts": r_filled,
                                        "entry_price_cents": r_avg,
                                        "entry_fee_cents": entry_fee,
                                        "order_id": rest_oid,
                                        "tier": tier_r,
                                        "resting_state": "filled",
                                    })
                                    log_fp.write(json.dumps({
                                        "kind": "resting_fill",
                                        "ts_ms": int(time.time() * 1000),
                                        "ticker": ticker, "leg": "LATE",
                                        "tier": tier_r, "side": rest_side,
                                        "contracts": r_filled,
                                        "entry_price_cents": r_avg,
                                        "entry_fee_cents": entry_fee,
                                        "order_id": rest_oid,
                                        "close_ts_ms": close_ts_ms,
                                        "secs_to_close": round(secs_to_close, 1),
                                        "yes_ask_cents": yes_ask,
                                        "no_ask_cents": no_ask,
                                    }, default=str) + "\n")
                                    log_fp.flush()
                                    print(
                                        f"[live-unified] RESTING FILL {ticker} "
                                        f"tier={tier_r} {rest_side} "
                                        f"{r_filled}@{r_avg}c "
                                        f"secs_left={secs_to_close:.0f}",
                                        flush=True,
                                    )
                                    continue

                                # Not filled. Check for flip.
                                fav_side_now = "yes" if yes_ask >= no_ask else "no"
                                if fav_side_now != rest_side:
                                    # Market flipped: cancel + clear state so
                                    # Phase 1 below can re-evaluate placement
                                    # on the new favorite this same cycle.
                                    if not args.dry_run and rest_oid:
                                        try:
                                            await client.cancel_order(rest_oid)
                                        except Exception:  # noqa: BLE001
                                            pass
                                    log_fp.write(json.dumps({
                                        "kind": "resting_cancel_flip",
                                        "ts_ms": now_ms, "ticker": ticker,
                                        "tier": tier_r, "side": rest_side,
                                        "new_fav_side": fav_side_now,
                                        "yes_ask_cents": yes_ask,
                                        "no_ask_cents": no_ask,
                                        "order_id": rest_oid,
                                    }, default=str) + "\n")
                                    log_fp.flush()
                                    print(
                                        f"[live-unified] RESTING FLIP-CANCEL "
                                        f"{ticker} was={rest_side} "
                                        f"now_fav={fav_side_now} "
                                        f"yes_ask={yes_ask} no_ask={no_ask}",
                                        flush=True,
                                    )
                                    state["resting_state"] = None
                                    state.pop("resting_side", None)
                                    state.pop("resting_oid", None)
                                    state.pop("resting_tier", None)
                                    rstate = None
                                    # Fall through to Phase 1 placement.
                                else:
                                    # Still resting on favorite — wait.
                                    continue

                            # ── Phase 1: place on current favorite ──
                            if rstate is None and state.get("leg_taken") is None:
                                fav_side_now = "yes" if yes_ask >= no_ask else "no"
                                fav_ask_now = yes_ask if fav_side_now == "yes" else no_ask

                                if fav_ask_now <= 0:
                                    # Degenerate book — wait.
                                    continue

                                if fav_ask_now >= LATE_TRIGGER_CENTS:
                                    # Already at/above 80 — let the IOC
                                    # path catch it. Mark resting skipped
                                    # so we don't re-evaluate placement.
                                    state["resting_state"] = "skipped_late_arrival"
                                    log_fp.write(json.dumps({
                                        "kind": "resting_skip",
                                        "ts_ms": now_ms, "ticker": ticker,
                                        "reason": "ALREADY_AT_TRIGGER",
                                        "fav_side": fav_side_now,
                                        "fav_ask_cents": fav_ask_now,
                                        "yes_ask_cents": yes_ask,
                                        "no_ask_cents": no_ask,
                                        "secs_to_close": round(secs_to_close, 1),
                                    }, default=str) + "\n")
                                    log_fp.flush()
                                    # Fall through to IOC code below.
                                elif fav_ask_now < RESTING_PLACE_MIN_CENTS:
                                    # Neither side approaching threshold yet.
                                    # Keep polling; do not mark state so we
                                    # re-check next cycle.
                                    continue
                                else:
                                    # 65 <= fav_ask <= 79: place resting on
                                    # the favorite side at 80c.
                                    v60_snap = btc_buf.velocity_60s(now_ms)
                                    latest_snap = btc_buf.latest()
                                    btc_now_snap = latest_snap[1] if latest_snap else None
                                    strike_snap = float(state.get("strike", 0.0) or 0.0)
                                    if strike_snap <= 0.0:
                                        s = await fetch_floor_strike(client, ticker)
                                        if s and s > 0:
                                            strike_snap = s
                                            state["strike"] = s
                                    cushion_snap = (
                                        abs(btc_now_snap - strike_snap)
                                        if (btc_now_snap and strike_snap > 0) else None
                                    )

                                    if (v60_snap is None or btc_now_snap is None
                                            or strike_snap <= 0 or cushion_snap is None):
                                        tier_r = "MOVING_SMALL_FALLBACK"
                                        contracts_r = N_MOVING_SMALL
                                    else:
                                        moving_r = abs(v60_snap) > VELOCITY_THRESHOLD_USD
                                        big_r = cushion_snap >= CUSHION_THRESHOLD_USD
                                        if moving_r and big_r:
                                            tier_r, contracts_r = "MOVING_BIG", N_MOVING_BIG
                                        elif moving_r:
                                            tier_r, contracts_r = "MOVING_SMALL", N_MOVING_SMALL
                                        elif big_r:
                                            tier_r, contracts_r = "FLAT_BIG", N_FLAT_BIG
                                        else:
                                            tier_r, contracts_r = "FLAT_SMALL", 0

                                    if contracts_r == 0:
                                        # FLAT_SMALL — skip resting; legacy
                                        # IOC may flip below (or skip if
                                        # --disable-flat-small-flip is set).
                                        state["resting_state"] = "skipped_flat_small"
                                        log_fp.write(json.dumps({
                                            "kind": "resting_skip",
                                            "ts_ms": now_ms, "ticker": ticker,
                                            "tier": tier_r, "reason": "FLAT_SMALL",
                                            "fav_side": fav_side_now,
                                            "fav_ask_cents": fav_ask_now,
                                            "v60_usd": round(v60_snap, 2) if v60_snap is not None else None,
                                            "cushion_usd": round(cushion_snap, 2) if cushion_snap is not None else None,
                                            "btc_now": btc_now_snap, "strike": strike_snap,
                                            "secs_to_close": round(secs_to_close, 1),
                                        }, default=str) + "\n")
                                        log_fp.flush()
                                        # Fall through to IOC code below.
                                    else:
                                        # Balance check before placing.
                                        try:
                                            bal = await client.get_balance()
                                        except Exception as e:  # noqa: BLE001
                                            log_fp.write(json.dumps({
                                                "kind": "balance_error",
                                                "ts_ms": now_ms, "ticker": ticker,
                                                "stage": "resting", "error": repr(e),
                                            }, default=str) + "\n")
                                            log_fp.flush()
                                            continue  # retry next cycle
                                        if bal.balance < MIN_BALANCE_CENTS:
                                            halt_reason = "MIN-BALANCE"
                                            log_fp.write(json.dumps({
                                                "kind": "balance_halt",
                                                "ts_ms": now_ms, "ticker": ticker,
                                                "balance_cents": bal.balance,
                                                "stage": "resting",
                                            }, default=str) + "\n")
                                            log_fp.flush()
                                            continue

                                        attempted_tickers.add(ticker)
                                        late_triggers += 1

                                        place_rec = {
                                            "kind": "resting_place",
                                            "ts_ms": now_ms, "ticker": ticker,
                                            "tier": tier_r, "side": fav_side_now,
                                            "contracts": contracts_r,
                                            "limit_cents": RESTING_LIMIT_CENTS,
                                            "fav_ask_cents": fav_ask_now,
                                            "v60_usd": round(v60_snap, 2) if v60_snap is not None else None,
                                            "cushion_usd": round(cushion_snap, 2) if cushion_snap is not None else None,
                                            "btc_now": btc_now_snap, "strike": strike_snap,
                                            "yes_ask_cents": yes_ask, "no_ask_cents": no_ask,
                                            "yes_bid_cents": yes_bid, "no_bid_cents": no_bid,
                                            "secs_to_close": round(secs_to_close, 1),
                                            "balance_cents": bal.balance,
                                            "book_source": book_source,
                                            "dry_run": args.dry_run,
                                        }
                                        log_fp.write(json.dumps(place_rec, default=str) + "\n")
                                        log_fp.flush()

                                        new_oid: str | None = None
                                        if args.dry_run:
                                            new_oid = f"DRY-RUN-{fav_side_now.upper()}"
                                        else:
                                            try:
                                                p_ord = await client.place_order(
                                                    ticker=ticker,
                                                    side=fav_side_now,
                                                    count=contracts_r,
                                                    price=RESTING_LIMIT_CENTS,
                                                    order_type="limit",
                                                    action="buy",
                                                    post_only=True,
                                                    time_in_force=None,
                                                )
                                                new_oid = p_ord.order_id
                                            except KalshiAPIError as e:
                                                log_fp.write(json.dumps({
                                                    "kind": "resting_place_error",
                                                    "ts_ms": int(time.time() * 1000),
                                                    "ticker": ticker,
                                                    "side": fav_side_now,
                                                    "status": e.status, "body": e.body,
                                                }, default=str) + "\n")
                                                log_fp.flush()
                                            except Exception as e:  # noqa: BLE001
                                                log_fp.write(json.dumps({
                                                    "kind": "resting_place_error",
                                                    "ts_ms": int(time.time() * 1000),
                                                    "ticker": ticker,
                                                    "side": fav_side_now,
                                                    "error": repr(e),
                                                }, default=str) + "\n")
                                                log_fp.flush()

                                        if new_oid:
                                            state["resting_state"] = "active"
                                            state["resting_side"] = fav_side_now
                                            state["resting_oid"] = new_oid
                                            state["resting_tier"] = tier_r
                                            print(
                                                f"[live-unified] RESTING PLACED {ticker} "
                                                f"tier={tier_r} {fav_side_now} "
                                                f"{contracts_r}@{RESTING_LIMIT_CENTS}c "
                                                f"fav_ask={fav_ask_now}c "
                                                f"v60=${v60_snap if v60_snap is not None else 'NA'} "
                                                f"cushion=${cushion_snap if cushion_snap is not None else 'NA'} "
                                                f"secs_left={secs_to_close:.0f}",
                                                flush=True,
                                            )
                                            continue  # skip IOC path
                                        else:
                                            state["resting_state"] = "failed"
                                            print(
                                                f"[live-unified] RESTING FAILED {ticker} "
                                                f"tier={tier_r} {fav_side_now} — "
                                                f"falling back to IOC",
                                                flush=True,
                                            )
                                            # Fall through to IOC code below.

                            # Terminal resting states (skipped_late_arrival,
                            # skipped_flat_small, failed, filled, expired):
                            # filled/expired are handled above via status
                            # transitions; skipped/failed states fall
                            # through to the IOC code below.

                        # ── Shadow resting logging ─────────────────────
                        # When --no-resting is set, the real resting code
                        # above is skipped. This block simulates what it
                        # WOULD have done — pure logging, no orders, no
                        # impact on the IOC path that follows. Lets us
                        # evaluate resting-vs-IOC counterfactually from
                        # the live decision log.
                        #
                        # Limitation: if IOC fires on the opposite side
                        # before this side's ask reaches 80c, leg_taken
                        # is set and the shadow becomes orphaned (no
                        # would_fill/would_expire). Such cases are
                        # identifiable by a shadow_resting_place followed
                        # by a late_fill on the same ticker with no
                        # intervening shadow terminal event.
                        if args.no_resting:
                            s_state = state.get("shadow_resting_state")

                            # Phase 2: monitor a previously-placed shadow.
                            if s_state == "placed":
                                s_side = state.get("shadow_resting_side")
                                s_tier = state.get("shadow_resting_tier")
                                s_ask_now = yes_ask if s_side == "yes" else no_ask

                                if now_ms >= close_ts_ms:
                                    state["shadow_resting_state"] = "would_expire"
                                    log_fp.write(json.dumps({
                                        "kind": "shadow_resting_would_expire",
                                        "ts_ms": now_ms, "ticker": ticker,
                                        "tier": s_tier, "side": s_side,
                                        "limit_cents": RESTING_LIMIT_CENTS,
                                        "final_ask_cents": s_ask_now,
                                        "yes_ask_cents": yes_ask,
                                        "no_ask_cents": no_ask,
                                        "secs_to_close": round(secs_to_close, 1),
                                    }, default=str) + "\n")
                                    log_fp.flush()
                                elif s_ask_now >= LATE_TRIGGER_CENTS:
                                    state["shadow_resting_state"] = "would_fill"
                                    log_fp.write(json.dumps({
                                        "kind": "shadow_resting_would_fill",
                                        "ts_ms": now_ms, "ticker": ticker,
                                        "tier": s_tier, "side": s_side,
                                        "limit_cents": RESTING_LIMIT_CENTS,
                                        "ask_at_fill_cents": s_ask_now,
                                        "yes_ask_cents": yes_ask,
                                        "no_ask_cents": no_ask,
                                        "secs_to_close": round(secs_to_close, 1),
                                    }, default=str) + "\n")
                                    log_fp.flush()

                            # Phase 1: not yet placed — check whether to
                            # shadow-place. Mirrors the real resting code's
                            # placement conditions (favorite ask in
                            # [RESTING_PLACE_MIN_CENTS, LATE_TRIGGER_CENTS),
                            # tier from velocity+cushion, FLAT_SMALL skip).
                            elif s_state is None:
                                fav_side_now = "yes" if yes_ask >= no_ask else "no"
                                fav_ask_now = (
                                    yes_ask if fav_side_now == "yes" else no_ask
                                )
                                if (fav_ask_now > 0
                                        and RESTING_PLACE_MIN_CENTS <= fav_ask_now
                                        < LATE_TRIGGER_CENTS):
                                    v60_s = btc_buf.velocity_60s(now_ms)
                                    latest_s = btc_buf.latest()
                                    btc_now_s = latest_s[1] if latest_s else None
                                    strike_s = float(state.get("strike", 0.0) or 0.0)
                                    cushion_s = (
                                        abs(btc_now_s - strike_s)
                                        if (btc_now_s and strike_s > 0) else None
                                    )
                                    if (v60_s is None or btc_now_s is None
                                            or strike_s <= 0 or cushion_s is None):
                                        tier_s = "MOVING_SMALL_FALLBACK"
                                        contracts_s = N_MOVING_SMALL
                                    else:
                                        moving_s = abs(v60_s) > VELOCITY_THRESHOLD_USD
                                        big_s = cushion_s >= CUSHION_THRESHOLD_USD
                                        if moving_s and big_s:
                                            tier_s, contracts_s = "MOVING_BIG", N_MOVING_BIG
                                        elif moving_s:
                                            tier_s, contracts_s = "MOVING_SMALL", N_MOVING_SMALL
                                        elif big_s:
                                            tier_s, contracts_s = "FLAT_BIG", N_FLAT_BIG
                                        else:
                                            tier_s, contracts_s = "FLAT_SMALL", 0

                                    if contracts_s > 0:
                                        state["shadow_resting_state"] = "placed"
                                        state["shadow_resting_side"] = fav_side_now
                                        state["shadow_resting_tier"] = tier_s
                                        log_fp.write(json.dumps({
                                            "kind": "shadow_resting_place",
                                            "ts_ms": now_ms, "ticker": ticker,
                                            "tier": tier_s, "side": fav_side_now,
                                            "contracts": contracts_s,
                                            "limit_cents": RESTING_LIMIT_CENTS,
                                            "fav_ask_cents": fav_ask_now,
                                            "v60_usd": round(v60_s, 2) if v60_s is not None else None,
                                            "cushion_usd": round(cushion_s, 2) if cushion_s is not None else None,
                                            "btc_now": btc_now_s, "strike": strike_s,
                                            "yes_ask_cents": yes_ask,
                                            "no_ask_cents": no_ask,
                                            "yes_bid_cents": yes_bid,
                                            "no_bid_cents": no_bid,
                                            "secs_to_close": round(secs_to_close, 1),
                                            "book_source": book_source,
                                        }, default=str) + "\n")
                                        log_fp.flush()

                        # ── POC "leader_at_min12" late trigger ──
                        # On the first poll of the late window, pick the
                        # leader (higher ask) and IOC buy it at current
                        # ask, sized by the leader's price tier. No 80c
                        # threshold. Velocity/cushion are computed for
                        # logging only and no longer drive sizing.
                        leader_side = "yes" if yes_ask >= no_ask else "no"
                        leader_ask = yes_ask if leader_side == "yes" else no_ask

                        if leader_ask <= 0:
                            # Degenerate book — skip this window.
                            log_fp.write(json.dumps({
                                "kind": "late_skip",
                                "ts_ms": now_ms, "ticker": ticker,
                                "reason": "DEGENERATE_BOOK",
                                "entry_mode": "leader_at_min12",
                                "yes_ask_cents": yes_ask,
                                "no_ask_cents": no_ask,
                                "secs_to_close": round(secs_to_close, 1),
                            }, default=str) + "\n")
                            log_fp.flush()
                            state["leg_taken"] = "LATE"
                            state["status"] = "skipped_late_poc"
                            continue

                        # Tier by leader's ask price (POC sizing).
                        if leader_ask >= 80:
                            tier = "LEADER_80PLUS"
                            contracts = 5
                        elif leader_ask >= 65:
                            tier = "LEADER_65_79"
                            contracts = 5
                        elif leader_ask >= 55:
                            tier = "LEADER_55_64"
                            contracts = 2
                        else:
                            log_fp.write(json.dumps({
                                "kind": "late_skip",
                                "ts_ms": now_ms, "ticker": ticker,
                                "reason": "LEADER_BELOW_55",
                                "entry_mode": "leader_at_min12",
                                "leader_side": leader_side,
                                "leader_ask_cents": leader_ask,
                                "yes_ask_cents": yes_ask,
                                "no_ask_cents": no_ask,
                                "secs_to_close": round(secs_to_close, 1),
                            }, default=str) + "\n")
                            log_fp.flush()
                            state["leg_taken"] = "LATE"
                            state["status"] = "skipped_late_poc"
                            continue

                        # ── Smart-V5 LATE gate ──
                        # Compute features for regime + velocity-alignment
                        # gates. By default (no flags) behavior is unchanged.
                        v60_gate = btc_buf.velocity_60s(now_ms)
                        late_rv5 = btc_buf.realized_vol_5m(now_ms)
                        late_side = leader_side
                        late_entry_ask = leader_ask
                        late_contracts = contracts  # from tier assignment above
                        late_size_tag = tier
                        late_regime_flipped = False
                        late_skip_reason: str | None = None

                        # Gate A: RV regime
                        if (args.enable_rv_regime_gate
                                and late_rv5 is not None
                                and late_rv5 >= args.rv_cutoff_late):
                            if args.rv_regime_mode == "skip":
                                late_skip_reason = (
                                    f"RV_REGIME_SKIP rv5={late_rv5:.4f}"
                                    f" >= cutoff={args.rv_cutoff_late:.4f}"
                                )
                            else:  # flip
                                other_side_late = (
                                    "no" if leader_side == "yes" else "yes"
                                )
                                other_ask_late = (
                                    no_ask if other_side_late == "no" else yes_ask
                                )
                                if 0 < other_ask_late < 100:
                                    late_side = other_side_late
                                    late_entry_ask = other_ask_late
                                    late_contracts = LATE_REGIME_FLIP_CONTRACTS
                                    late_size_tag = f"{tier}_REGIME_FLIP"
                                    late_regime_flipped = True
                                else:
                                    late_skip_reason = (
                                        "RV_REGIME_FLIP_NO_UNDERDOG_LIQ "
                                        f"rv5={late_rv5:.4f}"
                                    )

                        # Gate B: velocity alignment (skip if vel goes against
                        # the leader or is flat). Only if not already flipping
                        # — a flip already takes the "against" side intentionally.
                        if (late_skip_reason is None
                                and not late_regime_flipped
                                and args.enable_late_vel_align
                                and v60_gate is not None):
                            if abs(v60_gate) < LATE_VEL_ALIGN_FLAT_THRESHOLD_USD:
                                late_skip_reason = f"VEL_FLAT v60=${v60_gate:.2f}"
                            else:
                                vel_sign_up = v60_gate > 0
                                leader_sign_up = (leader_side == "yes")
                                if vel_sign_up != leader_sign_up:
                                    late_skip_reason = (
                                        f"VEL_AGAINST v60=${v60_gate:.2f} "
                                        f"leader={leader_side}"
                                    )

                        if late_skip_reason is not None:
                            log_fp.write(json.dumps({
                                "kind": "late_skip",
                                "ts_ms": now_ms, "ticker": ticker,
                                "reason": late_skip_reason,
                                "entry_mode": "leader_at_min12",
                                "leader_side": leader_side,
                                "leader_ask_cents": leader_ask,
                                "tier": tier,
                                "rv_5m": (round(late_rv5, 4)
                                          if late_rv5 is not None else None),
                                "rv_cutoff_late": args.rv_cutoff_late,
                                "v60_usd": (round(v60_gate, 2)
                                            if v60_gate is not None else None),
                                "yes_ask_cents": yes_ask,
                                "no_ask_cents": no_ask,
                                "secs_to_close": round(secs_to_close, 1),
                            }, default=str) + "\n")
                            log_fp.flush()
                            state["leg_taken"] = "LATE"
                            state["status"] = "skipped_late_smart"
                            attempted_tickers.add(ticker)
                            print(
                                f"[live-unified] LATE SMART-SKIP "
                                f"{ticker} {late_skip_reason}",
                                flush=True,
                            )
                            continue

                        side_to_buy = late_side
                        fav_side = leader_side
                        fav_ask = leader_ask
                        slippage_c = adaptive_ioc_slippage_cents(late_entry_ask)
                        limit_cents = min(LATE_CAP_CENTS, late_entry_ask + slippage_c)

                        # Velocity + cushion for logging only.
                        v60 = btc_buf.velocity_60s(now_ms)
                        latest = btc_buf.latest()
                        btc_now = latest[1] if latest else None
                        strike = float(state.get("strike", 0.0) or 0.0)
                        if strike <= 0.0:
                            s = await fetch_floor_strike(client, ticker)
                            if s and s > 0:
                                strike = s
                                state["strike"] = s
                        cushion = abs(btc_now - strike) if (btc_now and strike > 0) else None

                        late_triggers += 1
                        attempted_tickers.add(ticker)

                        # Balance check.
                        try:
                            bal = await client.get_balance()
                        except Exception as e:  # noqa: BLE001
                            log_fp.write(json.dumps({
                                "kind": "balance_error",
                                "ts_ms": now_ms, "ticker": ticker, "stage": "late",
                                "error": repr(e),
                            }, default=str) + "\n")
                            log_fp.flush()
                            continue
                        if bal.balance < MIN_BALANCE_CENTS:
                            halt_reason = "MIN-BALANCE"
                            log_fp.write(json.dumps({
                                "kind": "balance_halt",
                                "ts_ms": now_ms, "ticker": ticker,
                                "balance_cents": bal.balance, "stage": "late",
                            }, default=str) + "\n")
                            log_fp.flush()
                            continue

                        trig_rec = {
                            "kind": "late_trigger",
                            "ts_ms": now_ms, "ticker": ticker,
                            "entry_mode": "leader_at_min12",
                            "tier": tier, "side_to_buy": side_to_buy,
                            "size_tag": late_size_tag,
                            "regime_flipped": late_regime_flipped,
                            "rv_5m": (round(late_rv5, 4)
                                      if late_rv5 is not None else None),
                            "contracts": late_contracts,
                            "entry_ask_cents": late_entry_ask,
                            "limit_cents": limit_cents,
                            "slippage_cents": slippage_c,
                            "fav_side": fav_side, "fav_ask_cents": fav_ask,
                            "leader_side": leader_side,
                            "leader_ask_cents": leader_ask,
                            "yes_ask_cents": yes_ask, "no_ask_cents": no_ask,
                            "yes_bid_cents": yes_bid, "no_bid_cents": no_bid,
                            "v60_usd": round(v60, 2) if v60 is not None else None,
                            "btc_now": btc_now, "strike": strike,
                            "cushion_usd": round(cushion, 2) if cushion is not None else None,
                            "close_ts_ms": close_ts_ms,
                            "secs_to_close": round(secs_to_close, 1),
                            "balance_cents": bal.balance,
                            "book_source": book_source,
                        }
                        log_fp.write(json.dumps(trig_rec, default=str) + "\n")
                        log_fp.flush()

                        base = {
                            "ts_ms": now_ms, "ticker": ticker,
                            "leg": "LATE", "tier": tier,
                            "entry_mode": "leader_at_min12",
                            "size_tag": late_size_tag,
                            "regime_flipped": late_regime_flipped,
                            "rv_5m": (round(late_rv5, 4)
                                      if late_rv5 is not None else None),
                            "side": side_to_buy, "contracts": late_contracts,
                            "entry_ask_cents": late_entry_ask,
                            "limit_cents": limit_cents,
                            "slippage_cents": slippage_c,
                            "leader_side": leader_side,
                            "leader_ask_cents": leader_ask,
                            "yes_ask_cents": yes_ask, "no_ask_cents": no_ask,
                            "close_ts_ms": close_ts_ms,
                            "secs_to_close": round(secs_to_close, 1),
                            "v60_usd": round(v60, 2) if v60 is not None else None,
                            "cushion_usd": round(cushion, 2) if cushion is not None else None,
                            "balance_cents": bal.balance,
                            "dry_run": args.dry_run,
                        }
                        filled, avg_cents, oid = await place_ioc(
                            client, ticker, side_to_buy, late_contracts,
                            limit_cents, log_fp, base, args.dry_run,
                        )
                        if filled == 0:
                            log_fp.write(json.dumps({
                                "kind": "late_no_fill",
                                "ts_ms": int(time.time() * 1000),
                                "ticker": ticker, "tier": tier,
                                "entry_mode": "leader_at_min12",
                                "side": side_to_buy, "limit_cents": limit_cents,
                                "slippage_cents": slippage_c,
                                "leader_ask_cents": leader_ask,
                                "order_id": oid,
                            }, default=str) + "\n")
                            log_fp.flush()
                            state["status"] = "late_no_fill"
                            state["leg_taken"] = "LATE"
                            print(
                                f"[live-unified] LATE NO-FILL {ticker} tier={tier} "
                                f"{side_to_buy} leader_ask={leader_ask}c "
                                f"slip=+{slippage_c}c limit={limit_cents}c "
                                f"entry_mode=leader_at_min12",
                                flush=True,
                            )
                            continue

                        entry_fee = kalshi_taker_fee_cents(avg_cents, filled)
                        late_fills += 1
                        state.update({
                            "status": "entered",
                            "leg_taken": "LATE",
                            "side": side_to_buy,
                            "contracts": filled,
                            "entry_price_cents": avg_cents,
                            "entry_fee_cents": entry_fee,
                            "order_id": oid,
                            "tier": tier,
                            "size_tag": late_size_tag,
                            "regime_flipped": late_regime_flipped,
                        })
                        log_fp.write(json.dumps({
                            "kind": "late_fill",
                            "ts_ms": int(time.time() * 1000),
                            "ticker": ticker, "leg": "LATE", "tier": tier,
                            "entry_mode": "leader_at_min12",
                            "size_tag": late_size_tag,
                            "regime_flipped": late_regime_flipped,
                            "rv_5m": (round(late_rv5, 4)
                                      if late_rv5 is not None else None),
                            "side": side_to_buy, "contracts": filled,
                            "entry_price_cents": avg_cents,
                            "entry_fee_cents": entry_fee, "order_id": oid,
                            "limit_cents": limit_cents,
                            "slippage_cents": slippage_c,
                            "close_ts_ms": close_ts_ms,
                            "secs_to_close": round(secs_to_close, 1),
                            "leader_side": leader_side,
                            "leader_ask_cents": leader_ask,
                            "v60_usd": round(v60, 2) if v60 is not None else None,
                            "cushion_usd": round(cushion, 2) if cushion is not None else None,
                            "btc_now": btc_now, "strike": strike,
                            "yes_ask_cents": yes_ask, "no_ask_cents": no_ask,
                        }, default=str) + "\n")
                        log_fp.flush()
                        print(
                            f"[live-unified] LATE FILL {ticker} tier={tier} "
                            f"{side_to_buy} {filled}@{avg_cents}c "
                            f"entry_mode=leader_at_min12 leader_ask={leader_ask}c "
                            f"slip=+{slippage_c}c limit={limit_cents}c "
                            f"v60=${v60 if v60 is not None else 'NA'} "
                            f"cushion=${cushion if cushion is not None else 'NA'} "
                            f"secs_left={secs_to_close:.0f}",
                            flush=True,
                        )

                    # ── Settlement polling ────────────────────────────
                    if now_wall - last_settle_poll_t >= SETTLE_POLL_INTERVAL_S:
                        last_settle_poll_t = now_wall
                        for ticker in list(tracked.keys()):
                            state = tracked[ticker]
                            if state["status"] != "entered":
                                continue
                            close_ts_ms = int(state.get("close_ts_ms", 0))
                            if now_ms < close_ts_ms:
                                continue
                            try:
                                contract = await client.get_contract(ticker)
                            except Exception:  # noqa: BLE001
                                continue
                            result = contract.result
                            if not result:
                                continue
                            n = int(state.get("contracts", 0))
                            side = state.get("side")
                            entry_cents = int(state.get("entry_price_cents", 0))
                            entry_fee = int(state.get("entry_fee_cents", 0))
                            if side == result:
                                gross = n * (100 - entry_cents)
                            else:
                                gross = -n * entry_cents
                            net = gross - entry_fee

                            log_fp.write(json.dumps({
                                "kind": "settle",
                                "ts_ms": now_ms, "ticker": ticker,
                                "leg": state.get("leg_taken"),
                                "tier": state.get("tier"),
                                "side": side, "result": result,
                                "contracts": n, "entry_price_cents": entry_cents,
                                "entry_fee_cents": entry_fee,
                                "gross_cents": gross, "net_cents": net,
                                "close_ts_ms": close_ts_ms,
                                "order_id": state.get("order_id"),
                            }, default=str) + "\n")
                            log_fp.flush()
                            state["status"] = "settled"
                            settles += 1
                            if net < 0:
                                daily_loss_cents += -net
                                if (daily_loss_cents >= DAILY_LOSS_CAP_CENTS
                                        and halt_reason is None):
                                    halt_reason = "DAILY-LOSS-CAP"
                                    print(
                                        f"[live-unified] HALT DAILY-LOSS-CAP "
                                        f"loss=${daily_loss_cents/100:.2f}",
                                        flush=True,
                                    )
                            print(
                                f"[live-unified] SETTLE {ticker} leg={state.get('leg_taken')} "
                                f"tier={state.get('tier')} {side} result={result} "
                                f"{n}@{entry_cents}c net={net:+d}c "
                                f"daily_loss=${daily_loss_cents/100:.2f}",
                                flush=True,
                            )

                    # ── Status ────────────────────────────────────────
                    if now_wall - last_status_t >= args.status_every_s:
                        last_status_t = now_wall
                        n_watch = sum(1 for s in tracked.values() if s["status"] == "watching")
                        n_poll = sum(1 for s in tracked.values() if s["status"] == "polling")
                        n_entered = sum(1 for s in tracked.values() if s["status"] == "entered")
                        n_settled = sum(1 for s in tracked.values() if s["status"] == "settled")
                        n_resting = sum(
                            1 for s in tracked.values()
                            if s.get("resting_state") == "active"
                        )
                        latest_btc = btc_buf.latest()
                        v60_dbg = btc_buf.velocity_60s(now_ms)
                        ws_state = "off"
                        if ws is not None:
                            ws_state = (
                                f"sub={ws_subscribed_ticker[-15:] if ws_subscribed_ticker else 'none'}"
                                f" reads={ws_book_reads} fb={ws_rest_fallbacks}"
                            )
                        print(
                            f"[live-unified] tracked={len(tracked)} watch={n_watch} "
                            f"poll={n_poll} resting={n_resting} entered={n_entered} "
                            f"settled={n_settled} "
                            f"early_trig={early_triggers}/{early_fills} "
                            f"late_trig={late_triggers}/{late_fills} settles={settles} "
                            f"btc={(latest_btc[1] if latest_btc else 'NA')} "
                            f"v60={(round(v60_dbg,2) if v60_dbg is not None else 'NA')} "
                            f"btc_errs={btc_buf.poll_errors} "
                            f"daily_loss=${daily_loss_cents/100:.2f} halt={halt_reason} "
                            f"resting_mode={not args.no_resting} ws[{ws_state}]",
                            flush=True,
                        )

                    # Dynamic sleep: if WS is on and any ticker is in its
                    # active window, tighten to ACTIVE_WS_SLEEP_S (book
                    # reads via WS are free). Otherwise honor CLI arg.
                    sleep_s = args.poll_interval_s
                    if ws is not None:
                        for s in tracked.values():
                            if s.get("status") != "polling" or s.get("leg_taken") is not None:
                                continue
                            cts = int(s.get("close_ts_ms", 0))
                            if cts <= 0:
                                continue
                            ots = int(s.get("open_ts_ms", cts - 900_000))
                            secs_to_close = (cts - now_ms) / 1000.0
                            secs_from_open = (now_ms - ots) / 1000.0
                            in_late = secs_to_close <= LATE_WINDOW_S and secs_to_close > 0
                            in_early = (
                                not args.disable_early
                                and 0 <= secs_from_open <= EARLY_WINDOW_S
                                and s.get("early_state") != "done"
                            )
                            in_em = (
                                not args.disable_earlier_moderate
                                and secs_to_close <= EARLIER_MODERATE_MIN_S
                                and secs_to_close > LATE_WINDOW_S
                                and s.get("earlier_moderate_state") != "done"
                            )
                            if in_late or in_early or in_em:
                                sleep_s = ACTIVE_WS_SLEEP_S
                                break
                    await asyncio.sleep(sleep_s)
            finally:
                stop["flag"] = True
                btc_task.cancel()
                try:
                    await btc_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:  # noqa: BLE001
                        pass
                if ws_task is not None:
                    ws_task.cancel()
                    try:
                        await ws_task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
                log_fp.close()

    print(
        f"[live-unified] stopped. early={early_fills} late={late_fills} "
        f"settles={settles} halt={halt_reason}",
        flush=True,
    )
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
