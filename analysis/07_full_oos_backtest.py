"""Full out-of-sample fair-value backtest across all logged tickers.

Uses analysis/strikes_cache.json (from fetch_strikes.py) plus per-fill
context from paper_ta, shadow_velocity, and live_ta logs to evaluate the
gradient engine's settlement_fair_probability model across hundreds of
markets spanning multiple days.

For each settled ticker with a known strike:
  1. Find the settle event and its entry context
  2. Compute BTC spot at entry (from snapshots or btc_price_at_entry field)
  3. Compute time-to-close (cycle_close_ms - decided_at_ts_ms)
  4. Compute realized vol from prior BTC spot history
  5. Run settlement_fair_probability(spot, strike, tau, sigma) -> p_model
  6. Compare to implied market probability (entry_price as proxy)
  7. Simulate trades at various |p_model - p_market| thresholds

This is genuine OOS validation: the model is fixed math, the strikes come
from Kalshi REST, and outcomes are real. Different markets, different days,
no overlap with the 2026-05-18 capture used in 05_fair_value_backtest.py.
"""
from __future__ import annotations
import json, math, sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(r'C:/Trading/kalshi_btc_gradient_engine/src')))
from kalshi_btc_gradient.models.probability import (
    settlement_fair_probability, SettlementProbabilityInput,
    SettlementProbabilityConfig,
)

DATA_DIR = Path(__file__).resolve().parent.parent / 'data'
CACHE = json.loads((Path(__file__).resolve().parent / 'strikes_cache.json').read_text())
SLIP = 2


def kalshi_fee_taker(price_c, contracts):
    p = price_c / 100
    return math.ceil(0.07 * contracts * p * (1 - p) * 100)

def kalshi_fee_maker(price_c, contracts):
    p = price_c / 100
    return math.ceil(0.0175 * contracts * p * (1 - p) * 100)


def build_btc_history(*log_paths):
    """Collect (ts_ms, btc_price) from every spot-bearing event."""
    history = []
    for path in log_paths:
        if not path.exists(): continue
        for line in path.open(encoding='utf-8', errors='ignore'):
            try: e = json.loads(line)
            except: continue
            bp = (e.get('spot_close') or e.get('cycle_open_price')
                  or e.get('btc_price') or e.get('btc_price_at_entry'))
            ts = e.get('ts_minute_ms') or e.get('decided_at_ts_ms') or e.get('ts_ms')
            if bp and ts: history.append((ts, float(bp)))
    history.sort()
    return history


def spot_at(history, ts_ms):
    import bisect
    ts_arr = [s[0] for s in history]
    idx = bisect.bisect_right(ts_arr, ts_ms) - 1
    return history[idx][1] if idx >= 0 else None


def realized_vol_annualized(history, end_ts_ms, window_ms=300_000):
    import bisect
    ts_arr = [s[0] for s in history]
    end_idx = bisect.bisect_right(ts_arr, end_ts_ms) - 1
    if end_idx < 5: return 0.5
    window_start = end_ts_ms - window_ms
    start_idx = bisect.bisect_left(ts_arr, window_start)
    prices = [history[i][1] for i in range(start_idx, end_idx + 1) if history[i][1] > 0]
    if len(prices) < 3: return 0.5
    log_rets = [math.log(prices[i+1]/prices[i]) for i in range(len(prices)-1)]
    if not log_rets: return 0.5
    mean = sum(log_rets) / len(log_rets)
    var = sum((r - mean)**2 for r in log_rets) / max(1, len(log_rets) - 1)
    sigma_per_sample = math.sqrt(var)
    # samples are roughly per-minute
    sigma_per_sec = sigma_per_sample / math.sqrt(60)
    sigma_ann = sigma_per_sec * math.sqrt(365 * 24 * 3600)
    return max(0.1, min(3.0, sigma_ann))


