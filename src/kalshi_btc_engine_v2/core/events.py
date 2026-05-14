from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

KalshiBookSide = Literal["yes", "no"]
KalshiBookEventType = Literal["snapshot", "delta"]


@dataclass(frozen=True, slots=True)
class MarketDim:
    ticker: str
    series_ticker: str
    event_ticker: str | None
    market_type: str | None
    title: str | None
    open_time: str | None
    close_time: str | None
    expiration_time: str | None
    settlement_source: str | None
    status: str | None
    fee_type: str | None
    fee_multiplier: str | None
    price_level_structure_json: str | None
    raw_json: str
    created_at_ms: int
    updated_at_ms: int


@dataclass(frozen=True, slots=True)
class KalshiL2Event:
    received_ts_ms: int
    market_ticker: str
    event_type: KalshiBookEventType
    seq: int | None
    exchange_ts_ms: int | None = None
    side: KalshiBookSide | None = None
    price: Decimal | None = None
    size: Decimal | None = None
    delta: Decimal | None = None
    yes_levels_json: str | None = None
    no_levels_json: str | None = None
    best_yes_bid: Decimal | None = None
    best_yes_ask: Decimal | None = None
    spread: Decimal | None = None
    source_channel: str | None = None
    raw_json: str | None = None


@dataclass(frozen=True, slots=True)
class SpotQuote:
    received_ts_ms: int
    venue: str
    symbol: str
    bid: Decimal | None
    ask: Decimal | None
    mid: Decimal
    exchange_ts_ms: int | None = None
    last: Decimal | None = None
    label_confidence: Decimal | None = None
    raw_json: str | None = None


@dataclass(frozen=True, slots=True)
class SpotTrade:
    received_ts_ms: int
    venue: str
    symbol: str
    price: Decimal
    size: Decimal | None
    side: str | None = None
    trade_id: str | None = None
    exchange_ts_ms: int | None = None
    raw_json: str | None = None


@dataclass(frozen=True, slots=True)
class ReplayEvent:
    event_time_ms: int
    table: str
    event_id: int
    payload: dict[str, Any]
