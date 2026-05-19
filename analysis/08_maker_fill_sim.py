"""Maker-fill simulation: do resting limit orders actually fill?

The +$74.58 maker result in 07_full_oos_backtest.py assumes 100% fill on
resting limit orders. This script tests that assumption by replaying the
gradient engine's raw orderbook_delta stream for each market and computing,
for every candidate trade decision, the probability that a maker order
placed at (best_bid + 1c) would have been filled within N seconds.

Pipeline:
  1. Stream raw_events_*.jsonl, extracting (ts, ticker, best_yes_bid,
     best_yes_ask, best_no_bid, best_no_ask) per orderbook_delta.
  2. For each decision tick in the OOS backtest (subset to the 16
     gradient-engine markets), walk forward in the BBO stream and detect
     fills.
  3. Apply realistic fill rates back to the OOS P&L.

For markets without raw_events coverage (paper_ta, shadow_velocity), we
report fill-rate-dependent sensitivities at fixed assumed rates.
"""
from __future__ import annotations
import json, math, sys, bisect
from collections import defaultdict
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(r'C:/Trading/kalshi_btc_gradient_engine/src')))
from kalshi_btc_gradient.models.probability import (
    settlement_fair_probability, SettlementProbabilityInput,
    SettlementProbabilityConfig,
)

CAPTURE_DIR = Path(r'C:/Trading/kalshi_btc_gradient_engine/data/captures/2026-05-18')
STRIKES = json.loads((Path(__file__).resolve().parent / 'strikes_cache.json').read_text())
SLIP = 2


def extract_bbo_stream():
    """Stream all raw_events_*.jsonl, yield (ts_ms, ticker, yb_c, ya_c, nb_c, na_c)."""
    files = sorted(CAPTURE_DIR.glob('raw_events_*.jsonl'))
    for fn in files:
        with fn.open(encoding='utf-8', errors='ignore') as f:
            for line in f:
                try: e = json.loads(line)
                except: continue
                et = e.get('event_type')
                if et not in ('orderbook_delta', 'orderbook_snapshot'): continue
                ts = e.get('wall_clock_ts_ms')
                t = e.get('market_ticker')
                norm = e.get('normalized') or {}
                yb = norm.get('best_yes_bid'); ya = norm.get('best_yes_ask')
                if yb is None or ya is None: continue
                try:
                    yb_c = int(round(float(yb) * 100))
                    ya_c = int(round(float(ya) * 100))
                except: continue
                nb_c = 100 - ya_c
                na_c = 100 - yb_c
                yield (ts, t, yb_c, ya_c, nb_c, na_c)


def build_bbo_index() -> dict:
    """{ticker: sorted_list_of_(ts, yb, ya, nb, na)}
    Downsampled to ~1 point per second per ticker to bound memory."""
    by_t: dict[str, list] = defaultdict(list)
    last_ts_per_ticker: dict[str, int] = {}
    n_seen = n_kept = 0
    for ts, t, yb, ya, nb, na in extract_bbo_stream():
        n_seen += 1
        # Keep only if ts is at least 1s after the last kept for this ticker
        last = last_ts_per_ticker.get(t, 0)
        if ts - last >= 1000:
            by_t[t].append((ts, yb, ya, nb, na))
            last_ts_per_ticker[t] = ts
            n_kept += 1
        if n_seen % 5_000_000 == 0:
            print(f'  ... {n_seen/1e6:.0f}M deltas scanned, {n_kept} kept, {len(by_t)} tickers')
    for t in by_t:
        by_t[t].sort()
    print(f'BBO points: scanned={n_seen} kept={n_kept} tickers={len(by_t)}')
    return dict(by_t)


def simulate_maker_fill(bbo_index: dict, ticker: str, decision_ts: int, side: str,
                         limit_c: int, timeout_s: int = 60) -> tuple[bool, int | None, int | None]:
    """Place a maker BUY at limit_c on `side`. Fill if subsequent quote crosses.
    Returns (filled, fill_ts_ms, fill_price_c)."""
    quotes = bbo_index.get(ticker)
    if not quotes: return False, None, None
    # Find first quote at or after decision_ts
    ts_arr = [q[0] for q in quotes]
    idx = bisect.bisect_left(ts_arr, decision_ts)
    deadline = decision_ts + timeout_s * 1000
    while idx < len(quotes) and quotes[idx][0] <= deadline:
        ts, yb, ya, nb, na = quotes[idx]
        if side == 'yes':
            # Filled if current best_yes_ask <= our bid
            if ya <= limit_c: return True, ts, limit_c
        else:
            if na <= limit_c: return True, ts, limit_c
        idx += 1
    return False, None, None


