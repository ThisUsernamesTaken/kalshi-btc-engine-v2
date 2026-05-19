"""Bootstrap CIs on the veto-with-flip hybrid strategy."""
from __future__ import annotations
import json, math, sys, bisect, random
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(r'C:/Trading/kalshi-btc-engine-v2/src')))
from kalshi_btc_engine_v2.model_veto import fair_p_yes

DATA_DIR = Path(__file__).resolve().parent.parent / 'data'
STRIKES = json.loads((Path(__file__).resolve().parent / 'strikes_cache.json').read_text())
SLIP = 2


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


def kalshi_fee_taker(price_c, contracts):
    p = price_c/100; return math.ceil(0.07 * contracts * p * (1-p) * 100)


def collect():
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
            ctr = e.get('contracts', 10)
            cc = e.get('cycle_close_ms') or e.get('close_ts_ms')
            if not cc:
                import datetime
                ct = ce.get('close_time')
                if ct:
                    try: cc = int(datetime.datetime.fromisoformat(ct.replace('Z','+00:00')).timestamp()*1000)
                    except: pass
            if not all([entry_c is not None, side, cc, ctr]): continue
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
            tau = (cc - ets) / 1000.0
            sigma = vol_ann(history, ets)
            p_model = fair_p_yes(spot_btc=spot, strike=strike,
                                  seconds_to_close=tau, sigma_annualized=sigma)
            engine_implied = entry_c/100 if side == 'yes' else (100-entry_c)/100
            diverge_c = (p_model - engine_implied) * 100
            disagrees = ((side == 'yes' and diverge_c < 0) or
                         (side == 'no' and diverge_c > 0))
            rows.append(dict(side=side, entry_c=entry_c, ctr=ctr,
                             won=won, net_c=e.get('net_cents', 0),
                             diverge_c=diverge_c, disagrees=disagrees,
                             outcome=outcome))
    return rows


def trade_net(r, mode):
    """Return the per-trade net for the given strategy mode."""
    if mode == 'orig':
        return r['net_c']
    if mode == 'flip':
        opp_entry_c = 100 - r['entry_c']
        opp_eff_c = opp_entry_c + SLIP
        if not 1 <= opp_eff_c <= 99: return 0
        opp_side = 'no' if r['side'] == 'yes' else 'yes'
        opp_won = (r['outcome'] == opp_side)
        payoff = ((100 - opp_eff_c) * r['ctr'] if opp_won else -opp_eff_c * r['ctr'])
        fee = kalshi_fee_taker(opp_eff_c, r['ctr'])
        return payoff - fee
    return 0  # skip


def simulate(rows, t_skip=8, t_flip=30):
    out = []
    for r in rows:
        if not r['disagrees'] or abs(r['diverge_c']) < t_skip:
            out.append(trade_net(r, 'orig'))
        elif abs(r['diverge_c']) >= t_flip:
            out.append(trade_net(r, 'flip'))
        else:
            out.append(0)  # skip
    return out


def main():
    rows = collect()
    print(f'Loaded {len(rows)} trades\n')
    # Apply best config
    nets = simulate(rows, t_skip=8, t_flip=30)
    total = sum(nets)
    print(f'Strategy (t_skip=8, t_flip=30): total ${total/100:+.2f}')
    print(f'Original P&L: ${sum(r["net_c"] for r in rows)/100:+.2f}')
    swing = total - sum(r['net_c'] for r in rows)
    print(f'Swing: ${swing/100:+.2f}\n')

    print('======== BOOTSTRAP CIs (B=5000) ========')
    print(f'{"config":40s} {"mean":>9s} {"5%":>9s} {"95%":>9s} {"frac>0":>8s}')
    for t_skip, t_flip in [(5, None), (8, None), (5, 15), (5, 20), (8, 25), (8, 30),
                            (10, 30), (10, 40)]:
        if t_flip is None:
            # veto-only mode: skip if disagree by >= t_skip
            def sim_func(samp):
                return [r['net_c'] if (not r['disagrees'] or abs(r['diverge_c']) < t_skip)
                        else 0 for r in samp]
        else:
            tsk, tfl = t_skip, t_flip
            def sim_func(samp, tsk=tsk, tfl=tfl):
                out = []
                for r in samp:
                    if not r['disagrees'] or abs(r['diverge_c']) < tsk:
                        out.append(trade_net(r, 'orig'))
                    elif abs(r['diverge_c']) >= tfl:
                        out.append(trade_net(r, 'flip'))
                    else:
                        out.append(0)
                return out
        # Bootstrap
        B = 5000; rng = random.Random(42); swings = []
        for _ in range(B):
            samp = [rows[rng.randrange(len(rows))] for _ in range(len(rows))]
            new_nets = sim_func(samp)
            orig_nets = [r['net_c'] for r in samp]
            swings.append(sum(new_nets) - sum(orig_nets))
        swings.sort()
        m = sum(swings)/B
        lo = swings[int(B*0.05)]; hi = swings[int(B*0.95)]
        frac_pos = sum(1 for s in swings if s > 0) / B * 100
        config_label = f't_skip={t_skip:>2d}c'
        if t_flip is not None: config_label += f' t_flip={t_flip:>2d}c'
        else: config_label += ' veto-only'
        print(f'  {config_label:40s} ${m/100:>+8.2f} ${lo/100:>+8.2f} ${hi/100:>+8.2f} {frac_pos:>6.1f}%')


if __name__ == '__main__':
    main()
