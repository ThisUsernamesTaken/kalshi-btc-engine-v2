"""Measure realistic maker fill rates from the SQLite BBO data.

For each market in the burnin_holdpure SQLite, sample N decision points
across the cycle. At each point: place a hypothetical maker buy at
best_yes_bid + 1c (and best_no_bid + 1c). Walk forward up to T seconds.
Count fills (limit price <= subsequent best_ask).

Aggregate by:
  - side (yes vs no)
  - tau bucket (300-600s, 60-300s, etc.)
  - "depth" of our limit (1c above bid = aggressive; 5c above bid = mid)
"""
from __future__ import annotations
import json, sqlite3, bisect
from collections import defaultdict
from pathlib import Path

DB = r'D:/Trading/kalshi-btc-engine-v2/data/burnin_holdpure_2026_05_12.sqlite'


def load_bbo(con, ticker, start_ts, end_ts, downsample_ms=1000):
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
        if ts - last_emit < downsample_ms: continue
        try:
            yb = int(round(float(yb_s)*100))
            ya = int(round(float(ya_s)*100))
        except: continue
        if yb >= ya: continue
        bbo.append((ts, yb, ya))
        last_emit = ts
    return bbo


def simulate_fill(bbo, place_ts, side, limit_c, timeout_s=60):
    """Returns True if filled within timeout, False otherwise."""
    ts_arr = [b[0] for b in bbo]
    idx = bisect.bisect_left(ts_arr, place_ts)
    deadline = place_ts + timeout_s * 1000
    while idx < len(bbo) and bbo[idx][0] <= deadline:
        ts, yb, ya = bbo[idx]
        if side == 'yes':
            # filled if yes_ask <= our_bid
            if ya <= limit_c: return True
        else:
            # buying NO = pay 100-yb for NO at the cross. Fill if no_ask <= our_bid_on_no
            # no_ask = 100 - yb (cheapest YES bid is the matching NO sell)
            no_ask = 100 - yb
            if no_ask <= limit_c: return True
        idx += 1
    return False


def main():
    con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
    cur = con.cursor()
    # Get markets with substantial L2 activity
    cur.execute("""
        select ticker, close_time
          from market_dim
         where ticker like 'KXBTC15M-%'
         order by close_time
         limit 30
    """)
    markets = cur.fetchall()
    print(f'Testing fill rates on {len(markets)} markets')

    # Stats by (side, tau_bucket, depth)
    stats = defaultdict(lambda: [0, 0])  # [attempts, fills]
    for ticker, close_str in markets:
        import datetime
        try:
            close_dt = datetime.datetime.fromisoformat(close_str.replace('Z', '+00:00'))
            close_ts = int(close_dt.timestamp() * 1000)
        except: continue
        bbo = load_bbo(con, ticker, close_ts - 15*60*1000, close_ts + 5000)
        if len(bbo) < 30: continue
        # Sample at offsets
        for tau_s in [600, 480, 360, 240, 180, 120, 60, 45]:
            place_ts = close_ts - tau_s * 1000
            idx = bisect.bisect_left([b[0] for b in bbo], place_ts)
            if idx >= len(bbo): continue
            _, yb, ya = bbo[idx]
            spread = ya - yb
            # Try various depth offsets for our maker limit
            for depth_offset in [1, 2, 3, 5]:
                # Place YES bid at yb+depth_offset (must be < ya)
                lim_y = yb + depth_offset
                if lim_y >= ya: continue
                fill = simulate_fill(bbo, place_ts, 'yes', lim_y, timeout_s=60)
                # tau bucket
                if tau_s <= 90: tb = '30-90s'
                elif tau_s <= 240: tb = '90-240s'
                elif tau_s <= 480: tb = '240-480s'
                else: tb = '480-600s'
                key = (f'yes_bid+{depth_offset}', tb)
                stats[key][0] += 1
                if fill: stats[key][1] += 1
                # NO bid at (100-ya) + depth_offset (must be < (100-yb))
                lim_n = (100 - ya) + depth_offset
                if lim_n >= (100 - yb): continue
                fill_n = simulate_fill(bbo, place_ts, 'no', lim_n, timeout_s=60)
                key = (f'no_bid+{depth_offset}', tb)
                stats[key][0] += 1
                if fill_n: stats[key][1] += 1
    con.close()

    print('\n======== MAKER FILL RATES (60s timeout) ========')
    print(f'{"strategy":18s} {"tau_bucket":12s} {"attempts":>10s} {"fills":>7s} {"fill_rate":>10s}')
    for k in sorted(stats.keys()):
        att, fl = stats[k]
        if att < 5: continue
        fr = fl / att * 100
        print(f'  {k[0]:18s} {k[1]:12s} {att:>10d} {fl:>7d} {fr:>9.1f}%')

    # Aggregate across all tau buckets per depth
    print('\n======== AGGREGATE (all tau buckets combined) ========')
    agg = defaultdict(lambda: [0, 0])
    for k, v in stats.items():
        agg[k[0]][0] += v[0]; agg[k[0]][1] += v[1]
    for k in sorted(agg.keys()):
        att, fl = agg[k]
        if att < 5: continue
        print(f'  {k:18s} attempts={att:>4d} fills={fl:>4d} fill_rate={fl/att*100:>5.1f}%')


if __name__ == '__main__':
    main()
