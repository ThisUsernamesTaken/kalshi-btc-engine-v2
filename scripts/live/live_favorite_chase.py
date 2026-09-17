"""LIVE favorite-chase order placer (REAL MONEY).

User authorized this on 2026-05-21 with explicit override of safety-rails
suggestion. See memory/project_favorite_chase_2026_05_20.md and the
"Live deployment (favorite-chase)" chapter.

Differences from scripts/live/paper_favorite_chase.py:
  - Places real orders via D:/Trading/btc-bias-engine/kalshi_client.KalshiClient
  - Adds a 4-bps strike-distance filter (skip if |spot - strike| / spot < 4 bps)
  - Spot for the bps filter is polled from Coinbase REST per asset family
  - Markets whose asset has no Coinbase feed (e.g. KXBNB) are excluded from
    trading because we cannot evaluate the 4-bps gate without spot.

Rule (unchanged from the spec):
  After T+8min of session, the first trade at >=75c on either YES or NO
  triggers a 1-contract market buy on that side at the next ask. Stop at 50c
  mid (market sell at bid). Hold to settlement otherwise. ``saw_below_trigger``
  gate prevents firing on markets we subscribed to mid-session.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import signal
import sys
import time
from datetime import UTC, datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
# Reuse the btc-bias-engine's authenticated client (load-bearing for live_ta.py).
sys.path.insert(0, r"D:\Trading\btc-bias-engine")

from kalshi_btc_engine_v2.adapters.kalshi import (  # noqa: E402
    KalshiRestClient,
    KalshiWebSocketClient,
    apply_l2_payload,
)
from kalshi_btc_engine_v2.config import load_settings  # noqa: E402
from kalshi_btc_engine_v2.core.orderbook import KalshiOrderBook  # noqa: E402
from kalshi_btc_engine_v2.core.time import parse_rfc3339_ms  # noqa: E402
from kalshi_btc_engine_v2.strategies.favorite_chase import (  # noqa: E402
    MAX_CONTRACTS_PER_TRADE,
    MIN_STRIKE_DISTANCE_BPS,
    BookTick,
    Side,
    TradeTick,
    ask_for_side,
    bid_for_side,
    detect_entry,
    detect_stop,
    passes_strike_distance,
)

from kalshi_client import KalshiClient  # type: ignore[import-not-found]  # noqa: E402


# --- crypto series → Coinbase spot product id ---
# Kalshi 15-MINUTE crypto series ONLY. Restricted 2026-05-21 after observing
# the engine had been subscribed to hourly/daily/weekly markets only — Kalshi's
# 15-min series_ticker has a "15M" suffix (e.g., "KXBTC15M"), and only 5
# cryptos currently have 15M variants on Kalshi (BTC/ETH/SOL/XRP/DOGE).
# Keys are the prefix of the Kalshi series ticker (the part before the first '-').
SERIES_TO_COINBASE: dict[str, str] = {
    "KXBTC15M": "BTC-USD",
    "KXETH15M": "ETH-USD",
    "KXSOL15M": "SOL-USD",
    "KXXRP15M": "XRP-USD",
    "KXDOGE15M": "DOGE-USD",
}

# Expected 15-min market duration. Defensive check in _register_market —
# skip any market whose close_ms - open_ms != this (belt-and-suspenders in
# case Kalshi adds non-15M markets to one of these series in the future).
FIFTEEN_MIN_MS: int = 15 * 60 * 1000


def threshold_key(series_ticker: str) -> str:
    """KXBTC15M -> KXBTC for per_crypto_thresholds.json lookup
    (the JSON keys are the asset prefix without the 15M suffix)."""
    if series_ticker.endswith("15M"):
        return series_ticker[:-3]
    return series_ticker


# --- Safety + per-crypto calibration ---
# Hard-coded daily realized-PnL loss cap. Authorized 2026-05-21.
# When daily_realized_cents <= -DAILY_LOSS_CAP_CENTS, new entries halted
# until UTC midnight rollover.
DAILY_LOSS_CAP_CENTS: int = 1000  # $10

# Per-crypto calibrated bps thresholds (0.5 x median 15m directional move).
# Loaded at startup; series not in the table fall back to --min-strike-bps.
PER_CRYPTO_THRESHOLDS_PATH: Path = Path(
    r"C:\Trading\kalshi_btc_gradient_engine\scripts\monitors\per_crypto_thresholds.json"
)
# Sanity floor: ignore per-series values below this and fall back to default.
# Protects against bad-data rows (e.g., KXOP read 0.0 from sparse 15m candles).
MIN_SENSIBLE_THRESHOLD_BPS: float = 1.0


def load_per_crypto_thresholds(path: Path) -> dict[str, float]:
    """Load {series_ticker: threshold_bps} from per_crypto_thresholds.json.

    Filters out null / zero / sub-1bps entries so they fall back to default.
    Returns empty dict if the file is missing or unreadable.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, float] = {}
    for series, info in (data or {}).items():
        v = info.get("proposed_threshold_bps") if isinstance(info, dict) else None
        if isinstance(v, (int, float)) and v >= MIN_SENSIBLE_THRESHOLD_BPS:
            out[str(series)] = float(v)
    return out


