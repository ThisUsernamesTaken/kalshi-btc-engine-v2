"""Calibration diagnostics for the fair-value model.

Checks whether the model's probability estimate is:
  - well-calibrated (predicted p ≈ realized p in each bucket)
  - sharper than the market's implied probability
  - improvable via a simple post-hoc shrinkage / drift correction

If the model is calibrated AND sharper than the market, it should be
profitable. If it's calibrated but not sharper, no edge. If sharper but
biased, calibration correction would help.
"""
from __future__ import annotations
import json, math, sys, bisect
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(r'C:/Trading/kalshi_btc_gradient_engine/src')))
from kalshi_btc_gradient.models.probability import (
    settlement_fair_probability, SettlementProbabilityInput,
    SettlementProbabilityConfig,
)

DATA_DIR = Path(__file__).resolve().parent.parent / 'data'
STRIKES = json.loads((Path(__file__).resolve().parent / 'strikes_cache.json').read_text())


def build_btc_history(*paths):
    h = []
    for path in paths:
        if not path.exists(): continue
        for line in path.open(encoding='utf-8', errors='ignore'):
            try: e = json.loads(line)
            except: continue
            bp = (e.get('spot_close') or e.get('cycle_open_price')
                  or e.get('btc_price') or e.get('btc_price_at_entry'))
            ts = e.get('ts_minute_ms') or e.get('decided_at_ts_ms') or e.get('ts_ms')
            if bp and ts: h.append((ts, float(bp)))
    h.sort(); return h


def vol_ann(history, end_ts, window_ms=300_000):
    ts_arr = [s[0] for s in history]
    end_idx = bisect.bisect_right(ts_arr, end_ts) - 1
    if end_idx < 5: return 0.5
    start_idx = bisect.bisect_left(ts_arr, end_ts - window_ms)
    prices = [history[i][1] for i in range(start_idx, end_idx + 1) if history[i][1] > 0]
    if len(prices) < 3: return 0.5
    lrets = [math.log(prices[i+1]/prices[i]) for i in range(len(prices)-1)]
    if not lrets: return 0.5
    m = sum(lrets)/len(lrets); v = sum((r-m)**2 for r in lrets)/max(1,len(lrets)-1)
    sps = math.sqrt(v); sigsec = sps/math.sqrt(60)
    return max(0.1, min(3.0, sigsec * math.sqrt(365*24*3600)))


def collect_predictions():
    """For each settled market, collect model p_yes (with several vol assumptions)
    and market p_yes (entry-implied), plus realized outcome."""
    sources = [('paper_ta','paper_ta_2026_05_12.jsonl'),
               ('shadow_velocity','shadow_velocity_2026_05_14.jsonl'),
               ('live_ta','live_ta_trades.jsonl'),
               ('live_ta_v2','live_ta_v2_trades.jsonl'),
               ('live_v5_unified','live_v5_unified_trades.jsonl'),
               ('live_v5','live_v5_trades.jsonl')]
    history = build_btc_history(*(DATA_DIR/fn for _,fn in sources))
    records = []
    for label, fn in sources:
        p = DATA_DIR/fn
        if not p.exists(): continue
        for line in p.open(encoding='utf-8', errors='ignore'):
            try: e = json.loads(line)
            except: continue
            if e.get('kind') not in ('settle','settle_with_ladder'): continue
            t = e.get('ticker'); ce = STRIKES.get(t, {})
            strike = ce.get('strike')
            if not strike: continue
            rest = ce.get('result')
            log_out = e.get('outcome') or e.get('result')
            if rest not in ('yes','no'):
                if log_out not in ('yes','no'): continue
                yes = (log_out == 'yes')
            else: yes = (rest == 'yes')
            ets = e.get('decided_at_ts_ms') or e.get('entered_at_ms') or e.get('ts_ms')
            cc = e.get('cycle_close_ms'); side = e.get('side'); ec = e.get('entry_price_cents')
            if not all([ets, side, ec is not None]): continue
            spot = e.get('btc_price_at_entry') or e.get('spot_close')
            if not spot:
                idx = bisect.bisect_right([s[0] for s in history], ets) - 1
                if idx < 0: continue
                spot = history[idx][1]
            tau = (cc-ets)/1000 if cc else e.get('seconds_to_close')
            if not tau or tau <= 0: continue
            sigma = vol_ann(history, ets)
            try:
                pm_yes = settlement_fair_probability(
                    SettlementProbabilityInput(
                        spot=spot, strike=strike, seconds_to_close=tau,
                        realized_vol_annualized=sigma, drift_annualized=0.0,
                    ),
                    SettlementProbabilityConfig(sigma_floor_annualized=0.15),
                ).probability_yes
            except: continue
            pmk_yes = ec/100 if side=='yes' else (100-ec)/100
            records.append(dict(
                src=label, ticker=t, ts=ets, tau=tau, spot=spot, strike=strike,
                sigma=sigma, p_model=pm_yes, p_market=pmk_yes, outcome_yes=yes,
            ))
    return records


