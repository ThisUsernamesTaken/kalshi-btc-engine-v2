# HANDOFF: owned by Claude (models/). Edit only via HANDOFF.md Open Request.
"""Fair-probability ensemble — blends signals in logit space.

Per the deep-research blueprint:

    logit(p*) = α + β·logit(p_bin_recal) + γ·logit(p_spot)
                + θ·divergence + φ·ECR + ψ·reflexivity

where:
* ``p_spot``  is the diffusion-based settlement probability from ``fair_prob``.
* ``p_bin_recal`` is the contract mid passed through power-logit recalibration.
* ``divergence`` is the binary-vs-spot logit gap.
* ``ECR`` is the entropy compression rate (already standardized).
* ``reflexivity`` is the binary-leads-spot residual.

Coefficients should eventually be regime-dependent (α_r, β_r, ...). For v1 the
:class:`EnsembleConfig` exposes flat weights; regime-aware configs can be built
by swapping in different :class:`EnsembleConfig` values per :mod:`models.regime`
label.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from kalshi_btc_engine_v2.models.calibration import power_logit_recalibrate

LOGIT_CLAMP = 1.0e-6


def _clamp01(p: float) -> float:
    if p < LOGIT_CLAMP:
        return LOGIT_CLAMP
    if p > 1.0 - LOGIT_CLAMP:
        return 1.0 - LOGIT_CLAMP
    return p


def _logit(p: float) -> float:
    p = _clamp01(p)
    return math.log(p / (1.0 - p))


def _sigmoid(z: float) -> float:
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


@dataclass(frozen=True, slots=True)
class EnsembleConfig:
    weight_p_spot: float = 0.60
    weight_p_binary_recal: float = 0.40
    weight_divergence: float = 0.0
    weight_ecr: float = 0.0
    weight_reflexivity: float = 0.0
    power_logit_theta: float = 1.0
    intercept_logit: float = 0.0


@dataclass(frozen=True, slots=True)
class EnsembleInputs:
    p_spot: float
    p_binary_mid: float | None = None
    divergence_logit: float | None = None
    entropy_compression_rate: float | None = None
    reflexivity: float | None = None


@dataclass(frozen=True, slots=True)
class EnsembleResult:
    probability: float
    base_logit: float
    p_binary_recal: float | None
    contributing_weight: float


def ensemble_probability(
    inputs: EnsembleInputs,
    *,
    config: EnsembleConfig | None = None,
) -> EnsembleResult:
    cfg = config or EnsembleConfig()

    spot_logit = _logit(inputs.p_spot)
    weighted_logit = cfg.weight_p_spot * spot_logit
    total_weight = cfg.weight_p_spot

    p_binary_recal: float | None = None
    if inputs.p_binary_mid is not None and cfg.weight_p_binary_recal > 0.0:
        p_binary_recal = power_logit_recalibrate(inputs.p_binary_mid, cfg.power_logit_theta)
        weighted_logit += cfg.weight_p_binary_recal * _logit(p_binary_recal)
        total_weight += cfg.weight_p_binary_recal

    base = 0.0 if total_weight <= 0.0 else weighted_logit / total_weight

    base += cfg.intercept_logit
    if inputs.divergence_logit is not None:
        base += cfg.weight_divergence * inputs.divergence_logit
    if inputs.entropy_compression_rate is not None:
        base += cfg.weight_ecr * inputs.entropy_compression_rate
    if inputs.reflexivity is not None:
        base += cfg.weight_reflexivity * inputs.reflexivity

    return EnsembleResult(
        probability=_sigmoid(base),
        base_logit=base,
        p_binary_recal=p_binary_recal,
        contributing_weight=total_weight,
    )