def _utc_today_floor_ms() -> int:
    today = datetime.now(tz=UTC).date()
    return int(datetime(today.year, today.month, today.day, tzinfo=UTC).timestamp() * 1000)


def kalshi_entry_fee_cents(fill_price_dollars: float, contracts: int) -> int:
    """Kalshi taker fee on entry: ceil(0.07 * n * P * (1-P) * 100) / 100 dollars."""
    p = max(0.0, min(1.0, fill_price_dollars))
    fee_dollars = math.ceil(0.07 * contracts * p * (1.0 - p) * 100.0) / 100.0
    return int(round(fee_dollars * 100))


def replay_today_realized_pnl(log_path: Path) -> int:
    """Sum (exit_price - fill_price) * 100 - entry_fee_cents for today's UTC exits."""
    if not log_path.exists():
        return 0
    today_floor = _utc_today_floor_ms()
    today_end = today_floor + 24 * 60 * 60 * 1000
    total = 0
    with log_path.open("r", encoding="utf-8") as fp:
        for line in fp:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("kind") != "exit":
                continue
            ts = e.get("log_ts_ms")
            if not isinstance(ts, int) or ts < today_floor or ts >= today_end:
                continue
            fill = e.get("fill_price")
            exit_price = e.get("exit_price")
            if fill is None or exit_price is None:
                continue
            try:
                fill_f = float(fill)
                exit_f = float(exit_price)
            except (TypeError, ValueError):
                continue
            gross = int(round((exit_f - fill_f) * 100.0))
            fee = kalshi_entry_fee_cents(fill_f, 1)
            total += gross - fee
    return total


@dataclass
class SessionState:
    ticker: str
    series_ticker: str
    event_ticker: str
    open_ms: int
    close_ms: int
    strike: float | None
    book: KalshiOrderBook = field(default_factory=lambda: KalshiOrderBook(""))
    saw_below_trigger: bool = False
    side: Side | None = None
    fill_price: float | None = None
    fill_ts_ms: int | None = None
    contracts: int = 0
    closed: bool = False
    exit_reason: str | None = None
    exit_price: float | None = None
    exit_ts_ms: int | None = None
    entry_order_id: str | None = None
    exit_order_id: str | None = None


def now_ms() -> int:
    return int(time.time() * 1000)


def log_event(fp, kind: str, **fields: Any) -> None:
    fields["kind"] = kind
    fields["log_ts_ms"] = now_ms()
    fp.write(json.dumps(fields, default=str) + "\n")
    fp.flush()


def series_for(ticker: str) -> str:
    return ticker.split("-", 1)[0] if "-" in ticker else ticker


# --- spot price cache (Coinbase REST) ---


class CoinbaseSpotCache:
    """Polls Coinbase /products/{id}/ticker for needed products.

    Cache is in-process; we don't share with anything else. Refresh every
    `refresh_s`. Bursts capped so we don't get rate-limited.
    """

    def __init__(self, refresh_s: float = 2.0) -> None:
        self.refresh_s = refresh_s
        self._spots: dict[str, tuple[float, int]] = {}  # product_id -> (mid, ts_ms)
        self._active: set[str] = set()

    def activate(self, product_id: str) -> None:
        self._active.add(product_id)

    def get(self, product_id: str, max_age_ms: int = 5_000) -> float | None:
        v = self._spots.get(product_id)
        if not v:
            return None
        mid, ts = v
        if now_ms() - ts > max_age_ms:
            return None
        return mid

    async def poll_loop(self, log_fp) -> None:
        try:
            import aiohttp
        except ImportError:
            raise RuntimeError("aiohttp required")
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while True:
                for product_id in list(self._active):
                    try:
                        async with session.get(
                            f"https://api.exchange.coinbase.com/products/{product_id}/ticker"
                        ) as r:
                            data = await r.json()
                        bid = float(data.get("bid", 0) or 0)
                        ask = float(data.get("ask", 0) or 0)
                        if bid > 0 and ask > 0:
                            self._spots[product_id] = ((bid + ask) / 2.0, now_ms())
                    except Exception as e:
                        log_event(log_fp, "spot_poll_error", product=product_id, error=repr(e))
                    # rate-limit-friendly tiny sleep between products
                    await asyncio.sleep(0.1)
                await asyncio.sleep(self.refresh_s)


