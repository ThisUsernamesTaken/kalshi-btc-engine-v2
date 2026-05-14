from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

SECONDS_PER_BTC_YEAR = 365.0 * 24.0 * 60.0 * 60.0

SettlementCase = Literal["pre_window", "inside_window", "expired"]


@dataclass(frozen=True, slots=True)
class SettlementProbabilityConfig:
    """Configuration for settlement-aware KXBTC-style binary probabilities.

    Volatility and drift inputs are annualized log-return quantities. The model
    converts them to per-second units internally.
    """

    settlement_window_seconds: float = 60.0
    seconds_per_year: float = SECONDS_PER_BTC_YEAR
    sigma_floor_annualized: float = 0.20
    drift_shrinkage: float = 0.25
    min_probability: float = 1.0e-6


@dataclass(frozen=True, slots=True)
class SettlementProbabilityInput:
    spot: float
    strike: float
    seconds_to_close: float
    realized_vol_annualized: float | None = None
    implied_vol_annualized: float | None = None
    drift_annualized: float = 0.0
    observed_settlement_average: float | None = None
    observed_settlement_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class SettlementProbabilityResult:
    probability_yes: float
    case: SettlementCase
    effective_sigma_annualized: float
    effective_drift_annualized: float
    z_score: float | None
    k_required: float | None
    variance_time_seconds: float
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def fair_value_cents(self) -> float:
        return 100.0 * self.probability_yes


def settlement_fair_probability(
    inputs: SettlementProbabilityInput,
    config: SettlementProbabilityConfig | None = None,
) -> SettlementProbabilityResult:
    """Estimate P(final 60s settlement average > strike).

    Case one, before the final settlement window (`h > w`), approximates the
    future window average in log space:

        z = [ln(S/K) + mu_eff * (h - w/2)] / [sigma * sqrt(h - 2w/3)]

    Case two, inside the window (`h <= w`), converts the partially observed
    settlement sum into a required remaining average:

        K_req = (K * w - observed_sum) / h

    then prices P(remaining average > K_req) with variance clock h/3.
    """

    cfg = config or SettlementProbabilityConfig()
    _validate_inputs(inputs, cfg)

    spot = float(inputs.spot)
    strike = float(inputs.strike)
    h = max(float(inputs.seconds_to_close), 0.0)
    w = float(cfg.settlement_window_seconds)
    warnings: list[str] = []

    sigma_ann = choose_effective_sigma(inputs, cfg)
    sigma_per_second = sigma_ann / math.sqrt(cfg.seconds_per_year)
    drift_ann = float(inputs.drift_annualized) * float(cfg.drift_shrinkage)
    drift_per_second = drift_ann / cfg.seconds_per_year

    if h <= 0.0:
        final_avg = inputs.observed_settlement_average or spot
        probability = 1.0 if final_avg > strike else 0.0
        return SettlementProbabilityResult(
            probability_yes=_clip_probability(probability, cfg),
            case="expired",
            effective_sigma_annualized=sigma_ann,
            effective_drift_annualized=drift_ann,
            z_score=None,
            k_required=None,
            variance_time_seconds=0.0,
            warnings=tuple(warnings),
        )

    if h > w:
        variance_time = max(h - (2.0 * w / 3.0), 1.0e-9)
        mean_time = h - (w / 2.0)
        z_score = _normal_z(
            spot=spot,
            threshold=strike,
            drift_per_second=drift_per_second,
            mean_time_seconds=mean_time,
            sigma_per_second=sigma_per_second,
            variance_time_seconds=variance_time,
        )
        return SettlementProbabilityResult(
            probability_yes=_clip_probability(_normal_cdf(z_score), cfg),
            case="pre_window",
            effective_sigma_annualized=sigma_ann,
            effective_drift_annualized=drift_ann,
            z_score=z_score,
            k_required=None,
            variance_time_seconds=variance_time,
            warnings=tuple(warnings),
        )

    elapsed = _observed_elapsed_seconds(inputs, w, h, warnings)
    observed_average = _observed_average(inputs, spot, warnings)
    observed_sum = observed_average * elapsed
    remaining = max(h, 1.0e-9)
    k_required = (strike * w - observed_sum) / remaining

    if k_required <= 0.0:
        return SettlementProbabilityResult(
            probability_yes=1.0 - cfg.min_probability,
            case="inside_window",
            effective_sigma_annualized=sigma_ann,
            effective_drift_annualized=drift_ann,
            z_score=None,
            k_required=k_required,
            variance_time_seconds=0.0,
            warnings=tuple(warnings),
        )

    variance_time = max(remaining / 3.0, 1.0e-9)
    mean_time = remaining / 2.0
    z_score = _normal_z(
        spot=spot,
        threshold=k_required,
        drift_per_second=drift_per_second,
        mean_time_seconds=mean_time,
        sigma_per_second=sigma_per_second,
        variance_time_seconds=variance_time,
    )
    return SettlementProbabilityResult(
        probability_yes=_clip_probability(_normal_cdf(z_score), cfg),
        case="inside_window",
        effective_sigma_annualized=sigma_ann,
        effective_drift_annualized=drift_ann,
        z_score=z_score,
        k_required=k_required,
        variance_time_seconds=variance_time,
        warnings=tuple(warnings),
    )


