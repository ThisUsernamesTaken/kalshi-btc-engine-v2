"""Robustness checks on the model-veto layer.

13 showed the model-veto turns -$64 -> +$27 across 3 live engines. But it
uses ets = close_ts - 90s as proxy entry time for v5 engines (which don't
log exact entries). Test sensitivity to that assumption.

Also bootstrap the veto's loser-flag-rate to check it's not a statistical
artifact at this N.
"""
from __future__ import annotations
import json, math, sys, bisect, random
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


def spot_at(history, ts):
    idx = bisect.bisect_right([s[0] for s in history], ts) - 1
    return history[idx][1] if idx >= 0 else None


def collect_trades(entry_proxy_s):
    """Collect v5_unified + v5_old + live_ta settles with model evaluated at entry_proxy."""
    sources = [('v5_unified','live_v5_unified_trades.jsonl'),
               ('v5_old','live_v5_trades.jsonl'),
               ('live_ta','live_ta_trades.jsonl')]
    history = build_btc_history(*(DATA_DIR/fn for _, fn in sources),
                                DATA_DIR/'paper_ta_2026_05_12.jsonl')
    out = []
    for label, fn in sources:
        p = DATA_DIR/fn
        if not p.exists(): continue
        for line in p.open(encoding='utf-8', errors='ignore'):
            try: e = json.loads(line)
            except: continue
            if e.get('kind') != 'settle': continue
            t = e.get('ticker'); ce = STRIKES.get(t, {})
            strike = ce.get('strike')
            if not strike: continue
            rest = ce.get('result')
            outcome = rest if rest in ('yes','no') else e.get('result') or e.get('outcome')
            if outcome not in ('yes','no'): continue
            side = e.get('side'); entry_c = e.get('entry_price_cents')
            cc = e.get('cycle_close_ms') or e.get('close_ts_ms')
            if not cc:
                try:
                    import datetime
                    ct = ce.get('close_time')
                    if ct: cc = int(datetime.datetime.fromisoformat(ct.replace('Z','+00:00')).timestamp()*1000)
                except: pass
            if not all([entry_c is not None, cc, side]): continue
            # Entry timestamp
            if label == 'live_ta':
                ets = e.get('decided_at_ts_ms') or (cc - entry_proxy_s * 1000)
            else:
                ets = cc - entry_proxy_s * 1000
            if ets <= 0 or ets >= cc: continue
            spot = e.get('btc_price_at_entry') or spot_at(history, ets)
            if not spot: continue
            tau = (cc - ets) / 1000.0
            if tau <= 0: continue
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
            engine_implied = entry_c/100 if side=='yes' else (100-entry_c)/100
            won = (outcome == side)
            net_c = e.get('net_cents', 0)
            out.append(dict(src=label, ticker=t, side=side, entry_c=entry_c,
                            p_model=pm, p_engine=engine_implied,
                            net_c=net_c, won=won))
    return out


def veto_impact(trades, diverge_thresh=0.05):
    """For each trade, check if model disagrees with engine; report net P&L change."""
    def model_disagrees(r):
        if r['side'] == 'yes': return r['p_model'] < r['p_engine'] - diverge_thresh
        else:                   return r['p_model'] > r['p_engine'] + diverge_thresh
    kept = [r for r in trades if not model_disagrees(r)]
    skipped = [r for r in trades if model_disagrees(r)]
    orig = sum(r['net_c'] for r in trades)
    new  = sum(r['net_c'] for r in kept)
    skipped_loss = sum(r['net_c'] for r in skipped if not r['won'])
    skipped_win  = sum(r['net_c'] for r in skipped if r['won'])
    return dict(orig=orig, new=new, kept_n=len(kept), skipped_n=len(skipped),
                skipped_loss=skipped_loss, skipped_win=skipped_win)


def main():
    print('======== ROBUSTNESS: vary entry-time proxy for v5 engines ========')
    print(f'{"proxy(s)":>9s} {"src":18s} {"orig":>8s} {"after_veto":>11s} {"swing":>7s} {"flag_rate":>10s}')
    for proxy_s in [30, 60, 90, 120, 180, 300, 480, 600]:
        trades = collect_trades(proxy_s)
        for src in ('v5_unified','v5_old','live_ta'):
            sub = [t for t in trades if t['src']==src]
            if not sub: continue
            r = veto_impact(sub)
            losers = [t for t in sub if not t['won']]
            flagged_losers = sum(1 for t in losers if
                                  ((t['side']=='yes' and t['p_model'] < t['p_engine']-0.05) or
                                   (t['side']=='no'  and t['p_model'] > t['p_engine']+0.05)))
            flag_rate = flagged_losers / max(len(losers), 1)
            print(f'  {proxy_s:>7d}s {src:18s} ${r["orig"]/100:>+7.2f} ${r["new"]/100:>+10.2f} '
                  f'${(r["new"]-r["orig"])/100:>+6.2f} {flag_rate*100:>8.0f}% ({flagged_losers}/{len(losers)})')

    print()
    print('======== THRESHOLD SENSITIVITY (proxy=90s, all engines combined) ========')
    trades = collect_trades(90)
    for thr in [0.02, 0.05, 0.08, 0.10, 0.15, 0.20]:
        r = veto_impact(trades, diverge_thresh=thr)
        print(f'  thresh={thr*100:>4.0f}c   orig=${r["orig"]/100:+7.2f}  after_veto=${r["new"]/100:+7.2f}  '
              f'swing=${(r["new"]-r["orig"])/100:+6.2f}  kept={r["kept_n"]}/{len(trades)} '
              f'(skipped: {-r["skipped_loss"]/100:+.2f} loss, {-r["skipped_win"]/100:.2f} win)')

    print()
    print('======== BOOTSTRAP: stability of veto result (proxy=90s, thresh=5c) ========')
    trades = collect_trades(90)
    B = 1000
    rng = random.Random(42)
    swings = []
    for _ in range(B):
        sample = [trades[rng.randrange(len(trades))] for _ in range(len(trades))]
        r = veto_impact(sample, diverge_thresh=0.05)
        swings.append(r['new'] - r['orig'])
    swings.sort()
    print(f'  n={len(trades)} trades, B={B} resamples')
    print(f'  veto swing mean: ${sum(swings)/B/100:+.2f}')
    print(f'  95% CI: [${swings[int(B*0.025)]/100:+.2f}, ${swings[int(B*0.975)]/100:+.2f}]')
    print(f'  fraction of resamples with positive swing: {sum(1 for s in swings if s>0)/B*100:.1f}%')


if __name__ == '__main__':
    main()
