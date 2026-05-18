"""Live V5 LATE-CERTAINTY trader — late-window buy, hold to settle.

Strategy (from late_certainty_backtest.py V5 variant + cancel-pair extension):

Two parallel entry paths fire only in the late window (last LATE_WINDOW_S
seconds, default 180s = minute 12+ of a 15-min cycle). Only ONE entry per
window — first path to fill cancels the other.

Path A — IOC at MIN_ENTRY_PRICE (default 80c) up to MAX_ENTRY_PRICE (95c):
  - Each poll cycle, fetch order book.
  - If yes_ask in [80c, 95c]: IOC buy YES at 95c limit (fills at best ask ≤ 95).
    Else if no_ask in [80c, 95c]: IOC buy NO at 95c limit.

Path B — Cancel-pair resting limit at PAIR_LIMIT_CENTS (default 78c):
  - On the first poll after entering the late window, place two GTC limit
    buys at 78c — one YES, one NO — but ONLY on a side whose current ask
    is ABOVE 78c (otherwise the order would cross and accidentally buy the
    loser at low price).
  - Poll get_order on each pair-limit; if either reports a fill, cancel
    the other and record entry. (First-fill wins; cancel-pair guards against
    double exposure.)
  - If neither fills within PAIR_TIMEOUT_S (default 60s), cancel both —
    the favorite-at-78c +EV band closes fast after early min 12 per backtest.
  - If Path A fires first, cancel both pair limits before placing the IOC.

After entry, hold to settlement. Kalshi auto-resolves at 100c (win) or 0c
(loss) — no sell needed.

Resume-on-restart: replay_log_state() rebuilds attempted_tickers + open
positions + any unresolved pair-limit order IDs from the JSONL. On startup,
resume_pair_orders() queries Kalshi for each unresolved order, cancels if
still resting, recovers as a fill if executed.

Backtest evidence (n=2,819 KXBTC15M):
  - 80–95c bucket at end of min 12: ~91% win rate, +3.5c/contract net.
  - 75–80c bucket at end of min 12: 82.8% win rate, +3.76c/contract net.
    (This is the band Path B targets.)
  - <75c at min 12: -EV. (We refuse to rest limits below 78c.)

JSONL kinds: startup, discover, order_attempt, order_response,
order_rejected, order_error, order_no_fill, fill, settle, balance_halt,
poll_skip_too_pricey, poll_skip_halt, poll_skip_closed,
pair_place, pair_place_error, pair_place_skipped, pair_fill, pair_cancelled,
pair_cancel_error, pair_expired, pair_resume_filled, pair_resume_cancelled.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

_V1_ROOT = Path(r"C:\Trading\btc-bias-engine")
if str(_V1_ROOT) not in sys.path:
    sys.path.insert(0, str(_V1_ROOT))
from kalshi_client import (  # noqa: E402
    KalshiAPIError,
    KalshiClient,
    KalshiContract,
    KalshiOrderBook,
)

# ── Constants ─────────────────────────────────────────────────────────────

# Path A — IOC trigger thresholds (dollars).
MIN_ENTRY_PRICE = 0.80
MAX_ENTRY_PRICE = 0.95

# Path B — Cancel-pair resting-limit price. Below this is -EV per backtest.
PAIR_LIMIT_ENABLED = True
PAIR_LIMIT_CENTS = 78
PAIR_TIMEOUT_S = 60.0       # cancel both if no fill within this window
PAIR_POLL_INTERVAL_S = 2.0  # don't hammer get_order every cycle

# Contracts per trade — starting conservative.
CONTRACTS_PER_TRADE = 5

# Late window: only act in the last LATE_WINDOW_S seconds of the cycle.
LATE_WINDOW_S = 180  # 3 minutes ≈ backtest's "minute 12+"

# How often to call /markets to refresh the active-market list.
DISCOVERY_INTERVAL_S = 30.0

# Safety caps.
DAILY_LOSS_CAP_CENTS = 2000     # $20/day stops further entries (conservative)
MIN_BALANCE_CENTS = 500         # $5 — below this, halt entries
STALE_BOOK_TIMEOUT_MS = 5_000
SETTLE_POLL_INTERVAL_S = 15.0

LIMIT_CAP_CENTS = int(round(MAX_ENTRY_PRICE * 100))   # 95
TRIGGER_CENTS = int(round(MIN_ENTRY_PRICE * 100))     # 80

KALSHI_CREDS_PATH = Path(r"C:\Trading\btc-bias-engine\credentials\kalshi.env")


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
    import math
    p = price_cents / 100.0
    fee_dollars = math.ceil(0.07 * count * p * (1.0 - p) * 100.0) / 100.0
    return int(round(fee_dollars * 100))


def replay_log_state(
    log_path: Path,
) -> tuple[int, set[str], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Rebuild state from JSONL: daily loss, attempted tickers, open positions,
    and any unresolved pair-limit orders (placed but not yet filled/cancelled/expired).

    Returns:
        (daily_loss_cents, attempted_tickers, open_positions, unresolved_pairs)
        unresolved_pairs[ticker] = {"yes_oid": str|None, "no_oid": str|None,
                                    "placed_at_ms": int, "close_ts_ms": int}
    """
    if not log_path.exists():
        return 0, set(), {}, {}
    today_floor_ms = _utc_today_floor_ms()
    today_end_ms = today_floor_ms + 24 * 60 * 60 * 1000

    total_net = 0
    attempted: set[str] = set()
    fills: dict[str, dict[str, Any]] = {}
    settled: set[str] = set()
    # pair_place_open: ticker -> latest unresolved pair_place record
    pair_open: dict[str, dict[str, Any]] = {}
    pair_resolved: set[str] = set()

    try:
        with log_path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = rec.get("kind")
                tkr = rec.get("ticker")
                if kind in ("order_attempt", "fill", "order_rejected",
                            "order_error", "order_no_fill", "pair_place",
                            "pair_fill"):
                    if tkr:
                        attempted.add(tkr)
                if kind == "fill" and tkr:
                    fills[tkr] = {
                        "ticker": tkr,
                        "side": rec.get("side"),
                        "contracts": int(rec.get("contracts", 0) or 0),
                        "entry_price_cents": int(rec.get("entry_price_cents", 0) or 0),
                        "entry_fee_cents": int(rec.get("entry_fee_cents", 0) or 0),
                        "close_ts_ms": int(rec.get("close_ts_ms", 0) or 0),
                        "order_id": rec.get("order_id"),
                    }
                if kind == "pair_fill" and tkr:
                    # pair_fill is a kind of fill — record both
                    fills[tkr] = {
                        "ticker": tkr,
                        "side": rec.get("side"),
                        "contracts": int(rec.get("contracts", 0) or 0),
                        "entry_price_cents": int(rec.get("entry_price_cents", 0) or 0),
                        "entry_fee_cents": int(rec.get("entry_fee_cents", 0) or 0),
                        "close_ts_ms": int(rec.get("close_ts_ms", 0) or 0),
                        "order_id": rec.get("order_id"),
                    }
                if kind == "pair_place" and tkr:
                    pair_open[tkr] = {
                        "yes_oid": rec.get("yes_order_id"),
                        "no_oid": rec.get("no_order_id"),
                        "placed_at_ms": int(rec.get("ts_ms", 0) or 0),
                        "close_ts_ms": int(rec.get("close_ts_ms", 0) or 0),
                    }
                if kind in ("pair_fill", "pair_expired", "pair_cancelled",
                            "pair_resume_filled", "pair_resume_cancelled"):
                    if tkr:
                        pair_resolved.add(tkr)
                if kind == "settle" and tkr:
                    settled.add(tkr)
                    close_ts_ms = int(rec.get("close_ts_ms", 0) or 0)
                    if today_floor_ms <= close_ts_ms < today_end_ms:
                        total_net += int(rec.get("net_cents", 0) or 0)
    except OSError:
        return 0, set(), {}, {}

    open_positions = {t: v for t, v in fills.items() if t not in settled}
    unresolved_pairs = {t: v for t, v in pair_open.items() if t not in pair_resolved}
    return max(0, -total_net), attempted, open_positions, unresolved_pairs


