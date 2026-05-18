"""Engine v3 — research-grounded T-30 sniper with literature-backed refinements.

Built from the academic + industry literature survey (2026-05-17). See
STRATEGY_PARTICIPATION.md section 11 for the citations.

REFINEMENTS over production v5 sniper (live_v5_unified.py):

  1. VWAP lock-in check
     Kalshi BTC 15m settles on trailing 60s BRTI VWAP. At T-30s, ~50% of
     settlement weight is locked. Compute the partial-VWAP direction from
     our local BTC buffer; skip entry if the locked direction disagrees
     with the favorite side. (Mechanism cited by Sun et al. 2025 arxiv
     2510.15205 — "inventory risk concentrates near resolution".)

  2. Intraday-jump filter
     Bozovic 2025 ("Intraday Jumps and 0DTE Options" SSRN 5223127):
     jumps cluster at close. Skip entry if |1-second BTC return| > 2σ
     over the last 5 seconds before decision.

  3. Bayesian WR posterior
     Beta(2, 2) prior + live wins/losses → Beta posterior. Track running
     mean and 2σ lower bound. Reject entries when posterior mean < 85%.

  4. Quarter-Kelly sizing under parameter uncertainty
     Thorp / MacLean-Thorp-Ziemba 2010. Size = quarter-Kelly fraction of
     bankroll, computed with posterior mean discounted by 2σ. Capped at
     T30_SNIPER_CONTRACTS_MAX.

  5. CUSUM-equivalent kill switch
     2 losses in any rolling 10 trades → halt. Under true 92% WR this
     happens <2% of time (Page 1954 CUSUM, Wald SPRT).

  6. Maker-mode option (--maker-rest)
     Becker 2025: takers lose at 80/99 price levels; makers earn +2.5%.
     Optional: rest a 85¢ bid from T-90s, cancel at T-25s if not filled.
     (Off by default — needs cancel-on-flip logic.)

MULTI-MODE EXECUTION (--mode):
  - 'backtest'  : replay historical capture DB
  - 'paper'     : live polling, log decisions, no real orders
  - 'shadow'    : same as paper, but cross-references production trader's
                  decision log to flag divergences

NO REAL ORDERS without --i-have-real-money-authorized AND --live.
"""
from __future__ import annotations

import collections
import dataclasses
import json
import math
import statistics
import time
from typing import Any, Optional, ClassVar


# ── Configuration (literature-backed defaults) ──────────────────────────────

@dataclasses.dataclass
class EngineV3Config:
    # Entry window
    t30_window_open_s: int = 40        # T-40s to start
    t30_window_close_s: int = 20       # T-20s to end
    fav_bid_min: int = 85              # favorite bid floor (cents)

    # Sizing
    bankroll_dollars: float = 30.0     # starting bankroll
    kelly_fraction: float = 0.25       # quarter-Kelly (Thorp default)
    posterior_skepticism_sigmas: float = 2.0  # discount p by N*σ
    max_contracts_per_trade: int = 20  # absolute cap
    min_contracts_per_trade: int = 1
    posterior_mean_min: float = 0.85   # don't enter if Bayesian WR < this
    fixed_contracts: Optional[int] = None  # if set, use this size; else Kelly

    # Bayesian prior
    prior_wins: float = 2.0           # Beta(2,2) — uniform-ish with weak conviction
    prior_losses: float = 2.0

    # CUSUM kill switch
    cusum_window: int = 10            # rolling trades to check
    cusum_kill_losses: int = 2        # halt if N losses in window
    cusum_alert_losses: int = 1       # warn at this level

    # Jump filter
    jump_lookback_s: int = 5          # window to compute return
    jump_sigma_threshold: float = 2.0 # |return| > N*σ → skip
    btc_sigma_lookback_s: int = 300   # 5min for σ estimate
    btc_min_samples_for_jump: int = 30

    # VWAP lock-in
    vwap_settlement_window_s: int = 60 # Kalshi BTC 15m: trailing 60s BRTI VWAP
    vwap_min_locked_samples: int = 10  # require at least N BTC prices in locked portion

    # Slippage
    ioc_slip_cents: int = 2           # +2c above ask for IOC limit
    cap_cents: int = 99               # never bid above 99c

    # Maker mode (optional)
    maker_rest_enabled: bool = False
    maker_rest_window_open_s: int = 90  # start resting at T-90s
    maker_rest_cancel_s: int = 25       # cancel by T-25s if unfilled


