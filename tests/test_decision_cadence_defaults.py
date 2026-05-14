from __future__ import annotations

from kalshi_btc_engine_v2.backtest.runner import (
    DEFAULT_DECISION_INTERVAL_MS,
    BacktestConfig,
)
from kalshi_btc_engine_v2.cli import build_parser


def test_backtest_config_default_decision_interval_is_latency_budgeted() -> None:
    assert DEFAULT_DECISION_INTERVAL_MS == 250
    assert BacktestConfig().decision_interval_ms == 250


def test_backtest_cli_default_decision_interval_is_latency_budgeted() -> None:
    parser = build_parser()
    args = parser.parse_args(["backtest", "--db", "dummy.sqlite"])

    assert args.decision_interval_ms == DEFAULT_DECISION_INTERVAL_MS
