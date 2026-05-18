"""
Strike-distance analysis of late-certainty backtest losses (V1 + V5).

Reuses helpers from late_certainty_backtest.py.
"""
from __future__ import annotations

import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from late_certainty_backtest import (  # type: ignore
    load_markets, load_btc_1m, find_entry, fee, Trade, Market,
)


def btc_at_ts(btc: dict, ts: int) -> float | None:
    """Return BTC close at exact bar with open_ts=ts; if missing, search +/- a few minutes."""
    bar = btc.get(ts)
    if bar:
        return bar[3]
    for delta in (-60, -120, 60, 120, -180, 180):
        bar = btc.get(ts + delta)
        if bar:
            return bar[3]
    return None


def entry_ts(m: Market, entry_minute: int) -> int:
    return m.open_ts + (entry_minute + 1) * 60


def settle_ts(m: Market) -> int:
    # bar covering minute 14 has open_ts = m.open_ts + 14*60
    return m.open_ts + 14 * 60


def trade_strike_info(t: Trade, m: Market, btc: dict) -> dict | None:
    if m.floor_strike < 1000:
        return None
    e_ts = entry_ts(m, t.entry_minute)
    s_ts = settle_ts(m)
    btc_e = btc_at_ts(btc, e_ts - 60)  # bar covering entry minute
    btc_s = btc_at_ts(btc, s_ts)
    if btc_e is None or btc_s is None:
        return None
    dist_entry = abs(btc_e - m.floor_strike)
    dist_settle = abs(btc_s - m.floor_strike)
    # Was BTC on the favorable side of strike at entry?
    if t.side == "YES":
        fav_entry = btc_e >= m.floor_strike
        fav_settle = btc_s >= m.floor_strike
    else:
        fav_entry = btc_e < m.floor_strike
        fav_settle = btc_s < m.floor_strike
    return {
        "btc_entry": btc_e,
        "btc_settle": btc_s,
        "strike": m.floor_strike,
        "dist_entry": dist_entry,
        "dist_settle": dist_settle,
        "fav_entry": fav_entry,
        "fav_settle": fav_settle,
        "signed_dist_entry": (btc_e - m.floor_strike) if t.side == "YES" else (m.floor_strike - btc_e),
    }


BUCKETS = [
    ("$0-25",   0.0,   25.0),
    ("$25-50",  25.0,  50.0),
    ("$50-100", 50.0,  100.0),
    ("$100-200",100.0, 200.0),
    ("$200+",   200.0, float("inf")),
]


def bucket_for(dist: float) -> str:
    for name, lo, hi in BUCKETS:
        if lo <= dist < hi:
            return name
    return "$200+"


def run_variant(markets, btc, threshold: float, min_minute: int, dist_filter: float = 0.0):
    """Run a variant; return list of (trade, info_dict) for trades passing the dist filter."""
    out = []
    mkt_by_t = {m.ticker: m for m in markets}
    for m in markets:
        if not any(cd.minute_idx >= min_minute for cd in m.candles):
            continue
        t = find_entry(m, threshold, min_minute, None)
        if t is None:
            continue
        info = trade_strike_info(t, mkt_by_t[t.ticker], btc)
        if info is None:
            continue
        if info["dist_entry"] < dist_filter:
            continue
        out.append((t, info))
    return out


def analyze_buckets(trades_info):
    """Per-bucket: n_total, n_wins, n_losses, winrate, avg_entry_price (paid), net P&L."""
    buckets = {name: {"n": 0, "wins": 0, "losses": 0, "entries": [], "net": 0.0,
                      "fav_at_entry": 0, "fav_at_settle": 0}
               for name, _, _ in BUCKETS}
    for t, info in trades_info:
        b = bucket_for(info["dist_entry"])
        buckets[b]["n"] += 1
        if t.payout > 0:
            buckets[b]["wins"] += 1
        else:
            buckets[b]["losses"] += 1
        buckets[b]["entries"].append(t.entry_price)
        buckets[b]["net"] += t.net_pnl
        if info["fav_entry"]:
            buckets[b]["fav_at_entry"] += 1
        if info["fav_settle"]:
            buckets[b]["fav_at_settle"] += 1
    return buckets


def fmt_buckets(buckets):
    total_n = sum(b["n"] for b in buckets.values())
    total_losses = sum(b["losses"] for b in buckets.values())
    rows = []
    for name, _, _ in BUCKETS:
        b = buckets[name]
        if b["n"] == 0:
            rows.append((name, 0, 0, 0, 0.0, 0.0, 0.0, 0.0))
            continue
        winrate = b["wins"] / b["n"]
        loss_share = b["losses"] / total_losses if total_losses else 0.0
        avg_entry = statistics.mean(b["entries"])
        net_per = b["net"] / b["n"]
        rows.append((name, b["n"], b["wins"], b["losses"], winrate, loss_share, avg_entry, net_per))
    return rows, total_n, total_losses


def loss_kind(t, info):
    """Classify a LOSS using entry-side BTC + the authoritative market result.
    btc_1m bars are too sparse to verify Kalshi's settle reference, so we trust:
      - the entry-time BTC reading (good — bar coverage in-window is fine), and
      - the market's actual win/loss outcome (authoritative).
    A loss with btc favorable at entry → BTC must have crossed back ('reversed').
    A loss with btc unfavorable at entry → was already against ('always_against').
    """
    return "reversed" if info["fav_entry"] else "always_against"


