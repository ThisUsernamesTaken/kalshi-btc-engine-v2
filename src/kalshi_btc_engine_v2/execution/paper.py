"""Paper execution simulator.

Models aggressive IOC fills against a snapshot of the Kalshi book. For BUY,
takes the ask side (yes_ask = 1 - best_no_bid). For SELL (exit), takes the
bid side directly. Sweeps up to ``max_sweep_levels`` of displayed depth; any
remainder beyond that is rejected (no partial guesswork).

The executor:
* tracks ``Position`` state per market
* feeds realized fills back into :class:`RiskGuard` via ``record_fill``
* uses Kalshi quadratic taker fees (override with realized fee where available)
* never reaches the live order path — that lives in :mod:`execution.live`
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from kalshi_btc_engine_v2.core.orderbook import KalshiOrderBook
from kalshi_btc_engine_v2.core.time import utc_now_ms
from kalshi_btc_engine_v2.execution.types import (
    BookSide,
    ExecutionFill,
    ExecutionResult,
    Position,
)
from kalshi_btc_engine_v2.policy.edge import kalshi_taker_fee_cents
from kalshi_btc_engine_v2.risk.guards import RiskGuard


@dataclass(frozen=True, slots=True)
class PaperExecutorConfig:
    max_sweep_levels: int = 3
    slippage_cents: int = 0


class PaperExecutor:
    def __init__(
        self,
        risk_guard: RiskGuard,
        *,
        config: PaperExecutorConfig | None = None,
    ) -> None:
        self.risk_guard = risk_guard
        self.config = config or PaperExecutorConfig()
        self.positions: dict[str, Position] = {}
        self.fills: list[ExecutionFill] = []
        self._order_counter = 0

    def position(self, market_ticker: str) -> Position:
        return self.positions.setdefault(market_ticker, Position(market_ticker=market_ticker))

    def submit_buy(
        self,
        *,
        market_ticker: str,
        side: BookSide,
        contracts: int,
        book: KalshiOrderBook,
        max_price_cents: int | None = None,
        now_ms: int | None = None,
    ) -> ExecutionResult:
        if contracts <= 0:
            return ExecutionResult(False, (), "non_positive_contracts")
        ask_levels = _ask_levels(book, side)
        if not ask_levels:
            return ExecutionResult(False, (), "no_offers")
        fills, remaining = self._sweep(
            levels=ask_levels,
            contracts=contracts,
            max_price_cents=max_price_cents,
            max_levels=self.config.max_sweep_levels,
        )
        if not fills:
            return ExecutionResult(False, (), "no_fillable_levels")
        if remaining > 0:
            return ExecutionResult(False, (), f"partial_unfilled={remaining}")
        return self._book_fills(market_ticker, side, "buy", fills, now_ms or utc_now_ms())

    def submit_sell(
        self,
        *,
        market_ticker: str,
        side: BookSide,
        contracts: int,
        book: KalshiOrderBook,
        min_price_cents: int | None = None,
        now_ms: int | None = None,
    ) -> ExecutionResult:
        if contracts <= 0:
            return ExecutionResult(False, (), "non_positive_contracts")
        bid_levels = _bid_levels(book, side)
        if not bid_levels:
            return ExecutionResult(False, (), "no_bids")
        position = self.position(market_ticker)
        if position.contracts < contracts or position.side != side:
            return ExecutionResult(False, (), "oversell_position_short")
        fills, remaining = self._sweep(
            levels=bid_levels,
            contracts=contracts,
            max_price_cents=None,
            min_price_cents=min_price_cents,
            max_levels=self.config.max_sweep_levels,
        )
        if not fills:
            return ExecutionResult(False, (), "no_fillable_levels")
        if remaining > 0:
            return ExecutionResult(False, (), f"partial_unfilled={remaining}")
        return self._book_fills(market_ticker, side, "sell", fills, now_ms or utc_now_ms())

    def submit_passive_buy(
        self,
        *,
        market_ticker: str,
        side: BookSide,
        contracts: int,
        post_price_cents: int,
        queue_ahead: int,
        expected_tape_consumption_30s: int,
        adverse_selection_cents: int = 0,
        now_ms: int | None = None,
    ) -> ExecutionResult:
        """Simulate a maker post-only fill against a queue model.

        Per the blueprint::

            fill_score = expected_tape_consumption_30s / (queue_ahead + own_size)

        Below 1.0 the post is treated as un-fillable (would be cancelled or
        replaced in production). At or above 1.0 we treat it as filled at
        ``post_price_cents`` with maker fees; ``adverse_selection_cents`` is an
        optional realized P&L drag for passive fills.
        """
        if contracts <= 0:
            return ExecutionResult(False, (), "non_positive_contracts")
        if post_price_cents <= 0 or post_price_cents >= 100:
            return ExecutionResult(False, (), "post_price_out_of_range")
        denom = max(queue_ahead + contracts, 1)
        fill_score = expected_tape_consumption_30s / denom
        if fill_score < 1.0:
            return ExecutionResult(False, (), f"fill_score={fill_score:.2f}_below_1")

        self._order_counter += 1
        ts_ms = now_ms or utc_now_ms()
        client_id = f"PAPER-MAKER-{market_ticker}-{ts_ms}-{self._order_counter}"
        from kalshi_btc_engine_v2.policy.edge import kalshi_maker_fee_cents

        fee_cents = kalshi_maker_fee_cents(post_price_cents, contracts)
        fill = ExecutionFill(
            market_ticker=market_ticker,
            side=side,
            action="buy",
            contracts=contracts,
            price_cents=post_price_cents + adverse_selection_cents,
            fee_cents=fee_cents,
            client_order_id=client_id,
            mode="paper",
            timestamp_ms=ts_ms,
            is_taker=False,
        )
        position = self.position(market_ticker)
        self._apply_fill(position, fill)
        self.fills.append(fill)
        self.risk_guard.record_fill(
            market_ticker=market_ticker,
            count=contracts,
            price_cents=fill.price_cents,
            source="engine",
        )
        return ExecutionResult(accepted=True, fills=(fill,), position_after=position)

    def _sweep(
        self,
        *,
        levels: list[tuple[int, int]],
        contracts: int,
        max_levels: int,
        max_price_cents: int | None = None,
        min_price_cents: int | None = None,
    ) -> tuple[list[tuple[int, int]], int]:
        # Each level entry is (price_cents, displayed_qty).
        remaining = contracts
        out: list[tuple[int, int]] = []
        for level_index, (price_cents, qty) in enumerate(levels):
            if level_index >= max_levels:
                break
            if max_price_cents is not None and price_cents > max_price_cents:
                break
            if min_price_cents is not None and price_cents < min_price_cents:
                break
            take = min(qty, remaining)
            if take <= 0:
                continue
            out.append((price_cents, take))
            remaining -= take
            if remaining == 0:
                break
        return out, remaining

    def _book_fills(
        self,
        market_ticker: str,
        side: BookSide,
        action: str,
        fills: list[tuple[int, int]],
        now_ms: int,
    ) -> ExecutionResult:
        position = self.position(market_ticker)
        emitted: list[ExecutionFill] = []
        for price_cents, qty in fills:
            self._order_counter += 1
            client_id = f"PAPER-{market_ticker}-{now_ms}-{self._order_counter}"
            fee_cents = kalshi_taker_fee_cents(price_cents, qty)
            fill = ExecutionFill(
                market_ticker=market_ticker,
                side=side,
                action=action,  # type: ignore[arg-type]
                contracts=qty,
                price_cents=price_cents,
                fee_cents=fee_cents,
                client_order_id=client_id,
                mode="paper",
                timestamp_ms=now_ms,
                is_taker=True,
            )
            self._apply_fill(position, fill)
            self.fills.append(fill)
            emitted.append(fill)
            self.risk_guard.record_fill(
                market_ticker=market_ticker,
                count=qty,
                price_cents=price_cents,
                source="engine",
            )
        return ExecutionResult(
            accepted=True,
            fills=tuple(emitted),
            position_after=Position(
                market_ticker=position.market_ticker,
                side=position.side,
                contracts=position.contracts,
                avg_entry_price_cents=position.avg_entry_price_cents,
                realized_pnl_cents=position.realized_pnl_cents,
                fees_paid_cents=position.fees_paid_cents,
            ),
        )

    def _apply_fill(self, position: Position, fill: ExecutionFill) -> None:
        position.fees_paid_cents += fill.fee_cents
        if fill.action == "buy":
            if position.is_flat or position.side == fill.side:
                new_total = position.contracts + fill.contracts
                if new_total > 0:
                    new_avg = (
                        position.avg_entry_price_cents * position.contracts
                        + fill.price_cents * fill.contracts
                    ) / new_total
                else:
                    new_avg = float(fill.price_cents)
                position.contracts = new_total
                position.avg_entry_price_cents = new_avg
                position.side = fill.side
            else:
                # opposite-side buy: not a normal path; treat as new position after flatten
                position.contracts = fill.contracts
                position.avg_entry_price_cents = float(fill.price_cents)
                position.side = fill.side
        else:  # sell
            if position.side == fill.side and position.contracts >= fill.contracts:
                # Realized P&L per contract = sell_price - avg_entry
                pnl = (fill.price_cents - position.avg_entry_price_cents) * fill.contracts
                position.realized_pnl_cents += pnl
                position.contracts -= fill.contracts
                if position.contracts == 0:
                    position.side = None
                    position.avg_entry_price_cents = 0.0
            else:
                # Defensive: blocked at submit_sell, should not reach here.
                position.realized_pnl_cents -= fill.contracts * fill.price_cents


def _ask_levels(book: KalshiOrderBook, side: BookSide) -> list[tuple[int, int]]:
    """Return ascending-by-price ask levels for `side` as (cents, displayed_qty)."""
    # YES ask = 1 - NO bid, YES ask depth = NO bid depth (mirror).
    opposite = book.no_bids if side == "yes" else book.yes_bids
    out: list[tuple[int, int]] = []
    for opp_price, opp_qty in opposite.items():
        ask_price = Decimal("1") - opp_price
        out.append((_dollars_to_cents(ask_price), _qty_to_int(opp_qty)))
    out.sort(key=lambda item: item[0])
    return out


def _bid_levels(book: KalshiOrderBook, side: BookSide) -> list[tuple[int, int]]:
    """Return descending-by-price bid levels for `side` as (cents, displayed_qty)."""
    same = book.yes_bids if side == "yes" else book.no_bids
    out = [(_dollars_to_cents(price), _qty_to_int(qty)) for price, qty in same.items()]
    out.sort(key=lambda item: item[0], reverse=True)
    return out


def _dollars_to_cents(value: Decimal) -> int:
    return int((value * Decimal("100")).to_integral_value())


def _qty_to_int(value: Decimal) -> int:
    return int(value.to_integral_value())


def fill_summary(fills: Iterable[ExecutionFill]) -> dict[str, Any]:
    total_contracts = 0
    total_notional_cents = 0
    total_fees_cents = 0
    for fill in fills:
        total_contracts += fill.contracts
        total_notional_cents += fill.contracts * fill.price_cents
        total_fees_cents += fill.fee_cents
    return {
        "fills": list(fills) if not isinstance(fills, list) else fills,
        "total_contracts": total_contracts,
        "total_notional_dollars": total_notional_cents / 100.0,
        "total_fees_dollars": total_fees_cents / 100.0,
    }
