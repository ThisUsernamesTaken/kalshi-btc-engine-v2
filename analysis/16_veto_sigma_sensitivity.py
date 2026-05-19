"""Veto sigma sensitivity.

The veto needs `sigma_annualized` to compute p_model. In the OOS data,
we use rv_5m from the trigger event when available, else 0.5 default.
Test whether changing the default sigma materially changes the veto's
effectiveness.
"""
from __future__ import annotations
import json, math, sys, bisect
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(r'C:/Trading/kalshi-btc-engine-v2/src')))
from kalshi_btc_engine_v2.model_veto import veto_decision

DATA_DIR = Path(__file__).resolve().parent.parent / 'data'
STRIKES = json.loads((Path(__file__).resolve().parent / 'strikes_cache.json').read_text())


def build_btc_history():
    h = []
    for fn in ('paper_ta_2026_05_12.jsonl', 'shadow_velocity_2026_05_14.jsonl',
               'live_v5_unified_trades.jsonl', 'live_v5_trades.jsonl',
               'live_ta_trades.jsonl', 'live_ta_v2_trades.jsonl'):
        p = DATA_DIR / fn
        if not p.exists(): continue
        for line in p.open(encoding='utf-8', errors='ignore'):
            try: e = json.loads(line)
            except: continue
            bp = (e.get('spot_close') or e.get('cycle_open_price')
                  or e.get('btc_price') or e.get('btc_price_at_entry'))
            ts = e.get('ts_minute_ms') or e.get('decided_at_ts_ms') or e.get('ts_ms')
            if bp and ts: h.append((ts, float(bp)))
    h.sort(); return h


def vol_ann(history, end_ts, window_ms):
    ts_arr = [s[0] for s in history]
    end_idx = bisect.bisect_right(ts_arr, end_ts) - 1
    if end_idx < 5: return None
    start_idx = bisect.bisect_left(ts_arr, end_ts - window_ms)
    prices = [history[i][1] for i in range(start_idx, end_idx + 1) if history[i][1] > 0]
    if len(prices) < 3: return None
    lrets = [math.log(prices[i+1]/prices[i]) for i in range(len(prices)-1)]
    if not lrets: return None
    m = sum(lrets)/len(lrets); v = sum((r-m)**2 for r in lrets)/max(1,len(lrets)-1)
    sps = math.sqrt(v); sigsec = sps/math.sqrt(60)
    return max(0.05, min(3.0, sigsec * math.sqrt(365*24*3600)))


def collect_trades():
    history = build_btc_history()
    sources = [('v5_unified','live_v5_unified_trades.jsonl'),
               ('v5_old','live_v5_trades.jsonl'),
               ('live_ta','live_ta_trades.jsonl')]
    rows = []
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
                import datetime
                ct = ce.get('close_time')
                if ct:
                    try: cc = int(datetime.datetime.fromisoformat(ct.replace('Z','+00:00')).timestamp()*1000)
                    except: pass
            if not all([entry_c is not None, side, cc]): continue
            if label == 'live_ta':
                ets = e.get('decided_at_ts_ms') or (cc - 90_000)
            else:
                ets = cc - 90_000
            if ets >= cc: continue
            spot = e.get('btc_price_at_entry') or e.get('spot_close')
            if not spot:
                idx = bisect.bisect_right([s[0] for s in history], ets) - 1
                if idx < 0: continue
                spot = history[idx][1]
            won = (outcome == side)
            rows.append(dict(src=label, ticker=t, side=side, entry_c=entry_c,
                             spot=spot, strike=strike, cc=cc, ets=ets,
                             won=won, net_c=e.get('net_cents', 0),
                             history=history))
    return rows


def apply_veto(trades, sigma_value):
    """Apply veto with a fixed sigma_annualized."""
    saved = 0; sacrificed = 0; total_change = 0
    flagged_losers = total_losers = 0
    flagged_winners = total_winners = 0
    for r in trades:
        tau = (r['cc'] - r['ets']) / 1000.0
        if tau <= 0: continue
        skip, p_model, reason = veto_decision(
            spot_btc=r['spot'], strike=r['strike'],
            seconds_to_close=tau, sigma_annualized=sigma_value,
            engine_side=r['side'], engine_price_cents=r['entry_c'],
            threshold_cents=5,
        )
        if r['won']:
            total_winners += 1
            if skip: flagged_winners += 1; sacrificed += r['net_c']
        else:
            total_losers += 1
            if skip: flagged_losers += 1; saved += r['net_c']  # net_c is negative
        if skip: total_change -= r['net_c']  # we're SAVING this loss / GIVING UP this win
    return dict(
        flagged_losers=flagged_losers, total_losers=total_losers,
        flagged_winners=flagged_winners, total_winners=total_winners,
        saved_loss=saved, sacrificed_win=sacrificed, swing=total_change,
    )


def main():
    trades = collect_trades()
    print(f'Loaded {len(trades)} live trades')

    print('\n======== SIGMA SENSITIVITY ========')
    print(f'{"sigma_ann":>10s} {"loser_flag":>14s} {"winner_flag":>14s} {"swing_$":>10s}')
    for sigma in [0.15, 0.25, 0.35, 0.45, 0.50, 0.60, 0.75, 1.0, 1.25, 1.50]:
        r = apply_veto(trades, sigma)
        print(f'  {sigma:>8.2f}  {r["flagged_losers"]}/{r["total_losers"]} '
              f'({r["flagged_losers"]/max(r["total_losers"],1)*100:>5.0f}%)  '
              f'{r["flagged_winners"]}/{r["total_winners"]} '
              f'({r["flagged_winners"]/max(r["total_winners"],1)*100:>5.0f}%)  '
              f'${r["swing"]/100:>+7.2f}')

    print('\n======== ADAPTIVE SIGMA (5-min realized vol from history) ========')
    history = trades[0]['history'] if trades else []
    flagged_losers = flagged_winners = total_losers = total_winners = 0
    total_change = 0; sigmas_used = []
    for r in trades:
        sigma = vol_ann(history, r['ets'], 300_000) or 0.5
        sigmas_used.append(sigma)
        tau = (r['cc'] - r['ets']) / 1000.0
        if tau <= 0: continue
        skip, p_model, reason = veto_decision(
            spot_btc=r['spot'], strike=r['strike'],
            seconds_to_close=tau, sigma_annualized=sigma,
            engine_side=r['side'], engine_price_cents=r['entry_c'],
        )
        if r['won']: total_winners += 1
        else: total_losers += 1
        if skip:
            if r['won']: flagged_winners += 1
            else: flagged_losers += 1
            total_change -= r['net_c']
    sigmas_used.sort()
    n = len(sigmas_used)
    print(f'  sigma distribution: min={sigmas_used[0]:.3f} median={sigmas_used[n//2]:.3f} max={sigmas_used[-1]:.3f}')
    print(f'  losers flagged: {flagged_losers}/{total_losers} ({flagged_losers/max(total_losers,1)*100:.0f}%)')
    print(f'  winners flagged: {flagged_winners}/{total_winners} ({flagged_winners/max(total_winners,1)*100:.0f}%)')
    print(f'  swing: ${total_change/100:+.2f}')


if __name__ == '__main__':
    main()
