"""Cross-check: would the fair-value model have prevented the engines' losing trades?

For every live trade in the data (especially the v5_unified losers):
  - Compute the model's p_yes at the entry time
  - Compute the market's implied p_yes from the entry price
  - Was the model warning us off this trade (disagreed with engine direction)?
  - Or was the model also wrong (agreed with engine direction)?

If the model would have flagged the losers, that's strong evidence the
model has structural skill. If the model agreed with the engine on the
losers, the model has the same blind spots.
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


def main():
    sources = [
        ('v5_unified',     'live_v5_unified_trades.jsonl'),
        ('v5_old',         'live_v5_trades.jsonl'),
        ('live_ta',        'live_ta_trades.jsonl'),
    ]
    history = build_btc_history(*(DATA_DIR/fn for _, fn in sources),
                                *(DATA_DIR/'paper_ta_2026_05_12.jsonl' for _ in [1]),
                                *(DATA_DIR/'shadow_velocity_2026_05_14.jsonl' for _ in [1]))
    print(f'BTC history: {len(history)} pts')

    losers_by_src = defaultdict(list)
    winners_by_src = defaultdict(list)
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
            outcome = rest if rest in ('yes', 'no') else e.get('result') or e.get('outcome')
            if outcome not in ('yes', 'no'): continue
            side = e.get('side'); entry_c = e.get('entry_price_cents')
            cc = e.get('cycle_close_ms') or e.get('close_ts_ms')
            if not cc:
                try:
                    import datetime
                    ct = ce.get('close_time')
                    if ct:
                        cc = int(datetime.datetime.fromisoformat(ct.replace('Z','+00:00')).timestamp()*1000)
                except: pass
            # Entry timestamp: prefer explicit fields, else assume LATE-leg avg of T-90s for v5
            ets = e.get('decided_at_ts_ms') or e.get('entered_at_ms')
            if not ets and label in ('v5_unified', 'v5_old'):
                # v5 entries cluster around 60-180s before close; assume T-90s proxy
                if cc: ets = cc - 90_000
            if not ets:
                ets = e.get('ts_ms')
            net_c = e.get('net_cents', 0)
            won = (outcome == side)
            if not all([entry_c is not None, ets, cc]): continue
            if ets > cc: continue  # don't use settle ts
            spot = e.get('btc_price_at_entry') or e.get('spot_close')
            if not spot:
                idx = bisect.bisect_right([s[0] for s in history], ets) - 1
                if idx < 0: continue
                spot = history[idx][1]
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
            # Engine's implied: paid entry_c for `side`
            engine_implied = entry_c/100 if side=='yes' else (100-entry_c)/100
            # Model agrees with engine if model also gives this side high p
            model_agrees = pm >= 0.5 if side == 'yes' else pm < 0.5
            rec = dict(ticker=t, side=side, entry_c=entry_c, net_c=net_c,
                       won=won, p_model=pm, p_engine_implied=engine_implied,
                       diverge_c=(pm*100) - (engine_implied*100), tau=tau)
            (winners_by_src if won else losers_by_src)[label].append(rec)

    print(f'\n======== ENGINE LOSER ANALYSIS ========')
    print(f'For each losing trade: did the model agree with the engine direction?')
    for src in ('v5_unified','v5_old','live_ta'):
        losers = losers_by_src[src]
        if not losers: continue
        agreed = sum(1 for r in losers if (r['p_model'] >= 0.5) == (r['side'] == 'yes'))
        avg_pm = sum(r['p_model'] for r in losers)/len(losers)
        avg_ei = sum(r['p_engine_implied'] for r in losers)/len(losers)
        avg_div = sum(r['diverge_c'] for r in losers)/len(losers)
        print(f'  {src:15s} losers={len(losers):>2d}  model_agreed={agreed}/{len(losers)} '
              f'({agreed/len(losers)*100:.0f}%)  '
              f'avg_p_model={avg_pm*100:.1f}%  avg_engine_implied={avg_ei*100:.1f}%  '
              f'avg_diverge={avg_div:+.1f}c')

    print()
    print(f'======== ENGINE WINNER ANALYSIS (control) ========')
    for src in ('v5_unified','v5_old','live_ta'):
        wins = winners_by_src[src]
        if not wins: continue
        agreed = sum(1 for r in wins if (r['p_model'] >= 0.5) == (r['side'] == 'yes'))
        avg_pm = sum(r['p_model'] for r in wins)/len(wins)
        avg_ei = sum(r['p_engine_implied'] for r in wins)/len(wins)
        avg_div = sum(r['diverge_c'] for r in wins)/len(wins)
        print(f'  {src:15s} winners={len(wins):>3d}  model_agreed={agreed}/{len(wins)} '
              f'({agreed/len(wins)*100:.0f}%)  '
              f'avg_p_model={avg_pm*100:.1f}%  avg_engine_implied={avg_ei*100:.1f}%  '
              f'avg_diverge={avg_div:+.1f}c')

    # Specifically the 13 v5_unified EM cursed-stripe losers
    print()
    print(f'======== V5_UNIFIED LOSERS - DETAIL ========')
    print(f'(would model have flagged?  threshold = model disagrees with engine by >=5c)')
    losers = losers_by_src['v5_unified']
    if losers:
        flagged = 0
        for r in losers:
            engine_p_yes = r['entry_c']/100 if r['side']=='yes' else (100-r['entry_c'])/100
            model_p_yes = r['p_model']
            # Engine wanted side X; would model also want side X?
            disagrees = (r['side'] == 'yes' and model_p_yes < engine_p_yes - 0.05) or \
                        (r['side'] == 'no'  and model_p_yes > engine_p_yes + 0.05)
            marker = ' FLAGGED' if disagrees else '  agreed'
            if disagrees: flagged += 1
            print(f'  {r["ticker"][-15:]:15s} side={r["side"]:3s}@{r["entry_c"]:>3d}c '
                  f'tau={r["tau"]:>6.0f}s  '
                  f'p_engine={engine_p_yes*100:>5.1f}%  p_model={model_p_yes*100:>5.1f}%  '
                  f'div={(model_p_yes-engine_p_yes)*100:+5.1f}c  net={r["net_c"]:+5d}c  {marker}')
        print(f'\n  Model would have flagged {flagged}/{len(losers)} v5_unified losers')

        # Compute realized P&L impact of veto
        print('\n======== VETO IMPACT SIMULATION ========')
        for src in ('v5_unified','v5_old','live_ta'):
            wins = winners_by_src[src]; losses = losers_by_src[src]
            if not wins and not losses: continue
            # For each trade, check if model disagrees by >5c
            def model_disagrees(r):
                e_p = r['p_engine_implied']
                m_p = r['p_model']
                if r['side'] == 'yes':
                    return m_p < e_p - 0.05
                else:
                    return m_p > e_p + 0.05
            kept_wins = [r for r in wins if not model_disagrees(r)]
            kept_losses = [r for r in losses if not model_disagrees(r)]
            skipped_wins = [r for r in wins if model_disagrees(r)]
            skipped_losses = [r for r in losses if model_disagrees(r)]
            orig_net = sum(r['net_c'] for r in wins + losses)
            new_net  = sum(r['net_c'] for r in kept_wins + kept_losses)
            saved_loss = sum(r['net_c'] for r in skipped_losses)
            sacrificed_win = sum(r['net_c'] for r in skipped_wins)
            print(f'  {src:15s} orig=${orig_net/100:+7.2f}  after_veto=${new_net/100:+7.2f}  '
                  f'(saved_loss=${-saved_loss/100:+.2f}, sacrificed_win=${-sacrificed_win/100:.2f})  '
                  f'kept {len(kept_wins+kept_losses)}/{len(wins+losses)} trades')


if __name__ == '__main__':
    main()
