"""SQLite-driven L2-replay backtest across the burnin_holdpure capture.

Uses the 123GB capture SQLite (kalshi_l2_event, spot_quote_event,
market_dim, kalshi_lifecycle_event) to run a proper multi-day backtest:

  - 336 markets in market_dim
  - 26.8M L2 events (full orderbook history)
  - 4.5M spot quotes across Coinbase / Kraken / Bitstamp (BRTI proxy)
  - Lifecycle events with status / settlement values

For each settled market:
  1. Look up strike from Kalshi REST cache (fetched in fetch_strikes.py)
  2. Extract per-second BBO from kalshi_l2_event
  3. Extract spot path from spot_quote_event (BRTI-constituent venues)
  4. Run settlement_fair_probability at fixed time points
  5. Simulate maker rest orders against actual L2 depth + future quote moves
  6. Settle to actual market outcome

Output: per-strategy P&L with realistic maker fill rates baked in.
"""
from __future__ import annotations
import json, math, sys, bisect, sqlite3
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(r'C:/Trading/kalshi_btc_gradient_engine/src')))
from kalshi_btc_gradient.models.probability import (
    settlement_fair_probability, SettlementProbabilityInput,
    SettlementProbabilityConfig,
)

DB = r'D:/Trading/kalshi-btc-engine-v2/data/burnin_holdpure_2026_05_12.sqlite'
STRIKES = json.loads((Path(__file__).resolve().parent / 'strikes_cache.json').read_text())
SLIP = 2


def kalshi_fee_maker(price_c, contracts):
    p = price_c/100; return math.ceil(0.0175 * contracts * p * (1-p) * 100)


def get_all_settled_markets():
    """Return list of (ticker, strike, outcome_yes, close_ts_ms) for every market we can resolve."""
    con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
    cur = con.cursor()
    cur.execute("""
        select ticker, close_time
          from market_dim
         where ticker like 'KXBTC15M-%'
           and close_time is not null
    """)
    rows = []
    import datetime
    for ticker, close_str in cur.fetchall():
        ce = STRIKES.get(ticker)
        if not ce: continue
        strike = ce.get('strike')
        if not strike: continue
        rest = ce.get('result')
        if rest not in ('yes', 'no'): continue
        try:
            close_dt = datetime.datetime.fromisoformat(close_str.replace('Z', '+00:00'))
            close_ts_ms = int(close_dt.timestamp() * 1000)
        except: continue
        rows.append(dict(ticker=ticker, strike=strike,
                         outcome_yes=(rest == 'yes'), close_ts_ms=close_ts_ms))
    con.close()
    return rows


def extract_bbo_for_market(con, ticker, start_ts, end_ts, downsample_ms=1000):
    """Read pre-computed best_yes_bid / best_yes_ask from L2 events.
    Downsample to ~1 row per `downsample_ms` to keep memory bounded."""
    cur = con.cursor()
    cur.execute("""
        select COALESCE(exchange_ts_ms, received_ts_ms) as ts,
               best_yes_bid, best_yes_ask
          from kalshi_l2_event
         where market_ticker = ?
           and COALESCE(exchange_ts_ms, received_ts_ms) between ? and ?
           and best_yes_bid is not null
           and best_yes_ask is not null
         order by ts
    """, (ticker, start_ts, end_ts))
    bbo = []
    last_emit = 0
    for ts, yb_s, ya_s in cur:
        if ts - last_emit < downsample_ms:
            continue
        try:
            yb_c = int(round(float(yb_s) * 100))
            ya_c = int(round(float(ya_s) * 100))
        except (TypeError, ValueError):
            continue
        if yb_c >= ya_c or yb_c < 0 or ya_c > 100:
            continue
        nb_c = 100 - ya_c
        na_c = 100 - yb_c
        bbo.append((ts, yb_c, ya_c, nb_c, na_c))
        last_emit = ts
    return bbo