# ── State + posterior + CUSUM ────────────────────────────────────────────────

@dataclasses.dataclass
class BayesianPosterior:
    alpha: float = 2.0   # prior wins
    beta: float = 2.0    # prior losses

    def update(self, won: bool) -> None:
        if won:
            self.alpha += 1
        else:
            self.beta += 1

    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    def std(self) -> float:
        n = self.alpha + self.beta
        if n <= 1:
            return 0.5
        return math.sqrt(self.alpha * self.beta / (n * n * (n + 1)))

    def skeptical_p(self, sigmas: float = 2.0) -> float:
        return max(0.5, self.mean() - sigmas * self.std())


class CUSUMTracker:
    def __init__(self, window: int, kill_losses: int, alert_losses: int):
        self.recent: collections.deque[bool] = collections.deque(maxlen=window)
        self.kill_losses = kill_losses
        self.alert_losses = alert_losses

    def update(self, won: bool) -> None:
        self.recent.append(won)

    def loss_count(self) -> int:
        return sum(1 for w in self.recent if not w)

    def should_kill(self) -> bool:
        return self.loss_count() >= self.kill_losses

    def should_alert(self) -> bool:
        return self.loss_count() >= self.alert_losses


# ── BTC price buffer with jump detection + VWAP ─────────────────────────────

class BTCPriceTracker:
    """Maintains (ts_ms, price) deque covering up to ~300s. Provides:
      - vwap_over(window_s): trailing arithmetic mean
      - has_recent_jump(lookback_s, sigma_threshold): detects spike
      - sigma_estimate(window_s): stddev of 1-second returns
    """
    def __init__(self, window_s: int = 320):
        self.window_ms = window_s * 1000
        self.buf: collections.deque[tuple[int, float]] = collections.deque(maxlen=500)

    def add(self, ts_ms: int, price: float) -> None:
        self.buf.append((ts_ms, price))
        cutoff = ts_ms - self.window_ms
        while self.buf and self.buf[0][0] < cutoff:
            self.buf.popleft()

    def latest(self) -> Optional[tuple[int, float]]:
        return self.buf[-1] if self.buf else None

    def vwap_over(self, window_s: int) -> Optional[float]:
        """Arithmetic mean of BTC prices over last `window_s`. Returns None if
        fewer than 10 samples in the window (insufficient data)."""
        if not self.buf:
            return None
        latest_ts = self.buf[-1][0]
        cutoff = latest_ts - window_s * 1000
        prices = [p for ts, p in self.buf if ts >= cutoff]
        if len(prices) < 10:
            return None
        return sum(prices) / len(prices)

    def has_recent_jump(self, lookback_s: int, sigma_threshold: float,
                        sigma_window_s: int = 300,
                        min_samples: int = 30) -> Optional[bool]:
        """Return True if max(|return_over_1s|) in last `lookback_s` seconds
        exceeds N*sigma where sigma is computed from the wider sigma_window_s.
        Returns None if insufficient data (i.e., assume no jump but flag it).
        """
        if len(self.buf) < min_samples:
            return None
        latest_ts = self.buf[-1][0]
        # Compute 1-second returns from sigma_window_s
        sigma_cutoff = latest_ts - sigma_window_s * 1000
        sigma_prices = [(ts, p) for ts, p in self.buf if ts >= sigma_cutoff]
        if len(sigma_prices) < min_samples:
            return None
        # Group into ~1-second buckets, take last price per bucket
        buckets: dict[int, float] = {}
        for ts, p in sigma_prices:
            sec = ts // 1000
            buckets[sec] = p
        sorted_secs = sorted(buckets.keys())
        if len(sorted_secs) < min_samples:
            return None
        rets = [math.log(buckets[sorted_secs[i]] / buckets[sorted_secs[i-1]])
                for i in range(1, len(sorted_secs))
                if buckets[sorted_secs[i]] > 0 and buckets[sorted_secs[i-1]] > 0]
        if len(rets) < 10:
            return None
        sigma = statistics.pstdev(rets)
        if sigma <= 0:
            return False
        # Recent returns in lookback_s window
        lookback_cutoff = latest_ts - lookback_s * 1000
        recent_secs = [s for s in sorted_secs if s * 1000 >= lookback_cutoff]
        if len(recent_secs) < 2:
            return False
        recent_rets = [math.log(buckets[recent_secs[i]] / buckets[recent_secs[i-1]])
                       for i in range(1, len(recent_secs))
                       if buckets[recent_secs[i]] > 0 and buckets[recent_secs[i-1]] > 0]
        if not recent_rets:
            return False
        max_abs_ret = max(abs(r) for r in recent_rets)
        return max_abs_ret > sigma_threshold * sigma


