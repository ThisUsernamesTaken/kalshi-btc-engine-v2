# HANDOFF: owned by Claude (backtest/). Edit only via HANDOFF.md Open Request.
"""Walk-forward validation harness.

Splits the captured event horizon into rolling (train, validate, test) windows
and runs the backtester on each test slice. The training slice is reserved for
fitting calibration / regime / ensemble parameters; v1 just records the slice
boundaries so a future fit step can consume them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from kalshi_btc_engine_v2.backtest.runner import Backtester, BacktestSummary

MS_PER_DAY = 24 * 60 * 60 * 1000


@dataclass(frozen=True, slots=True)
class WalkForwardConfig:
    train_days: int = 5
    validate_days: int = 1
    test_days: int = 1
    step_days: int = 1


@dataclass(frozen=True, slots=True)
class WalkForwardWindow:
    index: int
    train_start_ms: int
    train_end_ms: int
    validate_start_ms: int
    validate_end_ms: int
    test_start_ms: int
    test_end_ms: int


@dataclass(slots=True)
class WalkForwardResult:
    window: WalkForwardWindow
    summary: BacktestSummary


@dataclass(slots=True)
class WalkForwardReport:
    windows: list[WalkForwardResult] = field(default_factory=list)

    def total_net_pnl_cents(self) -> float:
        return sum(r.summary.net_pnl_cents for r in self.windows)

    def total_fills(self) -> int:
        return sum(r.summary.fills for r in self.windows)

    def per_window_pnl(self) -> list[float]:
        return [r.summary.net_pnl_cents for r in self.windows]


def generate_windows(
    available_start_ms: int,
    available_end_ms: int,
    *,
    config: WalkForwardConfig | None = None,
) -> list[WalkForwardWindow]:
    cfg = config or WalkForwardConfig()
    span_ms = (cfg.train_days + cfg.validate_days + cfg.test_days) * MS_PER_DAY
    step_ms = cfg.step_days * MS_PER_DAY
    out: list[WalkForwardWindow] = []
    cursor = available_start_ms
    index = 0
    while cursor + span_ms <= available_end_ms:
        train_start = cursor
        train_end = train_start + cfg.train_days * MS_PER_DAY
        validate_start = train_end
        validate_end = validate_start + cfg.validate_days * MS_PER_DAY
        test_start = validate_end
        test_end = test_start + cfg.test_days * MS_PER_DAY
        out.append(
            WalkForwardWindow(
                index=index,
                train_start_ms=train_start,
                train_end_ms=train_end,
                validate_start_ms=validate_start,
                validate_end_ms=validate_end,
                test_start_ms=test_start,
                test_end_ms=test_end,
            )
        )
        cursor += step_ms
        index += 1
    return out


def run_walk_forward(
    db_path: str | Path,
    *,
    available_start_ms: int,
    available_end_ms: int,
    backtester_factory: Callable[[], Backtester],
    config: WalkForwardConfig | None = None,
) -> WalkForwardReport:
    windows = generate_windows(available_start_ms, available_end_ms, config=config)
    report = WalkForwardReport()
    for window in windows:
        bt = backtester_factory()
        summary = bt.run_db(
            db_path,
            start_ms=window.test_start_ms,
            end_ms=window.test_end_ms,
        )
        report.windows.append(WalkForwardResult(window=window, summary=summary))
    return report
