"""Pine Script directional-score port.

Port of the 2026-03-22 confidence-tiers Pine Script that the user has
validated as profitable on TradingView. The Pine Script predicts BTC
direction (CALL/PUT) over a 15-minute window using BTC OHLCV directly,
without reference to contract pricing. This module reproduces the score
math in pure Python so the engine can use it as either:

1. A standalone signal (see ``scripts/live_paper_ta.py``)
2. A sidecar diagnostic alongside ``q_cal`` (see Backtester integration)

The score is computed per 1-minute OHLC bar:

    cycleReturnPct = (close - cycleOpen) / cycleOpen * 100
    emaSpreadPct   = (emaFast - emaSlow) / close * 100
    rsiBias        = (rsi - 50) / 50
    candlePressure = (close - open) / (high - low)   if high != low else 0
    relVol         = clamp(volume / sma(volume, N), 0, 3)

    rawScore = 120*cycleReturnPct + 200*emaSpreadPct + 25*rsiBias
             + 15*candlePressure + 10*(relVol - 1)
    score    = ema(rawScore, 2)

We don't have BTC spot trade volume in the capture (``spot_trade_event``
is empty). Volume term is supported but defaults to relVol=1.0 (zero
contribution to score) when no volume data is available.

Reference: ``successful-pinescript/PS-03222026-confidence-tiers-@version=5.txt``
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Literal

Tier = Literal["STRONG", "MEDIUM", "WEAK", "MIMIC", "SKIP"]
Side = Literal["call", "put", "none"]


@dataclass(frozen=True, slots=True)
class OHLCBar:
    """One 1-minute bar of BTC spot mid-price data.

    ``cycle_open_price`` is the open price of the 15-min cycle this bar
    belongs to. ``bars_in_cycle`` is the 1-indexed position within the
    cycle (1, 2, ..., 15).
    """

    ts_minute_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    cycle_open_price: float
    bars_in_cycle: int


@dataclass(frozen=True, slots=True)
class TAScoreConfig:
    fast_ema_len: int = 5
    slow_ema_len: int = 13
    rsi_len: int = 7
    vol_avg_len: int = 20

    # Phase thresholds
    decision_start_bar: int = 3
    late_fallback_bar: int = 7
    force_entry_bar: int = 13

    # Confidence floors
    entry_thresh: float = 20.0
    late_entry_thresh: float = 1.0

    # Filter flags
    score_velocity_cap: float = 30.0
    require_decel: bool = False
    require_vel_align: bool = True
    filter_bad_hours_utc: tuple[int, ...] = (9, 15, 16)
    confirm_bars: int = 1

    # Tier thresholds
    strong_thresh: float = 75.0
    medium_thresh: float = 50.0

    # Tier multipliers
    strong_mult: float = 4.0
    medium_mult: float = 2.0
    weak_mult: float = 1.0
    mimic_mult: float = 0.5

    # Sweet-spot bonus (extra stake in conf ∈ [lo, hi])
    sweet_spot_lo: float = 50.0
    sweet_spot_hi: float = 80.0
    sweet_spot_bonus: float = 1.0


@dataclass(frozen=True, slots=True)
class ScoreSnapshot:
    """One score computation. All Pine Script intermediates exposed for
    decision logging and offline analysis."""

    ts_minute_ms: int
    bars_in_cycle: int

    cycle_return_pct: float
    ema_fast: float
    ema_slow: float
    ema_spread_pct: float
    rsi: float
    rsi_bias: float
    rel_vol: float
    candle_pressure: float

    raw_score: float
    score: float
    score_velocity: float

    bull_conf: float
    bear_conf: float
    bull_tier: int  # 4=STRONG ... 0=SKIP
    bear_tier: int


@dataclass
class TAScoreState:
    """Mutable rolling state. One instance per BTC symbol you track."""

    config: TAScoreConfig = field(default_factory=TAScoreConfig)

    # Rolling buffers
    _closes: deque[float] = field(default_factory=lambda: deque(maxlen=64))
    _vols: deque[float] = field(default_factory=lambda: deque(maxlen=64))
    _gains: deque[float] = field(default_factory=lambda: deque(maxlen=64))
    _losses: deque[float] = field(default_factory=lambda: deque(maxlen=64))

    # EMA state (Wilder-style or simple — Pine's ta.ema uses 2/(n+1) alpha)
    _ema_fast_prev: float | None = None
    _ema_slow_prev: float | None = None
    _ema_score_prev: float | None = None

    # RSI state (Wilder-style)
    _avg_gain: float | None = None
    _avg_loss: float | None = None

    # Score history (for velocity)
    _score_prev: float | None = None
    _score_prev2: float | None = None

    @staticmethod
    def _ema_step(prev: float | None, value: float, length: int) -> float:
        """Pine's ta.ema is exponential with alpha = 2/(n+1).

        First value seeds the EMA (Pine uses ta.sma to seed; for simplicity
        we use the value itself, which converges within a few bars).
        """
        if prev is None:
            return value
        alpha = 2.0 / (length + 1)
        return alpha * value + (1.0 - alpha) * prev

    def _update_rsi(self, close: float, prev_close: float | None) -> float:
        """Wilder RSI update."""
        if prev_close is None:
            return 50.0
        delta = close - prev_close
        gain = max(0.0, delta)
        loss = max(0.0, -delta)
        n = self.config.rsi_len
        if self._avg_gain is None or self._avg_loss is None:
            self._gains.append(gain)
            self._losses.append(loss)
            if len(self._gains) < n:
                return 50.0
            self._avg_gain = sum(self._gains) / n
            self._avg_loss = sum(self._losses) / n
        else:
            self._avg_gain = (self._avg_gain * (n - 1) + gain) / n
            self._avg_loss = (self._avg_loss * (n - 1) + loss) / n
        if self._avg_loss == 0:
            return 100.0
        rs = self._avg_gain / self._avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def update(self, bar: OHLCBar) -> ScoreSnapshot:
        """Ingest one 1-minute bar, return the score snapshot for that bar."""
        cfg = self.config
        prev_close = self._closes[-1] if self._closes else None

        # EMA fast/slow
        ema_fast = self._ema_step(self._ema_fast_prev, bar.close, cfg.fast_ema_len)
        ema_slow = self._ema_step(self._ema_slow_prev, bar.close, cfg.slow_ema_len)
        self._ema_fast_prev = ema_fast
        self._ema_slow_prev = ema_slow

        ema_spread_pct = (
            ((ema_fast - ema_slow) / bar.close * 100.0) if bar.close != 0 else 0.0
        )

        # RSI
        rsi = self._update_rsi(bar.close, prev_close)
        rsi_bias = (rsi - 50.0) / 50.0

        # Volume
        if bar.volume is not None:
            self._vols.append(float(bar.volume))
        if bar.volume is not None and len(self._vols) >= cfg.vol_avg_len:
            avg_vol = sum(list(self._vols)[-cfg.vol_avg_len :]) / cfg.vol_avg_len
            rel_vol = bar.volume / avg_vol if avg_vol > 0 else 1.0
        else:
            rel_vol = 1.0
        rel_vol_clamped = max(0.0, min(rel_vol, 3.0))

        # Candle pressure
        rng = bar.high - bar.low
        candle_pressure = ((bar.close - bar.open) / rng) if rng > 0 else 0.0

        # Cycle return
        cycle_return_pct = (
            ((bar.close - bar.cycle_open_price) / bar.cycle_open_price * 100.0)
            if bar.cycle_open_price != 0
            else 0.0
        )

        # Raw score
        raw_score = (
            120.0 * cycle_return_pct
            + 200.0 * ema_spread_pct
            + 25.0 * rsi_bias
            + 15.0 * candle_pressure
            + 10.0 * (rel_vol_clamped - 1.0)
        )

        # Smoothed score
        score = self._ema_step(self._ema_score_prev, raw_score, 2)
        score_velocity = (score - self._score_prev) if self._score_prev is not None else 0.0

        # Roll buffers
        self._score_prev2 = self._score_prev
        self._score_prev = score
        self._ema_score_prev = score
        self._closes.append(bar.close)

        # Tier (per side)
        bull_conf = max(0.0, min(score, 100.0))
        bear_conf = max(0.0, min(-score, 100.0))

        def _tier(conf: float, bars: int) -> int:
            active_floor = cfg.late_entry_thresh if bars >= cfg.late_fallback_bar else cfg.entry_thresh
            if conf >= cfg.strong_thresh:
                return 4
            if conf >= cfg.medium_thresh:
                return 3
            if conf >= cfg.entry_thresh:
                return 2
            if conf >= active_floor:
                return 1
            return 0

        bull_tier = _tier(bull_conf, bar.bars_in_cycle)
        bear_tier = _tier(bear_conf, bar.bars_in_cycle)

        return ScoreSnapshot(
            ts_minute_ms=bar.ts_minute_ms,
            bars_in_cycle=bar.bars_in_cycle,
            cycle_return_pct=cycle_return_pct,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            ema_spread_pct=ema_spread_pct,
            rsi=rsi,
            rsi_bias=rsi_bias,
            rel_vol=rel_vol_clamped,
            candle_pressure=candle_pressure,
            raw_score=raw_score,
            score=score,
            score_velocity=score_velocity,
            bull_conf=bull_conf,
            bear_conf=bear_conf,
            bull_tier=bull_tier,
            bear_tier=bear_tier,
        )


@dataclass(frozen=True, slots=True)
class TADecision:
    """The Pine Script lock-in decision for a cycle."""

    side: Side
    tier: int
    tier_name: Tier
    stake_multiplier: float
    confidence: float
    locked_at_bar: int
    locked_at_ts_ms: int
    forced: bool
    snapshot: ScoreSnapshot


def evaluate_entry(
    snapshot: ScoreSnapshot,
    *,
    config: TAScoreConfig,
    hour_utc: int,
    already_decided: bool,
    consecutive_call_bars: int,
    consecutive_put_bars: int,
) -> TADecision | None:
    """Apply the Pine Script's three-phase entry logic to a score snapshot.

    Returns a TADecision if a CALL or PUT lock-in should fire on this bar,
    else None. The caller is responsible for tracking consecutive-bar streaks
    and whether the cycle already has a decision.
    """
    cfg = config
    if already_decided:
        return None
    if snapshot.bars_in_cycle < cfg.decision_start_bar:
        return None

    is_late = snapshot.bars_in_cycle >= cfg.late_fallback_bar
    is_force = snapshot.bars_in_cycle >= cfg.force_entry_bar

    velocity_ok = abs(snapshot.score_velocity) <= cfg.score_velocity_cap
    time_ok = hour_utc not in cfg.filter_bad_hours_utc

    vel_align_call = (not cfg.require_vel_align) or (snapshot.score_velocity > 0)
    vel_align_put = (not cfg.require_vel_align) or (snapshot.score_velocity < 0)

    # Late-phase relaxations: vel-align + time filter bypassed
    if is_late:
        vel_align_call = True
        vel_align_put = True
        time_ok = True

    bull_qualifies = snapshot.bull_tier >= 1
    bear_qualifies = snapshot.bear_tier >= 1

    call_qualified = bull_qualifies and velocity_ok and time_ok and vel_align_call
    put_qualified = bear_qualifies and velocity_ok and time_ok and vel_align_put

    # Forced entry overrides everything except score-sign
    force_call = is_force and snapshot.score > 0
    force_put = is_force and snapshot.score < 0
    if force_call:
        call_qualified = True
    if force_put:
        put_qualified = True

    # Confirmation: late/force = 1 bar, otherwise cfg.confirm_bars
    active_confirm = 1 if (is_late or is_force) else cfg.confirm_bars
    call_active = call_qualified and (consecutive_call_bars + 1) >= active_confirm
    put_active = put_qualified and (consecutive_put_bars + 1) >= active_confirm

    if call_active and call_qualified:
        tier = snapshot.bull_tier if snapshot.bull_tier > 0 else 1  # forced → MIMIC
        return _build_decision(
            "call", tier, snapshot.bull_conf, snapshot, force_call, cfg
        )
    if put_active and put_qualified:
        tier = snapshot.bear_tier if snapshot.bear_tier > 0 else 1
        return _build_decision(
            "put", tier, snapshot.bear_conf, snapshot, force_put, cfg
        )
    return None


def _tier_name(tier: int) -> Tier:
    return {4: "STRONG", 3: "MEDIUM", 2: "WEAK", 1: "MIMIC"}.get(tier, "SKIP")


def _tier_mult(tier: int, cfg: TAScoreConfig) -> float:
    return {
        4: cfg.strong_mult,
        3: cfg.medium_mult,
        2: cfg.weak_mult,
        1: cfg.mimic_mult,
    }.get(tier, 0.0)


def _build_decision(
    side: Side,
    tier: int,
    confidence: float,
    snapshot: ScoreSnapshot,
    forced: bool,
    cfg: TAScoreConfig,
) -> TADecision:
    return TADecision(
        side=side,
        tier=tier,
        tier_name=_tier_name(tier),
        stake_multiplier=_tier_mult(tier, cfg),
        confidence=confidence,
        locked_at_bar=snapshot.bars_in_cycle,
        locked_at_ts_ms=snapshot.ts_minute_ms,
        forced=forced,
        snapshot=snapshot,
    )
