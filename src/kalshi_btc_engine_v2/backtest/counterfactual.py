# HANDOFF: owned by Claude (backtest/). Edit only via HANDOFF.md Open Request.
"""Hold-to-settlement counterfactual.

Given a decision-log JSONL (from `engine-v2 backtest --decision-log ...`) and
a captured SQLite with settled markets, compute the hypothetical P&L if every
entry the engine took had been held all the way to settlement. Compare to the
actual P&L (whatever the exit rules produced) to estimate how much the exit
rules cost or saved.

Useful for tuning the `adverse_revaluation` threshold and similar exit knobs
— the engine's entry signal can be measured against the ideal-hold baseline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from kalshi_btc_engine_v2.backtest.settlement import scan_settled_markets
from kalshi_btc_engine_v2.policy.edge import kalshi_taker_fee_cents


@dataclass(frozen=True, slots=True)
class CounterfactualTrade:
    market_ticker: str
    side: str
    contracts: int
    entry_price_cents: int
    yes_won: int
    directionally_correct: bool
    hold_pnl_cents: float
    estimated_round_trip_fees_cents: int
    actual_exit_estimated_cents: float | None


@dataclass(frozen=True, slots=True)
class CounterfactualReport:
    entries: int
    settled_entries: int
    directionally_correct: int
    hold_to_settlement_gross_cents: float
    hold_to_settlement_fees_cents: int
    hold_to_settlement_net_cents: float
    actual_exit_estimated_cents: float | None
    delta_cents: float | None
    trades: tuple[CounterfactualTrade, ...]


def _hold_pnl(side: str, contracts: int, entry_cents: int, yes_won: int) -> float:
    """Per the binary payoff: NO settles at 100 if NO won, 0 if YES won; vice versa."""
    side_won = (side == "yes" and yes_won == 1) or (side == "no" and yes_won == 0)
    payout = 100.0 if side_won else 0.0
    return (payout - entry_cents) * contracts


def hold_to_settlement(
    decision_log_path: str | Path,
    db_path: str | Path,
) -> CounterfactualReport:
    decision_log_path = Path(decision_log_path)
    db_path = Path(db_path)

    settled = {s.market_ticker: s.yes_won for s in scan_settled_markets(db_path)}
    entries: list[dict] = []
    exits_by_market: dict[str, list[dict]] = {}
    with decision_log_path.open(encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            action = record.get("action")
            if action in {"BUY_YES", "BUY_NO"}:
                entries.append(record)
            elif action == "EXIT":
                exits_by_market.setdefault(record.get("market_ticker", ""), []).append(record)

    trades: list[CounterfactualTrade] = []
    settled_entries = 0
    correct = 0
    hold_gross = 0.0
    fees_total = 0
    actual_total: float | None = 0.0
    actual_known_any = False
    for entry in entries:
        ticker = entry.get("market_ticker", "")
        side = entry.get("side") or ""
        contracts = int(entry.get("contracts") or 0)
        entry_cents = int(
            entry.get("yes_ask_cents") if side == "yes" else entry.get("no_ask_cents") or 0
        )
        yes_won = settled.get(ticker)
        if yes_won is None:
            trades.append(
                CounterfactualTrade(
                    market_ticker=ticker,
                    side=side,
                    contracts=contracts,
                    entry_price_cents=entry_cents,
                    yes_won=-1,
                    directionally_correct=False,
                    hold_pnl_cents=0.0,
                    estimated_round_trip_fees_cents=0,
                    actual_exit_estimated_cents=None,
                )
            )
            continue

        settled_entries += 1
        bet_yes = side == "yes"
        is_correct = (bet_yes and yes_won == 1) or (not bet_yes and yes_won == 0)
        if is_correct:
            correct += 1

        hold_pnl = _hold_pnl(side, contracts, entry_cents, yes_won)
        # Round-trip fees: entry taker + (zero) settlement (no fee at settlement on Kalshi)
        entry_fee = kalshi_taker_fee_cents(entry_cents, contracts)
        round_trip_fees = entry_fee  # holding to settlement => no exit fee

        actual_exit_cents: float | None = None
        related_exit = next(
            (e for e in exits_by_market.get(ticker, []) if (e.get("side") or "") == side),
            None,
        )
        if related_exit is not None:
            exit_bid = (
                related_exit.get("yes_bid_cents")
                if side == "yes"
                else related_exit.get("no_bid_cents")
            )
            if exit_bid is not None:
                exit_price = int(exit_bid)
                exit_fee = kalshi_taker_fee_cents(exit_price, contracts)
                actual_exit_cents = (exit_price - entry_cents) * contracts - entry_fee - exit_fee

        trades.append(
            CounterfactualTrade(
                market_ticker=ticker,
                side=side,
                contracts=contracts,
                entry_price_cents=entry_cents,
                yes_won=yes_won,
                directionally_correct=is_correct,
                hold_pnl_cents=hold_pnl,
                estimated_round_trip_fees_cents=round_trip_fees,
                actual_exit_estimated_cents=actual_exit_cents,
            )
        )
        hold_gross += hold_pnl
        fees_total += round_trip_fees
        if actual_exit_cents is not None:
            actual_total = (actual_total or 0.0) + actual_exit_cents
            actual_known_any = True

    if not actual_known_any:
        actual_total = None

    delta = None
    if actual_total is not None:
        delta = (hold_gross - fees_total) - actual_total

    return CounterfactualReport(
        entries=len(entries),
        settled_entries=settled_entries,
        directionally_correct=correct,
        hold_to_settlement_gross_cents=hold_gross,
        hold_to_settlement_fees_cents=fees_total,
        hold_to_settlement_net_cents=hold_gross - fees_total,
        actual_exit_estimated_cents=actual_total,
        delta_cents=delta,
        trades=tuple(trades),
    )