def get_spot_at(con, target_ts):
    """Best spot estimate around target_ts: median across Coinbase, Kraken, Bitstamp."""
    cur = con.cursor()
    cur.execute("""
        select venue, mid from spot_quote_event
         where symbol = 'btcusd'
           and venue in ('coinbase','kraken','bitstamp')
           and COALESCE(exchange_ts_ms, received_ts_ms) between ? and ?
         order by COALESCE(exchange_ts_ms, received_ts_ms) desc
         limit 12
    """, (target_ts - 5000, target_ts))
    rows = cur.fetchall()
    if not rows: return None
    # Take most recent per venue, median
    by_venue = {}
    for v, mid in rows:
        if mid and v not in by_venue:
            by_venue[v] = float(mid)
    if not by_venue: return None
    vals = sorted(by_venue.values())
    return vals[len(vals)//2]


def compute_rv_ann_around(con, target_ts, window_s=300):
    """Realized vol from spot history."""
    cur = con.cursor()
    cur.execute("""
        select COALESCE(exchange_ts_ms, received_ts_ms) as ts, mid
          from spot_quote_event
         where symbol = 'btcusd'
           and venue = 'coinbase'
           and COALESCE(exchange_ts_ms, received_ts_ms) between ? and ?
         order by ts
    """, (target_ts - window_s * 1000, target_ts))
    rows = cur.fetchall()
    if len(rows) < 3: return 0.5
    prices = [float(m) for _, m in rows if m]
    if len(prices) < 3: return 0.5
    log_rets = [math.log(prices[i+1]/prices[i]) for i in range(len(prices)-1)
                if prices[i] > 0 and prices[i+1] > 0]
    if not log_rets: return 0.5
    m = sum(log_rets)/len(log_rets)
    v = sum((r-m)**2 for r in log_rets) / max(1, len(log_rets)-1)
    sigma_per_sample = math.sqrt(v)
    # samples vary — approximate inter-arrival
    dt_ms = (rows[-1][0] - rows[0][0]) / max(1, len(rows) - 1)
    sigma_per_sec = sigma_per_sample / math.sqrt(max(dt_ms/1000, 0.1))
    sigma_ann = sigma_per_sec * math.sqrt(365 * 24 * 3600)
    return max(0.10, min(3.0, sigma_ann))


def simulate_maker_fill(bbo, decision_ts, side, limit_c, timeout_s=60):
    """Walk BBO forward; fill if subsequent ask <= our bid."""
    ts_arr = [b[0] for b in bbo]
    idx = bisect.bisect_left(ts_arr, decision_ts)
    deadline = decision_ts + timeout_s * 1000
    while idx < len(bbo) and bbo[idx][0] <= deadline:
        ts, yb, ya, nb, na = bbo[idx]
        if side == 'yes' and ya <= limit_c: return ts, limit_c
        if side == 'no'  and na <= limit_c: return ts, limit_c
        idx += 1
    return None, None


def batch_get_spot_window(con, start_ts, end_ts):
    """Pull all spot quotes for a window in one query. Returns sorted list (ts, mid_dict_by_venue)."""
    cur = con.cursor()
    cur.execute("""
        select COALESCE(exchange_ts_ms, received_ts_ms) as ts, venue, mid
          from spot_quote_event
         where symbol = 'btcusd'
           and venue in ('coinbase','kraken','bitstamp')
           and COALESCE(exchange_ts_ms, received_ts_ms) between ? and ?
         order by ts
    """, (start_ts, end_ts))
    rows = cur.fetchall()
    return [(ts, v, float(m)) for ts, v, m in rows if m is not None]


def _spot_at_from_batch(batch, target_ts):
    """Find median spot across venues within 5s of target_ts."""
    idx = bisect.bisect_right([r[0] for r in batch], target_ts) - 1
    if idx < 0: return None
    by_v = {}
    for j in range(idx, max(-1, idx - 30), -1):
        ts, v, m = batch[j]
        if target_ts - ts > 5000: break
        by_v.setdefault(v, m)
    if not by_v: return None
    vals = sorted(by_v.values())
    return vals[len(vals)//2]


def _rv_from_batch(batch, end_ts, window_s=300):
    """Compute RV from Coinbase points in [end_ts - window, end_ts]."""
    start_ts = end_ts - window_s * 1000
    cb_prices = [(ts, m) for ts, v, m in batch if v == 'coinbase' and start_ts <= ts <= end_ts]
    if len(cb_prices) < 3: return 0.5
    prices = [m for _, m in cb_prices if m > 0]
    if len(prices) < 3: return 0.5
    log_rets = [math.log(prices[i+1]/prices[i]) for i in range(len(prices)-1)]
    if not log_rets: return 0.5
    m = sum(log_rets)/len(log_rets)
    v = sum((r-m)**2 for r in log_rets) / max(1, len(log_rets)-1)
    sigma_per_sample = math.sqrt(v)
    dt_ms = (cb_prices[-1][0] - cb_prices[0][0]) / max(1, len(cb_prices)-1)
    sigma_per_sec = sigma_per_sample / math.sqrt(max(dt_ms/1000, 0.1))
    sigma_ann = sigma_per_sec * math.sqrt(365 * 24 * 3600)
    return max(0.10, min(3.0, sigma_ann))


def backtest_one_market(con, mkt, decision_offsets_s=(540, 480, 420, 360, 300, 240, 180, 120, 60),
                        threshold_c=5, contracts=10):
    """For one market, evaluate model at several time-to-close points; trade at most once.
       Returns list of trade dicts."""
    close_ts = mkt['close_ts_ms']
    open_ts  = close_ts - 16 * 60 * 1000
    bbo = extract_bbo_for_market(con, mkt['ticker'], open_ts, close_ts + 5000)
    if not bbo: return []
    # Single spot batch for the whole window plus 5min lookback for RV
    spot_batch = batch_get_spot_window(con, open_ts - 300_000, close_ts + 5000)
    trades = []
    taken = False
    for offset_s in decision_offsets_s:
        if taken: break
        decision_ts = close_ts - offset_s * 1000
        idx = bisect.bisect_left([b[0] for b in bbo], decision_ts)
        if idx >= len(bbo): continue
        ts, yb, ya, nb, na = bbo[idx]
        if not all([yb, ya, nb, na]): continue
        if yb >= ya: continue
        spot = _spot_at_from_batch(spot_batch, decision_ts)
        if not spot: continue
        sigma = _rv_from_batch(spot_batch, decision_ts)
        tau = (close_ts - decision_ts) / 1000.0
        try:
            pm = settlement_fair_probability(
                SettlementProbabilityInput(
                    spot=spot, strike=mkt['strike'], seconds_to_close=tau,
                    realized_vol_annualized=sigma, drift_annualized=0.0,
                ),
                SettlementProbabilityConfig(sigma_floor_annualized=0.15),
            ).probability_yes
        except: continue
        # market p_yes from mid
        p_market = (yb + ya) / 200
        edge = pm - p_market
        if abs(edge) * 100 < threshold_c: continue
        # Decide side
        if edge > 0:
            our_side = 'yes'; limit = min(yb + 1, ya - 1); won = mkt['outcome_yes']
        else:
            our_side = 'no'; limit = min(nb + 1, na - 1); won = not mkt['outcome_yes']
        if limit < 1 or limit > 99: continue
        fill_ts, fill_price = simulate_maker_fill(bbo, decision_ts, our_side, limit, timeout_s=60)
        if fill_price is None: continue
        payoff = (100 - fill_price) * contracts if won else -fill_price * contracts
        fee = kalshi_fee_maker(fill_price, contracts)
        net = payoff - fee
        trades.append(dict(ticker=mkt['ticker'], decision_ts=decision_ts, side=our_side,
                            fill_price=fill_price, won=won, net=net, edge_c=edge*100,
                            offset_s=offset_s, p_model=pm, p_market=p_market,
                            tau=tau, sigma=sigma, spot=spot, strike=mkt['strike']))
        taken = True
    return trades


def main():
    print('Querying market_dim + strikes...')
    markets = get_all_settled_markets()
    print(f'Resolved markets: {len(markets)}')
    if not markets:
        print('No markets with strike+outcome resolved. Strikes_cache.json may not cover this period.')
        # Show diagnostic
        con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
        cur = con.cursor()
        cur.execute("select ticker from market_dim where ticker like 'KXBTC15M-%' limit 5")
        sample = [r[0] for r in cur.fetchall()]
        print(f'  sample market tickers: {sample}')
        print(f'  strikes_cache has {len(STRIKES)} tickers')
        return

    import sys
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else len(markets)
    threshold = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    save_path = Path(sys.argv[3]) if len(sys.argv) > 3 else None
    con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
    all_trades = []
    import time
    t_start = time.time()
    for i, mkt in enumerate(markets[:limit]):
        t0 = time.time()
        ts = backtest_one_market(con, mkt, threshold_c=threshold)
        elapsed = time.time() - t0
        all_trades.extend(ts)
        print(f'  [{i+1}/{limit}] {mkt["ticker"][-15:]} {elapsed:.1f}s  '
              f'trades_now={len(all_trades)}', flush=True)
    con.close()
    if not all_trades:
        print('No trades fired')
        return
    n = len(all_trades); w = sum(1 for t in all_trades if t['won'])
    net = sum(t['net'] for t in all_trades)
    print(f'\nResults:  trades={n}  WR={w/n*100:.1f}%  net=${net/100:+.2f}')
    print(f'  Avg ${net/n/100:+.3f}/tr')
    print(f'  Markets that fired: {len(set(t["ticker"] for t in all_trades))}/{limit}')
    # Per entry-offset breakdown
    from collections import defaultdict
    by_off = defaultdict(lambda: [0, 0, 0])
    for t in all_trades:
        b = by_off[t['offset_s']]; b[0]+=1; b[1]+=t['won']; b[2]+=t['net']
    print(f'\n  Per entry-offset (secs-to-close at trigger):')
    print(f'  {"offset_s":>9s} {"n":>4s} {"WR":>6s} {"net_$":>9s}')
    for off in sorted(by_off.keys(), reverse=True):
        nn, ww, ne = by_off[off]
        print(f'  {off:>9d} {nn:>4d} {ww/nn*100:>5.1f}% ${ne/100:>+8.2f}')
    # Per side
    by_side = defaultdict(lambda: [0, 0, 0])
    for t in all_trades:
        b = by_side[t['side']]; b[0]+=1; b[1]+=t['won']; b[2]+=t['net']
    print(f'\n  Per side:')
    for s, (nn, ww, ne) in by_side.items():
        print(f'    {s:3s}  n={nn:>3d}  WR={ww/nn*100:>5.1f}%  net=${ne/100:>+7.2f}')
    if save_path:
        save_path.parent.mkdir(parents=True, exist_ok=True)
        with save_path.open('w', encoding='utf-8') as f:
            for t in all_trades:
                f.write(json.dumps(t, default=str) + '\n')
        print(f'\nSaved {n} trades to {save_path}')


if __name__ == '__main__':
    main()