# ── Kalshi helpers ────────────────────────────────────────────────────────

async def discover_markets(client: KalshiClient) -> list[KalshiContract]:
    """Find currently-open KXBTC15M markets via REST."""
    try:
        return await client.find_btc_contracts(min_minutes_remaining=0.0)
    except Exception:  # noqa: BLE001
        return []


async def fetch_book(client: KalshiClient, ticker: str) -> KalshiOrderBook | None:
    try:
        return await client.get_orderbook(ticker)
    except Exception:  # noqa: BLE001
        return None


async def safe_get_order(client: KalshiClient, order_id: str):
    """Best-effort get_order; returns None on error."""
    try:
        return await client.get_order(order_id)
    except Exception:  # noqa: BLE001
        return None


async def safe_cancel(client: KalshiClient, order_id: str) -> bool:
    """Best-effort cancel; returns True if Kalshi reports success."""
    try:
        return await client.cancel_order(order_id)
    except Exception:  # noqa: BLE001
        return False


async def place_pair_side(
    client: KalshiClient,
    ticker: str,
    side: str,
    contracts: int,
    log_fp,
    base_rec: dict,
) -> str | None:
    """Place ONE side of the cancel-pair. Returns order_id or None on error."""
    try:
        order = await client.place_order(
            ticker=ticker,
            side=side,
            count=contracts,
            price=PAIR_LIMIT_CENTS,
            order_type="limit",
            action="buy",
            time_in_force=None,  # GTC — will rest passively
        )
        return order.order_id
    except KalshiAPIError as e:
        log_fp.write(json.dumps({
            **base_rec,
            "kind": "pair_place_error",
            "side": side,
            "status": e.status,
            "body": e.body,
        }, default=str) + "\n")
        log_fp.flush()
        return None
    except Exception as e:  # noqa: BLE001
        log_fp.write(json.dumps({
            **base_rec,
            "kind": "pair_place_error",
            "side": side,
            "error": repr(e),
        }, default=str) + "\n")
        log_fp.flush()
        return None