# Reuse the 07 backtest record building, just import
from analysis.common import DATA_DIR


def kalshi_fee_maker(price_c, contracts):
    p = price_c / 100
    return math.ceil(0.0175 * contracts * p * (1 - p) * 100)

def kalshi_fee_taker(price_c, contracts):
    p = price_c / 100
    return math.ceil(0.07 * contracts * p * (1 - p) * 100)


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
    h.sort()
    return h


def realized_vol_annualized(history, end_ts_ms, window_ms=300_000):
    ts_arr = [s[0] for s in history]
    end_idx = bisect.bisect_right(ts_arr, end_ts_ms) - 1
    if end_idx < 5: return 0.5
    start_idx = bisect.bisect_left(ts_arr, end_ts_ms - window_ms)
    prices = [history[i][1] for i in range(start_idx, end_idx + 1) if history[i][1] > 0]
    if len(prices) < 3: return 0.5
    log_rets = [math.log(prices[i+1]/prices[i]) for i in range(len(prices)-1)]
    if not log_rets: return 0.5
    mean = sum(log_rets) / len(log_rets)
    var = sum((r - mean)**2 for r in log_rets) / max(1, len(log_rets) - 1)
    sigma_per_sample = math.sqrt(var)
    sigma_per_sec = sigma_per_sample / math.sqrt(60)
    sigma_ann = sigma_per_sec * math.sqrt(365 * 24 * 3600)
    return max(0.1, min(3.0, sigma_ann))


def load_settles():
    sources = [
        ('paper_ta',         'paper_ta_2026_05_12.jsonl'),
        ('shadow_velocity',  'shadow_velocity_2026_05_14.jsonl'),
        ('live_ta',          'live_ta_trades.jsonl'),
        ('live_ta_v2',       'live_ta_v2_trades.jsonl'),
        ('live_v5_unified',  'live_v5_unified_trades.jsonl'),
        ('live_v5',          'live_v5_trades.jsonl'),
    ]
    history = build_btc_history(*(DATA_DIR / fn for _, fn in sources))
    records = []
    for label, fn in sources:
        p = DATA_DIR / fn
        if not p.exists(): continue
        for line in p.open(encoding='utf-8', errors='ignore'):
            try: e = json.loads(line)
            except: continue
            if e.get('kind') not in ('settle','settle_with_ladder'): continue
            t = e.get('ticker')
            ce = STRIKES.get(t, {})
            strike = ce.get('strike')
            if not strike: continue
            rest_result = ce.get('result')
            log_outcome = e.get('outcome') or e.get('result')
            if rest_result not in ('yes','no'):
                if log_outcome not in ('yes','no'): continue
                outcome_yes = (log_outcome == 'yes')
            else: outcome_yes = (rest_result == 'yes')
            entry_ts = e.get('decided_at_ts_ms') or e.get('entered_at_ms') or e.get('ts_ms')
            cycle_close = e.get('cycle_close_ms')
            side = e.get('side'); entry_c = e.get('entry_price_cents')
            if not all([entry_ts, side, entry_c is not None]): continue
            spot = e.get('btc_price_at_entry') or e.get('spot_close')
            if not spot:
                idx = bisect.bisect_right([s[0] for s in history], entry_ts) - 1
                if idx < 0: continue
                spot = history[idx][1]
            if cycle_close: tau = (cycle_close - entry_ts) / 1000.0
            else: tau = e.get('seconds_to_close')
            if not tau or tau <= 0: continue
            sigma = realized_vol_annualized(history, entry_ts)
            records.append(dict(
                src=label, ticker=t, side=side, entry_c=int(entry_c),
                spot=spot, strike=strike, tau_sec=tau, sigma_ann=sigma,
                outcome_yes=outcome_yes, entry_ts=entry_ts,
            ))
    return records


def compute_p_model(r):
    try:
        return settlement_fair_probability(
            SettlementProbabilityInput(
                spot=r['spot'], strike=r['strike'],
                seconds_to_close=r['tau_sec'],
                realized_vol_annualized=r['sigma_ann'],
                drift_annualized=0.0,
            ),
            SettlementProbabilityConfig(sigma_floor_annualized=0.15),
        ).probability_yes
    except: return None


