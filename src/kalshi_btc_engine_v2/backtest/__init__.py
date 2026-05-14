# HANDOFF: owned by Claude (backtest/). Edit only via HANDOFF.md Open Request.
"""Event-driven backtester replaying captured SQLite events through the full
policy/execution pipeline."""

from kalshi_btc_engine_v2.backtest.backfill import backfill_from_lifecycle
from kalshi_btc_engine_v2.backtest.counterfactual import (
    CounterfactualReport,
    CounterfactualTrade,
    hold_to_settlement,
)
from kalshi_btc_engine_v2.backtest.divergence_stats import (
    DivergenceStats,
    divergence_stats,
)
from kalshi_btc_engine_v2.backtest.per_market_report import (
    PerMarketStats,
    per_market_report,
    report_to_dict,
)
from kalshi_btc_engine_v2.backtest.runner import (
    BacktestConfig,
    Backtester,
    BacktestSummary,
    default_strike_provider,
)
from kalshi_btc_engine_v2.backtest.settlement import (
    SettledMarket,
    scan_settled_markets,
)
from kalshi_btc_engine_v2.backtest.state import SimulationState
from kalshi_btc_engine_v2.backtest.trade_patterns import (
    TradePatternConfig,
    TradePatternReport,
    detect_patterns,
)

__all__ = [
    "Backtester",
    "BacktestConfig",
    "BacktestSummary",
    "CounterfactualReport",
    "CounterfactualTrade",
    "DivergenceStats",
    "PerMarketStats",
    "SettledMarket",
    "SimulationState",
    "TradePatternConfig",
    "TradePatternReport",
    "detect_patterns",
    "divergence_stats",
    "per_market_report",
    "report_to_dict",
    "backfill_from_lifecycle",
    "default_strike_provider",
    "hold_to_settlement",
    "scan_settled_markets",
]
