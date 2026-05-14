from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kalshi_btc_engine_v2.config import KalshiConfig
from kalshi_btc_engine_v2.core.decimal import decimal_from_fixed, decimal_to_str
from kalshi_btc_engine_v2.core.events import KalshiL2Event
from kalshi_btc_engine_v2.core.orderbook import KalshiOrderBook, normalize_levels
from kalshi_btc_engine_v2.core.time import parse_rfc3339_ms, utc_now_ms

TRADE_API_ROOT = "/trade-api/v2"
WS_SIGN_PATH = "/trade-api/ws/v2"


@dataclass(frozen=True, slots=True)
class KalshiCredentials:
    key_id: str
    private_key_path: Path


class KalshiSigner:
    def __init__(self, credentials: KalshiCredentials) -> None:
        self.credentials = credentials
        self._private_key: Any | None = None

    def _load_key(self) -> Any:
        if self._private_key is not None:
            return self._private_key
        try:
            from cryptography.hazmat.primitives import serialization
        except ImportError as exc:
            raise RuntimeError("Install cryptography to use Kalshi authentication") from exc

        with self.credentials.private_key_path.open("rb") as handle:
            self._private_key = serialization.load_pem_private_key(handle.read(), password=None)
        return self._private_key

    def sign(self, timestamp_ms: str, method: str, signed_path: str) -> str:
        try:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding
        except ImportError as exc:
            raise RuntimeError("Install cryptography to use Kalshi authentication") from exc

        message = f"{timestamp_ms}{method.upper()}{signed_path}".encode()
        signature = self._load_key().sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("ascii")

    def headers(self, method: str, signed_path: str) -> dict[str, str]:
        timestamp_ms = str(int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.credentials.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": self.sign(timestamp_ms, method, signed_path),
        }


def credentials_from_config(config: KalshiConfig) -> KalshiCredentials | None:
    if not config.key_id or not config.private_key_path:
        return None
    return KalshiCredentials(key_id=config.key_id, private_key_path=config.private_key_path)


class KalshiRestClient:
    def __init__(
        self,
        config: KalshiConfig,
        *,
        live_enabled: bool = False,
        timeout_s: int = 10,
    ) -> None:
        self.base_url = config.rest_base_url.rstrip("/")
        self.live_enabled = live_enabled
        self.timeout_s = timeout_s
        credentials = credentials_from_config(config)
        self.signer = KalshiSigner(credentials) if credentials else None

    def _signed_path(self, path: str) -> str:
        clean_path = path.split("?", 1)[0]
        if clean_path.startswith(TRADE_API_ROOT):
            return clean_path
        return f"{TRADE_API_ROOT}{clean_path}"

    def _headers(self, method: str, path: str, auth: bool) -> dict[str, str]:
        if not auth:
            return {}
        if self.signer is None:
            raise RuntimeError("Kalshi authenticated request needs ENGINE_V2 credentials")
        return self.signer.headers(method, self._signed_path(path))

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        auth: bool = False,
        write: bool = False,
    ) -> dict[str, Any]:
        if write and not self.live_enabled:
            raise RuntimeError(
                "live Kalshi writes are disabled; set ENGINE_V2_LIVE only after approval"
            )
        try:
            import aiohttp
        except ImportError as exc:
            raise RuntimeError("Install aiohttp to use Kalshi REST") from exc

        url = f"{self.base_url}{path}"
        headers = self._headers(method, path, auth)
        if json_body is not None:
            headers = {"Content-Type": "application/json", **headers}

        timeout = aiohttp.ClientTimeout(total=self.timeout_s)
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.request(
                method.upper(), url, params=params, json=json_body, headers=headers
            ) as response,
        ):
            if response.status == 204:
                return {}
            payload = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"Kalshi REST {response.status}: {payload}")
            return json.loads(payload) if payload else {}

    async def get_markets(self, *, series_ticker: str, status: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"series_ticker": series_ticker}
        if status:
            params["status"] = status
        return await self.request("GET", "/markets", params=params)

    async def get_market(self, ticker: str) -> dict[str, Any]:
        return await self.request("GET", f"/markets/{ticker}")

    async def get_orderbook(self, ticker: str) -> dict[str, Any]:
        return await self.request("GET", f"/markets/{ticker}/orderbook")

    async def get_trades(self, *, ticker: str, limit: int = 100) -> dict[str, Any]:
        return await self.request(
            "GET", "/markets/trades", params={"ticker": ticker, "limit": limit}
        )

    async def create_order(self, order: dict[str, Any]) -> dict[str, Any]:
        return await self.request(
            "POST", "/portfolio/orders", json_body=order, auth=True, write=True
        )


