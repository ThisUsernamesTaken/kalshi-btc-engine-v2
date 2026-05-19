"""Quick CLI to check the fair-value model probability for a given market.

Usage:
    python scripts/live/check_model_prob.py \\
        --spot 78250 --strike 78100 --secs-to-close 120 --sigma 0.45

Or with an engine trade context (shows whether veto would fire):
    python scripts/live/check_model_prob.py \\
        --spot 78250 --strike 78100 --secs-to-close 120 --sigma 0.45 \\
        --engine-side yes --engine-price 89

Useful for ad-hoc validation when looking at live markets.
"""
from __future__ import annotations
import argparse, sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / 'src'))

from kalshi_btc_engine_v2.model_veto import fair_p_yes, veto_decision  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--spot', type=float, required=True, help='BTC spot price USD')
    ap.add_argument('--strike', type=float, required=True, help='Contract strike USD')
    ap.add_argument('--secs-to-close', type=float, required=True, help='Seconds until contract close')
    ap.add_argument('--sigma', type=float, default=0.5, help='Annualized vol (default 0.5)')
    ap.add_argument('--drift', type=float, default=0.0, help='Annualized drift (default 0)')
    ap.add_argument('--engine-side', type=str, default=None, choices=['yes', 'no'],
                    help='If supplied, runs the veto and reports skip/keep')
    ap.add_argument('--engine-price', type=int, default=None,
                    help='Cents paid for engine-side (required if --engine-side)')
    ap.add_argument('--threshold', type=int, default=5,
                    help='Veto disagreement threshold in cents (default 5)')
    args = ap.parse_args()

    p_yes = fair_p_yes(
        spot_btc=args.spot, strike=args.strike,
        seconds_to_close=args.secs_to_close,
        sigma_annualized=args.sigma,
        drift_annualized=args.drift,
    )
    print(f'Inputs:  spot=${args.spot:,.2f}  strike=${args.strike:,.2f}  '
          f'tau={args.secs_to_close:.0f}s  sigma_ann={args.sigma:.3f}')
    print(f'Distance to strike: ${args.spot - args.strike:+,.2f}  '
          f'({(args.spot - args.strike) / args.strike * 100:+.4f}%)')
    print(f'\nModel p_yes:  {p_yes*100:.2f}%')
    print(f'Implied fair (cents):  YES={p_yes*100:.1f}c  NO={(1-p_yes)*100:.1f}c')

    if args.engine_side:
        if args.engine_price is None:
            print('\nERROR: --engine-price required when --engine-side supplied', file=sys.stderr)
            return 1
        skip, p_model, reason = veto_decision(
            spot_btc=args.spot, strike=args.strike,
            seconds_to_close=args.secs_to_close,
            sigma_annualized=args.sigma,
            drift_annualized=args.drift,
            engine_side=args.engine_side,
            engine_price_cents=args.engine_price,
            threshold_cents=args.threshold,
        )
        engine_implied = (args.engine_price / 100 if args.engine_side == 'yes'
                         else 1.0 - args.engine_price / 100)
        print(f'\nEngine context:  side={args.engine_side} @ {args.engine_price}c  '
              f'(implied p_yes = {engine_implied*100:.1f}%)')
        print(f'Disagreement: {(p_model - engine_implied)*100:+.1f}c  '
              f'(threshold +/-{args.threshold}c)')
        verdict = 'VETO (skip)' if skip else 'OK (keep)'
        print(f'Veto verdict: {verdict}')
        print(f'Reason: {reason}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
