from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import sqlite3
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kalshi_btc_engine_v2.adapters.kalshi import (
    KalshiRestClient,
    KalshiWebSocketClient,
    apply_l2_payload,
    l2_event_to_record,
)
from kalshi_btc_engine_v2.adapters.spot import (
    BitstampTickerPoller,
    CoinbaseTickerFeed,
    KrakenTickerFeed,
    fuse_spot_quotes,
    quote_to_record,
)
from kalshi_btc_engine_v2.config import Settings, load_settings
from kalshi_btc_engine_v2.core.decimal import decimal_from_fixed, decimal_to_str
from kalshi_btc_engine_v2.core.events import SpotQuote
from kalshi_btc_engine_v2.core.orderbook import KalshiOrderBook
from kalshi_btc_engine_v2.core.time import parse_rfc3339_ms, utc_now_ms
from kalshi_btc_engine_v2.monitoring.continuity import analyze_kalshi_l2_rows
from kalshi_btc_engine_v2.storage.sqlite import connect, init_db, insert_record, upsert_market

HEALTH_KINDS = {"reconnect", "staleness_breach", "quorum_loss", "quorum_regained", "heartbeat"}
PUBLIC_KALSHI_CHANNELS = ["orderbook_delta", "trade"]
LIFECYCLE_KALSHI_CHANNELS = ["market_lifecycle_v2"]
PRIVATE_KALSHI_CHANNELS = ["user_orders", "fill", "market_positions"]
CLOSING_LIFECYCLE_EVENTS = {"closed", "deactivated", "determined", "settled", "finalized"}


@dataclass(frozen=True, slots=True)
class BurnInConfig:
    db_path: Path
    hours: float
    market_ticker: str | None = None
    bitstamp_poll_interval_s: float = 1.0
    commit_events: int = 500
    commit_interval_s: float = 2.0
    heartbeat_interval_s: float = 60.0
    staleness_check_interval_s: float = 1.0


@dataclass(frozen=True, slots=True)
class CaptureItem:
    source: str
    table: str
    record: dict[str, Any]
    count_as_message: bool = True


@dataclass(slots=True)
class BurnInReport:
    runtime_seconds: float
    messages_per_second_by_source: dict[str, float]
    row_counts_by_source: dict[str, int]
    kalshi_sequence_gaps: int
    kalshi_duplicate_sequences: int
    reconnect_count: int
    staleness_breach_count: int
    quorum_loss_count: int
    quorum_regained_count: int
    max_spot_staleness_ms: int
    spot_fusion_quorum_coverage: float
    rollover_count: int
    market_tickers_captured: list[str]

    def console_text(self) -> str:
        mps_json = json.dumps(self.messages_per_second_by_source, sort_keys=True)
        return "\n".join(
            [
                "capture burn-in continuity report",
                f"runtime_seconds={self.runtime_seconds:.3f}",
                f"messages_per_second_by_source={mps_json}",
                f"row_counts_by_source={json.dumps(self.row_counts_by_source, sort_keys=True)}",
                f"kalshi_sequence_gaps={self.kalshi_sequence_gaps}",
                f"kalshi_duplicate_sequences={self.kalshi_duplicate_sequences}",
                f"reconnect_count={self.reconnect_count}",
                f"staleness_breach_count={self.staleness_breach_count}",
                f"quorum_loss_count={self.quorum_loss_count}",
                f"quorum_regained_count={self.quorum_regained_count}",
                f"max_spot_staleness_ms={self.max_spot_staleness_ms}",
                f"spot_fusion_quorum_coverage={self.spot_fusion_quorum_coverage:.6f}",
                f"rollover_count={self.rollover_count}",
                f"market_tickers_captured={','.join(self.market_tickers_captured)}",
            ]
        )


@dataclass(slots=True)
class _Stats:
    started_monotonic: float = field(default_factory=time.monotonic)
    source_counts: Counter[str] = field(default_factory=Counter)
    row_counts: Counter[str] = field(default_factory=Counter)
    health_counts: Counter[str] = field(default_factory=Counter)
    market_tickers: set[str] = field(default_factory=set)
    rollover_count: int = 0
    last_quotes: dict[str, SpotQuote] = field(default_factory=dict)
    max_spot_staleness_ms: int = 0
    spot_tick_count: int = 0
    spot_quorum_tick_count: int = 0
    spot_quorum_ok: bool = True