class KalshiWebSocketClient:
    def __init__(self, config: KalshiConfig) -> None:
        self.ws_url = config.ws_url
        credentials = credentials_from_config(config)
        self.signer = KalshiSigner(credentials) if credentials else None

    def auth_headers(self) -> dict[str, str]:
        if self.signer is None:
            return {}
        return self.signer.headers("GET", WS_SIGN_PATH)

    async def messages(
        self,
        *,
        channels: list[str],
        market_tickers: list[str] | None = None,
        reconnect_delay_s: float = 2.0,
    ) -> AsyncIterator[dict[str, Any]]:
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("Install websockets to use Kalshi WebSocket") from exc

        params: dict[str, Any] = {"channels": channels}
        if market_tickers:
            params["market_tickers"] = market_tickers
        subscription = {
            "id": 1,
            "cmd": "subscribe",
            "params": params,
        }
        while True:
            try:
                async with websockets.connect(
                    self.ws_url,
                    additional_headers=self.auth_headers(),
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
                    await ws.send(json.dumps(subscription))
                    async for raw in ws:
                        yield json.loads(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                yield {
                    "type": "connection_error",
                    "received_ts_ms": utc_now_ms(),
                    "error": repr(exc),
                }
                await asyncio.sleep(reconnect_delay_s)


def extract_orderbook_levels(payload: dict[str, Any]) -> tuple[list[list[Any]], list[list[Any]]]:
    book = payload.get("orderbook_fp") or payload.get("orderbook") or payload
    yes_levels = (
        book.get("yes_dollars")
        or book.get("yes_dollars_fp")
        or book.get("yes")
        or book.get("yes_levels")
        or book.get("yes_bids")
        or []
    )
    no_levels = (
        book.get("no_dollars")
        or book.get("no_dollars_fp")
        or book.get("no")
        or book.get("no_levels")
        or book.get("no_bids")
        or []
    )
    return yes_levels, no_levels


def snapshot_event_from_payload(
    *,
    market_ticker: str,
    payload: dict[str, Any],
    seq: int | None,
    received_ts_ms: int | None = None,
) -> KalshiL2Event:
    yes_levels, no_levels = extract_orderbook_levels(payload)
    book = KalshiOrderBook(market_ticker)
    book.apply_snapshot(yes_levels, no_levels, seq)
    yes_json, no_json = book.snapshot_json()
    return KalshiL2Event(
        received_ts_ms=received_ts_ms or utc_now_ms(),
        market_ticker=market_ticker,
        event_type="snapshot",
        seq=seq,
        yes_levels_json=yes_json,
        no_levels_json=no_json,
        best_yes_bid=book.best_yes_bid,
        best_yes_ask=book.best_yes_ask,
        spread=book.spread_yes,
        source_channel="rest_orderbook",
        raw_json=json.dumps(payload, separators=(",", ":")),
    )


def apply_l2_payload(book: KalshiOrderBook, payload: dict[str, Any]) -> KalshiL2Event | None:
    received_ts_ms = payload.get("received_ts_ms") or utc_now_ms()
    msg = payload.get("msg") or payload
    event_type = str(payload.get("type") or msg.get("type") or "").lower()
    seq = msg.get("seq") or payload.get("seq")
    exchange_ts_ms = _payload_ts_ms(msg)
    market_ticker = msg.get("market_ticker") or msg.get("ticker") or book.market_ticker

    if "snapshot" in event_type:
        yes_levels, no_levels = extract_orderbook_levels(msg)
        book.apply_snapshot(yes_levels, no_levels, int(seq) if seq is not None else None)
        yes_json, no_json = book.snapshot_json()
        return KalshiL2Event(
            received_ts_ms=int(received_ts_ms),
            market_ticker=market_ticker,
            event_type="snapshot",
            seq=int(seq) if seq is not None else None,
            exchange_ts_ms=exchange_ts_ms,
            yes_levels_json=yes_json,
            no_levels_json=no_json,
            best_yes_bid=book.best_yes_bid,
            best_yes_ask=book.best_yes_ask,
            spread=book.spread_yes,
            source_channel=str(payload.get("type") or "orderbook_snapshot"),
            raw_json=json.dumps(payload, separators=(",", ":")),
        )

    if "delta" not in event_type:
        return None

    side = str(msg.get("side") or msg.get("market_side") or "").lower()
    price = msg.get("price_dollars") or msg.get("price") or msg.get("yes_price_dollars")
    size = msg.get("size") or msg.get("count") or msg.get("quantity")
    delta = msg.get("delta_fp") or msg.get("delta") or msg.get("delta_size") or msg.get("change")
    if side not in {"yes", "no"} or price is None:
        return None

    book.apply_delta(side, price, size, int(seq) if seq is not None else None, delta=delta)
    yes_json, no_json = book.snapshot_json()
    size_dec = decimal_from_fixed(size, default=None) if size is not None else None
    delta_dec = decimal_from_fixed(delta, default=None) if delta is not None else None
    return KalshiL2Event(
        received_ts_ms=int(received_ts_ms),
        market_ticker=market_ticker,
        event_type="delta",
        seq=int(seq) if seq is not None else None,
        exchange_ts_ms=exchange_ts_ms,
        side=side,  # type: ignore[arg-type]
        price=decimal_from_fixed(price),
        size=size_dec,
        delta=delta_dec,
        yes_levels_json=yes_json,
        no_levels_json=no_json,
        best_yes_bid=book.best_yes_bid,
        best_yes_ask=book.best_yes_ask,
        spread=book.spread_yes,
        source_channel=str(payload.get("type") or "orderbook_delta"),
        raw_json=json.dumps(payload, separators=(",", ":")),
    )


def _payload_ts_ms(msg: dict[str, Any]) -> int | None:
    raw = msg.get("ts_ms") or msg.get("exchange_ts_ms")
    if raw is not None:
        return int(raw)
    raw_ts = msg.get("ts")
    if raw_ts is None:
        return None
    if isinstance(raw_ts, int | float):
        return int(raw_ts if raw_ts > 10_000_000_000 else raw_ts * 1000)
    return parse_rfc3339_ms(str(raw_ts))


def l2_event_to_record(event: KalshiL2Event) -> dict[str, Any]:
    return {
        "received_ts_ms": event.received_ts_ms,
        "exchange_ts_ms": event.exchange_ts_ms,
        "seq": event.seq,
        "market_ticker": event.market_ticker,
        "event_type": event.event_type,
        "side": event.side,
        "price": decimal_to_str(event.price),
        "size": decimal_to_str(event.size),
        "delta": decimal_to_str(event.delta),
        "yes_levels_json": event.yes_levels_json,
        "no_levels_json": event.no_levels_json,
        "best_yes_bid": decimal_to_str(event.best_yes_bid),
        "best_yes_ask": decimal_to_str(event.best_yes_ask),
        "spread": decimal_to_str(event.spread),
        "source_channel": event.source_channel,
        "raw_json": event.raw_json,
    }


def orderbook_from_snapshot_record(record: dict[str, Any]) -> KalshiOrderBook:
    book = KalshiOrderBook(str(record["market_ticker"]))
    yes_levels = json.loads(record.get("yes_levels_json") or "[]")
    no_levels = json.loads(record.get("no_levels_json") or "[]")
    book.yes_bids = normalize_levels(yes_levels)
    book.no_bids = normalize_levels(no_levels)
    if record.get("seq") is not None:
        book.last_seq = int(record["seq"])
    return book
