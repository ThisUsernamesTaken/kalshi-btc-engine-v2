"""Hybrid: veto + flip-when-extreme.

The veto layer (analysis/13, 14) skips trades where the model disagrees
with the engine direction by ≥5c. But when the disagreement is *large*
(say ≥20c), the model isn't just saying "uncertain" — it's saying
"engine is decisively wrong." On those trades, we could actively bet the
OPPOSITE side.

This iteration tests:
  - Pure veto (skip if disagreement ≥ T_skip)
  - Veto + flip (skip if T_skip ≤ disagreement < T_flip; flip if ≥ T_flip)
  - Pure flip (flip if disagreement ≥ T_flip; otherwise keep)

Flip means: instead of the engine's trade, place the OPPOSITE-side trade
at the implied opposite ask. This requires placing a NEW trade, not just
skipping. Realistic execution still subject to slippage.
"""
from __future__ import annotations
import json, math, sys, bisect
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(r'C:/Trading/kalshi-btc-engine-v2/src')))
from kalshi_btc_engine_v2.model_veto import fair_p_yes  # noqa: E402

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
            # Disagreement towards opposite side
            disagrees = ((side == 'yes' and diverge_c < 0) or
                         (side == 'no' and diverge_c > 0))
            rows.append(dict(src=label, side=side, entry_c=entry_c, ctr=ctr,
                             won=won, net_c=e.get('net_cents', 0),
                             p_model=p_model, engine_implied=engine_implied,
                             diverge_c=diverge_c, disagrees=disagrees,
                             tau=tau, outcome=outcome, spot=spot, strike=strike))
    return rows


def simulate_strategy(rows, t_skip=5, t_flip=None):
    """
    For each trade:
      - If model disagrees by < t_skip: take engine's trade (KEEP)
      - If t_skip <= disagreement < t_flip: SKIP
      - If disagreement >= t_flip: FLIP (place opposite side at engine's implied)
    """
    total = 0; n_keep = 0; n_skip = 0; n_flip = 0
    flip_wins = flip_losses = 0
    for r in rows:
        if not r['disagrees'] or abs(r['diverge_c']) < t_skip:
            # Keep engine's trade
            total += r['net_c']
            n_keep += 1
        elif t_flip is not None and abs(r['diverge_c']) >= t_flip:
            # Flip: opposite side
            # opposite ask ~ 100 - engine_paid (rough)
            opp_entry_c = 100 - r['entry_c']
            # Add slip if taker
            opp_eff_c = opp_entry_c + SLIP
            if 1 <= opp_eff_c <= 99:
                opp_side = 'no' if r['side'] == 'yes' else 'yes'
                opp_won = (r['outcome'] == opp_side)
                if opp_won:
                    payoff = (100 - opp_eff_c) * r['ctr']; flip_wins += 1
                else:
                    payoff = -opp_eff_c * r['ctr']; flip_losses += 1
                fee = kalshi_fee_taker(opp_eff_c, r['ctr'])
                total += payoff - fee
                n_flip += 1
            else:
                n_skip += 1
        else:
            # Veto: skip
            n_skip += 1
    return dict(total=total, n_keep=n_keep, n_skip=n_skip, n_flip=n_flip,
                flip_wins=flip_wins, flip_losses=flip_losses)


def main():
    rows = collect()
    print(f'Loaded {len(rows)} live trades\n')
    # Original P&L
    orig = sum(r['net_c'] for r in rows)
    print(f'Original P&L (no veto): ${orig/100:+.2f}\n')

    print('======== VETO-ONLY (skip if disagree by >= t_skip) ========')
    for t_skip in [2, 5, 8, 10, 15, 20]:
        r = simulate_strategy(rows, t_skip=t_skip)
        print(f'  t_skip={t_skip:2d}c   total=${r["total"]/100:+7.2f}  swing=${(r["total"]-orig)/100:+6.2f}  '
              f'kept={r["n_keep"]:3d}  skipped={r["n_skip"]:3d}')

    print()
    print('======== VETO + FLIP (skip if disagree, flip if extreme) ========')
    print(f'{"t_skip":>7s} {"t_flip":>7s} {"total":>9s} {"swing":>7s} {"kept":>5s} {"skip":>5s} {"flip":>5s} {"flip_W/L":>9s}')
    for t_skip in [5, 8, 10]:
        for t_flip in [15, 20, 25, 30, 40, 50]:
            r = simulate_strategy(rows, t_skip=t_skip, t_flip=t_flip)
            wl = f'{r["flip_wins"]}/{r["flip_losses"]}'
            print(f'  {t_skip:>5d}c {t_flip:>5d}c ${r["total"]/100:>+7.2f} ${(r["total"]-orig)/100:>+6.2f} '
                  f'{r["n_keep"]:>5d} {r["n_skip"]:>5d} {r["n_flip"]:>5d} {wl:>9s}')


if __name__ == '__main__':
    main()