async def resume_pair_orders(
    client: KalshiClient,
    unresolved_pairs: dict[str, dict[str, Any]],
    log_fp,
    open_positions: dict[str, dict[str, Any]],
    attempted_tickers: set[str],
) -> None:
    """For each pair_place without a terminating event in the log, query Kalshi
    state. If still resting → cancel. If filled → recover as a fill (added to
    open_positions). Either way → block re-entry on this ticker via attempted.
    """
    for ticker, info in unresolved_pairs.items():
        for side, oid in (("yes", info.get("yes_oid")), ("no", info.get("no_oid"))):
            if not oid:
                continue
            order = await safe_get_order(client, oid)
            now_ms = int(time.time() * 1000)
            if order is None:
                log_fp.write(json.dumps({
                    "kind": "pair_resume_cancelled",
                    "ts_ms": now_ms,
                    "ticker": ticker,
                    "side": side,
                    "order_id": oid,
                    "note": "get_order returned None — assuming gone",
                }, default=str) + "\n")
                log_fp.flush()
                continue
            filled = int(order.filled_count or 0)
            if filled > 0:
                avg_cents = (
                    int(order.average_price)
                    if order.average_price is not None
                    else PAIR_LIMIT_CENTS
                )
                entry_fee = kalshi_taker_fee_cents(avg_cents, filled)
                open_positions[ticker] = {
                    "ticker": ticker,
                    "side": side,
                    "contracts": filled,
                    "entry_price_cents": avg_cents,
                    "entry_fee_cents": entry_fee,
                    "close_ts_ms": info.get("close_ts_ms", 0),
                    "order_id": oid,
                }
                attempted_tickers.add(ticker)
                log_fp.write(json.dumps({
                    "kind": "pair_resume_filled",
                    "ts_ms": now_ms,
                    "ticker": ticker,
                    "side": side,
                    "order_id": oid,
                    "filled_count": filled,
                    "average_price_cents": avg_cents,
                    "entry_fee_cents": entry_fee,
                    "close_ts_ms": info.get("close_ts_ms", 0),
                }, default=str) + "\n")
                log_fp.flush()
                print(
                    f"[live-v5] RESUME pair fill recovered: {ticker} {side} "
                    f"{filled}@{avg_cents}c", flush=True,
                )
                # Cancel sibling order if it's still out there.
                sib_oid = info.get("no_oid") if side == "yes" else info.get("yes_oid")
                if sib_oid:
                    await safe_cancel(client, sib_oid)
            else:
                # Not filled — try to cancel.
                await safe_cancel(client, oid)
                attempted_tickers.add(ticker)
                log_fp.write(json.dumps({
                    "kind": "pair_resume_cancelled",
                    "ts_ms": now_ms,
                    "ticker": ticker,
                    "side": side,
                    "order_id": oid,
                    "order_status": order.status,
                }, default=str) + "\n")
                log_fp.flush()


