"""Paper-forward runner for the favorite-chase strategy.

Subscribes to all open Kalshi crypto markets via WebSocket. For each market
session, applies the favorite-chase rule (see strategies/favorite_chase.py):
  - After T+8min, the first trade at >=75c on either side triggers an entry.
  - Fill = next observed ask on the entered side (taker).
  - Exit: mid <=50c stop, else hold to settle.
  - 1 contract per market.

This process runs alongside any existing services. It DOES NOT touch the
capture service's database. Decisions are written to a dedicated JSONL log.

Paper-only. Never sends orders. Authorisation flag for live trading is not
present in this code path.

Crypto series are discovered automatically by polling /markets?status=open
and filtering on series_ticker prefix.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from kalshi_btc_engine_v2.adapters.kalshi import (  # noqa: E402
    KalshiRestClient,
    KalshiWebSocketClient,
    apply_l2_payload,
    extract_orderbook_levels,
)
from kalshi_btc_engine_v2.cli import _resolve_kalshi_creds_into_env  # noqa: E402
from kalshi_btc_engine_v2.config import load_settings  # noqa: E402
from kalshi_btc_engine_v2.core.orderbook import KalshiOrderBook  # noqa: E402
from kalshi_btc_engine_v2.core.time import parse_rfc3339_ms, utc_now_ms  # noqa: E402
from kalshi_btc_engine_v2.policy.edge import kalshi_taker_fee_cents  # noqa: E402
from kalshi_btc_engine_v2.strategies.favorite_chase import (  # noqa: E402
    ENTRY_AFTER_MS,
    STOP_PRICE,
    BookTick,
    Side,
    TradeTick,
    ask_for_side,
    bid_for_side,
    detect_entry,
    detect_stop,
)

# Series prefixes we consider crypto. The auto-discovery filter matches a
# series_ticker against this list (prefix). Adding a new symbol is one line.
CRYPTO_SERIES_PREFIXES = (
    "KXBTC",
    "KXETH",
    "KXSOL",
    "KXXRP",
    "KXDOGE",
    "KXADA",
    "KXBNB",
    "KXAVAX",
    "KXSUI",
    "KXLINK",
    "KXLTC",
    "KXTRX",
    "KXMATIC",
    "KXTON",
    "KXDOT",
    "KXSHIB",
    "KXNEAR",
    "KXAPT",
    "KXARB",
    "KXOP",
    "KXPEPE",
    "KXWLD",
    "KXBONK",
    "KXICP",
    "KXATOM",
    "KXHBAR",
    "KXFIL",
    "KXSTX",
    "KXKAS",
)


def is_crypto_series(series_ticker: str) -> bool:
    return any(series_ticker.startswith(p) for p in CRYPTO_SERIES_PREFIXES)


@dataclass
class SessionState:
    ticker: str
    series_ticker: str
    open_ms: int
    close_ms: int
    book: KalshiOrderBook = field(default_factory=lambda: KalshiOrderBook(""))
    # We require evidence the market was BELOW the trigger before crossing,
    # otherwise late-subscription on an already-high contract spuriously fires.
    saw_below_trigger: bool = False
    # Strategy state
    side: Side | None = None             # entered side (None until trigger)
    triggered_at_ms: int | None = None   # trigger trade ts
    fill_price: float | None = None      # ask we filled at (dollars)
    fill_ts_ms: int | None = None
    closed: bool = False                 # exit fired (stop or settle)
    exit_reason: str | None = None
    exit_price: float | None = None
    exit_ts_ms: int | None = None


def now_ms() -> int:
    return int(time.time() * 1000)


def log_event(fp, kind: str, **fields: Any) -> None:
    fields["kind"] = kind
    fields["log_ts_ms"] = now_ms()
    fp.write(json.dumps(fields, default=str) + "\n")
    fp.flush()


def market_series(m: dict[str, Any]) -> str:
    """Best-effort series_ticker for a Kalshi market dict.

    `series_ticker` may be absent on /markets responses; in that case derive
    from the ticker (prefix before the first dash, e.g. KXBTC15M-... → KXBTC15M).
    """
    raw = m.get("series_ticker")
    if raw:
        return str(raw)
    ticker = str(m.get("ticker") or "")
    if "-" in ticker:
        return ticker.split("-", 1)[0]
    return ticker


async def discover_markets(
    rest: KalshiRestClient, throttle_s: float = 0.25
) -> list[dict[str, Any]]:
    """Fetch all open crypto markets.

    Loops over CRYPTO_SERIES_PREFIXES and queries `/markets?series_ticker=...`
    for each. Throttles to stay under Kalshi's per-second rate limit.
    """
    out: list[dict[str, Any]] = []
    for series in CRYPTO_SERIES_PREFIXES:
        cursor: str | None = None
        for _ in range(5):  # cap per-series pages
            params: dict[str, Any] = {"series_ticker": series, "status": "open", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            try:
                resp = await rest.request("GET", "/markets", params=params)
            except Exception:
                break
            markets = resp.get("markets", [])
            for m in markets:
                out.append(m)
            cursor = resp.get("cursor") or None
            await asyncio.sleep(throttle_s)
            if not cursor:
                break
    return out


def parse_trade_msg(msg: dict[str, Any]) -> TradeTick | None:
    """Convert a Kalshi WS trade message to a TradeTick. Returns None if malformed."""
    yp = msg.get("yes_price_dollars") or msg.get("yes_price")
    np_ = msg.get("no_price_dollars") or msg.get("no_price")
    ts_raw = msg.get("ts_ms") or msg.get("ts")
    if ts_raw is None or (yp is None and np_ is None):
        return None
    try:
        ts_ms = int(ts_raw if int(ts_raw) > 10_000_000_000 else int(ts_raw) * 1000)
        yp_f = float(yp) if yp is not None else None
        np_f = float(np_) if np_ is not None else None
        if yp_f is None and np_f is not None:
            yp_f = 1.0 - np_f
        if np_f is None and yp_f is not None:
            np_f = 1.0 - yp_f
        return TradeTick(ts_ms=ts_ms, yes_price=yp_f or 0.0, no_price=np_f or 0.0)
    except (TypeError, ValueError):
        return None


def book_tick_from_book(book: KalshiOrderBook, ts_ms: int) -> BookTick:
    yb = book.best_yes_bid
    ya = book.best_yes_ask
    return BookTick(
        ts_ms=ts_ms,
        yes_bid=float(yb) if yb is not None else None,
        yes_ask=float(ya) if ya is not None else None,
    )


def settle_pnl(side: Side, fill_price: float, won: bool) -> tuple[int, int, int, int]:
    """Returns (gross_cents, entry_fee_cents, exit_fee_cents, net_cents) for a held-to-settle outcome."""
    entry_cents = int(round(fill_price * 100))
    exit_cents = 100 if won else 0
    gross = exit_cents - entry_cents
    entry_fee = kalshi_taker_fee_cents(entry_cents, count=1)
    exit_fee = 0  # settlement is passive
    return gross, entry_fee, exit_fee, gross - entry_fee - exit_fee


def stop_pnl(fill_price: float, bid_price: float) -> tuple[int, int, int, int]:
    entry_cents = int(round(fill_price * 100))
    exit_cents = int(round(bid_price * 100))
    gross = exit_cents - entry_cents
    entry_fee = kalshi_taker_fee_cents(entry_cents, count=1)
    exit_fee = kalshi_taker_fee_cents(exit_cents, count=1)
    return gross, entry_fee, exit_fee, gross - entry_fee - exit_fee


class FavoriteChasePaperEngine:
    def __init__(
        self,
        rest: KalshiRestClient,
        ws: KalshiWebSocketClient,
        decision_log: Path,
        discover_interval_s: float = 30.0,
        stale_feed_timeout_s: float = 600.0,
        side_filter: str = "all",
        max_entry_price: float = 1.01,
        max_slip: float = 1.0,
    ) -> None:
        self.rest = rest
        self.ws = ws
        self.decision_log = decision_log
        self.discover_interval_s = discover_interval_s
        self.stale_feed_timeout_s = stale_feed_timeout_s
        self.side_filter = side_filter         # "all" | "yes" | "no"
        self.max_entry_price = max_entry_price # skip fills above this ask
        self.max_slip = max_slip               # skip if (fill - trigger) > this
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
        open_ms = parse_rfc3339_ms(m.get("open_time") or "") or 0
        close_ms = parse_rfc3339_ms(m.get("close_time") or "") or 0
        if not open_ms or not close_ms:
            return None
        series = market_series(m)
        ss = SessionState(
            ticker=ticker,
            series_ticker=series,
            open_ms=open_ms,
            close_ms=close_ms,
            book=KalshiOrderBook(ticker),
        )
        self.sessions[ticker] = ss
        self.tracked_tickers.add(ticker)
        log_event(
            self._log_fp,
            "discover",
            ticker=ticker,
            series=series,
            open_ms=open_ms,
            close_ms=close_ms,
        )
        return ss

    def _retire_settled(self) -> None:
        """Drop closed sessions whose close_time is more than 5 minutes old."""
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
                    ss = self._register_market(m)
                    if ss is not None and ss.ticker not in self.tracked_tickers:
                        new += 1
                self._retire_settled()
                log_event(
                    self._log_fp,
                    "discovery",
                    tracked=len(self.sessions),
                    seen=len(markets),
                    new=new,
                )
            except Exception as exc:
                log_event(self._log_fp, "discovery_error", error=repr(exc))
            await asyncio.sleep(self.discover_interval_s)

    def _on_trade(self, ss: SessionState, trade: TradeTick) -> None:
        if ss.closed or ss.side is not None:
            return
        # Track whether we've seen this market below 0.75 on both sides.
        if max(trade.yes_price, trade.no_price) < 0.75:
            ss.saw_below_trigger = True
        side = detect_entry(trade, ss.open_ms)
        if side is None:
            return
        if not ss.saw_below_trigger:
            # Subscribed mid-session to a market already at >=75c on one side.
            # We cannot claim this is the "first to hit 75c". Skip.
            return
        # Side filter (data-supported in BTC: NO is +EV, YES bleeds in-sample).
        if self.side_filter != "all" and side.value != self.side_filter:
            log_event(
                self._log_fp,
                "skip_side_filter",
                ticker=ss.ticker,
                side=side.value,
                side_filter=self.side_filter,
            )
            return
        # Fill at the current ask on the entered side (next available quote).
        tick = book_tick_from_book(ss.book, trade.ts_ms)
        ask = ask_for_side(tick, side)
        if ask is None or ask <= 0:
            log_event(
                self._log_fp,
                "trigger_no_book",
                ticker=ss.ticker,
                side=side.value,
                trade_yes=trade.yes_price,
                trade_no=trade.no_price,
                trade_ts_ms=trade.ts_ms,
            )
            return
        # max-entry-price cap: refuse very-late fills (no reward room).
        if ask > self.max_entry_price:
            log_event(
                self._log_fp,
                "skip_max_price",
                ticker=ss.ticker,
                side=side.value,
                ask=ask,
                max_entry_price=self.max_entry_price,
            )
            return
        # max-slippage cap: refuse if ask moved too far from trigger.
        trigger_price = trade.yes_price if side is Side.YES else trade.no_price
        if ask - trigger_price > self.max_slip:
            log_event(
                self._log_fp,
                "skip_max_slip",
                ticker=ss.ticker,
                side=side.value,
                ask=ask,
                trigger=trigger_price,
                slip=ask - trigger_price,
                max_slip=self.max_slip,
            )
            return
        ss.side = side
        ss.triggered_at_ms = trade.ts_ms
        ss.fill_price = ask
        ss.fill_ts_ms = trade.ts_ms
        log_event(
            self._log_fp,
            "entry",
            ticker=ss.ticker,
            series=ss.series_ticker,
            side=side.value,
            fill_price=ask,
            fill_ts_ms=trade.ts_ms,
            trigger_yes=trade.yes_price,
            trigger_no=trade.no_price,
            slip=ask - trigger_price,
            session_open_ms=ss.open_ms,
            session_close_ms=ss.close_ms,
            session_elapsed_s=(trade.ts_ms - ss.open_ms) / 1000.0,
        )

    def _on_book_update(self, ss: SessionState, ts_ms: int) -> None:
        tick = book_tick_from_book(ss.book, ts_ms)
        # Update saw_below_trigger from the book itself so book-only markets
        # (no recent trades) still qualify once they cross.
        if not ss.saw_below_trigger:
            yb = tick.yes_bid
            ya = tick.yes_ask
            if yb is not None and ya is not None:
                yes_mid = (yb + ya) / 2.0
                no_mid = 1.0 - yes_mid
                if max(yes_mid, no_mid) < 0.75:
                    ss.saw_below_trigger = True
        if ss.closed or ss.side is None or ss.fill_price is None:
            return
        if not detect_stop(tick, ss.side):
            return
        bid = bid_for_side(tick, ss.side)
        if bid is None or bid <= 0:
            return
        ss.closed = True
        ss.exit_reason = "stop"
        ss.exit_price = bid
        ss.exit_ts_ms = ts_ms
        gross, ef, xf, net = stop_pnl(ss.fill_price, bid)
        log_event(
            self._log_fp,
            "exit",
            reason="stop",
            ticker=ss.ticker,
            side=ss.side.value,
            fill_price=ss.fill_price,
            exit_price=bid,
            exit_ts_ms=ts_ms,
            gross_cents=gross,
            entry_fee_cents=ef,
            exit_fee_cents=xf,
            net_cents=net,
        )

    def _on_settle(self, ss: SessionState, result: str, ts_ms: int) -> None:
        if ss.closed:
            return
        ss.closed = True
        ss.exit_ts_ms = ts_ms
        if ss.side is None or ss.fill_price is None:
            ss.exit_reason = "settle_no_entry"
            ss.exit_price = None
            log_event(
                self._log_fp,
                "settle",
                ticker=ss.ticker,
                result=result,
                reason="no_entry",
                ts_ms=ts_ms,
            )
            return
        won = result.lower() == ss.side.value
        ss.exit_reason = "settle_win" if won else "settle_loss"
        ss.exit_price = 1.0 if won else 0.0
        gross, ef, xf, net = settle_pnl(ss.side, ss.fill_price, won)
        log_event(
            self._log_fp,
            "exit",
            reason=ss.exit_reason,
            ticker=ss.ticker,
            side=ss.side.value,
            fill_price=ss.fill_price,
            exit_price=ss.exit_price,
            exit_ts_ms=ts_ms,
            result=result,
            gross_cents=gross,
            entry_fee_cents=ef,
            exit_fee_cents=xf,
            net_cents=net,
        )

    def _handle_message(self, payload: dict[str, Any]) -> None:
        self.last_event_ms = now_ms()
        msg_type = str(payload.get("type") or "").lower()
        msg = payload.get("msg") or payload
        # For lifecycle_v2 the meaningful fields live under msg.market.
        market_envelope = msg.get("market") if isinstance(msg.get("market"), dict) else msg
        ticker = str(
            msg.get("market_ticker")
            or msg.get("ticker")
            or market_envelope.get("market_ticker")
            or market_envelope.get("ticker")
            or ""
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
            self._on_book_update(ss, ts_ms)
            return

        if msg_type == "trade":
            trade = parse_trade_msg(msg)
            if trade is not None:
                self._on_trade(ss, trade)
                # A trade also implies a print at the same instant; check stop.
                self._on_book_update(ss, trade.ts_ms)
            return

        # Accept any lifecycle-flavoured message (market_lifecycle, market_lifecycle_v2,
        # market_status, …). The capture service uses the same matcher.
        if any(token in msg_type for token in ("lifecycle", "market_status")):
            status = str(market_envelope.get("status") or msg.get("status") or "").lower()
            if status == "determined":
                result = str(market_envelope.get("result") or msg.get("result") or "").lower()
                ts = market_envelope.get("ts_ms") or msg.get("ts_ms") or now_ms()
                try:
                    ts_ms = int(ts if int(ts) > 10_000_000_000 else int(ts) * 1000)
                except Exception:
                    ts_ms = now_ms()
                if result in ("yes", "no"):
                    self._on_settle(ss, result, ts_ms)
            return

    async def ws_loop(self) -> None:
        """Maintain a WS subscription. Re-subscribes if tracked_tickers changes."""
        while not self._stop:
            # Snapshot current tickers; if empty wait briefly for discovery.
            tickers = sorted(self.tracked_tickers)
            if not tickers:
                await asyncio.sleep(2.0)
                continue
            log_event(self._log_fp, "ws_subscribe", n_tickers=len(tickers))
            try:
                async for raw in self.ws.messages(
                    channels=["orderbook_delta", "trade", "market_lifecycle_v2"],
                    market_tickers=tickers,
                ):
                    if raw.get("type") == "connection_error":
                        log_event(self._log_fp, "ws_error", **raw)
                        break
                    try:
                        self._handle_message(raw)
                    except Exception as exc:
                        log_event(self._log_fp, "handle_error", error=repr(exc))
                    # Check if subscription set has changed — break to re-subscribe.
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


async def amain(args: argparse.Namespace) -> int:
    _resolve_kalshi_creds_into_env()
    settings = load_settings(args.config)
    rest = KalshiRestClient(settings.kalshi, live_enabled=False)
    ws = KalshiWebSocketClient(settings.kalshi)
    engine = FavoriteChasePaperEngine(
        rest=rest,
        ws=ws,
        decision_log=args.decision_log,
        discover_interval_s=args.discovery_interval_s,
        stale_feed_timeout_s=args.stale_feed_timeout_s,
        side_filter=args.side_filter,
        max_entry_price=args.max_entry_price,
        max_slip=args.max_slip,
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, engine.request_stop)
        except NotImplementedError:
            # Windows: signal handlers not supported.
            pass

    log_event(
        engine._log_fp,
        "boot",
        decision_log=str(args.decision_log),
        ws_url=settings.kalshi.ws_url,
        rest_base_url=settings.kalshi.rest_base_url,
    )

    discovery = asyncio.create_task(engine.discovery_loop(), name="discovery")
    ws_task = asyncio.create_task(engine.ws_loop(), name="ws")
    watchdog = asyncio.create_task(engine.watchdog_loop(), name="watchdog")

    try:
        while not engine._stop:
            await asyncio.sleep(1.0)
    finally:
        engine._stop = True
        for t in (discovery, ws_task, watchdog):
            t.cancel()
        await asyncio.gather(discovery, ws_task, watchdog, return_exceptions=True)
        engine.close()
    # Exit code 2 on stale feed so watchdog wrappers re-launch us.
    return 2 if engine.stale_exit else 0


def main() -> int:
    p = argparse.ArgumentParser(prog="paper-favorite-chase")
    p.add_argument(
        "--decision-log",
        type=Path,
        default=Path(r"C:\Trading\kalshi-btc-engine-v2\data\paper_favorite_chase.jsonl"),
    )
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("--discovery-interval-s", type=float, default=30.0)
    p.add_argument("--stale-feed-timeout-s", type=float, default=600.0)
    p.add_argument(
        "--side-filter",
        choices=["all", "yes", "no"],
        default="no",
        help="Trade only this side. Default 'no' — BTC backtest shows NO is +EV, "
        "YES bleeds. Change to 'all' or 'yes' to override.",
    )
    p.add_argument(
        "--max-entry-price",
        type=float,
        default=0.90,
        help="Skip the trigger if the next ask is above this. Caps the 'lose 99 to "
        "win 1' high-price fills. Default 0.90 (best avg/trade in BTC sweep).",
    )
    p.add_argument(
        "--max-slip",
        type=float,
        default=0.10,
        help="Skip if (fill_ask - trigger_trade_price) exceeds this. Caps fills "
        "that jumped way past where the trade printed. Default 0.10.",
    )
    args = p.parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