def main():
    print('Building BBO index from gradient engine raw_events...')
    bbo = build_bbo_index()

    print('Loading settles from all sources...')
    records = load_settles()
    print(f'  total settles with strikes: {len(records)}')
    # Filter to records whose ticker is in the BBO index
    has_bbo = [r for r in records if r['ticker'] in bbo]
    print(f'  records with BBO coverage: {len(has_bbo)}')

    # For each record with BBO, simulate trade with maker rest at fill price
    print()
    print('======== MAKER-FILL SIMULATION (gradient engine BBO coverage only) ========')
    print(f'(Limit at best_bid+1c, timeout 60s, fee=maker)')
    for threshold in [0.05, 0.08, 0.10, 0.12]:
        n_attempts = n_fills = 0; net = 0; w = 0
        for r in has_bbo:
            pm = compute_p_model(r)
            if pm is None: continue
            pmk = r['entry_c']/100 if r['side']=='yes' else (100 - r['entry_c'])/100
            edge = pm - pmk
            if abs(edge) * 100 < threshold * 100: continue
            n_attempts += 1
            # Determine side and resting limit
            if edge > 0:
                our_side = 'yes'
                # Place at best_yes_bid + 1
                ts_arr = [q[0] for q in bbo[r['ticker']]]
                idx = bisect.bisect_left(ts_arr, r['entry_ts'])
                if idx >= len(bbo[r['ticker']]): continue
                _, yb, ya, nb, na = bbo[r['ticker']][idx]
                limit = min(yb + 1, ya - 1)  # be inside the spread but not crossing
                won = r['outcome_yes']
            else:
                our_side = 'no'
                idx = bisect.bisect_left([q[0] for q in bbo[r['ticker']]], r['entry_ts'])
                if idx >= len(bbo[r['ticker']]): continue
                _, yb, ya, nb, na = bbo[r['ticker']][idx]
                limit = min(nb + 1, na - 1)
                won = not r['outcome_yes']
            if limit < 1 or limit > 99: continue
            filled, fill_ts, fill_price = simulate_maker_fill(bbo, r['ticker'],
                                                              r['entry_ts'], our_side,
                                                              limit, timeout_s=120)
            if not filled: continue
            n_fills += 1
            payoff = (100 - fill_price) * 10 if won else -fill_price * 10
            fee = kalshi_fee_maker(fill_price, 10)
            net += payoff - fee
            w += won
        fr = n_fills / max(n_attempts, 1) * 100
        print(f'  threshold {threshold*100:>4.1f}c   attempts={n_attempts:3d}  fills={n_fills:3d} '
              f'({fr:>4.1f}%)  net=${net/100:>+7.2f}  WR={w/max(n_fills,1)*100:.0f}%')

    # Apply average fill rate to the full 556-settle backtest as a sensitivity
    print()
    print('======== SENSITIVITY: applying fill-rate band to full OOS backtest ========')
    # First compute the 100%-fill maker P&L (from 07's logic) at threshold 5c
    full_trades = []
    full_history = None
    for r in records:
        pm = compute_p_model(r)
        if pm is None: continue
        pmk = r['entry_c']/100 if r['side']=='yes' else (100 - r['entry_c'])/100
        edge = pm - pmk
        if abs(edge) * 100 < 5: continue
        if edge > 0:
            eff = int(r['entry_c'] if r['side']=='yes' else 100 - r['entry_c'])
            won = r['outcome_yes']
        else:
            eff = int((100 - r['entry_c']) if r['side']=='yes' else r['entry_c'])
            won = not r['outcome_yes']
        if eff <= 0 or eff >= 100: continue
        # Resting maker: assume we'd rest at best_bid+1, so 2c better than current ask
        eff_maker = max(1, eff - 2)
        payoff = (100 - eff_maker) * 10 if won else -eff_maker * 10
        fee = kalshi_fee_maker(eff_maker, 10)
        full_trades.append((payoff - fee, won, eff_maker))

    if full_trades:
        # Sort by entry_ts conceptually — just use random sample
        n_full = len(full_trades)
        net_100 = sum(t[0] for t in full_trades)
        print(f'  Full OOS, 100% fill (5c thresh, rest at bid+1): n={n_full} net=${net_100/100:+.2f}')
        for fr_pct in [10, 20, 30, 40, 50, 60, 70, 80, 100]:
            # Subset to first fr_pct% of trades (proxy for prob-of-fill)
            # In reality fills are random — assume uniform fill probability
            # Expected net = fill_rate * net_per_trade * n (no selection)
            expected_net = sum(t[0] for t in full_trades) * fr_pct / 100
            print(f'  fill rate {fr_pct:>3d}%   expected net=${expected_net/100:+.2f}')


if __name__ == '__main__':
    main()