# --- discovery (per-series query) ---


CRYPTO_SERIES_PREFIXES = tuple(SERIES_TO_COINBASE.keys())


async def discover_markets(rest: KalshiRestClient, throttle_s: float = 0.25) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for series in CRYPTO_SERIES_PREFIXES:
        cursor: str | None = None
        for _ in range(5):
            params: dict[str, Any] = {"series_ticker": series, "status": "open", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            try:
                resp = await rest.request("GET", "/markets", params=params)
            except Exception:
                break
            out.extend(resp.get("markets", []))
            cursor = resp.get("cursor") or None
            await asyncio.sleep(throttle_s)
            if not cursor:
                break
    return out


# --- engine ---


class LiveFavoriteChaseEngine:
    def __init__(
        self,
        rest: KalshiRestClient,
        ws: KalshiWebSocketClient,
        kalshi: KalshiClient,
        spot: CoinbaseSpotCache,
        decision_log: Path,
        shadow: bool,
        max_entry_price: float,
        max_slip: float,
        per_series_bps: dict[str, float],
        default_min_bps: float,
        daily_loss_cap_cents: int,
        stop_mode: str = "none",
        bps_gate: str = "disabled",
        discover_interval_s: float = 30.0,
        stale_feed_timeout_s: float = 600.0,
    ) -> None:
        self.rest = rest
        self.ws = ws
        self.kalshi = kalshi
        self.spot = spot
        self.decision_log = decision_log
        self.shadow = shadow
        self.max_entry_price = max_entry_price
        self.max_slip = max_slip
        self.per_series_bps = per_series_bps
        self.default_min_bps = default_min_bps
        self.daily_loss_cap_cents = daily_loss_cap_cents
        self.stop_mode = stop_mode
        self.bps_gate = bps_gate
        self.daily_realized_cents: int = 0
        self.daily_loss_day_floor_ms: int = _utc_today_floor_ms()
        self.halt_reason: str | None = None
        self.discover_interval_s = discover_interval_s
        self.stale_feed_timeout_s = stale_feed_timeout_s
        self.sessions: dict[str, SessionState] = {}
        self.tracked_tickers: set[str] = set()
        self.last_event_ms = now_ms()
        self._stop = False
        self.stale_exit = False
        decision_log.parent.mkdir(parents=True, exist_ok=True)
        self._log_fp = decision_log.open("a", encoding="utf-8")

    def request_stop(self) -> None:
        self._stop = True

    def close(self) -> None:
        try:
            self._log_fp.flush()
            self._log_fp.close()
        except Exception:
            pass

    def _register_market(self, m: dict[str, Any]) -> SessionState | None:
        ticker = str(m.get("ticker") or "")
        if not ticker or ticker in self.sessions:
            return self.sessions.get(ticker)
        series_prefix = series_for(ticker)
        # Filter: only crypto with a spot feed mapping.
        if series_prefix not in SERIES_TO_COINBASE:
            return None
        product = SERIES_TO_COINBASE[series_prefix]
        self.spot.activate(product)
        open_ms = parse_rfc3339_ms(m.get("open_time") or "") or 0
        close_ms = parse_rfc3339_ms(m.get("close_time") or "") or 0
        if not open_ms or not close_ms:
            return None
        # Defensive: confirm 15-min market duration.
        if (close_ms - open_ms) != FIFTEEN_MIN_MS:
            return None
        # Strike. May not be present for non-BTC markets in the /markets response;
        # we'll resolve to None and skip the bps gate (effectively allow).
        strike: float | None = None
        for key in ("floor_strike", "cap_strike", "strike"):
            v = m.get(key)
            if v is not None:
                try:
                    strike = float(v)
                    break
                except (TypeError, ValueError):
                    continue
        ss = SessionState(
            ticker=ticker,
            series_ticker=series_prefix,
            event_ticker=str(m.get("event_ticker") or ""),
            open_ms=open_ms,
            close_ms=close_ms,
            strike=strike,
            book=KalshiOrderBook(ticker),
        )
        self.sessions[ticker] = ss
        self.tracked_tickers.add(ticker)
        log_event(
            self._log_fp,
            "discover",
            ticker=ticker,
            series=series_prefix,
            strike=strike,
            open_ms=open_ms,
            close_ms=close_ms,
        )
        return ss

    def _retire_settled(self) -> None:
        cutoff = now_ms() - 5 * 60_000
        gone = [t for t, ss in self.sessions.items() if ss.closed and ss.close_ms < cutoff]
        for t in gone:
            self.sessions.pop(t, None)
            self.tracked_tickers.discard(t)

    async def discovery_loop(self) -> None:
        while not self._stop:
            try:
                markets = await discover_markets(self.rest)
                new = 0
                for m in markets:
                    before = len(self.sessions)
                    ss = self._register_market(m)
                    if ss is not None and len(self.sessions) > before:
                        new += 1
                self._retire_settled()
                log_event(
                    self._log_fp,
                    "discovery",
                    tracked=len(self.sessions),
                    seen=len(markets),
                    new=new,
                    shadow=self.shadow,
                )
            except Exception as exc:
                log_event(self._log_fp, "discovery_error", error=repr(exc))
            await asyncio.sleep(self.discover_interval_s)

    def _book_tick(self, ss: SessionState, ts_ms: int) -> BookTick:
        return BookTick(
            ts_ms=ts_ms,
            yes_bid=float(ss.book.best_yes_bid) if ss.book.best_yes_bid is not None else None,
            yes_ask=float(ss.book.best_yes_ask) if ss.book.best_yes_ask is not None else None,
        )

    async def _place_entry(self, ss: SessionState, side: Side, ask: float, trade_ts_ms: int) -> bool:
        """Synthetic market BUY: a marketable IOC limit priced at 99c.

        Kalshi's order API requires a price field on every order, and the
        shared KalshiClient (which must not be modified) only attaches one
        for order_type='limit' — so a literal order_type='market' is rejected
        400 every time. A limit BUY priced at 99c is fully marketable: it
        crosses the whole book and fills at the REAL best ask with price
        improvement (you pay the ask, not 99c) — identical fill quality to a
        market order, while satisfying the API.

        Then confirm filled_count: a 0-fill books NO position (return False),
        so a later stop can never fire against a phantom — the bug that
        placed ~50c opposite-side positions on 2026-05-22."""
        ask_cents = int(round(ask * 100))
        if self.shadow:
            log_event(
                self._log_fp, "shadow_entry",
                ticker=ss.ticker, series=ss.series_ticker, side=side.value,
                fill_price=ask, ask_cents=ask_cents,
            )
            return True
        try:
            assert MAX_CONTRACTS_PER_TRADE == 1, "sizing must remain 1 per trade"
            order = await self.kalshi.place_order(
                ticker=ss.ticker,
                side=side.value,
                count=MAX_CONTRACTS_PER_TRADE,
                price=99,  # marketable-limit ceiling; fills at the real ask
                order_type="limit",
                action="buy",
                time_in_force="immediate_or_cancel",
            )
        except Exception as exc:
            log_event(self._log_fp, "live_entry_error", ticker=ss.ticker, side=side.value, error=repr(exc))
            return False
        filled = int(getattr(order, "filled_count", 0) or 0)
        order_id = getattr(order, "order_id", None) or getattr(order, "id", None)
        if filled <= 0:
            # Order accepted but nothing filled — do NOT book a position.
            log_event(
                self._log_fp, "entry_unfilled",
                ticker=ss.ticker, side=side.value, ask_cents=ask_cents, order_id=order_id,
            )
            return False
        log_event(
            self._log_fp, "live_entry_placed",
            ticker=ss.ticker, side=side.value, ask_cents=ask_cents,
            fill_count=filled, avg_price=getattr(order, "average_price", None),
            qty=MAX_CONTRACTS_PER_TRADE, order_id=order_id,
        )
        ss.entry_order_id = order_id
        return True

    async def _place_stop(self, ss: SessionState, bid: float, ts_ms: int) -> bool:
        """Synthetic market SELL: a marketable IOC limit priced at 1c.

        A limit SELL at 1c crosses the whole bid stack and fills at the REAL
        best bid (you receive the bid, not 1c) — market-order fill quality,
        API-compliant. filled_count==0 means the stop did NOT sell: return
        False so the caller leaves the position open and retries next tick
        rather than falsely marking it closed."""
        bid_cents = int(round(bid * 100))
        if self.shadow:
            log_event(
                self._log_fp, "shadow_stop",
                ticker=ss.ticker, side=ss.side.value if ss.side else None,
                bid=bid, bid_cents=bid_cents,
            )
            return True
        try:
            # Always exits the exact qty we opened with — never more.
            qty = min(ss.contracts or MAX_CONTRACTS_PER_TRADE, MAX_CONTRACTS_PER_TRADE)
            order = await self.kalshi.place_order(
                ticker=ss.ticker,
                side=ss.side.value if ss.side else "yes",
                count=qty,
                price=1,  # marketable-limit floor; fills at the real bid
                order_type="limit",
                action="sell",
                time_in_force="immediate_or_cancel",
            )
        except Exception as exc:
            log_event(self._log_fp, "live_stop_error", ticker=ss.ticker, error=repr(exc))
            return False
        filled = int(getattr(order, "filled_count", 0) or 0)
        order_id = getattr(order, "order_id", None) or getattr(order, "id", None)
        if filled <= 0:
            log_event(
                self._log_fp, "stop_unfilled",
                ticker=ss.ticker, bid=bid, bid_cents=bid_cents, order_id=order_id,
            )
            return False
        log_event(
            self._log_fp, "live_stop_placed",
            ticker=ss.ticker, bid=bid, bid_cents=bid_cents,
            fill_count=filled, avg_price=getattr(order, "average_price", None),
            order_id=order_id,
        )
        ss.exit_order_id = order_id
        return True

    async def _on_trade(self, ss: SessionState, trade: TradeTick) -> None:
        if ss.closed or ss.side is not None:
            return
        if max(trade.yes_price, trade.no_price) < 0.75:
            ss.saw_below_trigger = True
        side = detect_entry(trade, ss.open_ms)
        if side is None:
            return
        if not ss.saw_below_trigger:
            return
        if self.halt_reason == "DAILY_LOSS_CAP":
            log_event(
                self._log_fp, "skip_halted",
                ticker=ss.ticker, side=side.value,
                reason="DAILY_LOSS_CAP",
                realized_cents=self.daily_realized_cents,
                cap_cents=-self.daily_loss_cap_cents,
            )
            return
        tick = self._book_tick(ss, trade.ts_ms)
        ask = ask_for_side(tick, side)
        if ask is None or ask <= 0:
            log_event(
                self._log_fp, "trigger_no_book",
                ticker=ss.ticker, side=side.value,
                trade_yes=trade.yes_price, trade_no=trade.no_price,
            )
            return
        if ask > self.max_entry_price:
            log_event(
                self._log_fp, "skip_max_price",
                ticker=ss.ticker, side=side.value, ask=ask, max_entry_price=self.max_entry_price,
            )
            return
        trigger_price = trade.yes_price if side is Side.YES else trade.no_price
        slip = ask - trigger_price
        if slip > self.max_slip:
            log_event(
                self._log_fp, "skip_max_slip",
                ticker=ss.ticker, side=side.value, slip=slip, max_slip=self.max_slip,
            )
            return
        # Per-crypto strike-distance gate (default fallback for series not calibrated).
        threshold_lookup_key = threshold_key(ss.series_ticker)
        threshold_bps = self.per_series_bps.get(threshold_lookup_key, self.default_min_bps)
        threshold_source = "per_series" if threshold_lookup_key in self.per_series_bps else "default"
        if self.bps_gate == "enabled" and threshold_bps > 0 and ss.strike is not None:
            product = SERIES_TO_COINBASE.get(ss.series_ticker)
            spot = self.spot.get(product) if product else None
            if spot is None:
                log_event(
                    self._log_fp, "skip_no_spot",
                    ticker=ss.ticker, side=side.value, product=product,
                )
                return
            if not passes_strike_distance(spot, ss.strike, threshold_bps):
                bps = abs(spot - ss.strike) / spot * 10_000.0 if spot > 0 else 0.0
                log_event(
                    self._log_fp, "skip_min_strike_bps",
                    ticker=ss.ticker, side=side.value,
                    spot=spot, strike=ss.strike, bps=bps,
                    min_bps=threshold_bps, threshold_source=threshold_source,
                )
                return
        ok = await self._place_entry(ss, side, ask, trade.ts_ms)
        if not ok:
            return
        ss.side = side
        ss.fill_price = ask
        ss.fill_ts_ms = trade.ts_ms
        ss.contracts = MAX_CONTRACTS_PER_TRADE
        log_event(
            self._log_fp, "entry",
            ticker=ss.ticker, series=ss.series_ticker, side=side.value,
            fill_price=ask, fill_ts_ms=trade.ts_ms,
            trigger_yes=trade.yes_price, trigger_no=trade.no_price, slip=slip,
            strike=ss.strike,
            session_open_ms=ss.open_ms, session_close_ms=ss.close_ms,
            session_elapsed_s=(trade.ts_ms - ss.open_ms) / 1000.0,
            shadow=self.shadow,
        )

    async def _on_book_update(self, ss: SessionState, ts_ms: int) -> None:
        tick = self._book_tick(ss, ts_ms)
        if not ss.saw_below_trigger:
            yb, ya = tick.yes_bid, tick.yes_ask
            if yb is not None and ya is not None:
                yes_mid = (yb + ya) / 2.0
                if max(yes_mid, 1.0 - yes_mid) < 0.75:
                    ss.saw_below_trigger = True
        # --stop-mode gate: when not "price", never fire a stop (hold to settlement).
        if self.stop_mode != "price":
            return
        if ss.closed or ss.side is None or ss.fill_price is None:
            return
        if not detect_stop(tick, ss.side):
            return
        bid = bid_for_side(tick, ss.side)
        if bid is None or bid <= 0:
            return
        ok = await self._place_stop(ss, bid, ts_ms)
        if not ok:
            return
        ss.closed = True
        ss.exit_reason = "stop"
        ss.exit_price = bid
        ss.exit_ts_ms = ts_ms
        log_event(
            self._log_fp, "exit",
            reason="stop", ticker=ss.ticker, side=ss.side.value,
            fill_price=ss.fill_price, exit_price=bid, exit_ts_ms=ts_ms,
            shadow=self.shadow,
        )
        self._record_exit_pnl(ss, bid)

    def _on_settle(self, ss: SessionState, result: str, ts_ms: int) -> None:
        if ss.closed:
            return
        ss.closed = True
        ss.exit_ts_ms = ts_ms
        if ss.side is None:
            log_event(self._log_fp, "settle", ticker=ss.ticker, result=result, reason="no_entry")
            return
        won = result.lower() == ss.side.value
        ss.exit_reason = "settle_win" if won else "settle_loss"
        ss.exit_price = 1.0 if won else 0.0
        log_event(
            self._log_fp, "exit",
            reason=ss.exit_reason, ticker=ss.ticker, side=ss.side.value,
            fill_price=ss.fill_price, exit_price=ss.exit_price,
            result=result, shadow=self.shadow,
        )
        self._record_exit_pnl(ss, ss.exit_price)

    def _record_exit_pnl(self, ss: SessionState, exit_price: float) -> int:
        """Add this trade's realized PnL to the daily counter.

        Idempotent only within a single run; on restart, replay_today_realized_pnl
        recomputes from the JSONL.
        """
        if ss.fill_price is None:
            return 0
        contracts = max(1, ss.contracts)
        gross_cents = int(round((exit_price - ss.fill_price) * 100.0 * contracts))
        fee_cents = kalshi_entry_fee_cents(ss.fill_price, contracts)
        net_cents = gross_cents - fee_cents
        self.daily_realized_cents += net_cents
        log_event(
            self._log_fp, "pnl_recorded",
            ticker=ss.ticker, side=ss.side.value if ss.side else None,
            fill_price=ss.fill_price, exit_price=exit_price,
            gross_cents=gross_cents, entry_fee_cents=fee_cents, net_cents=net_cents,
            daily_realized_cents=self.daily_realized_cents,
        )
        if (self.daily_realized_cents <= -self.daily_loss_cap_cents
                and self.halt_reason != "DAILY_LOSS_CAP"):
            self.halt_reason = "DAILY_LOSS_CAP"
            log_event(
                self._log_fp, "halt",
                reason="DAILY_LOSS_CAP",
                realized_cents=self.daily_realized_cents,
                cap_cents=-self.daily_loss_cap_cents,
            )
        return net_cents

    def check_utc_rollover(self) -> None:
        today_floor = _utc_today_floor_ms()
        if today_floor != self.daily_loss_day_floor_ms:
            prev_realized = self.daily_realized_cents
            prev_floor = self.daily_loss_day_floor_ms
            self.daily_loss_day_floor_ms = today_floor
            self.daily_realized_cents = 0
            cleared_halt = self.halt_reason == "DAILY_LOSS_CAP"
            if cleared_halt:
                self.halt_reason = None
            log_event(
                self._log_fp, "daily_reset",
                prev_floor_ms=prev_floor, new_floor_ms=today_floor,
                prev_realized_cents=prev_realized, cleared_halt=cleared_halt,
            )

    async def _handle_message(self, payload: dict[str, Any]) -> None:
        self.last_event_ms = now_ms()
        msg_type = str(payload.get("type") or "").lower()
        msg = payload.get("msg") or payload
        market_env = msg.get("market") if isinstance(msg.get("market"), dict) else msg
        ticker = str(
            msg.get("market_ticker") or msg.get("ticker")
            or market_env.get("market_ticker") or market_env.get("ticker") or ""
        )
        if not ticker or ticker not in self.sessions:
            return
        ss = self.sessions[ticker]

        if "snapshot" in msg_type or "delta" in msg_type:
            apply_l2_payload(ss.book, payload)
            ts = msg.get("ts_ms") or msg.get("ts") or now_ms()
            try:
                ts_ms = int(ts if int(ts) > 10_000_000_000 else int(ts) * 1000)
            except Exception:
                ts_ms = now_ms()
            await self._on_book_update(ss, ts_ms)
            return

        if msg_type == "trade":
            yp = msg.get("yes_price_dollars") or msg.get("yes_price")
            np_ = msg.get("no_price_dollars") or msg.get("no_price")
            ts_raw = msg.get("ts_ms") or msg.get("ts")
            if ts_raw is None or (yp is None and np_ is None):
                return
            try:
                ts_ms = int(ts_raw if int(ts_raw) > 10_000_000_000 else int(ts_raw) * 1000)
                yp_f = float(yp) if yp is not None else None
                np_f = float(np_) if np_ is not None else None
                if yp_f is None and np_f is not None:
                    yp_f = 1.0 - np_f
                if np_f is None and yp_f is not None:
                    np_f = 1.0 - yp_f
                trade = TradeTick(ts_ms=ts_ms, yes_price=yp_f or 0.0, no_price=np_f or 0.0)
            except (TypeError, ValueError):
                return
            await self._on_trade(ss, trade)
            await self._on_book_update(ss, trade.ts_ms)
            return

        if any(token in msg_type for token in ("lifecycle", "market_status")):
            status = str(market_env.get("status") or msg.get("status") or "").lower()
            if status == "determined":
                result = str(market_env.get("result") or msg.get("result") or "").lower()
                ts = market_env.get("ts_ms") or msg.get("ts_ms") or now_ms()
                try:
                    ts_ms = int(ts if int(ts) > 10_000_000_000 else int(ts) * 1000)
                except Exception:
                    ts_ms = now_ms()
                if result in ("yes", "no"):
                    self._on_settle(ss, result, ts_ms)
            return

    async def ws_loop(self) -> None:
        while not self._stop:
            tickers = sorted(self.tracked_tickers)
            if not tickers:
                await asyncio.sleep(2.0)
                continue
            log_event(self._log_fp, "ws_subscribe", n_tickers=len(tickers), shadow=self.shadow)
            try:
                async for raw in self.ws.messages(
                    channels=["orderbook_delta", "trade", "market_lifecycle_v2"],
                    market_tickers=tickers,
                ):
                    if raw.get("type") == "connection_error":
                        log_event(self._log_fp, "ws_error", **raw)
                        break
                    try:
                        await self._handle_message(raw)
                    except Exception as exc:
                        log_event(self._log_fp, "handle_error", error=repr(exc))
                    if set(tickers) != self.tracked_tickers:
                        break
            except Exception as exc:
                log_event(self._log_fp, "ws_loop_error", error=repr(exc))
                await asyncio.sleep(2.0)

    async def watchdog_loop(self) -> None:
        while not self._stop:
            stale_s = (now_ms() - self.last_event_ms) / 1000.0
            if stale_s > self.stale_feed_timeout_s:
                log_event(self._log_fp, "stale_feed_exit", stale_s=stale_s)
                self.stale_exit = True
                self._stop = True
                return
            await asyncio.sleep(10.0)


def _read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


async def amain(args: argparse.Namespace) -> int:
    creds = _read_env_file(Path(r"D:\Trading\btc-bias-engine\credentials\kalshi.env"))
    api_key = creds.get("KALSHI_API_KEY")
    pem_path = creds.get("KALSHI_PRIVATE_KEY_PATH")
    if not api_key or not pem_path:
        print("missing Kalshi creds", file=sys.stderr)
        return 1
    # Engine-v2 settings (for WS + REST market discovery only).
    import os
    os.environ.setdefault("ENGINE_V2_KALSHI_KEY_ID", api_key)
    os.environ.setdefault("ENGINE_V2_KALSHI_PRIVATE_KEY_PATH", pem_path)
    settings = load_settings(args.config)
    rest = KalshiRestClient(settings.kalshi, live_enabled=False)  # discovery only
    ws = KalshiWebSocketClient(settings.kalshi)
    # Authenticated kalshi client for order placement.
    kalshi = KalshiClient(
        key_id=api_key,
        private_key_pem=Path(pem_path).read_text(),
        demo=False,
    )
    spot = CoinbaseSpotCache(refresh_s=args.spot_refresh_s)

    # Fix 2: load per-crypto calibrated thresholds.
    per_series_bps = load_per_crypto_thresholds(args.per_crypto_thresholds_path)

    # Fix 3: replay today's realized PnL so the cap is restart-resilient.
    initial_realized = replay_today_realized_pnl(args.decision_log)

    # Fix 1: enter KalshiClient as async context manager so place_order works.
    async with KalshiClient(
        key_id=api_key,
        private_key_pem=Path(pem_path).read_text(),
        demo=False,
    ) as kalshi:
        engine = LiveFavoriteChaseEngine(
            rest=rest,
            ws=ws,
            kalshi=kalshi,
            spot=spot,
            decision_log=args.decision_log,
            shadow=args.shadow,
            max_entry_price=args.max_entry_price,
            max_slip=args.max_slip,
            per_series_bps=per_series_bps,
            default_min_bps=args.min_strike_bps,
            daily_loss_cap_cents=DAILY_LOSS_CAP_CENTS,
            stop_mode=args.stop_mode,
            bps_gate=args.bps_gate,
            discover_interval_s=args.discovery_interval_s,
            stale_feed_timeout_s=args.stale_feed_timeout_s,
        )
        # Seed daily counter from JSONL replay.
        engine.daily_realized_cents = initial_realized
        if initial_realized <= -engine.daily_loss_cap_cents:
            engine.halt_reason = "DAILY_LOSS_CAP"

        log_event(
            engine._log_fp, "boot",
            shadow=args.shadow,
            max_entry_price=args.max_entry_price,
            max_slip=args.max_slip,
            default_min_strike_bps=args.min_strike_bps,
            per_series_bps=per_series_bps,
            per_series_count=len(per_series_bps),
            daily_loss_cap_cents=DAILY_LOSS_CAP_CENTS,
            stop_mode=args.stop_mode,
            bps_gate=args.bps_gate,
            initial_realized_cents=initial_realized,
            initial_halt_reason=engine.halt_reason,
            ws_url=settings.kalshi.ws_url,
            rest_base_url=settings.kalshi.rest_base_url,
            live_authorization="user explicit 2026-05-21",
            patch="kalshi_ctx_fix + per_crypto_bps + daily_cap_10usd 2026-05-21",
        )

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, engine.request_stop)
            except NotImplementedError:
                pass

        spot_task = asyncio.create_task(spot.poll_loop(engine._log_fp), name="spot")
        discovery = asyncio.create_task(engine.discovery_loop(), name="discovery")
        ws_task = asyncio.create_task(engine.ws_loop(), name="ws")
        watchdog = asyncio.create_task(engine.watchdog_loop(), name="watchdog")

        try:
            while not engine._stop:
                engine.check_utc_rollover()
                await asyncio.sleep(1.0)
        finally:
            engine._stop = True
            for t in (spot_task, discovery, ws_task, watchdog):
                t.cancel()
            await asyncio.gather(spot_task, discovery, ws_task, watchdog, return_exceptions=True)
            engine.close()
    return 2 if engine.stale_exit else 0


