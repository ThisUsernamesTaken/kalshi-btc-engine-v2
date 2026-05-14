# HANDOFF: owned by Claude (backtest/). Edit only via HANDOFF.md Open Request.
"""Simulation state replayed from captured events.

Holds per-market order books, the latest fused BTC spot, a 1-second log-return
buffer used by the volatility estimator, and market metadata (open/close
times, strike). All updates are pure functions of incoming ``ReplayEvent`` —
no I/O, no time-of-day assumptions.
"""

from __future__ import annotations

import json
import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from kalshi_btc_engine_v2.adapters.kalshi import orderbook_from_snapshot_record
from kalshi_btc_engine_v2.core.events import ReplayEvent
from kalshi_btc_engine_v2.core.orderbook import KalshiOrderBook

SPOT_RETURN_HISTORY_S = 600


@dataclass(slots=True)
class SimulationState:
    books: dict[str, KalshiOrderBook] = field(default_factory=dict)
    market_dims: dict[str, dict[str, Any]] = field(default_factory=dict)
    fused_spot: float | None = None
    last_fused_spot_ts_ms: int = 0
    spot_returns_1s: deque[float] = field(
        default_factory=lambda: deque(maxlen=SPOT_RETURN_HISTORY_S)
    )
    last_event_ts_ms: int = 0

    def apply_event(self, event: ReplayEvent) -> None:
        self.last_event_ts_ms = event.event_time_ms
        if event.table == "kalshi_l2_event":
            self._apply_l2(event)
        elif event.table == "spot_quote_event":
            self._apply_spot(event)
        # Trades and other event types are observed but don't change state we need
        # for the v1 policy snapshot.

    def upsert_market_dim(self, market_ticker: str, dim: dict[str, Any]) -> None:
        self.market_dims[market_ticker] = dim

    def _apply_l2(self, event: ReplayEvent) -> None:
        payload = event.payload
        ticker = str(payload["market_ticker"])
        yes_json = payload.get("yes_levels_json")
        no_json = payload.get("no_levels_json")
        if not yes_json or not no_json:
            return
        # Each captured L2 row carries the *full* reconstructed book snapshot
        # (the runner emits a full book on every delta). Rebuild from JSON.
        record = {
            "market_ticker": ticker,
            "yes_levels_json": yes_json,
            "no_levels_json": no_json,
            "seq": payload.get("seq"),
        }
        self.books[ticker] = orderbook_from_snapshot_record(record)

    def _apply_spot(self, event: ReplayEvent) -> None:
        venue = str(event.payload.get("source_channel") or "")
        # The backtester prefers the fused (median 2-of-3) feed but accepts
        # any spot quote when no fusion row has been captured yet.
        if venue and "fusion" not in venue and self.fused_spot is not None:
            return
        raw_price = event.payload.get("price")
        if raw_price is None:
            return
        try:
            mid = float(raw_price)
        except (TypeError, ValueError):
            return
        if mid <= 0.0:
            return
        if self.fused_spot is not None and self.last_fused_spot_ts_ms > 0:
            elapsed_ms = max(0, event.event_time_ms - self.last_fused_spot_ts_ms)
            if elapsed_ms > 0:
                log_return = math.log(mid / self.fused_spot)
                # Allocate the return across whole seconds elapsed (cheap
                # interpolation matching what the live feature engine does).
                seconds = max(1, int(round(elapsed_ms / 1000)))
                per_second = log_return / seconds
                for _ in range(seconds):
                    self.spot_returns_1s.append(per_second)
        self.fused_spot = mid
        self.last_fused_spot_ts_ms = event.event_time_ms


def parse_market_dim_row(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a raw ``market_dim`` SQLite row into a backtester-friendly dict."""
    out = dict(row)
    raw_json = out.get("raw_json")
    if raw_json:
        try:
            out["_raw"] = json.loads(raw_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            out["_raw"] = None
    return out
