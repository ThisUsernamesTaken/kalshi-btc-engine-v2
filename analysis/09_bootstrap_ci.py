"""Bootstrap confidence intervals on the fair-value-model OOS result.

The 07 backtest shows +$74.58 at 5c threshold + maker on 275 OOS trades. But
how stable is that number? Run B=10,000 bootstrap resamples of the trade
list to get a 95% CI around the mean P&L and Wilson CI on the WR.

If the lower-CI is above zero with reasonable B, the result is robust to
sampling noise. If it straddles zero, the +$74 might be a single-day lucky
cluster.
"""
from __future__ import annotations
import json, math, random, sys, bisect
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(r'C:/Trading/kalshi_btc_gradient_engine/src')))
from kalshi_btc_gradient.models.probability import (
    settlement_fair_probability, SettlementProbabilityInput,
    SettlementProbabilityConfig,
)

DATA_DIR = Path(__file__).resolve().parent.parent / 'data'
STRIKES = json.loads((Path(__file__).resolve().parent / 'strikes_cache.json').read_text())
SLIP = 2


def kalshi_fee_maker(price_c, contracts):
    p = price_c/100; return math.ceil(0.0175 * contracts * p * (1-p) * 100)
def kalshi_fee_taker(price_c, contracts):
    p = price_c/100; return math.ceil(0.07 * contracts * p * (1-p) * 100)


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


def build_trades(threshold_c: float, use_maker: bool = True, min_tau: float = 30,
                 max_tau: float = 900) -> list:
    """Same logic as 07; returns one P&L per trade."""
    sources = [('paper_ta','paper_ta_2026_05_12.jsonl'),
               ('shadow_velocity','shadow_velocity_2026_05_14.jsonl'),
               ('live_ta','live_ta_trades.jsonl'),
               ('live_ta_v2','live_ta_v2_trades.jsonl'),
               ('live_v5_unified','live_v5_unified_trades.jsonl'),
               ('live_v5','live_v5_trades.jsonl')]
    history = build_btc_history(*(DATA_DIR/fn for _,fn in sources))
    trades = []
    fee_fn = kalshi_fee_maker if use_maker else kalshi_fee_taker
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
            rest = ce.get('result'); log_out = e.get('outcome') or e.get('result')
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
            if not tau or tau <= 0 or tau < min_tau or tau > max_tau: continue
            sigma = vol_ann(history, ets)
            try:
                pm = settlement_fair_probability(
                    SettlementProbabilityInput(
                        spot=spot, strike=strike, seconds_to_close=tau,
                        realized_vol_annualized=sigma, drift_annualized=0.0,
                    ),
                    SettlementProbabilityConfig(sigma_floor_annualized=0.15),
                ).probability_yes
            except: continue
            pmk = ec/100 if side=='yes' else (100-ec)/100
            edge = pm - pmk
            if abs(edge) * 100 < threshold_c: continue
            if edge > 0:
                eff = ec if side == 'yes' else (100 - ec)
                won = yes
            else:
                eff = (100 - ec) if side == 'yes' else ec
                won = not yes
            if use_maker:
                eff = max(1, eff - 2)  # rest at bid+1 ~ 2c better than ask
            else:
                eff = eff + SLIP
            if eff <= 0 or eff >= 100: continue
            payoff = (100-eff)*10 if won else -eff*10
            fee = fee_fn(eff, 10)
            trades.append(dict(net=payoff-fee, won=won, src=label, ts=ets, eff=eff))
    return trades


def bootstrap_ci(values, B=10_000, alpha=0.05):
    """Mean bootstrap CI."""
    if not values: return 0, 0, 0
    n = len(values)
    means = []
    rng = random.Random(42)
    for _ in range(B):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample)/n)
    means.sort()
    lo = means[int(B*alpha/2)]
    hi = means[int(B*(1-alpha/2))]
    pt = sum(values)/n
    return pt, lo, hi


def wilson_lcb(wins, n, alpha=0.05):
    if n == 0: return 0
    p = wins/n; z = 1.96
    denom = 1 + z*z/n
    centre = (p + z*z/(2*n)) / denom
    half = z * math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / denom
    return centre - half


def main():
    print('======== BOOTSTRAP CONFIDENCE INTERVALS ========\n')
    print(f'{"config":50s} {"n":>4s} {"WR":>6s} {"Wilson_LCB":>11s} {"mean":>9s} {"CI95_low":>10s} {"CI95_hi":>10s}')
    configs = [
        (5,  True,  30,  900, 'maker, 5c, all windows'),
        (8,  True,  30,  900, 'maker, 8c, all windows'),
        (10, True,  30,  900, 'maker, 10c, all windows'),
        (12, True,  30,  900, 'maker, 12c, all windows'),
        (5,  True,  180, 600, 'maker, 5c, 180-600s window'),
        (10, True,  180, 600, 'maker, 10c, 180-600s window'),
        (5,  False, 30,  900, 'taker, 5c'),
        (10, False, 30,  900, 'taker, 10c'),
        (12, False, 30,  900, 'taker, 12c'),
        (10, False, 180, 600, 'taker, 10c, 180-600s window'),
    ]
    for thresh, mk, lo_tau, hi_tau, label in configs:
        trades = build_trades(thresh, use_maker=mk, min_tau=lo_tau, max_tau=hi_tau)
        if not trades:
            print(f'  {label:50s} (no trades)')
            continue
        n = len(trades); w = sum(1 for t in trades if t['won'])
        WR = w/n
        Wlcb = wilson_lcb(w, n)
        nets = [t['net'] for t in trades]
        pt, lo, hi = bootstrap_ci(nets, B=5000)
        total = sum(nets)
        print(f'  {label:50s} {n:>4d} {WR*100:>5.1f}% {Wlcb*100:>9.1f}% ${total/100:>+8.2f} '
              f'${lo*n/100:>+8.2f} ${hi*n/100:>+8.2f}')

    # Detail on the best result
    print()
    print('======== BEST: 5c threshold, maker, all windows ========')
    trades = build_trades(5, use_maker=True)
    n = len(trades); w = sum(1 for t in trades if t['won'])
    total = sum(t['net'] for t in trades)
    print(f'  Trades: {n}  Wins: {w}  WR: {w/n*100:.1f}%')
    print(f'  Net: ${total/100:+.2f}  Avg: ${total/n/100:+.3f}/trade')
    # Distribution
    nets = sorted([t['net'] for t in trades])
    print(f'  P&L distribution: min=${nets[0]/100:+.2f} median=${nets[n//2]/100:+.2f} max=${nets[-1]/100:+.2f}')
    losses = [n for n in nets if n < 0]
    wins   = [n for n in nets if n > 0]
    print(f'  {len(wins)} winners avg ${sum(wins)/max(len(wins),1)/100:+.3f}; '
          f'{len(losses)} losers avg ${sum(losses)/max(len(losses),1)/100:+.3f}')

    # Bootstrap walk-forward by day: pick a random day, train? — at our N just do
    # per-day stability
    print()
    print('======== PER-DAY P&L (5c maker, all windows) ========')
    import datetime
    by_d = defaultdict(list)
    for t in trades:
        d = datetime.datetime.fromtimestamp(t['ts']/1000, datetime.timezone.utc).strftime('%Y-%m-%d')
        by_d[d].append(t['net'])
    for d in sorted(by_d.keys()):
        ds = by_d[d]
        print(f'  {d}: n={len(ds):>3d}  net=${sum(ds)/100:>+7.2f}  WR={sum(1 for x in ds if x>0)/len(ds)*100:.0f}%')


if __name__ == '__main__':
    main()
