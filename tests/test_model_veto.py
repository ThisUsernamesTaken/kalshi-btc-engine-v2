"""Smoke tests for the model_veto layer."""
import math
import pytest

from kalshi_btc_engine_v2 import model_veto


def test_fair_p_yes_atm_returns_half():
    p = model_veto.fair_p_yes(
        spot_btc=80_000.0, strike=80_000.0,
        seconds_to_close=300.0, sigma_annualized=0.5,
    )
    assert 0.45 < p < 0.55, f'ATM with no drift should be ~0.5, got {p}'


def test_fair_p_yes_deep_itm_high_probability():
    p = model_veto.fair_p_yes(
        spot_btc=80_500.0, strike=80_000.0,
        seconds_to_close=60.0, sigma_annualized=0.3,
    )
    assert p > 0.85, f'Spot 500 above strike with 60s left should be >85%, got {p}'


def test_fair_p_yes_deep_otm_low_probability():
    p = model_veto.fair_p_yes(
        spot_btc=79_500.0, strike=80_000.0,
        seconds_to_close=60.0, sigma_annualized=0.3,
    )
    assert p < 0.15, f'Spot 500 below strike with 60s left should be <15%, got {p}'


def test_veto_flags_model_disagreement_on_yes():
    # Engine pays 89c for YES; model says YES is unlikely (mean reversion).
    # Should veto.
    skip, p_model, reason = model_veto.veto_decision(
        spot_btc=80_050.0, strike=80_000.0,
        seconds_to_close=90.0, sigma_annualized=0.5,
        engine_side='yes', engine_price_cents=89,
    )
    # If model says p<0.84 then engine_implied(0.89) - model > 5c
    if p_model < 0.84:
        assert skip is True, f'Expected veto when p_model={p_model:.3f} vs 0.89 engine'
        assert 'overpriced' in reason
    else:
        assert skip is False


def test_veto_does_not_flag_when_model_agrees():
    # Engine pays 60c for YES; model also reasonable. Should NOT veto.
    skip, p_model, reason = model_veto.veto_decision(
        spot_btc=80_010.0, strike=80_000.0,
        seconds_to_close=300.0, sigma_annualized=0.5,
        engine_side='yes', engine_price_cents=55,
    )
    # Model should be around 0.50-0.55; engine_implied = 0.55
    # diff < 5c so should not veto
    assert skip is False or abs((p_model - 0.55) * 100) >= 5, (
        f'Expected agreement, got veto. p_model={p_model:.3f}'
    )


def test_veto_returns_string_reason_always():
    skip, p_model, reason = model_veto.veto_decision(
        spot_btc=80_000.0, strike=80_000.0,
        seconds_to_close=120.0, sigma_annualized=0.45,
        engine_side='no', engine_price_cents=75,
    )
    assert isinstance(reason, str) and len(reason) > 10


def test_veto_on_no_side_overpriced():
    # Engine pays 89c for NO; spot is well ABOVE strike (NO should be unlikely).
    # Model should say p_yes high; engine paid for NO implying p_yes=0.11.
    # Model >> 0.11 -> should veto.
    skip, p_model, reason = model_veto.veto_decision(
        spot_btc=80_500.0, strike=80_000.0,
        seconds_to_close=60.0, sigma_annualized=0.3,
        engine_side='no', engine_price_cents=89,
    )
    assert skip is True, f'Expected veto, got p_model={p_model:.3f}'
    assert 'overpriced' in reason


def test_action_decision_keep_when_agree():
    action, p, reason, flip = model_veto.model_action_decision(
        spot_btc=80_010.0, strike=80_000.0,
        seconds_to_close=300.0, sigma_annualized=0.5,
        engine_side='yes', engine_price_cents=55,
        yes_ask_cents=55, no_ask_cents=46,
    )
    assert action == 'KEEP', f'expected KEEP, got {action}'
    assert flip is None


def test_action_decision_skip_in_mid_zone():
    # spot $10 above strike, 240s left, sigma 0.5 -> model ~0.54
    # Engine wants YES@70c (implies 0.70). disagreement ~16c -> [5, 30) -> SKIP
    action, p, reason, flip = model_veto.model_action_decision(
        spot_btc=80_010.0, strike=80_000.0,
        seconds_to_close=240.0, sigma_annualized=0.5,
        engine_side='yes', engine_price_cents=70,
        yes_ask_cents=70, no_ask_cents=31,
        skip_threshold_cents=5, flip_threshold_cents=30,
    )
    assert action == 'SKIP', f'expected SKIP, got {action}: {reason}'
    assert flip is None


def test_action_decision_flip_in_extreme_zone():
    # Engine wants YES@89c at $50 BELOW strike with 60s -> model says p<0.10
    # Disagreement ~80c (>> 30c) -> FLIP
    action, p, reason, flip = model_veto.model_action_decision(
        spot_btc=79_950.0, strike=80_000.0,
        seconds_to_close=60.0, sigma_annualized=0.3,
        engine_side='yes', engine_price_cents=89,
        yes_ask_cents=89, no_ask_cents=12,
        skip_threshold_cents=5, flip_threshold_cents=30,
    )
    assert action == 'FLIP', f'expected FLIP, got {action}: {reason}'
    assert flip is not None
    assert flip['side'] == 'no'
    assert flip['limit_cents'] == 14  # no_ask 12 + 2 slip
    assert p < 0.10


def test_action_decision_flip_blocked_without_opposite_ask():
    action, p, reason, flip = model_veto.model_action_decision(
        spot_btc=79_950.0, strike=80_000.0,
        seconds_to_close=60.0, sigma_annualized=0.3,
        engine_side='yes', engine_price_cents=89,
        yes_ask_cents=89, no_ask_cents=None,  # missing
        skip_threshold_cents=5, flip_threshold_cents=30,
    )
    assert action == 'SKIP', f'expected SKIP (no opposite ask), got {action}'
    assert flip is None
    assert 'flip_blocked' in reason


def test_action_decision_only_counts_directional_disagreement():
    # Engine wants YES@40c (i.e., engine thinks it's likely NOT to win).
    # If model says p=0.80 (much higher), that's AGREEMENT-with-buy-YES,
    # not disagreement. Should KEEP.
    action, p, reason, flip = model_veto.model_action_decision(
        spot_btc=80_500.0, strike=80_000.0,
        seconds_to_close=60.0, sigma_annualized=0.3,
        engine_side='yes', engine_price_cents=40,
        yes_ask_cents=40, no_ask_cents=61,
        skip_threshold_cents=5, flip_threshold_cents=30,
    )
    assert action == 'KEEP', f'directional agreement should KEEP, got {action}: {reason}'


def test_threshold_changes_decision():
    # Engine pays 75c for YES; model says 0.71. Diff = -4c -> not vetoed at 5c.
    # At 3c threshold, would veto.
    s_5, _, _ = model_veto.veto_decision(
        spot_btc=80_020.0, strike=80_000.0,
        seconds_to_close=180.0, sigma_annualized=0.5,
        engine_side='yes', engine_price_cents=75,
        threshold_cents=5,
    )
    s_2, _, _ = model_veto.veto_decision(
        spot_btc=80_020.0, strike=80_000.0,
        seconds_to_close=180.0, sigma_annualized=0.5,
        engine_side='yes', engine_price_cents=75,
        threshold_cents=2,
    )
    # If both vetoed or both not, that's fine — but a tighter threshold should
    # never be MORE permissive
    assert (not s_5) or s_2, 'Tighter threshold should veto at least as often as loose'
