"""Shared execution types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ExecutionMode = Literal["paper", "live"]
BookSide = Literal["yes", "no"]


@dataclass(frozen=True, slots=True)
class ExecutionFill:
    market_ticker: str
    side: BookSide
    action: Literal["buy", "sell"]
    contracts: int
    price_cents: int
    fee_cents: int
    client_order_id: str
    mode: ExecutionMode
    timestamp_ms: int
    is_taker: bool = True


@dataclass(slots=True)
class Position:
    market_ticker: str
    side: BookSide | None = None
    contracts: int = 0
    avg_entry_price_cents: float = 0.0
    realized_pnl_cents: float = 0.0
    fees_paid_cents: float = 0.0

    @property
    def is_flat(self) -> bool:
        return self.contracts == 0


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    accepted: bool
    fills: tuple[ExecutionFill, ...]
    rejection_reason: str = ""
    position_after: Position | None = None
