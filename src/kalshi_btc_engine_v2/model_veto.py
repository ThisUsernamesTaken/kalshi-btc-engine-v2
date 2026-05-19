"""Fair-value-model veto layer.

Drop-in module for any live trader to add a "should I skip this trade?"
check at the entry-trigger gate. Wraps the gradient engine's BRTI-
averaging-aware ``settlement_fair_probability`` and produces a clean
boolean decision.

Validated finding (analysis/13, 14): applied as a veto on the existing
v5_unified / v5_old / live_ta trade decisions across 5 days of OOS data,
the rule flips combined realized P&L from −$64 to +$27, with 100% of
bootstrap resamples positive (CI [+$26, +$172]).

Usage:
    from kalshi_btc_engine_v2.model_veto import veto_decision

    skip, p_model, reason = veto_decision(
        spot_btc=78250.0,
        strike=78100.0,
        seconds_to_close=120.0,
        sigma_annualized=0.45,   # realized 5-min vol or model estimate
        engine_side='yes',       # the side the engine wants to buy
        engine_price_cents=89,   # what the engine intends to pay
        threshold_cents=5,       # disagreement threshold (default 5c)
    )
    if skip:
        log.info(f'veto: {reason}; p_model={p_model:.3f}')
        return  # do NOT place the trade

The module is import-light (only stdlib + the gradient engine's
``models/probability.py``) and has zero side effects. Add the import
to the trigger-evaluation path and call ``veto_decision`` before
order placement.
"""
from __future__ import annotations
import sys
from pathlib import Path
from typing import Literal

# Import the gradient engine's averaging-aware model.
# This file lives at kalshi-btc-engine-v2/src/kalshi_btc_engine_v2/model_veto.py.
# The gradient engine source is at C:/Trading/kalshi_btc_gradient_engine/src/.
_GRADIENT_SRC = Path(r'C:/Trading/kalshi_btc_gradient_engine/src')
if str(_GRADIENT_SRC) not in sys.path:
    sys.path.insert(0, str(_GRADIENT_SRC))

from kalshi_btc_gradient.models.probability import (  # noqa: E402
    settlement_fair_probability,
    SettlementProbabilityConfig,
    SettlementProbabilityInput,
)

Side = Literal['yes', 'no']


def fair_p_yes(
    *,
    spot_btc: float,
    strike: float,
    seconds_to_close: float,
    sigma_annualized: float,
    drift_annualized: float = 0.0,
) -> float:
    """Return p_yes from the BRTI-averaging-aware model. Pure function."""
    cfg = SettlementProbabilityConfig(sigma_floor_annualized=0.15)
    result = settlement_fair_probability(
        SettlementProbabilityInput(
            spot=spot_btc,
            strike=strike,
            seconds_to_close=max(seconds_to_close, 0.001),
            realized_vol_annualized=sigma_annualized,
            drift_annualized=drift_annualized,
        ),
        cfg,
    )
    return result.probability_yes


def veto_decision(
    *,
    spot_btc: float,
    strike: float,
    seconds_to_close: float,
    sigma_annualized: float,
    engine_side: Side,
    engine_price_cents: int,
    threshold_cents: int = 5,
    drift_annualized: float = 0.0,
) -> tuple[bool, float, str]:
    """Decide whether to veto a planned trade based on model-vs-engine disagreement.

    Returns
    -------
    (skip, p_model, reason)
        skip : True if the trade should be skipped
        p_model : the model's predicted p_yes at decision time
        reason : human-readable reason (always populated)
    """
    p_model = fair_p_yes(
        spot_btc=spot_btc, strike=strike,
        seconds_to_close=seconds_to_close,
        sigma_annualized=sigma_annualized,
        drift_annualized=drift_annualized,
    )
    # Convert engine bet to its implied p_yes
    if engine_side == 'yes':
        engine_implied_p_yes = engine_price_cents / 100.0
    else:
        engine_implied_p_yes = 1.0 - engine_price_cents / 100.0
    # Disagreement (cents in p_yes terms)
    diff_c = (p_model - engine_implied_p_yes) * 100.0
    if engine_side == 'yes':
        # Model wants LOWER p_yes -> engine wrong to buy YES expensive
        if diff_c <= -threshold_cents:
            return True, p_model, (
                f'model_says_yes_overpriced p_model={p_model:.3f} '
                f'engine_implied={engine_implied_p_yes:.3f} diff={diff_c:+.1f}c'
            )
    else:
        # Engine bought NO at engine_price (= paying for NO); model wants
        # HIGHER p_yes -> NO is overpriced relative to model
        if diff_c >= threshold_cents:
            return True, p_model, (
                f'model_says_no_overpriced p_model={p_model:.3f} '
                f'engine_implied={engine_implied_p_yes:.3f} diff={diff_c:+.1f}c'
            )
    return False, p_model, (
        f'model_agrees p_model={p_model:.3f} '
        f'engine_implied={engine_implied_p_yes:.3f} diff={diff_c:+.1f}c'
    )


