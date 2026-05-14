"""Tests for the Pine Script TA-score port.

We're not trying to byte-match Pine Script (EMA seeding differs between
``ta.ema`` and our first-value-seed). We're checking that the score has
the right sign and ordering across deliberately constructed sequences,
and that the entry-phase logic matches the Pine Script's three-phase
description.
"""
from __future__ import annotations

from kalshi_btc_engine_v2.features.ta_score import (
    OHLCBar,
    ScoreSnapshot,
    TAScoreConfig,
    TAScoreState,
    evaluate_entry,
)


def _snap(*, bars_in_cycle: int, score: float, score_velocity: float,
          bull_conf: float | None = None, bear_conf: float | None = None,
          bull_tier: int | None = None, bear_tier: int | None = None) -> ScoreSnapshot:
    """Build a synthetic ScoreSnapshot for entry-logic tests in isolation."""
    bc = max(0.0, score) if bull_conf is None else bull_conf
    br = max(0.0, -score) if bear_conf is None else bear_conf
    cfg = TAScoreConfig()
    def _t(c, b):
        floor = cfg.late_entry_thresh if b >= cfg.late_fallback_bar else cfg.entry_thresh
        if c >= cfg.strong_thresh: return 4
        if c >= cfg.medium_thresh: return 3
        if c >= cfg.entry_thresh: return 2
        if c >= floor: return 1
        return 0
    bt = _t(bc, bars_in_cycle) if bull_tier is None else bull_tier
    rt = _t(br, bars_in_cycle) if bear_tier is None else bear_tier
    return ScoreSnapshot(
        ts_minute_ms=bars_in_cycle * 60_000,
        bars_in_cycle=bars_in_cycle,
        cycle_return_pct=0.0,
        ema_fast=0.0, ema_slow=0.0, ema_spread_pct=0.0,
        rsi=50.0, rsi_bias=0.0, rel_vol=1.0, candle_pressure=0.0,
        raw_score=score, score=score, score_velocity=score_velocity,
        bull_conf=bc, bear_conf=br, bull_tier=bt, bear_tier=rt,
    )


def _make_bar(
    ts: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    cycle_open: float,
    bars_in_cycle: int,
    volume: float | None = None,
) -> OHLCBar:
    return OHLCBar(
        ts_minute_ms=ts,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        cycle_open_price=cycle_open,
        bars_in_cycle=bars_in_cycle,
    )


def test_score_positive_for_uptrending_bars():
    """All bars closing higher than they opened, in a rising cycle, should
    produce a positive score (bullConf > 0, bearConf == 0)."""
    state = TAScoreState()
    cycle_open = 100_000.0
    last_close = cycle_open
    snapshot = None
    for i in range(1, 11):
        # Each bar: open at last_close, close 0.05% higher
        op = last_close
        cl = op * 1.0005
        snapshot = state.update(
            _make_bar(ts=i * 60_000, open_=op, high=cl, low=op, close=cl, cycle_open=cycle_open, bars_in_cycle=i)
        )
        last_close = cl
    assert snapshot is not None
    assert snapshot.score > 0.0
    assert snapshot.bull_conf > 0.0
    assert snapshot.bear_conf == 0.0
    assert snapshot.cycle_return_pct > 0.0


def test_score_negative_for_downtrending_bars():
    state = TAScoreState()
    cycle_open = 100_000.0
    last_close = cycle_open
    snapshot = None
    for i in range(1, 11):
        op = last_close
        cl = op * 0.9995  # 0.05% drop
        snapshot = state.update(
            _make_bar(ts=i * 60_000, open_=op, high=op, low=cl, close=cl, cycle_open=cycle_open, bars_in_cycle=i)
        )
        last_close = cl
    assert snapshot is not None
    assert snapshot.score < 0.0
    assert snapshot.bear_conf > 0.0
    assert snapshot.bull_conf == 0.0


def test_no_entry_before_decision_start_bar():
    """Pine Script: bars 1-2 cannot fire an entry, regardless of score."""
    state = TAScoreState()
    cfg = TAScoreConfig()
    cycle_open = 100_000.0
    # Bar 1: very strong bullish
    snap = state.update(
        _make_bar(ts=60_000, open_=cycle_open, high=cycle_open * 1.01, low=cycle_open, close=cycle_open * 1.01, cycle_open=cycle_open, bars_in_cycle=1)
    )
    decision = evaluate_entry(
        snap,
        config=cfg,
        hour_utc=12,
        already_decided=False,
        consecutive_call_bars=0,
        consecutive_put_bars=0,
    )
    assert decision is None  # before decision_start_bar=3


