from __future__ import annotations

import math

import pytest

from kalshi_btc_engine_v2.models.ensemble import (
    EnsembleConfig,
    EnsembleInputs,
    ensemble_probability,
)


def test_spot_only_passes_through():
    out = ensemble_probability(
        EnsembleInputs(p_spot=0.75),
        config=EnsembleConfig(weight_p_spot=1.0, weight_p_binary_recal=0.0),
    )
    assert out.probability == pytest.approx(0.75, abs=1e-6)


def test_blend_of_spot_and_binary_lands_between():
    out = ensemble_probability(
        EnsembleInputs(p_spot=0.9, p_binary_mid=0.5),
        config=EnsembleConfig(weight_p_spot=0.5, weight_p_binary_recal=0.5),
    )
    assert 0.5 < out.probability < 0.9


def test_power_logit_recalibration_applied_to_binary_mid():
    cfg = EnsembleConfig(weight_p_spot=0.0, weight_p_binary_recal=1.0, power_logit_theta=0.5)
    out = ensemble_probability(EnsembleInputs(p_spot=0.5, p_binary_mid=0.8), config=cfg)
    # theta=0.5 compresses toward 0.5
    assert out.p_binary_recal is not None
    assert out.p_binary_recal < 0.8


def test_divergence_adjustment_shifts_logit():
    base = ensemble_probability(EnsembleInputs(p_spot=0.5), config=EnsembleConfig())
    with_pos = ensemble_probability(
        EnsembleInputs(p_spot=0.5, divergence_logit=1.0),
        config=EnsembleConfig(weight_divergence=0.5),
    )
    assert with_pos.probability > base.probability


def test_clamp_extreme_probabilities():
    out = ensemble_probability(
        EnsembleInputs(p_spot=0.99999999),
        config=EnsembleConfig(weight_p_spot=1.0, weight_p_binary_recal=0.0),
    )
    assert 0.0 < out.probability < 1.0
    assert math.isfinite(out.base_logit)
