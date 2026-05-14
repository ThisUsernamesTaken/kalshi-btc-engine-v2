from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal

from kalshi_btc_engine_v2.adapters.kalshi import orderbook_from_snapshot_record
from kalshi_btc_engine_v2.core.decimal import decimal_from_fixed
from kalshi_btc_engine_v2.core.events import ReplayEvent, SpotQuote
from kalshi_btc_engine_v2.core.orderbook import KalshiOrderBook

REPLAY_SQL = """
SELECT
    COALESCE(exchange_ts_ms, received_ts_ms) AS event_time_ms,
    'kalshi_l2_event' AS table_name,
    event_id,
    received_ts_ms,
    exchange_ts_ms,
    market_ticker,
    event_type,
    seq,
    side,
    price,
    size,
    delta,
    yes_levels_json,
    no_levels_json,
    best_yes_bid,
    best_yes_ask,
    spread,
    source_channel,
    raw_json
FROM kalshi_l2_event
WHERE COALESCE(exchange_ts_ms, received_ts_ms) BETWEEN ? AND ?
UNION ALL
SELECT
    COALESCE(exchange_ts_ms, received_ts_ms) AS event_time_ms,
    'kalshi_trade_event' AS table_name,
    event_id,
    received_ts_ms,
    exchange_ts_ms,
    market_ticker,
    NULL AS event_type,
    NULL AS seq,
    side,
    price,
    count AS size,
    NULL AS delta,
    NULL AS yes_levels_json,
    NULL AS no_levels_json,
    NULL AS best_yes_bid,
    NULL AS best_yes_ask,
    NULL AS spread,
    NULL AS source_channel,
    raw_json
FROM kalshi_trade_event
WHERE COALESCE(exchange_ts_ms, received_ts_ms) BETWEEN ? AND ?
UNION ALL
SELECT
    COALESCE(exchange_ts_ms, received_ts_ms) AS event_time_ms,
    'spot_quote_event' AS table_name,
    event_id,
    received_ts_ms,
    exchange_ts_ms,
    NULL AS market_ticker,
    NULL AS event_type,
    NULL AS seq,
    NULL AS side,
    mid AS price,
    NULL AS size,
    NULL AS delta,
    NULL AS yes_levels_json,
    NULL AS no_levels_json,
    NULL AS best_yes_bid,
    NULL AS best_yes_ask,
    NULL AS spread,
    venue AS source_channel,
    raw_json
FROM spot_quote_event
WHERE COALESCE(exchange_ts_ms, received_ts_ms) BETWEEN ? AND ?
ORDER BY event_time_ms, table_name, event_id
"""


@dataclass(slots=True)
class ReplayState:
    books: dict[str, KalshiOrderBook] = field(default_factory=dict)
    latest_spot: dict[str, SpotQuote] = field(default_factory=dict)
    event_count: int = 0

    def apply(self, event: ReplayEvent) -> None:
        self.event_count += 1
        if event.table == "kalshi_l2_event":
            ticker = str(event.payload["market_ticker"])
            if event.payload.get("yes_levels_json") and event.payload.get("no_levels_json"):
                self.books[ticker] = orderbook_from_snapshot_record(event.payload)
        elif event.table == "spot_quote_event":
            venue = str(event.payload["source_channel"])
            mid = decimal_from_fixed(event.payload["price"])
            raw_json = event.payload.get("raw_json")
            self.latest_spot[venue] = SpotQuote(
                received_ts_ms=int(event.payload["received_ts_ms"]),
                exchange_ts_ms=event.payload.get("exchange_ts_ms"),
                venue=venue,
                symbol="BTC/USD",
                bid=None,
                ask=None,
                mid=mid if isinstance(mid, Decimal) else Decimal(str(mid)),
                raw_json=raw_json,
            )


@dataclass(frozen=True, slots=True)
class ReplayTick:
    event: ReplayEvent
    state: ReplayState

    def summary(self) -> str:
        if self.event.table == "kalshi_l2_event":
            ticker = self.event.payload["market_ticker"]
            mid = None
            bid = self.event.payload.get("best_yes_bid")
            ask = self.event.payload.get("best_yes_ask")
            if bid is not None and ask is not None:
                mid = (Decimal(str(bid)) + Decimal(str(ask))) / Decimal("2")
            seq = self.event.payload.get("seq")
            spread = self.event.payload.get("spread")
            return f"{self.event.event_time_ms} l2 {ticker} mid={mid} spread={spread} seq={seq}"
        if self.event.table == "spot_quote_event":
            return (
                f"{self.event.event_time_ms} spot {self.event.payload['source_channel']} "
                f"mid={self.event.payload['price']}"
            )
        return f"{self.event.event_time_ms} {self.event.table}#{self.event.event_id}"


def load_events(
    conn: sqlite3.Connection,
    *,
    start_ms: int = 0,
    end_ms: int = 9_999_999_999_999,
) -> Iterator[ReplayEvent]:
    params = (start_ms, end_ms, start_ms, end_ms, start_ms, end_ms)
    for row in conn.execute(REPLAY_SQL, params):
        payload = dict(row)
        table = str(payload.pop("table_name"))
        event_time_ms = int(payload.pop("event_time_ms"))
        event_id = int(payload["event_id"])
        yield ReplayEvent(
            event_time_ms=event_time_ms, table=table, event_id=event_id, payload=payload
        )


class DeterministicReplayer:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def run(
        self,
        *,
        start_ms: int = 0,
        end_ms: int = 9_999_999_999_999,
    ) -> Iterator[ReplayTick]:
        state = ReplayState()
        for event in load_events(self.conn, start_ms=start_ms, end_ms=end_ms):
            state.apply(event)
            yield ReplayTick(event=event, state=state)


def replay_sample_json(ticks: list[ReplayTick]) -> str:
    return json.dumps(
        [{"event_time_ms": tick.event.event_time_ms, "summary": tick.summary()} for tick in ticks],
        indent=2,
    )