class BurnInRunner:
    def __init__(
        self,
        config: BurnInConfig,
        *,
        settings: Settings | None = None,
        rest_client: Any | None = None,
        ws_client: Any | None = None,
        coinbase_feed: Any | None = None,
        kraken_feed: Any | None = None,
        bitstamp_feed: Any | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        print_line: Callable[[str], None] = print,
        on_commit: Callable[[], None] | None = None,
    ) -> None:
        self.config = config
        self.settings = settings or load_settings()
        self.rest_client = rest_client or KalshiRestClient(self.settings.kalshi, live_enabled=False)
        self.ws_client = ws_client or KalshiWebSocketClient(self.settings.kalshi)
        self.coinbase_feed = coinbase_feed or CoinbaseTickerFeed()
        self.kraken_feed = kraken_feed or KrakenTickerFeed()
        self.bitstamp_feed = bitstamp_feed or BitstampTickerPoller()
        self.sleep = sleep
        self.monotonic = monotonic
        self.print_line = print_line
        self.on_commit = on_commit
        self.queue: asyncio.Queue[CaptureItem] = asyncio.Queue()
        self.stop_event = asyncio.Event()
        self.market_changed_event = asyncio.Event()
        self.current_market_ticker: str | None = config.market_ticker
        self.stats = _Stats(started_monotonic=self.monotonic())

    async def run(self) -> BurnInReport:
        init_db(self.config.db_path)
        end_at = self.monotonic() + (self.config.hours * 3600)
        conn = connect(self.config.db_path)
        pending = 0
        last_commit = self.monotonic()
        tasks: list[asyncio.Task[None]] = []
        self._install_signal_handlers()
        try:
            self.current_market_ticker = await self._initial_market_ticker(conn)
            public_private = "enabled" if self.settings.kalshi.key_id else "disabled"
            if not self.settings.kalshi.key_id:
                self.print_line("kalshi private streams disabled; public-data-only capture")
            self.print_line(
                f"capture burn-in started market={self.current_market_ticker} "
                f"private_streams={public_private}"
            )
            tasks = [
                asyncio.create_task(self._kalshi_loop(), name="capture-kalshi"),
                asyncio.create_task(self._kalshi_lifecycle_loop(), name="capture-kalshi-lifecycle"),
                asyncio.create_task(self._spot_loop("coinbase", self.coinbase_feed.messages())),
                asyncio.create_task(self._spot_loop("kraken", self.kraken_feed.messages())),
                asyncio.create_task(
                    self._spot_loop(
                        "bitstamp",
                        self.bitstamp_feed.messages(
                            interval_s=self.config.bitstamp_poll_interval_s
                        ),
                    )
                ),
                asyncio.create_task(self._heartbeat_loop(), name="capture-heartbeat"),
                asyncio.create_task(self._staleness_loop(), name="capture-staleness"),
            ]
            while self.monotonic() < end_at and not self.stop_event.is_set():
                timeout = max(0.0, min(0.25, end_at - self.monotonic()))
                try:
                    item = await asyncio.wait_for(self.queue.get(), timeout=timeout)
                except TimeoutError:
                    item = None
                if item is not None:
                    insert_record(conn, item.table, item.record)
                    self.stats.row_counts[item.source] += 1
                    if item.count_as_message:
                        self.stats.source_counts[item.source] += 1
                    pending += 1
                now = self.monotonic()
                if pending and (
                    pending >= self.config.commit_events
                    or now - last_commit >= self.config.commit_interval_s
                ):
                    conn.commit()
                    if self.on_commit is not None:
                        self.on_commit()
                    pending = 0
                    last_commit = now
            self.stop_event.set()
        except (asyncio.CancelledError, KeyboardInterrupt):
            self.stop_event.set()
            raise
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            conn.commit()
            if self.on_commit is not None:
                self.on_commit()
            conn.close()
        report = self._build_report()
        self.print_line(report.console_text())
        return report

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
            if sig is None:
                continue
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(sig, self.stop_event.set)

    async def _initial_market_ticker(self, conn: sqlite3.Connection) -> str:
        if self.config.market_ticker:
            market = await self._get_market(self.config.market_ticker)
            if market:
                upsert_market(conn, self._market_record(market))
                conn.commit()
            self.stats.market_tickers.add(self.config.market_ticker)
            return self.config.market_ticker
        ticker = await self._discover_open_market(conn, exclude=set())
        if ticker is None:
            raise RuntimeError("no active KXBTC15M market found")
        return ticker

    async def _discover_open_market(
        self, conn: sqlite3.Connection | None, *, exclude: set[str]
    ) -> str | None:
        payload = await self.rest_client.get_markets(
            series_ticker=self.settings.kalshi.series_ticker,
            status="open",
        )
        markets = payload.get("markets") or payload.get("market") or []
        if isinstance(markets, dict):
            markets = [markets]
        candidates = [m for m in markets if str(m.get("ticker") or "") not in exclude]
        candidates.sort(
            key=lambda item: str(item.get("close_time") or item.get("expiration_time") or "")
        )
        if not candidates:
            return None
        market = candidates[0]
        ticker = str(market["ticker"])
        if conn is not None:
            upsert_market(conn, self._market_record(market))
            conn.commit()
        self.stats.market_tickers.add(ticker)
        return ticker

    async def _get_market(self, ticker: str) -> dict[str, Any] | None:
        try:
            payload = await self.rest_client.get_market(ticker)
        except Exception:
            return None
        market = payload.get("market") if isinstance(payload, dict) else None
        return market if isinstance(market, dict) else payload

    def _market_record(self, market: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_ms()
        return {
            "ticker": market.get("ticker"),
            "series_ticker": market.get("series_ticker") or self.settings.kalshi.series_ticker,
            "event_ticker": market.get("event_ticker"),
            "market_type": market.get("market_type"),
            "title": market.get("title"),
            "open_time": market.get("open_time"),
            "close_time": market.get("close_time"),
            "expiration_time": market.get("expiration_time"),
            "settlement_source": market.get("settlement_source"),
            "status": market.get("status"),
            "fee_type": market.get("fee_type"),
            "fee_multiplier": (
                str(market.get("fee_multiplier"))
                if market.get("fee_multiplier") is not None
                else None
            ),
            "price_level_structure_json": json.dumps(
                market.get("price_level_structure") or {}, separators=(",", ":")
            ),
            "raw_json": json.dumps(market, separators=(",", ":")),
            "created_at_ms": now,
            "updated_at_ms": now,
        }

    async def _kalshi_loop(self) -> None:
        last_discovery_attempt_ms = 0
        while not self.stop_event.is_set():
            ticker = self.current_market_ticker
            if ticker is None:
                # No active market — periodically re-attempt discovery. Without
                # this the runner would sit idle forever after a failed rollover.
                now_ms = utc_now_ms()
                if now_ms - last_discovery_attempt_ms >= 15_000:
                    last_discovery_attempt_ms = now_ms
                    try:
                        discovered = await self._discover_open_market(None, exclude=set())
                    except Exception as exc:  # noqa: BLE001
                        await self._health(
                            "kalshi", "reconnect", {"reason": "discovery_error", "error": repr(exc)}
                        )
                        discovered = None
                    if discovered is not None:
                        self.current_market_ticker = discovered
                        market = await self._get_market(discovered)
                        if market is not None:
                            await self.queue.put(
                                CaptureItem("market_dim", "market_dim", self._market_record(market))
                            )
                        self.print_line(f"kalshi discovery picked up next market={discovered}")
                await self.sleep(2.0)
                continue
            channels = list(PUBLIC_KALSHI_CHANNELS)
            if self.settings.kalshi.key_id:
                channels.extend(PRIVATE_KALSHI_CHANNELS)
            book = KalshiOrderBook(ticker)
            stream = self.ws_client.messages(channels=channels, market_tickers=[ticker])
            while not self.stop_event.is_set():
                payload = await self._next_market_payload(stream)
                if payload is None:
                    break
                if payload.get("type") == "connection_error":
                    await self._health("kalshi", "reconnect", payload)
                    continue
                rolled = await self._handle_kalshi_payload(payload, ticker, book)
                if rolled:
                    break

    async def _kalshi_lifecycle_loop(self) -> None:
        async for payload in self.ws_client.messages(
            channels=LIFECYCLE_KALSHI_CHANNELS,
            market_tickers=None,
        ):
            if self.stop_event.is_set():
                break
            if payload.get("type") == "connection_error":
                await self._health("kalshi_lifecycle", "reconnect", payload)
                continue
            ticker = self.current_market_ticker
            if ticker is None:
                continue
            await self._handle_kalshi_payload(payload, ticker, KalshiOrderBook(ticker))

    async def _handle_kalshi_payload(
        self, payload: dict[str, Any], ticker: str, book: KalshiOrderBook
    ) -> bool:
        l2_event = apply_l2_payload(book, payload)
        if l2_event is not None:
            await self.queue.put(
                CaptureItem("kalshi_l2", "kalshi_l2_event", l2_event_to_record(l2_event))
            )
            return False
        msg = payload.get("msg") or payload
        event_type = str(payload.get("type") or msg.get("type") or "").lower()
        if "trade" in event_type:
            await self.queue.put(
                CaptureItem(
                    "kalshi_trade", "kalshi_trade_event", self._trade_record(payload, ticker)
                )
            )
            return False
        if event_type == "fill":
            await self.queue.put(
                CaptureItem("kalshi_fill", "kalshi_fill_event", self._fill_record(payload, ticker))
            )
            return False
        if event_type == "user_order":
            await self.queue.put(
                CaptureItem(
                    "kalshi_user_order",
                    "kalshi_user_order_event",
                    self._user_order_record(payload, ticker),
                )
            )
            return False
        if event_type == "market_position":
            await self.queue.put(
                CaptureItem(
                    "kalshi_position",
                    "kalshi_position_event",
                    self._position_record(payload, ticker),
                )
            )
            return False
        if any(token in event_type for token in ("lifecycle", "market_status", "market")):
            record = self._lifecycle_record(payload, ticker)
            await self.queue.put(CaptureItem("kalshi_lifecycle", "kalshi_lifecycle_event", record))
            status = str(record.get("status") or "").lower()
            if record.get("market_ticker") == ticker and status in CLOSING_LIFECYCLE_EVENTS:
                await self._rollover(ticker, payload)
                return True
        return False

    def _trade_record(self, payload: dict[str, Any], fallback_ticker: str) -> dict[str, Any]:
        msg = payload.get("msg") or payload
        return {
            "received_ts_ms": int(payload.get("received_ts_ms") or utc_now_ms()),
            "exchange_ts_ms": _message_ts_ms(msg),
            "market_ticker": msg.get("market_ticker") or msg.get("ticker") or fallback_ticker,
            "trade_id": msg.get("trade_id"),
            "side": msg.get("side") or msg.get("taker_outcome_side"),
            "taker_side": msg.get("taker_side") or msg.get("taker_book_side"),
            "yes_price": msg.get("yes_price") or msg.get("yes_price_dollars"),
            "no_price": msg.get("no_price") or msg.get("no_price_dollars"),
            "price": msg.get("price") or msg.get("price_dollars") or msg.get("yes_price"),
            "count": msg.get("count_fp") or msg.get("count") or msg.get("size"),
            "raw_json": json.dumps(payload, separators=(",", ":")),
        }

    def _fill_record(self, payload: dict[str, Any], fallback_ticker: str) -> dict[str, Any]:
        msg = payload.get("msg") or payload
        return {
            "received_ts_ms": int(payload.get("received_ts_ms") or utc_now_ms()),
            "exchange_ts_ms": _message_ts_ms(msg),
            "market_ticker": msg.get("market_ticker") or msg.get("ticker") or fallback_ticker,
            "order_id": msg.get("order_id"),
            "client_order_id": msg.get("client_order_id"),
            "trade_id": msg.get("trade_id"),
            "side": msg.get("side") or msg.get("purchased_side"),
            "action": msg.get("action"),
            "price": msg.get("yes_price_dollars")
            or msg.get("no_price_dollars")
            or msg.get("price_dollars"),
            "count": msg.get("count_fp") or msg.get("count"),
            "fee": msg.get("fee_dollars") or msg.get("fees_dollars") or msg.get("fee"),
            "raw_json": json.dumps(payload, separators=(",", ":")),
        }

    def _user_order_record(self, payload: dict[str, Any], fallback_ticker: str) -> dict[str, Any]:
        msg = payload.get("msg") or payload
        return {
            "received_ts_ms": int(payload.get("received_ts_ms") or utc_now_ms()),
            "exchange_ts_ms": _message_ts_ms(msg),
            "market_ticker": msg.get("market_ticker") or msg.get("ticker") or fallback_ticker,
            "order_id": msg.get("order_id"),
            "client_order_id": msg.get("client_order_id"),
            "status": msg.get("status"),
            "side": msg.get("side"),
            "action": msg.get("action"),
            "price": msg.get("yes_price_dollars")
            or msg.get("no_price_dollars")
            or msg.get("price_dollars"),
            "count": msg.get("initial_count_fp") or msg.get("count_fp"),
            "filled_count": msg.get("fill_count_fp") or msg.get("filled_count_fp"),
            "queue_position": msg.get("queue_position") or msg.get("queue_position_fp"),
            "raw_json": json.dumps(payload, separators=(",", ":")),
        }

    def _position_record(self, payload: dict[str, Any], fallback_ticker: str) -> dict[str, Any]:
        msg = payload.get("msg") or payload
        position = decimal_from_fixed(msg.get("position_fp"), default=None)
        yes_count = None
        no_count = None
        if position is not None:
            if position >= 0:
                yes_count = decimal_to_str(position)
            else:
                no_count = decimal_to_str(abs(position))
        return {
            "received_ts_ms": int(payload.get("received_ts_ms") or utc_now_ms()),
            "exchange_ts_ms": _message_ts_ms(msg),
            "market_ticker": msg.get("market_ticker") or msg.get("ticker") or fallback_ticker,
            "yes_count": yes_count,
            "no_count": no_count,
            "realized_pnl": msg.get("realized_pnl_dollars") or msg.get("realized_pnl"),
            "raw_json": json.dumps(payload, separators=(",", ":")),
        }

    def _lifecycle_record(self, payload: dict[str, Any], fallback_ticker: str) -> dict[str, Any]:
        msg = payload.get("msg") or payload
        market = msg.get("market") if isinstance(msg.get("market"), dict) else msg
        metadata = market.get("additional_metadata") or {}
        return {
            "received_ts_ms": int(payload.get("received_ts_ms") or utc_now_ms()),
            "exchange_ts_ms": _message_ts_ms(market),
            "market_ticker": market.get("market_ticker") or market.get("ticker") or fallback_ticker,
            "event_ticker": market.get("event_ticker") or metadata.get("event_ticker"),
            "series_ticker": market.get("series_ticker") or self.settings.kalshi.series_ticker,
            "status": market.get("status") or market.get("event_type"),
            "open_time": market.get("open_time") or market.get("open_ts"),
            "close_time": market.get("close_time") or market.get("close_ts"),
            "expiration_time": market.get("expiration_time")
            or metadata.get("expected_expiration_ts"),
            "raw_json": json.dumps(payload, separators=(",", ":")),
        }

    async def _rollover(self, previous_ticker: str, payload: dict[str, Any]) -> None:
        self.stats.rollover_count += 1
        next_ticker = await self._discover_open_market(None, exclude={previous_ticker})
        if next_ticker is None:
            # Discovery failed (next market not yet open). Retry with backoff
            # rather than silently giving up — the runner used to sleep 1s and
            # return, which left current_market_ticker pointing at the dead one.
            self.print_line(f"kalshi rollover previous={previous_ticker} next=unavailable")
            await self._health("kalshi", "reconnect", {"reason": "rollover_waiting", **payload})
            for backoff_s in (2.0, 5.0, 10.0, 20.0, 30.0):
                if self.stop_event.is_set():
                    return
                await self.sleep(backoff_s)
                next_ticker = await self._discover_open_market(None, exclude={previous_ticker})
                if next_ticker is not None:
                    self.print_line(
                        f"kalshi rollover retried previous={previous_ticker} next={next_ticker}"
                    )
                    break
            if next_ticker is None:
                # Still no next market after backoff; clear ticker so the outer
                # loop's discovery retry takes over.
                self.current_market_ticker = None
                self.market_changed_event.set()
                return
        self.current_market_ticker = next_ticker
        self.market_changed_event.set()
        self.print_line(f"kalshi rollover previous={previous_ticker} next={next_ticker}")
        # Persist the new market's metadata so downstream tools (backtester,
        # settled-markets scanner) can resolve its strike and timestamps.
        next_market = await self._get_market(next_ticker)
        if next_market is not None:
            await self.queue.put(
                CaptureItem("market_dim", "market_dim", self._market_record(next_market))
            )
        await self.queue.put(
            CaptureItem(
                "kalshi_lifecycle",
                "kalshi_lifecycle_event",
                {
                    "received_ts_ms": utc_now_ms(),
                    "market_ticker": next_ticker,
                    "series_ticker": self.settings.kalshi.series_ticker,
                    "status": "open",
                    "raw_json": json.dumps(
                        {"event": "rollover", "previous": previous_ticker, "next": next_ticker},
                        separators=(",", ":"),
                    ),
                },
            )
        )

    async def _next_market_payload(
        self, stream: AsyncIterator[dict[str, Any]]
    ) -> dict[str, Any] | None:
        next_task = asyncio.create_task(anext(stream))
        changed_task = asyncio.create_task(self.market_changed_event.wait())
        done, pending = await asyncio.wait(
            {next_task, changed_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if changed_task in done and changed_task.result():
            self.market_changed_event.clear()
            if not next_task.done():
                next_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await next_task
            return None
        if next_task in done:
            with contextlib.suppress(StopAsyncIteration):
                return next_task.result()
        return None

    async def _spot_loop(self, source: str, stream: AsyncIterator[SpotQuote]) -> None:
        try:
            async for quote in stream:
                if self.stop_event.is_set():
                    break
                await self._capture_spot_quote(source, quote)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._health(source, "reconnect", {"error": repr(exc)})

    async def _capture_spot_quote(self, source: str, quote: SpotQuote) -> None:
        now = quote.received_ts_ms
        self.stats.last_quotes[source] = quote
        self.stats.spot_tick_count += 1
        fresh_count = self._fresh_spot_count(now)
        if fresh_count >= self.settings.spot.min_venues:
            self.stats.spot_quorum_tick_count += 1
            fused = fuse_spot_quotes(
                self.stats.last_quotes.values(),
                now_ms=now,
                max_age_ms=self.settings.spot.max_quote_age_ms,
                min_venues=self.settings.spot.min_venues,
            )
            if fused is not None:
                await self.queue.put(
                    CaptureItem("spot_fusion", "spot_quote_event", quote_to_record(fused.quote))
                )
            if not self.stats.spot_quorum_ok:
                self.stats.spot_quorum_ok = True
                await self._health("spot", "quorum_regained", {"fresh_venues": fresh_count})
        elif self.stats.spot_quorum_ok:
            self.stats.spot_quorum_ok = False
            await self._health("spot", "quorum_loss", {"fresh_venues": fresh_count})
        await self.queue.put(CaptureItem(source, "spot_quote_event", quote_to_record(quote)))

    def _fresh_spot_count(self, now_ms: int) -> int:
        return sum(
            1
            for quote in self.stats.last_quotes.values()
            if 0 <= now_ms - quote.received_ts_ms <= self.settings.spot.max_quote_age_ms
        )

    async def _heartbeat_loop(self) -> None:
        while not self.stop_event.is_set():
            await self.sleep(self.config.heartbeat_interval_s)
            await self._emit_heartbeat()

    async def _staleness_loop(self) -> None:
        while not self.stop_event.is_set():
            await self.sleep(self.config.staleness_check_interval_s)
            now = utc_now_ms()
            stale = {
                venue: now - quote.received_ts_ms
                for venue, quote in self.stats.last_quotes.items()
                if now - quote.received_ts_ms > self.settings.spot.max_quote_age_ms
            }
            if stale:
                self.stats.max_spot_staleness_ms = max(
                    self.stats.max_spot_staleness_ms, max(stale.values())
                )
                await self._health("spot", "staleness_breach", stale)

    async def _emit_heartbeat(self) -> None:
        runtime = max(0.001, self.monotonic() - self.stats.started_monotonic)
        mps = {source: count / runtime for source, count in self.stats.source_counts.items()}
        now_ms = utc_now_ms()
        fresh = self._fresh_spot_count(now_ms)
        detail = {
            "messages_per_second": mps,
            "spot_quorum": fresh,
            "market_ticker": self.current_market_ticker,
            "elapsed_seconds": runtime,
        }
        await self._health("capture", "heartbeat", detail, count=False)
        self.print_line(
            "heartbeat "
            f"elapsed={runtime:.1f}s market={self.current_market_ticker} "
            f"spot_quorum={fresh} mps={json.dumps(mps, sort_keys=True)}"
        )

    async def _health(
        self, source: str, event_kind: str, detail: dict[str, Any], *, count: bool = True
    ) -> None:
        if event_kind not in HEALTH_KINDS:
            raise ValueError(f"unknown health event kind: {event_kind}")
        self.stats.health_counts[event_kind] += 1
        await self.queue.put(
            CaptureItem(
                source,
                "capture_health_event",
                {
                    "ts_ms": utc_now_ms(),
                    "source": source,
                    "event_kind": event_kind,
                    "detail_json": json.dumps(detail, separators=(",", ":"), default=str),
                },
                count_as_message=count,
            )
        )

    def _build_report(self) -> BurnInReport:
        runtime = max(0.001, self.monotonic() - self.stats.started_monotonic)
        gaps = 0
        duplicates = 0
        try:
            with connect(self.config.db_path) as conn:
                rows = [dict(row) for row in conn.execute("""
                        SELECT event_id, received_ts_ms, market_ticker, seq
                        FROM kalshi_l2_event
                        ORDER BY market_ticker, received_ts_ms, event_id
                        """).fetchall()]
            for item in analyze_kalshi_l2_rows(rows):
                gaps += item.sequence_gaps
                duplicates += item.duplicate_sequences
        except sqlite3.Error:
            pass
        coverage = (
            self.stats.spot_quorum_tick_count / self.stats.spot_tick_count
            if self.stats.spot_tick_count
            else 0.0
        )
        return BurnInReport(
            runtime_seconds=runtime,
            messages_per_second_by_source={
                source: count / runtime
                for source, count in sorted(self.stats.source_counts.items())
            },
            row_counts_by_source=dict(sorted(self.stats.row_counts.items())),
            kalshi_sequence_gaps=gaps,
            kalshi_duplicate_sequences=duplicates,
            reconnect_count=self.stats.health_counts["reconnect"],
            staleness_breach_count=self.stats.health_counts["staleness_breach"],
            quorum_loss_count=self.stats.health_counts["quorum_loss"],
            quorum_regained_count=self.stats.health_counts["quorum_regained"],
            max_spot_staleness_ms=self.stats.max_spot_staleness_ms,
            spot_fusion_quorum_coverage=coverage,
            rollover_count=self.stats.rollover_count,
            market_tickers_captured=sorted(self.stats.market_tickers),
        )


async def run_capture_burnin(config: BurnInConfig) -> BurnInReport:
    return await BurnInRunner(config).run()


def _message_ts_ms(msg: dict[str, Any]) -> int | None:
    raw = msg.get("ts_ms") or msg.get("exchange_ts_ms")
    if raw is not None:
        return int(raw)
    raw_ts = msg.get("ts")
    if raw_ts is None:
        return None
    if isinstance(raw_ts, int | float):
        return int(raw_ts if raw_ts > 10_000_000_000 else raw_ts * 1000)
    return parse_rfc3339_ms(str(raw_ts))