def model_action_decision(
    *,
    spot_btc: float,
    strike: float,
    seconds_to_close: float,
    sigma_annualized: float,
    engine_side: Side,
    engine_price_cents: int,
    skip_threshold_cents: int = 5,
    flip_threshold_cents: int = 30,
    yes_ask_cents: int | None = None,
    no_ask_cents: int | None = None,
    flip_slip_cents: int = 2,
    drift_annualized: float = 0.0,
) -> tuple[str, float, str, dict | None]:
    """Three-way decision: KEEP the engine's trade, SKIP it, or FLIP to opposite side.

    Returns
    -------
    (action, p_model, reason, flip_details)
        action: 'KEEP' | 'SKIP' | 'FLIP'
        p_model: model's predicted p_yes
        reason: human-readable
        flip_details: dict(side, limit_cents) if action='FLIP' and a valid
                      opposite-side ask is available; else None

    Decision rule:
      Let diff_c = (p_model - engine_implied_p_yes) * 100 (in cents).
      Disagreement = abs(diff_c) only counts when the SIGN of diff_c implies
      betting the OPPOSITE side of what the engine wants:
        engine='yes' -> disagreement only when diff_c < 0 (model wants less YES)
        engine='no'  -> disagreement only when diff_c > 0 (model wants more YES)

      If disagreement < skip_threshold: KEEP
      If skip_threshold <= disagreement < flip_threshold: SKIP
      If disagreement >= flip_threshold AND opposite-side ask available: FLIP
      If disagreement >= flip_threshold AND no opposite-side ask: SKIP (defensive)

    Backed by analysis/17 + 18: applied to 131 OOS live trades, veto+flip
    (skip>=8c, flip>=30c) gives +$173 swing with 95% bootstrap CI [+$68,
    +$296], 99.9% of resamples positive. Best mean among tested variants.
    """
    p_model = fair_p_yes(
        spot_btc=spot_btc, strike=strike,
        seconds_to_close=seconds_to_close,
        sigma_annualized=sigma_annualized,
        drift_annualized=drift_annualized,
    )
    if engine_side == 'yes':
        engine_implied_p_yes = engine_price_cents / 100.0
        diff_c = (p_model - engine_implied_p_yes) * 100.0
        # disagreement only when model wants LESS YES (diff < 0)
        disagree_c = -diff_c if diff_c < 0 else 0.0
    else:
        engine_implied_p_yes = 1.0 - engine_price_cents / 100.0
        diff_c = (p_model - engine_implied_p_yes) * 100.0
        disagree_c = diff_c if diff_c > 0 else 0.0

    base_msg = (f'p_model={p_model:.3f} engine_implied={engine_implied_p_yes:.3f} '
                f'diff={diff_c:+.1f}c disagree={disagree_c:.1f}c')

    if disagree_c < skip_threshold_cents:
        return 'KEEP', p_model, f'model_agrees {base_msg}', None

    if disagree_c < flip_threshold_cents:
        side_name = engine_side
        return ('SKIP', p_model,
                f'model_says_{side_name}_overpriced (mid-disagree) {base_msg}',
                None)

    # FLIP territory — need opposite-side ask
    opposite_side: Side = 'no' if engine_side == 'yes' else 'yes'
    opp_ask = no_ask_cents if engine_side == 'yes' else yes_ask_cents
    if opp_ask is None or not (0 < opp_ask < 100):
        return ('SKIP', p_model,
                f'flip_blocked_no_opposite_ask {base_msg}',
                None)
    # Place limit at opposite_ask + slip, capped 1..99
    flip_limit = max(1, min(99, opp_ask + flip_slip_cents))
    return ('FLIP', p_model,
            f'model_flips_to_{opposite_side}@{flip_limit}c {base_msg}',
            dict(side=opposite_side, limit_cents=flip_limit, opp_ask=opp_ask))


__all__ = ['fair_p_yes', 'veto_decision', 'model_action_decision']