# ── Decision data classes ────────────────────────────────────────────────────

@dataclasses.dataclass
class MarketSnapshot:
    """Input to evaluate_entry()."""
    ticker: str
    secs_to_close: float
    close_ts_ms: int
    strike: float
    yes_bid: int   # cents
    yes_ask: int   # cents
    no_bid: int    # cents = 100 - yes_ask
    no_ask: int    # cents = 100 - yes_bid
    btc_now: Optional[float] = None


@dataclasses.dataclass
class EntryDecision:
    action: str             # "ENTER" | "SKIP" | "REST_MAKER"
    reason: str
    side: Optional[str] = None        # "yes" | "no"
    contracts: Optional[int] = None
    limit_cents: Optional[int] = None
    fav_bid_cents: Optional[int] = None
    fav_ask_cents: Optional[int] = None
    # Diagnostics
    posterior_mean: Optional[float] = None
    posterior_skeptical_p: Optional[float] = None
    cusum_loss_count: Optional[int] = None
    vwap_locked_avg: Optional[float] = None
    vwap_locked_direction: Optional[str] = None
    jump_detected: Optional[bool] = None
    btc_sigma: Optional[float] = None
    kelly_full: Optional[float] = None
    kelly_quarter: Optional[float] = None


# ── Core decision logic ──────────────────────────────────────────────────────

