from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from kalshi_btc_engine_v2.core.decimal import decimal_from_fixed, decimal_to_str, quantile_median
from kalshi_btc_engine_v2.core.events import SpotQuote
from kalshi_btc_engine_v2.core.time import parse_rfc3339_ms, utc_now_ms

COINBASE_WS_URL = "wss://ws-feed.exchange.coinbase.com"
KRAKEN_WS_URL = "wss://ws.kraken.com/v2"
BITSTAMP_TICKER_URL = "https://www.bitstamp.net/api/v2/ticker/btcusd/"


@dataclass(frozen=True, slots=True)
class SpotFusionResult:
    quote: SpotQuote
    source_venues: tuple[str, ...]


def quote_to_record(quote: SpotQuote) -> dict[str, Any]:
    return {
        "received_ts_ms": quote.received_ts_ms,
        "exchange_ts_ms": quote.exchange_ts_ms,
        "venue": quote.venue,
        "symbol": quote.symbol,
        "bid": decimal_to_str(quote.bid),
        "ask": decimal_to_str(quote.ask),
        "mid": decimal_to_str(quote.mid),
        "last": decimal_to_str(quote.last),
        "label_confidence": decimal_to_str(quote.label_confidence),
        "raw_json": quote.raw_json,
    }


def fuse_spot_quotes(
    quotes: Iterable[SpotQuote],
    *,
    now_ms: int | None = None,
    max_age_ms: int = 1500,
    min_venues: int = 2,
) -> SpotFusionResult | None:
    now = now_ms or utc_now_ms()
    latest_by_venue: dict[str, SpotQuote] = {}
    for quote in quotes:
        age = now - quote.received_ts_ms
        if age < 0 or age > max_age_ms:
            continue
        current = latest_by_venue.get(quote.venue)
        if current is None or quote.received_ts_ms > current.received_ts_ms:
            latest_by_venue[quote.venue] = quote

    fresh = list(latest_by_venue.values())
    if len(fresh) < min_venues:
        return None

    mids = [quote.mid for quote in fresh]
    fused_mid = quantile_median(mids)
    confidence = Decimal(str(len(fresh))) / Decimal("3")
    fused = SpotQuote(
        received_ts_ms=now,
        exchange_ts_ms=max((q.exchange_ts_ms or q.received_ts_ms for q in fresh), default=now),
        venue="fusion:median2of3",
        symbol="BTC/USD",
        bid=None,
        ask=None,
        mid=fused_mid,
        last=None,
        label_confidence=min(confidence, Decimal("1")),
        raw_json=json.dumps(
            {
                "source_venues": [q.venue for q in fresh],
                "source_mids": [format(q.mid, "f") for q in fresh],
            },
            separators=(",", ":"),
        ),
    )
    return SpotFusionResult(quote=fused, source_venues=tuple(q.venue for q in fresh))


class CoinbaseTickerFeed:
    url = COINBASE_WS_URL

    @staticmethod
    def subscribe_message(product_id: str = "BTC-USD") -> dict[str, Any]:
        return {"type": "subscribe", "product_ids": [product_id], "channels": ["ticker", "matches"]}

    @staticmethod
    def parse_message(
        payload: dict[str, Any], *, received_ts_ms: int | None = None
    ) -> SpotQuote | None:
        if payload.get("type") != "ticker":
            return None
        bid = decimal_from_fixed(payload.get("best_bid"), default=None)
        ask = decimal_from_fixed(payload.get("best_ask"), default=None)
        last = decimal_from_fixed(payload.get("price"), default=None)
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            mid = (bid + ask) / Decimal("2")
        elif last is not None:
            mid = last
        else:
            return None
        return SpotQuote(
            received_ts_ms=received_ts_ms or utc_now_ms(),
            exchange_ts_ms=parse_rfc3339_ms(payload.get("time")),
            venue="coinbase",
            symbol=str(payload.get("product_id") or "BTC-USD"),
            bid=bid,
            ask=ask,
            mid=mid,
            last=last,
            raw_json=json.dumps(payload, separators=(",", ":")),
        )

    async def messages(self) -> AsyncIterator[SpotQuote]:
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("Install websockets to use Coinbase WebSocket") from exc

        async with websockets.connect(self.url, ping_interval=20, ping_timeout=20) as ws:
            await ws.send(json.dumps(self.subscribe_message()))
            async for raw in ws:
                parsed = self.parse_message(json.loads(raw))
                if parsed is not None:
                    yield parsed


