from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from kalshi_btc_engine_v2.core.decimal import ONE, ZERO, decimal_from_fixed

Level = tuple[Decimal, Decimal]


def normalize_levels(levels: Iterable[Iterable[Any]] | None) -> dict[Decimal, Decimal]:
    out: dict[Decimal, Decimal] = {}
    if not levels:
        return out
    for raw_price, raw_size in levels:
        price = decimal_from_fixed(raw_price)
        size = decimal_from_fixed(raw_size, default=ZERO)
        if size > ZERO:
            out[price] = size
    return out


def levels_to_json(levels: dict[Decimal, Decimal]) -> str:
    ordered = [[format(price, "f"), format(size, "f")] for price, size in sorted(levels.items())]
    return json.dumps(ordered, separators=(",", ":"))


@dataclass(slots=True)
class SequenceStatus:
    previous_seq: int | None
    current_seq: int | None
    gap: bool
    duplicate: bool


@dataclass(slots=True)
class KalshiOrderBook:
    market_ticker: str
    yes_bids: dict[Decimal, Decimal] = field(default_factory=dict)
    no_bids: dict[Decimal, Decimal] = field(default_factory=dict)
    last_seq: int | None = None
    gaps: int = 0
    duplicates: int = 0

    def apply_snapshot(
        self,
        yes_levels: Iterable[Iterable[Any]] | None,
        no_levels: Iterable[Iterable[Any]] | None,
        seq: int | None,
    ) -> SequenceStatus:
        status = self._advance_seq(seq)
        self.yes_bids = normalize_levels(yes_levels)
        self.no_bids = normalize_levels(no_levels)
        return status

    def apply_delta(
        self,
        side: str,
        price: Any,
        size: Any | None,
        seq: int | None,
        *,
        delta: Any | None = None,
        size_is_delta: bool = False,
    ) -> SequenceStatus:
        status = self._advance_seq(seq)
        levels = self._side_levels(side)
        price_dec = decimal_from_fixed(price)

        if delta is not None or size_is_delta:
            delta_dec = decimal_from_fixed(delta if delta is not None else size, default=ZERO)
            next_size = levels.get(price_dec, ZERO) + delta_dec
        else:
            next_size = decimal_from_fixed(size, default=ZERO)

        if next_size <= ZERO:
            levels.pop(price_dec, None)
        else:
            levels[price_dec] = next_size
        return status

    def _side_levels(self, side: str) -> dict[Decimal, Decimal]:
        normalized = side.lower()
        if normalized == "yes":
            return self.yes_bids
        if normalized == "no":
            return self.no_bids
        raise ValueError(f"unknown Kalshi book side: {side!r}")

    def _advance_seq(self, seq: int | None) -> SequenceStatus:
        previous = self.last_seq
        duplicate = seq is not None and previous == seq
        gap = seq is not None and previous is not None and seq > previous + 1
        if duplicate:
            self.duplicates += 1
        elif seq is not None:
            if gap:
                self.gaps += seq - previous - 1 if previous is not None else 0
            self.last_seq = seq
        return SequenceStatus(previous_seq=previous, current_seq=seq, gap=gap, duplicate=duplicate)

    @property
    def best_yes_bid(self) -> Decimal | None:
        return max(self.yes_bids) if self.yes_bids else None

    @property
    def best_no_bid(self) -> Decimal | None:
        return max(self.no_bids) if self.no_bids else None

    @property
    def best_yes_ask(self) -> Decimal | None:
        if self.best_no_bid is None:
            return None
        return ONE - self.best_no_bid

    @property
    def mid_yes(self) -> Decimal | None:
        bid = self.best_yes_bid
        ask = self.best_yes_ask
        if bid is None or ask is None:
            return None
        return (bid + ask) / Decimal("2")

    @property
    def spread_yes(self) -> Decimal | None:
        bid = self.best_yes_bid
        ask = self.best_yes_ask
        if bid is None or ask is None:
            return None
        return ask - bid

    def depth(self, side: str, levels: int = 5) -> Decimal:
        book = self._side_levels(side)
        return sum((size for _, size in sorted(book.items(), reverse=True)[:levels]), ZERO)

    def l1_imbalance(self) -> Decimal | None:
        bid = self.best_yes_bid
        no_bid = self.best_no_bid
        if bid is None or no_bid is None:
            return None
        bid_qty = self.yes_bids.get(bid, ZERO)
        ask_qty = self.no_bids.get(no_bid, ZERO)
        denom = bid_qty + ask_qty
        if denom <= ZERO:
            return None
        return (bid_qty - ask_qty) / denom

    def snapshot_json(self) -> tuple[str, str]:
        return levels_to_json(self.yes_bids), levels_to_json(self.no_bids)