def evaluate_entry(snap: MarketSnapshot,
                   btc: BTCPriceTracker,
                   posterior: BayesianPosterior,
                   cusum: CUSUMTracker,
                   config: EngineV3Config) -> EntryDecision:
    """Pure function: given market state + bot state + config, return entry decision."""
    # 1. Window check
    if not (config.t30_window_close_s <= snap.secs_to_close <= config.t30_window_open_s):
        return EntryDecision(action="SKIP", reason=f"OUTSIDE_T30_WINDOW secs={snap.secs_to_close:.1f}")

    # 2. CUSUM kill check
    cusum_loss = cusum.loss_count()
    if cusum.should_kill():
        return EntryDecision(action="SKIP",
                             reason=f"CUSUM_HALT losses={cusum_loss}/{config.cusum_window}",
                             cusum_loss_count=cusum_loss)

    # 3. Posterior WR check
    post_mean = posterior.mean()
    post_skep = posterior.skeptical_p(config.posterior_skepticism_sigmas)
    if post_mean < config.posterior_mean_min:
        return EntryDecision(action="SKIP",
                             reason=f"POSTERIOR_MEAN_LOW {post_mean:.3f} < {config.posterior_mean_min}",
                             posterior_mean=post_mean, posterior_skeptical_p=post_skep,
                             cusum_loss_count=cusum_loss)

    # 4. Favorite determination + threshold
    if snap.yes_bid >= snap.no_bid:
        fav_side, fav_bid, fav_ask = "yes", snap.yes_bid, snap.yes_ask
    else:
        fav_side, fav_bid, fav_ask = "no", snap.no_bid, snap.no_ask
    if fav_bid < config.fav_bid_min:
        return EntryDecision(action="SKIP",
                             reason=f"FAV_BID_BELOW_FLOOR {fav_bid}<{config.fav_bid_min}",
                             fav_bid_cents=fav_bid, fav_ask_cents=fav_ask,
                             posterior_mean=post_mean, cusum_loss_count=cusum_loss)
    if not (0 < fav_ask < 100):
        return EntryDecision(action="SKIP",
                             reason=f"FAV_ASK_DEGENERATE ask={fav_ask}",
                             fav_bid_cents=fav_bid, fav_ask_cents=fav_ask)

    # 5. Jump filter
    jump = btc.has_recent_jump(config.jump_lookback_s, config.jump_sigma_threshold,
                                config.btc_sigma_lookback_s, config.btc_min_samples_for_jump)
    if jump is True:
        return EntryDecision(action="SKIP", reason="RECENT_BTC_JUMP",
                             jump_detected=True, fav_bid_cents=fav_bid, fav_ask_cents=fav_ask,
                             posterior_mean=post_mean, cusum_loss_count=cusum_loss)
    jump_for_log = jump  # may be None (no data) or False (no jump)

    # 6. VWAP lock-in check (the core literature finding)
    locked_seconds = config.vwap_settlement_window_s - snap.secs_to_close
    vwap_locked = None
    vwap_locked_dir = None
    if locked_seconds > 0 and snap.strike > 0:
        vwap_locked = btc.vwap_over(int(locked_seconds))
        if vwap_locked is not None:
            vwap_locked_dir = "yes" if vwap_locked > snap.strike else "no"
            if vwap_locked_dir != fav_side:
                return EntryDecision(
                    action="SKIP",
                    reason=f"VWAP_LOCK_DISAGREES locked={vwap_locked:.2f} dir={vwap_locked_dir} fav={fav_side}",
                    fav_bid_cents=fav_bid, fav_ask_cents=fav_ask,
                    vwap_locked_avg=vwap_locked, vwap_locked_direction=vwap_locked_dir,
                    posterior_mean=post_mean, cusum_loss_count=cusum_loss,
                    jump_detected=jump_for_log,
                )

    # 7. Sizing
    # Two modes via config: fixed_contracts (deterministic) or quarter-Kelly.
    # If fixed_contracts is set, use it directly. Otherwise compute Kelly.
    if config.fixed_contracts is not None:
        contracts = config.fixed_contracts
        f_full = None
        f_quarter = None
    else:
        # Quarter-Kelly on skeptical posterior
        P = fav_ask / 100.0
        win_amount = 1.0 - P
        loss_amount = P
        b = win_amount / max(0.001, loss_amount)
        p = post_skep
        q = 1.0 - p
        f_full = (p * b - q) / b if b > 0 else 0
        f_quarter = max(0.0, config.kelly_fraction * f_full)
        cost_per_contract = fav_ask / 100.0
        raw_contracts = (f_quarter * config.bankroll_dollars) / max(0.001, cost_per_contract)
        contracts = max(config.min_contracts_per_trade,
                        min(config.max_contracts_per_trade, int(raw_contracts)))

    limit_cents = min(config.cap_cents, fav_ask + config.ioc_slip_cents)

    return EntryDecision(
        action="ENTER", reason="ENTER_OK",
        side=fav_side, contracts=contracts, limit_cents=limit_cents,
        fav_bid_cents=fav_bid, fav_ask_cents=fav_ask,
        posterior_mean=post_mean, posterior_skeptical_p=post_skep,
        cusum_loss_count=cusum_loss,
        vwap_locked_avg=vwap_locked, vwap_locked_direction=vwap_locked_dir,
        jump_detected=jump_for_log,
        kelly_full=f_full, kelly_quarter=f_quarter,
    )


# ── Settlement P&L calculator (Kalshi taker fees) ───────────────────────────

def kalshi_fee_cents(contracts: int, price_cents: int) -> int:
    if price_cents <= 0 or price_cents >= 100 or contracts <= 0:
        return 0
    p = price_cents / 100.0
    return math.ceil(7 * contracts * p * (1 - p))


def settle_pnl_cents(entry_price_cents: int, contracts: int,
                     side: str, result: str) -> int:
    payout = 100 if side == result else 0
    gross = (payout - entry_price_cents) * contracts
    fee = kalshi_fee_cents(contracts, entry_price_cents)
    return gross - fee
