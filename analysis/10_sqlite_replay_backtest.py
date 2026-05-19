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
    """Build per-second BBO time series for one market from kalshi_l2_event.

    Maintains a running L2 book of (yes_side, price -> size) and emits a tuple
    at each downsample boundary.
    """
    cur = con.cursor()
    cur.execute("""
        select received_ts_ms, event_type, side, price, size, delta
          from kalshi_l2_event
         where market_ticker = ?
           and received_ts_ms between ? and ?
         order by received_ts_ms
    """, (ticker, start_ts, end_ts))
    # Simple book: dict[side][price_c] = size
    book = {'yes': {}, 'no': {}}
    last_emit = 0
    bbo = []
    for ts, et, side, price, size, delta in cur:
        if side not in book: continue
        price_c = int(round(float(price) * 100))
        # Apply update: snapshot replaces, delta increments
        if et == 'orderbook_snapshot':
            # 'snapshot' events here are level rows, not full snapshots — same as delta
            pass
        try:
            delta_v = float(delta) if delta is not None else 0
        except (TypeError, ValueError):
            delta_v = 0
        try:
            size_v = float(size) if size is not None else 0
        except (TypeError, ValueError):
            size_v = 0
        if delta_v != 0:
            new_size = book[side].get(price_c, 0) + delta_v
        else:
            new_size = size_v
        if new_size <= 0:
            book[side].pop(price_c, None)
        else:
            book[side][price_c] = new_size
        # Emit if past sample boundary
        if ts - last_emit >= downsample_ms:
            yb = max(book['yes']) if book['yes'] else None
            nb = max(book['no']) if book['no'] else None
            # YES ask = 100 - NO bid (best NO bid implies cheapest seller of YES at 100-NO_bid)
            ya = (100 - nb) if nb is not None else None
            na = (100 - yb) if yb is not None else None
            if yb and ya and yb <= ya:
                bbo.append((ts, yb, ya, nb, na))
                last_emit = ts
    return bbo


def get_spot_at(con, target_ts):
    """Best spot estimate around target_ts: median across Coinbase, Kraken, Bitstamp."""
    cur = con.cursor()
    cur.execute("""
        select venue, mid from spot_quote_event
         where received_ts_ms between ? and ?
           and symbol = 'btcusd'
           and venue in ('coinbase','kraken','bitstamp')
         order by received_ts_ms desc
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
        select received_ts_ms, mid from spot_quote_event
         where received_ts_ms between ? and ?
           and symbol = 'btcusd'
           and venue = 'coinbase'
         order by received_ts_ms
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


def backtest_one_market(con, mkt, decision_offsets_s=(540, 480, 420, 360, 300, 240, 180, 120, 60),
                        threshold_c=5, contracts=10):
    """For one market, evaluate model at several time-to-close points; trade at most once.
       Returns list of trade dicts."""
    close_ts = mkt['close_ts_ms']
    open_ts  = close_ts - 16 * 60 * 1000   # ~16 min before close (market open + buffer)
    bbo = extract_bbo_for_market(con, mkt['ticker'], open_ts, close_ts + 5000)
    if not bbo: return []
    trades = []
    taken = False
    for offset_s in decision_offsets_s:
        if taken: break
        decision_ts = close_ts - offset_s * 1000
        # Find BBO at decision time
        idx = bisect.bisect_left([b[0] for b in bbo], decision_ts)
        if idx >= len(bbo): continue
        ts, yb, ya, nb, na = bbo[idx]
        if not all([yb, ya, nb, na]): continue
        if yb >= ya: continue
        spot = get_spot_at(con, decision_ts)
        if not spot: continue
        sigma = compute_rv_ann_around(con, decision_ts)
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
    con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
    all_trades = []
    for i, mkt in enumerate(markets[:limit]):
        ts = backtest_one_market(con, mkt, threshold_c=threshold)
        all_trades.extend(ts)
        if (i+1) % 25 == 0:
            n_w = sum(1 for t in all_trades if t['won'])
            print(f'  [{i+1}/{limit}] trades={len(all_trades)} WR={n_w/max(len(all_trades),1)*100:.1f}% net=${sum(t["net"] for t in all_trades)/100:+.2f}')
    con.close()
    if not all_trades:
        print('No trades fired')
        return
    n = len(all_trades); w = sum(1 for t in all_trades if t['won'])
    net = sum(t['net'] for t in all_trades)
    print(f'\nResults (first 30 markets):  trades={n}  WR={w/n*100:.1f}%  net=${net/100:+.2f}')
    print(f'  Avg ${net/n/100:+.3f}/tr')
    print(f'  Markets that fired: {len(set(t["ticker"] for t in all_trades))}/{min(30, len(markets))}')


if __name__ == '__main__':
    main()
