"""Favorite-chase strategy.

Rule (one trade per market session):
  - After T+8 minutes from session open, the FIRST trade print where either
    yes_price or no_price >= 0.75 triggers an entry on that side.
  - Fill = next available ask on that side (taker buy).
  - Exit: if the mid of the held side falls to <= 0.50, sell at bid (stop).
          else hold to settlement (win = 1.00, loss = 0.00).
  - 1 contract per trade.

Pure rule logic — no I/O. Prices are in dollars (0..1). NO side prices derive
from YES book: no_bid = 1 - yes_ask, no_ask = 1 - yes_bid.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

ENTRY_AFTER_MS: int = 8 * 60 * 1000
ENTRY_TRIGGER_PRICE: float = 0.75
STOP_PRICE: float = 0.50
# Hard sizing cap. The live runner must use this constant — never a CLI flag.
# User-authorised on 2026-05-21. Do not raise without explicit re-authorisation.
CONTRACT_QTY: int = 1
MAX_CONTRACTS_PER_TRADE: int = 1
# 4 basis points = 0.0004 of spot. For BTC@$78k that's ~$31; for ETH@$2.7k
# that's ~$1.08. Filters out triggers where spot is sitting essentially on
# the strike (the most-prone-to-whipsaw bucket from the [25,50) heatmap).
MIN_STRIKE_DISTANCE_BPS: float = 4.0


def passes_strike_distance(spot: float, strike: float, min_bps: float = MIN_STRIKE_DISTANCE_BPS) -> bool:
    """True if |spot - strike| / spot >= min_bps / 10_000.

    Returns False if spot is non-positive (can't compute a ratio).
    """
    if spot <= 0:
        return False
    return abs(spot - strike) / spot >= (min_bps / 10_000.0)


class Side(str, Enum):
    YES = "yes"
    NO = "no"


@dataclass(frozen=True)
class TradeTick:
    """A trade print. yes_price + no_price == 1.0 (dollars)."""

    ts_ms: int
    yes_price: float
    no_price: float


@dataclass(frozen=True)
class BookTick:
    """Top-of-book quote. yes_bid/yes_ask are dollars (0..1). Either may be None."""

    ts_ms: int
    yes_bid: float | None
    yes_ask: float | None


def detect_entry(trade: TradeTick, session_open_ms: int) -> Side | None:
    """Return the side to enter if this trade triggers, else None.

    Tie-break (both sides >= 0.75 in same print — impossible for binaries
    since yes+no=1, so >=0.75 on both would require sum>=1.50). YES wins by
    convention if equal at exactly 0.75/0.25.
    """
    if trade.ts_ms - session_open_ms < ENTRY_AFTER_MS:
        return None
    if trade.yes_price >= ENTRY_TRIGGER_PRICE:
        return Side.YES
    if trade.no_price >= ENTRY_TRIGGER_PRICE:
        return Side.NO
    return None


def ask_for_side(book: BookTick, side: Side) -> float | None:
    if side is Side.YES:
        return book.yes_ask
    if book.yes_bid is None:
        return None
    return 1.0 - book.yes_bid


def bid_for_side(book: BookTick, side: Side) -> float | None:
    if side is Side.YES:
        return book.yes_bid
    if book.yes_ask is None:
        return None
    return 1.0 - book.yes_ask


def mid_for_side(book: BookTick, side: Side) -> float | None:
    bid = bid_for_side(book, side)
    ask = ask_for_side(book, side)
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2.0


def detect_stop(book: BookTick, side: Side) -> bool:
    """True if mid for held side has fallen to <= STOP_PRICE."""
    m = mid_for_side(book, side)
    return m is not None and m <= STOP_PRICE