def load_all_settles():
    """Return list of dicts: one per settle event with strike + entry context joined."""
    sources = [
        ('paper_ta',         'paper_ta_2026_05_12.jsonl'),
        ('shadow_velocity',  'shadow_velocity_2026_05_14.jsonl'),
        ('live_ta',          'live_ta_trades.jsonl'),
        ('live_ta_v2',       'live_ta_v2_trades.jsonl'),
        ('live_v5_unified',  'live_v5_unified_trades.jsonl'),
        ('live_v5',          'live_v5_trades.jsonl'),
    ]
    # Build BTC history across all sources
    history = build_btc_history(*(DATA_DIR / fn for _, fn in sources))
    print(f'BTC history points: {len(history)}')

    records = []
    for label, fn in sources:
        p = DATA_DIR / fn
        if not p.exists(): continue
        for line in p.open(encoding='utf-8', errors='ignore'):
            try: e = json.loads(line)
            except: continue
            if e.get('kind') not in ('settle', 'settle_with_ladder'): continue
            t = e.get('ticker')
            if not t: continue
            cache_entry = CACHE.get(t, {})
            strike = cache_entry.get('strike')
            if not strike: continue
            # Trust REST 'result' over inferred outcome
            rest_result = cache_entry.get('result')
            log_outcome = e.get('outcome') or e.get('result')
            if rest_result not in ('yes', 'no'):
                if log_outcome not in ('yes', 'no'): continue
                outcome_yes = (log_outcome == 'yes')
            else:
                outcome_yes = (rest_result == 'yes')

            entry_ts = (e.get('decided_at_ts_ms')
                        or e.get('entered_at_ms')
                        or e.get('ts_ms'))
            cycle_close = e.get('cycle_close_ms')
            side = e.get('side')
            entry_c = e.get('entry_price_cents')
            if not all([entry_ts, side, entry_c is not None]): continue

            # Spot at entry: prefer in-record field, fall back to history lookup
            spot = (e.get('btc_price_at_entry') or e.get('spot_close')
                    or spot_at(history, entry_ts))
            if not spot: continue

            # Time to close: prefer cycle_close - entry_ts, else infer
            if cycle_close:
                tau_sec = (cycle_close - entry_ts) / 1000.0
            else:
                tau_sec = e.get('seconds_to_close')
            if tau_sec is None or tau_sec <= 0: continue

            sigma_ann = realized_vol_annualized(history, entry_ts)
            records.append(dict(
                src=label, ticker=t, side=side,
                entry_c=int(entry_c), spot=spot, strike=strike,
                tau_sec=tau_sec, sigma_ann=sigma_ann,
                outcome_yes=outcome_yes, entry_ts=entry_ts,
                cycle_close=cycle_close,
            ))
    return records


def compute_p_model(rec):
    """Apply the BRTI-averaging-aware model."""
    try:
        result = settlement_fair_probability(
            SettlementProbabilityInput(
                spot=rec['spot'], strike=rec['strike'],
                seconds_to_close=rec['tau_sec'],
                realized_vol_annualized=rec['sigma_ann'],
                drift_annualized=0.0,
            ),
            SettlementProbabilityConfig(sigma_floor_annualized=0.15),
        )
        return result.probability_yes
    except Exception:
        return None


def simulate(records, threshold_c: float, contracts: int = 10, use_maker: bool = False,
             min_tau: float = 30.0, max_tau: float = 900.0, src_filter: str = None):
    fee_fn = kalshi_fee_maker if use_maker else kalshi_fee_taker
    trades = []
    for r in records:
        if src_filter and r['src'] != src_filter: continue
        if not (min_tau <= r['tau_sec'] <= max_tau): continue
        p_model = compute_p_model(r)
        if p_model is None: continue
        # Market-implied p: if r['side']=='yes', entry was paying entry_c for YES
        # so market p_yes ~ entry_c/100. If side='no', entry_c was paid for NO,
        # so market p_yes ~ (100 - entry_c)/100.
        if r['side'] == 'yes':
            p_market = r['entry_c'] / 100
        else:
            p_market = (100 - r['entry_c']) / 100
        edge = p_model - p_market
        if abs(edge) * 100 < threshold_c: continue
        # Trade: if edge > 0, buy YES; else buy NO
        if edge > 0:
            our_side = 'yes'
            ask_c = int(round(p_market * 100))  # paying p_market for YES
            won = r['outcome_yes']
        else:
            our_side = 'no'
            ask_c = int(round((1 - p_market) * 100))
            won = not r['outcome_yes']
        eff = ask_c + (0 if use_maker else SLIP)
        if eff <= 0 or eff >= 100: continue
        payoff = (100 - eff) * contracts if won else -eff * contracts
        fee = fee_fn(eff, contracts)
        net = payoff - fee
        trades.append(dict(rec=r, p_model=p_model, p_market=p_market, edge=edge,
                           our_side=our_side, eff=eff, won=won, net=net))
    return trades


