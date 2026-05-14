# HANDOFF: owned by Claude (models/). Edit only via HANDOFF.md Open Request.
"""Volatility and drift estimation from 1-second BTC log returns.

Bridges live 1s return streams to the inputs that
:func:`kalshi_btc_engine_v2.models.fair_prob.settlement_fair_probability`
expects. Implements the exact blueprint formulas:

* ``mu_t = 0.6 * mean_60s + 0.4 * mean_300s``
* ``sigma_t^2 = 0.7 * RV_60s_per_sec + 0.3 * BV_300s_per_sec``
* drift clip: ``mu <- clip(mu, +/- 0.25 * sigma / sqrt(max(h, 30)))``

Drift is clipped against horizon-scaled vol per blueprint, *not* multiplicatively
shrunk. Callers can either pass the returned annualized drift to the existing
``SettlementProbabilityConfig(drift_shrinkage=1.0)`` for a literal blueprint
match, or leave the default 0.25 to compound both rules.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from kalshi_btc_engine_v2.models.fair_prob import SECONDS_PER_BTC_YEAR

DEFAULT_MU_FAST_S = 60
DEFAULT_MU_SLOW_S = 300
DEFAULT_RV_WINDOW_S = 60
DEFAULT_BV_WINDOW_S = 300
DRIFT_CLIP_FACTOR = 0.25
DRIFT_CLIP_MIN_HORIZON_S = 30.0


@dataclass(frozen=True, slots=True)
class VolDriftEstimate:
    drift_per_second: float
    drift_per_second_clipped: float
    sigma_per_second: float
    sigma_squared_per_second: float
    drift_annualized: float
    sigma_annualized: float
    samples_used_mu_fast: int
    samples_used_mu_slow: int
    samples_used_rv: int
    samples_used_bv: int


def rolling_mean(returns: Sequence[float], n: int) -> float:
    if n <= 0 or not returns:
        return 0.0
    window = returns[-n:]
    return sum(window) / len(window)


def realized_variance_per_sec(returns: Sequence[float], n: int) -> float:
    if n <= 0 or not returns:
        return 0.0
    window = returns[-n:]
    return sum(r * r for r in window) / len(window)


def bipower_variance_per_sec(returns: Sequence[float], n: int) -> float:
    if n <= 0 or len(returns) < 2:
        return 0.0
    window = returns[-n:]
    if len(window) < 2:
        return 0.0
    pairs = sum(abs(window[i]) * abs(window[i - 1]) for i in range(1, len(window)))
    return (math.pi / 2.0) * pairs / (len(window) - 1)


def estimate_vol_drift(
    log_returns_1s: Sequence[float],
    seconds_to_close: float,
    *,
    mu_fast_s: int = DEFAULT_MU_FAST_S,
    mu_slow_s: int = DEFAULT_MU_SLOW_S,
    rv_window_s: int = DEFAULT_RV_WINDOW_S,
    bv_window_s: int = DEFAULT_BV_WINDOW_S,
    seconds_per_year: float = SECONDS_PER_BTC_YEAR,
) -> VolDriftEstimate:
    """Blueprint-exact estimator: blended drift, blended vol, horizon-clipped drift."""

    mu_fast = rolling_mean(log_returns_1s, mu_fast_s)
    mu_slow = rolling_mean(log_returns_1s, mu_slow_s)
    mu = 0.6 * mu_fast + 0.4 * mu_slow

    rv = realized_variance_per_sec(log_returns_1s, rv_window_s)
    bv = bipower_variance_per_sec(log_returns_1s, bv_window_s)
    sigma_sq = max(0.0, 0.7 * rv + 0.3 * bv)
    sigma = math.sqrt(sigma_sq)

    horizon = max(float(seconds_to_close), DRIFT_CLIP_MIN_HORIZON_S)
    clip_bound = DRIFT_CLIP_FACTOR * sigma / math.sqrt(horizon) if sigma > 0.0 else 0.0
    mu_clipped = max(-clip_bound, min(mu, clip_bound)) if clip_bound > 0.0 else 0.0

    drift_annualized = mu_clipped * seconds_per_year
    sigma_annualized = sigma * math.sqrt(seconds_per_year)

    fast_n = min(len(log_returns_1s), mu_fast_s)
    slow_n = min(len(log_returns_1s), mu_slow_s)
    rv_n = min(len(log_returns_1s), rv_window_s)
    bv_n = min(max(len(log_returns_1s) - 1, 0), max(bv_window_s - 1, 0))

    return VolDriftEstimate(
        drift_per_second=mu,
        drift_per_second_clipped=mu_clipped,
        sigma_per_second=sigma,
        sigma_squared_per_second=sigma_sq,
        drift_annualized=drift_annualized,
        sigma_annualized=sigma_annualized,
        samples_used_mu_fast=fast_n,
        samples_used_mu_slow=slow_n,
        samples_used_rv=rv_n,
        samples_used_bv=bv_n,
    )


def log_returns_from_prices(prices: Sequence[float]) -> list[float]:
    """Convert a strictly positive price series into 1-step log returns."""

    out: list[float] = []
    if len(prices) < 2:
        return out
    prev = float(prices[0])
    if prev <= 0.0:
        raise ValueError("prices must be strictly positive")
    for raw in prices[1:]:
        current = float(raw)
        if current <= 0.0:
            raise ValueError("prices must be strictly positive")
        out.append(math.log(current / prev))
        prev = current
    return out
