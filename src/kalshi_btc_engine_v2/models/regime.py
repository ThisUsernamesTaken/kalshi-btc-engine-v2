# HANDOFF: owned by Claude (models/). Edit only via HANDOFF.md Open Request.
"""Rule-based regime classifier (fallback before a LightGBM model is trained).

Labels follow the deep-research blueprint:

* ``info_absorption_trend``  — spot/options/binary aligned; depth stable;
  divergence narrowing. Trade with flow.
* ``reflexive_squeeze``      — binary reprices faster than spot/options;
  cancels and taker pressure concentrate one-sidedly. Smaller, faster trades.
* ``mean_revert_dislocation`` — binary overreacts locally while cross-market
  fair value stays calmer. Fade only if liquidity adequate.
* ``illiquid_no_trade``      — wide spread, poor depth, slow replenishment.
  Veto.
* ``settlement_hazard``      — very low τ, distorted greeks, benchmark risk.
  Flatten and veto new entries.
* ``data_fault``              — sequence gap, stale benchmark, paused, etc.
  Kill-switch.

Returns a ``(label, confidence in [0,1])`` tuple. Confidence is illustrative; a
trained model should replace this with calibrated probabilities.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RegimeLabel = Literal[
    "info_absorption_trend",
    "reflexive_squeeze",
    "mean_revert_dislocation",
    "illiquid_no_trade",
    "settlement_hazard",
    "data_fault",
]

TRADEABLE_REGIMES: frozenset[RegimeLabel] = frozenset(
    {
        "info_absorption_trend",
        "reflexive_squeeze",
        "mean_revert_dislocation",
    }
)


@dataclass(frozen=True, slots=True)
class RegimeInputs:
    seconds_to_close: float
    fresh_venues: int
    venue_disagreement_bp: float
    market_status_open: bool
    market_paused: bool
    spread_cents: int
    top5_depth: float
    fragility_score: float
    entropy_compression_rate: float | None = None
    reflexivity: float | None = None
    divergence_logit: float | None = None
    vpin: float | None = None
    cancel_add_ratio: float | None = None


@dataclass(frozen=True, slots=True)
class RegimeConfig:
    settlement_hazard_secs_to_close: float = 30.0
    min_venue_quorum: int = 2
    max_venue_disagreement_bp: float = 15.0
    illiquid_spread_cents: int = 4
    illiquid_min_depth: float = 50.0
    reflexive_min_score: float = 1.5
    reflexive_min_ecr: float = 1.0
    mean_revert_min_divergence: float = 0.5
    toxicity_squeeze_floor: float = 0.6


@dataclass(frozen=True, slots=True)
class RegimeDecision:
    label: RegimeLabel
    confidence: float
    reason: str


def classify_regime(
    inputs: RegimeInputs,
    *,
    config: RegimeConfig | None = None,
) -> RegimeDecision:
    cfg = config or RegimeConfig()

    if (
        not inputs.market_status_open
        or inputs.market_paused
        or inputs.fresh_venues < cfg.min_venue_quorum
        or inputs.venue_disagreement_bp > cfg.max_venue_disagreement_bp
    ):
        return RegimeDecision(
            label="data_fault",
            confidence=1.0,
            reason=(
                f"open={inputs.market_status_open} paused={inputs.market_paused} "
                f"fresh={inputs.fresh_venues} disagreement={inputs.venue_disagreement_bp:.1f}bp"
            ),
        )

    if inputs.seconds_to_close <= cfg.settlement_hazard_secs_to_close:
        return RegimeDecision(
            label="settlement_hazard",
            confidence=1.0,
            reason=f"τ={inputs.seconds_to_close:.1f}s <= {cfg.settlement_hazard_secs_to_close}",
        )

    if (
        inputs.spread_cents > cfg.illiquid_spread_cents
        or inputs.top5_depth < cfg.illiquid_min_depth
    ):
        return RegimeDecision(
            label="illiquid_no_trade",
            confidence=0.85,
            reason=f"spread={inputs.spread_cents}c depth={inputs.top5_depth:g}",
        )

    reflex = inputs.reflexivity if inputs.reflexivity is not None else 0.0
    ecr = inputs.entropy_compression_rate if inputs.entropy_compression_rate is not None else 0.0
    toxicity = inputs.vpin if inputs.vpin is not None else 0.0
    if reflex >= cfg.reflexive_min_score and ecr >= cfg.reflexive_min_ecr:
        confidence = min(1.0, 0.5 + 0.25 * (reflex - cfg.reflexive_min_score))
        if toxicity >= cfg.toxicity_squeeze_floor:
            confidence = min(1.0, confidence + 0.1)
        return RegimeDecision(
            label="reflexive_squeeze",
            confidence=confidence,
            reason=f"reflex={reflex:.2f} ECR={ecr:.2f} VPIN={toxicity:.2f}",
        )

    divergence = inputs.divergence_logit if inputs.divergence_logit is not None else 0.0
    if abs(divergence) >= cfg.mean_revert_min_divergence:
        return RegimeDecision(
            label="mean_revert_dislocation",
            confidence=0.6,
            reason=f"divergence_logit={divergence:.2f}",
        )

    return RegimeDecision(
        label="info_absorption_trend",
        confidence=0.55,
        reason="default tradeable regime",
    )


def is_tradeable(label: RegimeLabel) -> bool:
    return label in TRADEABLE_REGIMES
