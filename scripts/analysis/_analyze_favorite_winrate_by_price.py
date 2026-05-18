"""Favorite-side win rate by price bucket at minute 12+.

For each settled KXBTC15M market, look at the end-of-minute-12 quote
(the moment V5 enters). Determine:
  - favorite side (the higher ask = the side the market thinks will win)
  - favorite's ask price
  - whether the favorite ACTUALLY won at settlement

Bucket favorites by ask price and compute:
  - n
  - win rate
  - fee-aware net EV per contract if bought at the bucket midpoint

The point: if you place a resting limit BID on YES at, say, 65c at min 12,
when (if) that fills it means yes_ask dropped to 65c. The question is:
how often does the favorite win when priced at 65c at min 12?

This is what tells us whether cancel-pair sub-80c limits are +EV or -EV.
"""
import json
import math
import sqlite3
import statistics
from pathlib import Path

DB = Path("C:/Trading/btc-bias-engine/data/kalshi_external_backtest.db")

# Bucket cutoffs (cents) for the favorite-side ask at end of min 12.
BUCKETS = [(50,55),(55,60),(60,65),(65,70),(70,75),(75,80),(80,85),(85,90),(90,95),(95,100)]


def fnum(x):
    return float(x) if x is not None else 0.0


def fee_cents(price_cents: int, count: int = 1) -> float:
    """Kalshi taker fee: ceil(0.07 * n * P * (1-P) * 100) / 100. Cents/contract."""
    p = price_cents / 100.0
    return math.ceil(0.07 * count * p * (1.0 - p) * 100.0)


def main() -> None:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    markets = {}
    for r in conn.execute("SELECT ticker, open_ts, result FROM markets"):
        markets[r["ticker"]] = {"open_ts": r["open_ts"], "result": r["result"], "by_minute": {}}

    for r in conn.execute("SELECT ticker, end_period_ts, raw_json FROM candles"):
        m = markets.get(r["ticker"])
        if m is None:
            continue
        mi = (r["end_period_ts"] - m["open_ts"] - 60) // 60
        if mi < 0 or mi > 14:
            continue
        d = json.loads(r["raw_json"])
        ya = d.get("yes_ask", {}) or {}
        yb = d.get("yes_bid", {}) or {}
        m["by_minute"][int(mi)] = {
            "ya_close": fnum(ya.get("close_dollars")),
            "yb_close": fnum(yb.get("close_dollars")),
        }

    # For each market: pull end-of-min-12 quote, determine favorite + winrate.
    # Skip markets that have no min-12 candle (e.g., short or malformed).
    per_minute_records = {12: [], 13: [], 14: []}

    for tkr, m in markets.items():
        if m["result"] not in ("yes", "no"):
            continue
        for mi in (12, 13, 14):
            quote = m["by_minute"].get(mi)
            if not quote:
                continue
            ya = quote["ya_close"]
            yb = quote["yb_close"]
            if ya <= 0 or yb <= 0:
                continue
            yes_ask = ya
            no_ask = 1.0 - yb
            # Favorite = side with higher ask.
            if yes_ask >= no_ask:
                fav_side = "yes"; fav_ask = yes_ask
            else:
                fav_side = "no"; fav_ask = no_ask
            won = (fav_side == m["result"])
            per_minute_records[mi].append((fav_ask, won))

    for mi in (12, 13, 14):
        records = per_minute_records[mi]
        if not records:
            continue
        print(f"\n=== End-of-minute-{mi} favorite snapshot (n={len(records)}) ===")
        print(f"{'price bucket':>16}  {'n':>5}  {'winrate':>8}  "
              f"{'mid':>4}  {'net_per_c':>10}  {'+/-':>4}")
        print(f"{'-'*16}  {'-'*5}  {'-'*8}  {'-'*4}  {'-'*10}  {'-'*4}")
        total_n = 0
        for lo, hi in BUCKETS:
            in_bucket = [(p, w) for p, w in records if lo <= p*100 < hi]
            n = len(in_bucket)
            total_n += n
            if n == 0:
                print(f"  [{lo:>3},{hi:>3})    {n:>5}  {'-':>8}  {'-':>4}  {'-':>10}  {'-':>4}")
                continue
            wins = sum(1 for _, w in in_bucket if w)
            wr = wins / n
            mid_c = (lo + hi) // 2
            # Net EV per contract if bought at bucket midpoint, hold to settle.
            fee = fee_cents(mid_c, 1)
            gross = wr * (100 - mid_c) + (1 - wr) * (-mid_c)
            net = gross - fee
            sign = "+EV" if net > 0 else "-EV"
            print(f"  [{lo:>3},{hi:>3})    {n:>5}  {wr*100:>6.1f}%   {mid_c:>4}  "
                  f"{net:>+8.2f}c  {sign:>4}")
        print(f"  (total)         {total_n:>5}")

    # Quick "what fraction of windows have the favorite under 80c at min 12?"
    records12 = per_minute_records[12]
    if records12:
        sub80 = sum(1 for p, _ in records12 if p*100 < 80)
        print(f"\nAt end of min 12: {sub80}/{len(records12)} ({sub80/len(records12):.1%}) "
              f"of windows had the favorite priced below 80c.")
        sub70 = sum(1 for p, _ in records12 if p*100 < 70)
        print(f"At end of min 12: {sub70}/{len(records12)} ({sub70/len(records12):.1%}) "
              f"of windows had the favorite priced below 70c.")
        sub60 = sum(1 for p, _ in records12 if p*100 < 60)
        print(f"At end of min 12: {sub60}/{len(records12)} ({sub60/len(records12):.1%}) "
              f"of windows had the favorite priced below 60c.")


if __name__ == "__main__":
    main()