# ── Main loop ─────────────────────────────────────────────────────────────

async def main_async() -> int:
    parser = argparse.ArgumentParser(
        prog="live-v5-certainty",
        description="Late-certainty (80c IOC + 78c cancel-pair limits) LIVE Kalshi trader.",
    )
    parser.add_argument("--decision-log", required=True, type=Path)
    parser.add_argument("--poll-interval-s", type=float, default=1.5)
    parser.add_argument("--status-every-s", type=float, default=30.0)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Log decisions but don't call place_order/cancel_order. Balance "
        "fetch still runs.",
    )
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

    daily_loss_cents, attempted_tickers, open_positions, unresolved_pairs = (
        replay_log_state(args.decision_log)
    )
    daily_loss_day_floor = _utc_today_floor_ms()
    halt_reason: str | None = None
    if daily_loss_cents >= DAILY_LOSS_CAP_CENTS:
        halt_reason = "DAILY-LOSS-CAP"

    # Per-market state. status: watching → polling → entered → settled
    # pair_state: not_placed → placed → (filled | cancelled | expired | skipped)
    tracked: dict[str, dict[str, Any]] = {}
    for tkr, p in open_positions.items():
        tracked[tkr] = {
            "close_ts_ms": int(p.get("close_ts_ms", 0) or 0),
            "status": "entered",
            "side": p.get("side"),
            "contracts": int(p.get("contracts", 0) or 0),
            "entry_price_cents": int(p.get("entry_price_cents", 0) or 0),
            "entry_fee_cents": int(p.get("entry_fee_cents", 0) or 0),
            "order_id": p.get("order_id"),
            "pair_state": "filled",
        }

    triggers = 0
    fills = 0
    pair_fills = 0
    pair_expired = 0
    settles = 0
    skipped_too_pricey = 0
    last_discover_t = 0.0
    last_settle_poll_t = 0.0
    last_status_t = time.time()

    startup_rec = {
        "kind": "startup",
        "ts_ms": int(time.time() * 1000),
        "variant": "live_v5_certainty_v2",
        "dry_run": args.dry_run,
        "min_entry_price": MIN_ENTRY_PRICE,
        "max_entry_price": MAX_ENTRY_PRICE,
        "pair_limit_enabled": PAIR_LIMIT_ENABLED,
        "pair_limit_cents": PAIR_LIMIT_CENTS,
        "pair_timeout_s": PAIR_TIMEOUT_S,
        "contracts_per_trade": CONTRACTS_PER_TRADE,
        "late_window_s": LATE_WINDOW_S,
        "daily_loss_cap_cents": DAILY_LOSS_CAP_CENTS,
        "min_balance_cents": MIN_BALANCE_CENTS,
        "poll_interval_s": args.poll_interval_s,
        "daily_loss_cents_at_start": daily_loss_cents,
        "attempted_tickers_at_start": len(attempted_tickers),
        "open_positions_at_start": len(open_positions),
        "unresolved_pairs_at_start": len(unresolved_pairs),
        "halt_reason_at_start": halt_reason,
    }
    log_fp.write(json.dumps(startup_rec, default=str) + "\n")
    log_fp.flush()
    print(
        f"[live-v5] starting dry_run={args.dry_run} daily_loss={daily_loss_cents}c "
        f"attempted={len(attempted_tickers)} open={len(open_positions)} "
        f"unresolved_pairs={len(unresolved_pairs)} halt={halt_reason} "
        f"min={MIN_ENTRY_PRICE} max={MAX_ENTRY_PRICE} pair={PAIR_LIMIT_CENTS}c "
        f"ct={CONTRACTS_PER_TRADE}",
        flush=True,
    )

    async with KalshiClient(key_id=key_id, private_key_pem=pem, demo=False) as client:
        # Resume any orphaned pair-limit orders BEFORE entering the main loop.
        if unresolved_pairs and not args.dry_run:
            print(
                f"[live-v5] resuming {len(unresolved_pairs)} unresolved pair order(s)...",
                flush=True,
            )
            await resume_pair_orders(
                client, unresolved_pairs, log_fp, open_positions, attempted_tickers,
            )
            # Refresh tracked from any newly-recovered open positions.
            for tkr, p in open_positions.items():
                if tkr in tracked:
                    continue
                tracked[tkr] = {
                    "close_ts_ms": int(p.get("close_ts_ms", 0) or 0),
                    "status": "entered",
                    "side": p.get("side"),
                    "contracts": int(p.get("contracts", 0) or 0),
                    "entry_price_cents": int(p.get("entry_price_cents", 0) or 0),
                    "entry_fee_cents": int(p.get("entry_fee_cents", 0) or 0),
                    "order_id": p.get("order_id"),
                    "pair_state": "filled",
                }

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
                        print("[live-v5] new UTC day — daily loss reset", flush=True)

                # ── Discovery ─────────────────────────────────────────
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
                                "status": "skipped_attempted_prior",
                                "pair_state": "not_placed",
                            }
                            continue
                        tracked[m.ticker] = {
                            "close_ts_ms": m.expiry_ts,
                            "status": "watching",
                            "pair_state": "not_placed",
                        }

                # ── Per-market: pair-limit + IOC entry ────────────────
                for ticker in list(tracked.keys()):
                    state = tracked[ticker]
                    if state["status"] not in ("watching", "polling"):
                        continue

                    close_ts_ms = int(state["close_ts_ms"])
                    secs_to_close = (close_ts_ms - now_ms) / 1000.0

                    if secs_to_close > LATE_WINDOW_S:
                        continue
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

                    book = await fetch_book(client, ticker)
                    if book is None or (not book.yes_bids and not book.no_bids):
                        continue
                    if book.book_age_ms > STALE_BOOK_TIMEOUT_MS:
                        continue

                    yes_ask_cents = book.best_yes_ask
                    no_ask_cents = book.best_no_ask

                    # ── Path B: place cancel-pair limits ──────────────
                    if (PAIR_LIMIT_ENABLED
                            and state.get("pair_state") == "not_placed"
                            and not args.dry_run):
                        # Only place on a side whose ask is strictly ABOVE
                        # PAIR_LIMIT_CENTS — otherwise the order would cross
                        # and accidentally buy the loser at low price.
                        yes_eligible = yes_ask_cents > PAIR_LIMIT_CENTS
                        no_eligible = no_ask_cents > PAIR_LIMIT_CENTS
                        if not yes_eligible and not no_eligible:
                            state["pair_state"] = "skipped"
                            log_fp.write(json.dumps({
                                "kind": "pair_place_skipped",
                                "ts_ms": now_ms,
                                "ticker": ticker,
                                "reason": "no_eligible_side",
                                "yes_ask_cents": yes_ask_cents,
                                "no_ask_cents": no_ask_cents,
                                "pair_limit_cents": PAIR_LIMIT_CENTS,
                            }, default=str) + "\n")
                            log_fp.flush()
                        else:
                            # Balance check.
                            try:
                                bal = await client.get_balance()
                            except Exception as e:  # noqa: BLE001
                                log_fp.write(json.dumps({
                                    "kind": "pair_place_error",
                                    "ts_ms": now_ms,
                                    "ticker": ticker,
                                    "side": "balance_check",
                                    "error": repr(e),
                                }, default=str) + "\n")
                                log_fp.flush()
                                bal = None
                            if bal is None:
                                pass
                            elif bal.balance < MIN_BALANCE_CENTS:
                                halt_reason = "MIN-BALANCE"
                                log_fp.write(json.dumps({
                                    "kind": "balance_halt",
                                    "ts_ms": now_ms,
                                    "ticker": ticker,
                                    "balance_cents": bal.balance,
                                    "stage": "pair_place",
                                }, default=str) + "\n")
                                log_fp.flush()
                                print(
                                    f"[live-v5] HALT MIN-BALANCE bal=${bal.balance/100:.2f}",
                                    flush=True,
                                )
                                state["pair_state"] = "skipped"
                            else:
                                base = {
                                    "ts_ms": now_ms,
                                    "ticker": ticker,
                                    "balance_cents": bal.balance,
                                    "yes_ask_cents": yes_ask_cents,
                                    "no_ask_cents": no_ask_cents,
                                    "close_ts_ms": close_ts_ms,
                                    "secs_to_close": round(secs_to_close, 1),
                                    "pair_limit_cents": PAIR_LIMIT_CENTS,
                                    "contracts": CONTRACTS_PER_TRADE,
                                }
                                yes_oid = (
                                    await place_pair_side(
                                        client, ticker, "yes",
                                        CONTRACTS_PER_TRADE, log_fp, base,
                                    ) if yes_eligible else None
                                )
                                no_oid = (
                                    await place_pair_side(
                                        client, ticker, "no",
                                        CONTRACTS_PER_TRADE, log_fp, base,
                                    ) if no_eligible else None
                                )
                                if yes_oid or no_oid:
                                    state["pair_state"] = "placed"
                                    state["pair_placed_at_ms"] = now_ms
                                    state["pair_yes_order_id"] = yes_oid
                                    state["pair_no_order_id"] = no_oid
                                    state["pair_last_check_t"] = 0.0
                                    log_fp.write(json.dumps({
                                        **base,
                                        "kind": "pair_place",
                                        "yes_order_id": yes_oid,
                                        "no_order_id": no_oid,
                                    }, default=str) + "\n")
                                    log_fp.flush()
                                    attempted_tickers.add(ticker)
                                    print(
                                        f"[live-v5] PAIR PLACE {ticker} "
                                        f"yes_oid={yes_oid} no_oid={no_oid} "
                                        f"@{PAIR_LIMIT_CENTS}c "
                                        f"yes_ask={yes_ask_cents} no_ask={no_ask_cents}",
                                        flush=True,
                                    )
                                else:
                                    state["pair_state"] = "skipped"

                    # ── Path B: monitor pair fills / timeout ─────────
                    if state.get("pair_state") == "placed":
                        last_check = state.get("pair_last_check_t", 0.0)
                        if now_wall - last_check >= PAIR_POLL_INTERVAL_S:
                            state["pair_last_check_t"] = now_wall
                            yes_oid = state.get("pair_yes_order_id")
                            no_oid = state.get("pair_no_order_id")
                            yes_order = (
                                await safe_get_order(client, yes_oid) if yes_oid else None
                            )
                            no_order = (
                                await safe_get_order(client, no_oid) if no_oid else None
                            )
                            yes_filled = int(yes_order.filled_count) if yes_order else 0
                            no_filled = int(no_order.filled_count) if no_order else 0

                            if yes_filled > 0 or no_filled > 0:
                                # Pick the side that filled (prefer larger fill
                                # if both somehow filled).
                                if yes_filled >= no_filled and yes_filled > 0:
                                    side = "yes"
                                    n = yes_filled
                                    avg_cents = (
                                        int(yes_order.average_price)
                                        if yes_order and yes_order.average_price is not None
                                        else PAIR_LIMIT_CENTS
                                    )
                                    oid = yes_oid
                                    sib_oid = no_oid
                                else:
                                    side = "no"
                                    n = no_filled
                                    avg_cents = (
                                        int(no_order.average_price)
                                        if no_order and no_order.average_price is not None
                                        else PAIR_LIMIT_CENTS
                                    )
                                    oid = no_oid
                                    sib_oid = yes_oid

                                if sib_oid:
                                    await safe_cancel(client, sib_oid)

                                entry_fee = kalshi_taker_fee_cents(avg_cents, n)
                                state.update({
                                    "status": "entered",
                                    "side": side,
                                    "contracts": n,
                                    "entry_price_cents": avg_cents,
                                    "entry_fee_cents": entry_fee,
                                    "order_id": oid,
                                    "entered_at_ms": now_ms,
                                    "pair_state": "filled",
                                })
                                pair_fills += 1
                                fills += 1
                                log_fp.write(json.dumps({
                                    "kind": "pair_fill",
                                    "ts_ms": now_ms,
                                    "ticker": ticker,
                                    "side": side,
                                    "contracts": n,
                                    "entry_price_cents": avg_cents,
                                    "entry_fee_cents": entry_fee,
                                    "order_id": oid,
                                    "sibling_cancelled_oid": sib_oid,
                                    "close_ts_ms": close_ts_ms,
                                    "yes_ask_cents": yes_ask_cents,
                                    "no_ask_cents": no_ask_cents,
                                }, default=str) + "\n")
                                log_fp.flush()
                                print(
                                    f"[live-v5] PAIR FILL {ticker} {side} "
                                    f"{n}@{avg_cents}c sibling_cancelled={sib_oid}",
                                    flush=True,
                                )
                                continue  # done with this market this cycle

                            # No fills — check timeout.
                            placed_at = state.get("pair_placed_at_ms", now_ms)
                            elapsed_s = (now_ms - placed_at) / 1000.0
                            if elapsed_s >= PAIR_TIMEOUT_S:
                                if yes_oid:
                                    await safe_cancel(client, yes_oid)
                                if no_oid:
                                    await safe_cancel(client, no_oid)
                                state["pair_state"] = "expired"
                                pair_expired += 1
                                log_fp.write(json.dumps({
                                    "kind": "pair_expired",
                                    "ts_ms": now_ms,
                                    "ticker": ticker,
                                    "yes_order_id": yes_oid,
                                    "no_order_id": no_oid,
                                    "elapsed_s": round(elapsed_s, 1),
                                    "close_ts_ms": close_ts_ms,
                                }, default=str) + "\n")
                                log_fp.flush()
                                print(
                                    f"[live-v5] PAIR EXPIRED {ticker} after "
                                    f"{elapsed_s:.0f}s",
                                    flush=True,
                                )

                    # ── Path A: IOC at 80–95c ─────────────────────────
                    side: str | None = None
                    ask_cents: int = 0
                    if yes_ask_cents >= TRIGGER_CENTS:
                        side = "yes"
                        ask_cents = yes_ask_cents
                    elif no_ask_cents >= TRIGGER_CENTS:
                        side = "no"
                        ask_cents = no_ask_cents

                    if side is None:
                        continue

                    if ask_cents > LIMIT_CAP_CENTS:
                        skipped_too_pricey += 1
                        log_fp.write(json.dumps({
                            "kind": "poll_skip_too_pricey",
                            "ts_ms": now_ms,
                            "ticker": ticker,
                            "side": side,
                            "ask_cents": ask_cents,
                            "limit_cap_cents": LIMIT_CAP_CENTS,
                            "close_ts_ms": close_ts_ms,
                        }, default=str) + "\n")
                        log_fp.flush()
                        continue

                    # Before firing IOC, cancel any outstanding pair limits
                    # to avoid double exposure.
                    if state.get("pair_state") == "placed" and not args.dry_run:
                        yes_oid = state.get("pair_yes_order_id")
                        no_oid = state.get("pair_no_order_id")
                        if yes_oid:
                            await safe_cancel(client, yes_oid)
                        if no_oid:
                            await safe_cancel(client, no_oid)
                        state["pair_state"] = "cancelled_for_ioc"
                        log_fp.write(json.dumps({
                            "kind": "pair_cancelled",
                            "ts_ms": now_ms,
                            "ticker": ticker,
                            "reason": "ioc_trigger",
                            "yes_order_id": yes_oid,
                            "no_order_id": no_oid,
                        }, default=str) + "\n")
                        log_fp.flush()

                    # Balance check.
                    try:
                        bal = await client.get_balance()
                    except Exception as e:  # noqa: BLE001
                        log_fp.write(json.dumps({
                            "kind": "balance_error",
                            "ts_ms": now_ms,
                            "ticker": ticker,
                            "error": repr(e),
                        }, default=str) + "\n")
                        log_fp.flush()
                        continue

                    if bal.balance < MIN_BALANCE_CENTS:
                        halt_reason = "MIN-BALANCE"
                        log_fp.write(json.dumps({
                            "kind": "balance_halt",
                            "ts_ms": now_ms,
                            "ticker": ticker,
                            "balance_cents": bal.balance,
                            "stage": "ioc",
                        }, default=str) + "\n")
                        log_fp.flush()
                        continue

                    triggers += 1
                    attempted_tickers.add(ticker)
                    limit_cents = LIMIT_CAP_CENTS
                    contracts = CONTRACTS_PER_TRADE

                    attempt_rec = {
                        "kind": "order_attempt",
                        "ts_ms": now_ms,
                        "ticker": ticker,
                        "side": side,
                        "contracts": contracts,
                        "ask_cents": ask_cents,
                        "limit_cents": limit_cents,
                        "yes_ask_cents": yes_ask_cents,
                        "no_ask_cents": no_ask_cents,
                        "yes_bid_cents": book.best_yes_bid,
                        "no_bid_cents": book.best_no_bid,
                        "balance_cents": bal.balance,
                        "close_ts_ms": close_ts_ms,
                        "secs_to_close": round(secs_to_close, 1),
                        "dry_run": args.dry_run,
                    }
                    log_fp.write(json.dumps(attempt_rec, default=str) + "\n")
                    log_fp.flush()

                    if args.dry_run:
                        filled_count = contracts
                        avg_price_cents = ask_cents
                        order_id = "DRY-RUN"
                        order_status = "dry_run"
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
                                f"[live-v5] ORDER REJECTED ticker={ticker} "
                                f"status={e.status} body={e.body}", flush=True,
                            )
                            state["status"] = "rejected"
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
                            state["status"] = "errored"
                            continue

                        filled_count = int(order.filled_count or 0)
                        avg_price_cents = (
                            int(order.average_price)
                            if order.average_price is not None
                            else ask_cents
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
                            "ask_cents": ask_cents,
                            "order_id": order_id,
                        }, default=str) + "\n")
                        log_fp.flush()
                        print(
                            f"[live-v5] IOC NO-FILL ticker={ticker} "
                            f"side={side} limit={limit_cents}c", flush=True,
                        )
                        state["status"] = "no_fill"
                        continue

                    entry_fee = kalshi_taker_fee_cents(avg_price_cents, filled_count)
                    state.update({
                        "status": "entered",
                        "side": side,
                        "contracts": filled_count,
                        "entry_price_cents": avg_price_cents,
                        "entry_fee_cents": entry_fee,
                        "order_id": order_id,
                        "entered_at_ms": int(time.time() * 1000),
                    })
                    fills += 1
                    log_fp.write(json.dumps({
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
                        "ask_cents": ask_cents,
                        "close_ts_ms": close_ts_ms,
                        "secs_to_close": round(secs_to_close, 1),
                    }, default=str) + "\n")
                    log_fp.flush()
                    print(
                        f"[live-v5] IOC FILL ticker={ticker} side={side} "
                        f"{filled_count}@{avg_price_cents}c (ask={ask_cents}c) "
                        f"secs_left={secs_to_close:.0f} dry_run={args.dry_run}",
                        flush=True,
                    )

                # ── Settlement polling ────────────────────────────────
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
                            "ts_ms": now_ms,
                            "ticker": ticker,
                            "side": side,
                            "result": result,
                            "contracts": n,
                            "entry_price_cents": entry_cents,
                            "entry_fee_cents": entry_fee,
                            "gross_cents": gross,
                            "net_cents": net,
                            "close_ts_ms": close_ts_ms,
                            "order_id": state.get("order_id"),
                            "via_pair": state.get("pair_state") == "filled",
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
                                    f"[live-v5] HALT DAILY-LOSS-CAP "
                                    f"loss=${daily_loss_cents/100:.2f}",
                                    flush=True,
                                )
                        print(
                            f"[live-v5] SETTLE ticker={ticker} side={side} "
                            f"result={result} {n}@{entry_cents}c "
                            f"net={net:+d}c daily_loss=${daily_loss_cents/100:.2f}",
                            flush=True,
                        )

                # ── Status ────────────────────────────────────────────
                if now_wall - last_status_t >= args.status_every_s:
                    last_status_t = now_wall
                    n_watch = sum(1 for s in tracked.values() if s["status"] == "watching")
                    n_poll = sum(1 for s in tracked.values() if s["status"] == "polling")
                    n_entered = sum(1 for s in tracked.values() if s["status"] == "entered")
                    n_settled = sum(1 for s in tracked.values() if s["status"] == "settled")
                    n_pair_placed = sum(
                        1 for s in tracked.values() if s.get("pair_state") == "placed"
                    )
                    print(
                        f"[live-v5] tracked={len(tracked)} watch={n_watch} "
                        f"poll={n_poll} pair_open={n_pair_placed} "
                        f"entered={n_entered} settled={n_settled} "
                        f"trig={triggers} fills={fills} pair_fills={pair_fills} "
                        f"pair_exp={pair_expired} settles={settles} "
                        f"pricey_skip={skipped_too_pricey} "
                        f"daily_loss=${daily_loss_cents/100:.2f} halt={halt_reason}",
                        flush=True,
                    )

                await asyncio.sleep(args.poll_interval_s)
        finally:
            log_fp.close()

    print(
        f"[live-v5] stopped. triggers={triggers} fills={fills} "
        f"pair_fills={pair_fills} settles={settles} halt={halt_reason}",
        flush=True,
    )
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
