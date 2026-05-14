# HANDOFF: owned by Claude (backtest/). Edit only via HANDOFF.md Open Request.
"""Per-market P&L breakdown.

Given a decision log JSONL + the captured SQLite, produce one row per market:
entries, exits, total fills, realized P&L, average hold time, exit-mode mix,
settlement outcome (if known), and the hold-to-settlement counterfactual delta
(positive = exit rules cost money, negative = exit rules saved money).

Designed for quick post-hoc inspection of a backtest — narrower than the
aggregate `BacktestSummary` but wider than per-trade narratives.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from kalshi_btc_engine_v2.backtest.settlement import scan_settled_markets
from kalshi_btc_engine_v2.policy.edge import kalshi_taker_fee_cents


@dataclass(frozen=True, slots=True)
class PerMarketStats:
    market_ticker: str
    entries: int
    exits: int
    fills: int
    realized_pnl_cents: float
    realized_fees_cents: int
    realized_net_cents: float
    avg_hold_seconds: float
    exit_modes: dict[str, int]
    settled: bool
    yes_won: int | None
    hold_to_settlement_cents: float | None
    delta_vs_hold_cents: float | None


def _entries_and_exits(decisions: list[dict]) -> tuple[list[dict], list[dict]]:
    entries = [d for d in decisions if d.get("action") in {"BUY_YES", "BUY_NO"}]
    exits = [d for d in decisions if d.get("action") == "EXIT"]
    return entries, exits


def per_market_report(
    decision_log_path: str | Path,
    db_path: str | Path,
) -> list[PerMarketStats]:
    decision_log_path = Path(decision_log_path)
    db_path = Path(db_path)

    decisions: list[dict] = []
    with decision_log_path.open(encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            try:
                decisions.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    settled = {s.market_ticker: s.yes_won for s in scan_settled_markets(db_path)}

    by_market: dict[str, list[dict]] = {}
    for d in decisions:
        ticker = d.get("market_ticker") or ""
        by_market.setdefault(ticker, []).append(d)

    out: list[PerMarketStats] = []
    for ticker, market_decisions in sorted(by_market.items()):
        entries, exits = _entries_and_exits(market_decisions)
        if not entries:
            continue

        realized_pnl = 0.0
        realized_fees = 0
        hold_seconds: list[float] = []
        exit_modes: dict[str, int] = {}
        fills = 0

        # Pair each entry to the next same-side exit (chronological).
        sorted_decisions = sorted(market_decisions, key=lambda d: int(d.get("ts_ms", 0)))
        open_position: dict | None = None
        for d in sorted_decisions:
            action = d.get("action")
            if action in {"BUY_YES", "BUY_NO"}:
                open_position = d
                side = d.get("side")
                contracts = int(d.get("contracts") or 0)
                price = int(d.get("yes_ask_cents") if side == "yes" else d.get("no_ask_cents") or 0)
                realized_fees += kalshi_taker_fee_cents(price, contracts)
                fills += 1
            elif action == "EXIT" and open_position is not None:
                side = open_position.get("side")
                entry_px = int(
                    open_position.get("yes_ask_cents")
                    if side == "yes"
                    else open_position.get("no_ask_cents") or 0
                )
                exit_px = int(
                    d.get("yes_bid_cents") if side == "yes" else d.get("no_bid_cents") or 0
                )
                contracts = int(open_position.get("contracts") or 0)
                realized_pnl += (exit_px - entry_px) * contracts
                realized_fees += kalshi_taker_fee_cents(exit_px, contracts)
                fills += 1
                hold_seconds.append(
                    (int(d.get("ts_ms", 0)) - int(open_position.get("ts_ms", 0))) / 1000.0
                )
                mode = d.get("exit_mode") or "unknown"
                exit_modes[mode] = exit_modes.get(mode, 0) + 1
                open_position = None

        avg_hold = sum(hold_seconds) / len(hold_seconds) if hold_seconds else 0.0

        # Hold-to-settlement counterfactual on all entries.
        hold_pnl: float | None = None
        if ticker in settled:
            hold_pnl = 0.0
            yes_won = settled[ticker]
            for e in entries:
                side = e.get("side") or ""
                contracts = int(e.get("contracts") or 0)
                entry_px = int(
                    e.get("yes_ask_cents") if side == "yes" else e.get("no_ask_cents") or 0
                )
                side_won = (side == "yes" and yes_won == 1) or (side == "no" and yes_won == 0)
                payout = 100.0 if side_won else 0.0
                hold_pnl += (payout - entry_px) * contracts

        delta_vs_hold = (realized_pnl - hold_pnl) if hold_pnl is not None else None

        out.append(
            PerMarketStats(
                market_ticker=ticker,
                entries=len(entries),
                exits=len(exits),
                fills=fills,
                realized_pnl_cents=realized_pnl,
                realized_fees_cents=realized_fees,
                realized_net_cents=realized_pnl - realized_fees,
                avg_hold_seconds=avg_hold,
                exit_modes=exit_modes,
                settled=ticker in settled,
                yes_won=settled.get(ticker),
                hold_to_settlement_cents=hold_pnl,
                delta_vs_hold_cents=delta_vs_hold,
            )
        )
    return out


def report_to_dict(stats: list[PerMarketStats]) -> dict:
    total_net = sum(s.realized_net_cents for s in stats)
    total_hold = sum(s.hold_to_settlement_cents or 0.0 for s in stats if s.settled)
    total_delta = sum(s.delta_vs_hold_cents for s in stats if s.delta_vs_hold_cents is not None)
    return {
        "markets_with_entries": len(stats),
        "total_realized_net_cents": total_net,
        "total_hold_to_settlement_cents": total_hold,
        "total_delta_vs_hold_cents": total_delta,
        "per_market": [
            {
                "ticker": s.market_ticker,
                "entries": s.entries,
                "exits": s.exits,
                "fills": s.fills,
                "realized_pnl_cents": s.realized_pnl_cents,
                "realized_fees_cents": s.realized_fees_cents,
                "realized_net_cents": s.realized_net_cents,
                "avg_hold_seconds": round(s.avg_hold_seconds, 1),
                "exit_modes": s.exit_modes,
                "settled": s.settled,
                "yes_won": s.yes_won,
                "hold_to_settlement_cents": s.hold_to_settlement_cents,
                "delta_vs_hold_cents": s.delta_vs_hold_cents,
            }
            for s in stats
        ],
    }