def brier_score(predictions, outcomes):
    """Mean squared error between predicted prob and realized outcome."""
    return sum((p - (1 if o else 0))**2 for p, o in zip(predictions, outcomes)) / len(predictions)


def log_loss(predictions, outcomes, eps=1e-9):
    """Mean cross-entropy."""
    ll = 0
    for p, o in zip(predictions, outcomes):
        p = max(eps, min(1-eps, p))
        ll += -(math.log(p) if o else math.log(1-p))
    return ll / len(predictions)


def main():
    records = collect_predictions()
    print(f'Loaded {len(records)} settles\n')

    p_model = [r['p_model'] for r in records]
    p_market = [r['p_market'] for r in records]
    outcomes = [r['outcome_yes'] for r in records]

    print('======== SHARPNESS / CALIBRATION ========')
    print(f'  {"score":18s} {"model":>10s} {"market":>10s}')
    print(f'  {"Brier score":18s} {brier_score(p_model, outcomes):>10.4f} '
          f'{brier_score(p_market, outcomes):>10.4f}')
    print(f'  {"Log loss":18s} {log_loss(p_model, outcomes):>10.4f} '
          f'{log_loss(p_market, outcomes):>10.4f}')
    print('  (lower is better; model should beat market on at least one)')
    print()

    # Calibration table side-by-side
    print('======== CALIBRATION DETAIL (per-bucket empirical p) ========')
    print(f'  {"bucket":12s} {"model_n":>8s} {"model_emp_p":>13s} '
          f'{"market_n":>9s} {"mkt_emp_p":>11s}')
    bins = 10
    for i in range(bins):
        lo, hi = i/bins, (i+1)/bins
        mkts = [(r['p_model'], r['outcome_yes']) for r in records if lo <= r['p_model'] < hi]
        mks  = [(r['p_market'], r['outcome_yes']) for r in records if lo <= r['p_market'] < hi]
        if len(mkts) < 5 and len(mks) < 5: continue
        m_emp = sum(1 for _, o in mkts if o)/max(len(mkts),1) if mkts else None
        k_emp = sum(1 for _, o in mks if o)/max(len(mks),1) if mks else None
        print(f'  {lo*100:3.0f}-{hi*100:3.0f}%   {len(mkts):>8d} '
              f'{m_emp*100:>11.1f}% ' if mkts else f'  {lo*100:3.0f}-{hi*100:3.0f}%   {0:>8d}     -- ',
              end='')
        print(f' {len(mks):>9d} {k_emp*100:>9.1f}%' if mks else f' {0:>9d}      --')

    # Sharpness: does the model concentrate at extremes more than market?
    print()
    print('======== SHARPNESS DISTRIBUTION ========')
    extreme_model = sum(1 for p in p_model if p < 0.15 or p > 0.85)
    extreme_market = sum(1 for p in p_market if p < 0.15 or p > 0.85)
    print(f'  Extreme calls (p<15% or p>85%):  model={extreme_model} ({extreme_model/len(records)*100:.0f}%) '
          f'market={extreme_market} ({extreme_market/len(records)*100:.0f}%)')

    # Where does the model add value? Look at cases where model and market disagree
    disagrees = []
    for r in records:
        d = r['p_model'] - r['p_market']
        if abs(d) >= 0.05:
            disagrees.append((d, r['outcome_yes']))
    if disagrees:
        # When model says higher, does it win more often?
        higher = [(d, o) for d, o in disagrees if d > 0]
        lower  = [(d, o) for d, o in disagrees if d < 0]
        print(f'\n  Disagreement >5c on n={len(disagrees)} trades')
        print(f'    Model > Market: n={len(higher)} actual p_yes={sum(o for _,o in higher)/len(higher)*100:.1f}%')
        print(f'    Model < Market: n={len(lower)} actual p_yes={sum(o for _,o in lower)/len(lower)*100:.1f}%')
        # Both side bets at 50c breakeven
        higher_avg_market = sum(0.5 + abs(d) for d, _ in higher) / len(higher) if higher else 0
        lower_avg_market  = sum(0.5 - abs(d) for d, _ in lower) / len(lower) if lower else 0

    # Per-source calibration
    print()
    print('======== CALIBRATION BY SOURCE ========')
    by_src = defaultdict(list)
    for r in records: by_src[r['src']].append(r)
    print(f'  {"source":18s} {"n":>4s} {"Brier(model)":>13s} {"Brier(market)":>14s}')
    for s, rs in by_src.items():
        p_m = [r['p_model'] for r in rs]; p_k = [r['p_market'] for r in rs]
        o = [r['outcome_yes'] for r in rs]
        print(f'  {s:18s} {len(rs):>4d} {brier_score(p_m, o):>12.4f} {brier_score(p_k, o):>13.4f}')


if __name__ == '__main__':
    main()
