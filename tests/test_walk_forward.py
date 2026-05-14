from __future__ import annotations

from kalshi_btc_engine_v2.backtest.walk_forward import (
    MS_PER_DAY,
    WalkForwardConfig,
    generate_windows,
)


def test_generates_zero_windows_when_horizon_too_short():
    windows = generate_windows(
        available_start_ms=0,
        available_end_ms=2 * MS_PER_DAY,
        config=WalkForwardConfig(train_days=5, validate_days=1, test_days=1, step_days=1),
    )
    assert windows == []


def test_generates_rolling_windows():
    cfg = WalkForwardConfig(train_days=5, validate_days=1, test_days=1, step_days=1)
    windows = generate_windows(
        available_start_ms=0,
        available_end_ms=10 * MS_PER_DAY,
        config=cfg,
    )
    # span = 7 days, step = 1 day, horizon 10 → 4 windows fit
    assert len(windows) == 4
    for w in windows:
        assert w.train_end_ms - w.train_start_ms == cfg.train_days * MS_PER_DAY
        assert w.validate_end_ms - w.validate_start_ms == cfg.validate_days * MS_PER_DAY
        assert w.test_end_ms - w.test_start_ms == cfg.test_days * MS_PER_DAY
        assert w.validate_start_ms == w.train_end_ms
        assert w.test_start_ms == w.validate_end_ms


def test_step_size_controls_overlap():
    cfg = WalkForwardConfig(train_days=2, validate_days=1, test_days=1, step_days=2)
    windows = generate_windows(
        available_start_ms=0,
        available_end_ms=10 * MS_PER_DAY,
        config=cfg,
    )
    # span = 4 days, step = 2 → starts at 0, 2, 4, 6 → 4 windows fit in 10 days
    assert len(windows) == 4
    assert windows[0].train_start_ms == 0
    assert windows[1].train_start_ms == 2 * MS_PER_DAY
    assert windows[2].train_start_ms == 4 * MS_PER_DAY