def report(trades, label):
    n = len(trades); w = sum(1 for t in trades if t['won'])
    net = sum(t['net'] for t in trades)
    if n == 0:
        print(f'  {label:60s} (no trades)')
        return
    print(f'  {label:60s} n={n:>4d} WR={w/n*100:>5.1f}% net=${net/100:>+8.2f} avg=${net/n/100:>+6.3f}/tr')


def main():
    records = load_all_settles()
    print(f'Loaded {len(records)} settles with strikes')
    # Distribution by source
    by_src = defaultdict(int)
    for r in records: by_src[r['src']] += 1
    print('  by source:')
    for s, n in sorted(by_src.items()): print(f'    {s:18s} {n}')
    print()

    # Sanity: model vs market on raw probabilities
    print('======== MODEL vs MARKET CALIBRATION ========')
    cal_model = [[0,0] for _ in range(10)]
    cal_market = [[0,0] for _ in range(10)]
    for r in records[:5000]:
        pm = compute_p_model(r)
        if pm is None: continue
        # market-implied
        pmk = r['entry_c']/100 if r['side']=='yes' else (100-r['entry_c'])/100
        cal_model[min(9, int(pm*10))][0] += 1
        cal_market[min(9, int(pmk*10))][0] += 1
        if r['outcome_yes']:
            cal_model[min(9, int(pm*10))][1] += 1
            cal_market[min(9, int(pmk*10))][1] += 1
    print(f'  bucket  | model_n  emp_p_yes  market_n  emp_p_yes')
    for i in range(10):
        mn, mw = cal_model[i]; kn, kw = cal_market[i]
        if mn < 5 and kn < 5: continue
        mp = mw/mn*100 if mn else 0
        kp = kw/kn*100 if kn else 0
        print(f'  {i*10:2d}-{i*10+10:>3d}%  {mn:>5d}    {mp:>6.1f}%    {kn:>5d}    {kp:>6.1f}%')

    print()
    print('======== SIMULATION (all sources combined) ========')
    print('Taker fees, 10ct, +2c slip:')
    for th in [3, 5, 8, 10, 12, 15, 20]:
        report(simulate(records, th), f'|edge_yes| >= {th}c')
    print()
    print('Maker fees:')
    for th in [3, 5, 8, 10, 12, 15, 20]:
        report(simulate(records, th, use_maker=True), f'|edge_yes| >= {th}c (maker)')
    print()
    print('======== BY SOURCE (threshold 10c, taker) ========')
    for src in sorted(by_src.keys()):
        report(simulate(records, 10, src_filter=src), src)
    print()
    print('======== BY ENTRY WINDOW (tau bucket) (threshold 10c, taker) ========')
    for lo, hi in [(30,60),(60,180),(180,600),(600,900)]:
        report(simulate(records, 10, min_tau=lo, max_tau=hi), f'tau {lo}-{hi}s')
    print()
    print('======== WALK-FORWARD BY DAY (threshold 10c, taker) ========')
    # Sort records chronologically; group by date
    import datetime
    by_date = defaultdict(list)
    for r in records:
        d = datetime.datetime.fromtimestamp(r['entry_ts']/1000, datetime.timezone.utc).strftime('%Y-%m-%d')
        by_date[d].append(r)
    print(f'  {"date":12s} {"n_settles":>9s} {"trades":>7s} {"WR":>6s} {"net_$":>9s}')
    for d in sorted(by_date.keys()):
        sub = by_date[d]
        trades = simulate(sub, 10)
        n = len(trades); w = sum(1 for t in trades if t['won'])
        net = sum(t['net'] for t in trades)
        print(f'  {d:12s} {len(sub):>9d} {n:>7d} {w/max(n,1)*100:>5.1f}% ${net/100:>+8.2f}')


if __name__ == '__main__':
    main()