def main():
    print("Loading markets + candles + BTC ...")
    markets = load_markets()
    btc = load_btc_1m()
    print(f"  {len(markets)} markets / {len(btc)} btc bars\n")

    for label, thresh, min_m in [("V1", 0.80, 8), ("V5", 0.80, 12)]:
        print("=" * 78)
        print(f"{label}: threshold={thresh:.2f} min_minute={min_m}")
        print("=" * 78)
        trades_info = run_variant(markets, btc, thresh, min_m, 0.0)
        losses = [(t, i) for t, i in trades_info if t.payout == 0]
        wins   = [(t, i) for t, i in trades_info if t.payout > 0]
        print(f"Total trades w/ strike data: {len(trades_info)}  (wins={len(wins)}  losses={len(losses)})\n")

        print("--- LOSSES bucketed by |BTC@entry - strike| ---")
        b_loss = analyze_buckets(losses)
        rows, n_total, n_loss = fmt_buckets(b_loss)
        print(f"{'Bucket':<10} {'N':>6} {'%loss':>8} {'avgEntry':>10}")
        for name, n, w, l, wr, share, avg_e, net in rows:
            if n == 0:
                print(f"{name:<10} {0:>6} {'-':>8} {'-':>10}")
            else:
                print(f"{name:<10} {n:>6} {share*100:>7.1f}% {avg_e:>10.4f}")
        print(f"Total losses: {n_loss}\n")

        print("--- WINS bucketed by |BTC@entry - strike| ---")
        b_win = analyze_buckets(wins)
        rows_w, n_total_w, _ = fmt_buckets(b_win)
        print(f"{'Bucket':<10} {'N':>6}")
        for name, n, *_ in rows_w:
            print(f"{name:<10} {n:>6}")
        print(f"Total wins: {len(wins)}\n")

        print("--- WIN RATE by distance bucket (combined) ---")
        b_all = analyze_buckets(trades_info)
        rows_a, _, _ = fmt_buckets(b_all)
        print(f"{'Bucket':<10} {'N':>6} {'Wins':>6} {'Loss':>6} {'WinRate':>8} {'NetPnL/c':>10}")
        for name, n, w, l, wr, share, avg_e, net in rows_a:
            if n == 0:
                print(f"{name:<10} {0:>6} {'-':>6} {'-':>6} {'-':>8} {'-':>10}")
            else:
                print(f"{name:<10} {n:>6} {w:>6} {l:>6} {wr*100:>7.2f}% {net:>+10.4f}")
        print()

        print("--- LOSS dynamics: 'reversed' (favorable@entry) vs 'always against' (unfavorable@entry) ---")
        kinds = {"reversed": [], "always_against": []}
        for t, info in losses:
            kinds[loss_kind(t, info)].append((t, info))
        for k, v in kinds.items():
            pct = len(v) / len(losses) * 100 if losses else 0
            if v:
                med_dist = statistics.median(i["dist_entry"] for _, i in v)
                print(f"  {k:<16} {len(v):>4} ({pct:>5.1f}%)   median |dist@entry|=${med_dist:.2f}")
            else:
                print(f"  {k:<16} {0:>4}")

        # Per-distance-bucket: how many losses are 'reversed' vs 'always_against'?
        print(f"\n  By bucket — losses split by entry-side classification:")
        print(f"  {'Bucket':<10} {'Total':>6} {'Reversed':>10} {'AlwaysAgainst':>15}")
        for name, lo, hi in BUCKETS:
            rev = sum(1 for t, i in kinds["reversed"]      if lo <= i["dist_entry"] < hi)
            aa  = sum(1 for t, i in kinds["always_against"] if lo <= i["dist_entry"] < hi)
            tot = rev + aa
            if tot == 0:
                print(f"  {name:<10} {0:>6} {'-':>10} {'-':>15}")
                continue
            print(f"  {name:<10} {tot:>6} {rev:>4} ({rev/tot*100:>4.0f}%) {aa:>9} ({aa/tot*100:>4.0f}%)")
        print()

    # ---- V5 with distance filters ----
    print("=" * 78)
    print("V5 with strike-distance filters (only buy if |BTC@entry - strike| >= X)")
    print("=" * 78)
    print(f"{'Filter':<10} {'Trades':>7} {'Wins':>6} {'Loss':>6} {'WinRate':>8} {'AvgEntry':>9} {'NetPnL/c':>10} {'Total$':>10}")
    baseline = run_variant(markets, btc, 0.80, 12, 0.0)
    base_n = len(baseline)
    for filt in [0, 25, 50, 75, 100]:
        ti = run_variant(markets, btc, 0.80, 12, float(filt))
        n = len(ti)
        wins = sum(1 for t, _ in ti if t.payout > 0)
        loss = n - wins
        wr = wins / n if n else 0
        avg_e = statistics.mean(t.entry_price for t, _ in ti) if ti else 0
        net = sum(t.net_pnl for t, _ in ti)
        net_per = net / n if n else 0
        retained = n / base_n * 100 if base_n else 0
        wins_retained = wins / sum(1 for t, _ in baseline if t.payout > 0) * 100 if base_n else 0
        losses_retained = loss / sum(1 for t, _ in baseline if t.payout == 0) * 100 if base_n else 0
        print(f">=${filt:<8} {n:>7} {wins:>6} {loss:>6} {wr*100:>7.2f}% {avg_e:>9.4f} {net_per:>+10.4f} ${net:>+9.2f}    "
              f"(kept {retained:.1f}% trades / {wins_retained:.1f}% wins / {losses_retained:.1f}% losses)")
    print()


if __name__ == "__main__":
    main()