def choose_effective_sigma(
    inputs: SettlementProbabilityInput,
    config: SettlementProbabilityConfig,
) -> float:
    candidates = [
        value
        for value in (inputs.realized_vol_annualized, inputs.implied_vol_annualized)
        if value is not None and value > 0.0
    ]
    if not candidates:
        return float(config.sigma_floor_annualized)
    return max(float(config.sigma_floor_annualized), max(float(value) for value in candidates))


def _validate_inputs(
    inputs: SettlementProbabilityInput,
    config: SettlementProbabilityConfig,
) -> None:
    if inputs.spot <= 0.0:
        raise ValueError("spot must be positive")
    if inputs.strike <= 0.0:
        raise ValueError("strike must be positive")
    if config.settlement_window_seconds <= 0.0:
        raise ValueError("settlement window must be positive")
    if config.seconds_per_year <= 0.0:
        raise ValueError("seconds_per_year must be positive")
    if config.sigma_floor_annualized <= 0.0:
        raise ValueError("sigma_floor_annualized must be positive")
    if not 0.0 <= config.drift_shrinkage <= 1.0:
        raise ValueError("drift_shrinkage must be in [0, 1]")


def _observed_elapsed_seconds(
    inputs: SettlementProbabilityInput,
    window_seconds: float,
    seconds_to_close: float,
    warnings: list[str],
) -> float:
    inferred = max(window_seconds - seconds_to_close, 0.0)
    if inputs.observed_settlement_seconds is None:
        warnings.append("observed_seconds_missing_inferred_from_clock")
        return inferred
    return min(max(float(inputs.observed_settlement_seconds), 0.0), window_seconds)


def _observed_average(
    inputs: SettlementProbabilityInput,
    spot: float,
    warnings: list[str],
) -> float:
    if inputs.observed_settlement_average is None:
        warnings.append("observed_average_missing_assumed_spot")
        return spot
    if inputs.observed_settlement_average <= 0.0:
        raise ValueError("observed_settlement_average must be positive when provided")
    return float(inputs.observed_settlement_average)


def _normal_z(
    *,
    spot: float,
    threshold: float,
    drift_per_second: float,
    mean_time_seconds: float,
    sigma_per_second: float,
    variance_time_seconds: float,
) -> float:
    if threshold <= 0.0:
        return math.inf
    sigma_clock = sigma_per_second * math.sqrt(max(variance_time_seconds, 1.0e-12))
    if sigma_clock <= 0.0:
        return math.inf if spot > threshold else -math.inf
    numerator = math.log(spot / threshold) + drift_per_second * mean_time_seconds
    return numerator / sigma_clock


def _normal_cdf(z_score: float) -> float:
    if z_score == math.inf:
        return 1.0
    if z_score == -math.inf:
        return 0.0
    return 0.5 * (1.0 + math.erf(z_score / math.sqrt(2.0)))


def _clip_probability(probability: float, config: SettlementProbabilityConfig) -> float:
    lo = float(config.min_probability)
    hi = 1.0 - lo
    return min(max(float(probability), lo), hi)