def test_forced_entry_at_bar_13_bypasses_filters():
    """Pine Script: at bar 13+, a non-zero score lean fires an entry even when
    standard filters (time-of-day, velocity-alignment) would normally block.

    We construct a bullish score in a 'bad hour' (UTC 15) — phase 1 would
    block via time filter; phase 3 (bar 13+) overrides it.
    """
    state = TAScoreState()
    cfg = TAScoreConfig()
    cycle_open = 100_000.0
    snap = None
    last_close = cycle_open
    # Strong sustained move — same setup as test_tier_strong_threshold
    for i in range(1, 14):
        op = last_close
        cl = op * 1.001  # 0.1% per minute, sustained
        snap = state.update(
            _make_bar(ts=i * 60_000, open_=op, high=cl, low=op, close=cl,
                       cycle_open=cycle_open, bars_in_cycle=i)
        )
        last_close = cl
    assert snap is not None
    assert snap.score > 0
    # In phase 1, hour 15 would block. In phase 3, it should fire.
    early_state = TAScoreState()
    early_last = cycle_open
    early_snap = None
    for i in range(1, 5):  # bars 1..4, phase 1
        op = early_last
        cl = op * 1.001
        early_snap = early_state.update(
            _make_bar(ts=i * 60_000, open_=op, high=cl, low=op, close=cl,
                       cycle_open=cycle_open, bars_in_cycle=i)
        )
        early_last = cl
    phase1_blocked = evaluate_entry(
        early_snap, config=cfg, hour_utc=15, already_decided=False,
        consecutive_call_bars=0, consecutive_put_bars=0,
    )
    assert phase1_blocked is None  # bad-hour blocks in phase 1
    phase3_fires = evaluate_entry(
        snap, config=cfg, hour_utc=15, already_decided=False,
        consecutive_call_bars=0, consecutive_put_bars=0,
    )
    assert phase3_fires is not None, "phase 3 (bar 13+) overrides bad-hour filter"
    assert phase3_fires.side == "call"


def test_bad_hour_filter_blocks_early_entry_only():
    """Pine Script: bad-hour filter applies in phase 1 (bars 3-6), is
    relaxed in late phase (bar 7+). Synthetic snapshots isolate the
    entry logic from score computation."""
    cfg = TAScoreConfig()
    snap = _snap(bars_in_cycle=4, score=40.0, score_velocity=5.0)
    blocked = evaluate_entry(
        snap, config=cfg, hour_utc=15, already_decided=False,
        consecutive_call_bars=0, consecutive_put_bars=0,
    )
    assert blocked is None, "bad-hour should block entry in phase 1"
    ok = evaluate_entry(
        snap, config=cfg, hour_utc=12, already_decided=False,
        consecutive_call_bars=0, consecutive_put_bars=0,
    )
    assert ok is not None, "non-bad-hour should allow entry"
    assert ok.side == "call"
    # Late phase: bad-hour relaxed, fires even at hour 15
    snap_late = _snap(bars_in_cycle=8, score=40.0, score_velocity=5.0)
    late_ok = evaluate_entry(
        snap_late, config=cfg, hour_utc=15, already_decided=False,
        consecutive_call_bars=0, consecutive_put_bars=0,
    )
    assert late_ok is not None, "late phase bypasses bad-hour filter"


def test_velocity_alignment_blocks_misaligned_signal():
    """If require_vel_align is true (default), a bullish-tier signal whose
    score velocity is negative should not qualify in phase 1."""
    state = TAScoreState()
    cfg = TAScoreConfig()
    cycle_open = 100_000.0
    # Rip up, then drift down — so score is positive but velocity is negative
    closes = [100_500, 101_500, 102_500, 103_000, 102_500, 102_000]  # up then down
    snap = None
    for i, c in enumerate(closes, start=1):
        op = c * 0.999 if i > 1 else cycle_open
        snap = state.update(
            _make_bar(ts=i * 60_000, open_=op, high=max(op, c), low=min(op, c), close=c, cycle_open=cycle_open, bars_in_cycle=i)
        )
    assert snap is not None
    # Score still positive (bullish) but velocity negative (rolling over)
    assert snap.score > 0
    assert snap.score_velocity < 0
    # Should NOT fire CALL because velocity disagrees
    decision = evaluate_entry(
        snap, config=cfg, hour_utc=12, already_decided=False,
        consecutive_call_bars=0, consecutive_put_bars=0,
    )
    # Either skip (None) or fire PUT if score had inverted; this combo should skip
    if decision is not None:
        assert decision.side != "call"


def test_tier_strong_threshold():
    """Score above 75 should hit STRONG tier (4x multiplier). Synthetic
    snapshot so the velocity cap doesn't filter it as a parabolic spike."""
    cfg = TAScoreConfig()
    snap = _snap(bars_in_cycle=5, score=80.0, score_velocity=10.0)
    assert snap.score > cfg.strong_thresh
    assert snap.bull_tier == 4
    decision = evaluate_entry(
        snap, config=cfg, hour_utc=12, already_decided=False,
        consecutive_call_bars=0, consecutive_put_bars=0,
    )
    assert decision is not None
    assert decision.tier == 4
    assert decision.tier_name == "STRONG"
    assert decision.stake_multiplier == cfg.strong_mult


def test_velocity_cap_filters_parabolic_spike():
    """A bullish score with velocity above the 30 cap should NOT fire — Pine
    Script's anti-spike filter. This is the behavior that blocked the
    sustained-0.5%-moves test setup."""
    cfg = TAScoreConfig()
    snap = _snap(bars_in_cycle=5, score=80.0, score_velocity=50.0)  # vel > cap
    decision = evaluate_entry(
        snap, config=cfg, hour_utc=12, already_decided=False,
        consecutive_call_bars=0, consecutive_put_bars=0,
    )
    assert decision is None, "velocity above cap should block entry in phase 1"


def test_no_double_decision_when_already_decided():
    cfg = TAScoreConfig()
    snap = _snap(bars_in_cycle=5, score=40.0, score_velocity=5.0)
    decision = evaluate_entry(
        snap, config=cfg, hour_utc=12, already_decided=True,
        consecutive_call_bars=0, consecutive_put_bars=0,
    )
    assert decision is None