class KrakenTickerFeed:
    url = KRAKEN_WS_URL

    @staticmethod
    def subscribe_message(symbol: str = "BTC/USD") -> dict[str, Any]:
        return {
            "method": "subscribe",
            "params": {"channel": "ticker", "symbol": [symbol], "event_trigger": "bbo"},
        }

    @staticmethod
    def parse_message(
        payload: dict[str, Any], *, received_ts_ms: int | None = None
    ) -> SpotQuote | None:
        if payload.get("channel") != "ticker":
            return None
        data = payload.get("data") or []
        if not data:
            return None
        item = data[0]
        bid = decimal_from_fixed(item.get("bid"), default=None)
        ask = decimal_from_fixed(item.get("ask"), default=None)
        last = decimal_from_fixed(item.get("last"), default=None)
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            mid = (bid + ask) / Decimal("2")
        elif last is not None:
            mid = last
        else:
            return None
        return SpotQuote(
            received_ts_ms=received_ts_ms or utc_now_ms(),
            exchange_ts_ms=parse_rfc3339_ms(item.get("timestamp")),
            venue="kraken",
            symbol=str(item.get("symbol") or "BTC/USD"),
            bid=bid,
            ask=ask,
            mid=mid,
            last=last,
            raw_json=json.dumps(payload, separators=(",", ":")),
        )

    async def messages(self) -> AsyncIterator[SpotQuote]:
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("Install websockets to use Kraken WebSocket") from exc

        async with websockets.connect(self.url, ping_interval=20, ping_timeout=20) as ws:
            await ws.send(json.dumps(self.subscribe_message()))
            async for raw in ws:
                parsed = self.parse_message(json.loads(raw))
                if parsed is not None:
                    yield parsed


class BitstampTickerPoller:
    url = BITSTAMP_TICKER_URL

    @staticmethod
    def parse_payload(
        payload: dict[str, Any], *, received_ts_ms: int | None = None
    ) -> SpotQuote | None:
        bid = decimal_from_fixed(payload.get("bid"), default=None)
        ask = decimal_from_fixed(payload.get("ask"), default=None)
        last = decimal_from_fixed(payload.get("last"), default=None)
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            mid = (bid + ask) / Decimal("2")
        elif last is not None:
            mid = last
        else:
            return None
        exchange_ts_ms = None
        if payload.get("timestamp"):
            exchange_ts_ms = int(decimal_from_fixed(payload["timestamp"]) * Decimal("1000"))
        return SpotQuote(
            received_ts_ms=received_ts_ms or utc_now_ms(),
            exchange_ts_ms=exchange_ts_ms,
            venue="bitstamp",
            symbol="btcusd",
            bid=bid,
            ask=ask,
            mid=mid,
            last=last,
            raw_json=json.dumps(payload, separators=(",", ":")),
        )

    async def messages(self, *, interval_s: float = 1.0) -> AsyncIterator[SpotQuote]:
        try:
            import aiohttp
        except ImportError as exc:
            raise RuntimeError("Install aiohttp to use Bitstamp poller") from exc

        async with aiohttp.ClientSession() as session:
            while True:
                async with session.get(self.url) as response:
                    payload = await response.json()
                    parsed = self.parse_payload(payload)
                    if parsed is not None:
                        yield parsed
                await asyncio.sleep(interval_s)