def main() -> int:
    p = argparse.ArgumentParser(prog="live-favorite-chase")
    p.add_argument(
        "--decision-log", type=Path,
        default=Path(r"C:\Trading\kalshi-btc-engine-v2\data\live_favorite_chase.jsonl"),
    )
    p.add_argument("--config", type=Path, default=None)
    p.add_argument(
        "--shadow", action="store_true",
        help="Log decisions only; do NOT place real orders.",
    )
    p.add_argument(
        "--max-entry-price", type=float, default=0.90,
        help="Skip if next ask > this. Default 0.90.",
    )
    p.add_argument(
        "--max-slip", type=float, default=0.10,
        help="Skip if (ask - trigger_trade_price) > this. Default 0.10.",
    )
    p.add_argument(
        "--min-strike-bps", type=float, default=MIN_STRIKE_DISTANCE_BPS,
        help="Fallback bps threshold for series not in per_crypto_thresholds.json. Default 4.0.",
    )
    p.add_argument(
        "--per-crypto-thresholds-path", type=Path,
        default=PER_CRYPTO_THRESHOLDS_PATH,
        help="JSON file with per-series proposed_threshold_bps. Loaded at startup.",
    )
    p.add_argument(
        "--stop-mode", choices=("price", "none"), default="none",
        help="price = stop-loss fires when held-side mid <= 0.50; "
             "none = stops OFF, hold to settlement (default).",
    )
    p.add_argument(
        "--bps-gate", choices=("enabled", "disabled"), default="disabled",
        help="enabled = skip entries with |spot-strike| below the per-crypto "
             "threshold; disabled = gate OFF (default).",
    )
    p.add_argument("--discovery-interval-s", type=float, default=30.0)
    p.add_argument("--stale-feed-timeout-s", type=float, default=600.0)
    p.add_argument("--spot-refresh-s", type=float, default=2.0)
    args = p.parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
