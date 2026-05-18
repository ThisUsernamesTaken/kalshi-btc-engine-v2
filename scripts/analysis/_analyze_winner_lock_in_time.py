"""When does the winning side first hit 80c? Backtest-data analysis.

For each settled KXBTC15M market in kalshi_external_backtest.db:
  - Compute the EARLIEST minute (0..14) where the side that ultimately
    won reached >= 80c on a trade (price.high for YES, 1 - price.low for NO).
  - Report the distribution.

This tells us how often by minute 12 (V5 entry) the winner has already locked
in vs. how often it's still moving.
"""
import json
import sqlite3
import statistics
from collections import Counter
from pathlib import Path

DB = Path("C:/Trading/btc-bias-engine/data/kalshi_external_backtest.db")
THRESHOLD = 0.80
V5_ENTRY_MIN = 12


def fnum(x):
    return float(x) if x is not None else 0.0


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    mkts = {}
    for row in conn.execute("SELECT ticker, open_ts, result FROM markets"):
        mkts[row["ticker"]] = {"open_ts": row["open_ts"], "result": row["result"], "candles": []}

    for row in conn.execute(
        "SELECT ticker, end_period_ts, raw_json FROM candles ORDER BY ticker, end_period_ts"
    ):
        m = mkts.get(row["ticker"])
        if m is None:
            continue
        mi = (row["end_period_ts"] - m["open_ts"] - 60) // 60
        if mi < 0 or mi > 14:
            continue
        d = json.loads(row["raw_json"])
        pr = d.get("price", {}) or {}
        m["candles"].append((int(mi), fnum(pr.get("high_dollars")), fnum(pr.get("low_dollars"))))

    first_hit_minutes_winners = []   # minute index where winning side first hits 80c
    never_hit_winners = 0
    first_hit_minutes_losers = []    # minute the losing side first hits 80c (and obviously reverses)
    by_v5_status = Counter()         # already-locked / locked-at-12 / locked-late / never

    for ticker, m in mkts.items():
        if m["result"] not in ("yes", "no"):
            continue
        result = m["result"]
        first_minute_winner = None
        first_minute_loser = None
        ever_locked_by_loser = False
        for mi, p_high, p_low in sorted(m["candles"]):
            # YES traded at p_high (last-trade high during the minute).
            # NO traded at 1 - p_low.
            yes_hit = p_high >= THRESHOLD
            no_hit = p_low > 0 and (1.0 - p_low) >= THRESHOLD
            winner_hit = yes_hit if result == "yes" else no_hit
            loser_hit  = no_hit  if result == "yes" else yes_hit
            if winner_hit and first_minute_winner is None:
                first_minute_winner = mi
            if loser_hit and first_minute_loser is None:
                first_minute_loser = mi
            if loser_hit:
                ever_locked_by_loser = True
        if first_minute_winner is None:
            never_hit_winners += 1
            by_v5_status["never_hit_80c"] += 1
            continue
        first_hit_minutes_winners.append(first_minute_winner)
        if first_minute_loser is not None:
            first_hit_minutes_losers.append(first_minute_loser)

        if first_minute_winner < V5_ENTRY_MIN:
            by_v5_status[f"locked_before_min{V5_ENTRY_MIN}"] += 1
        elif first_minute_winner == V5_ENTRY_MIN:
            by_v5_status[f"locked_at_min{V5_ENTRY_MIN}"] += 1
        else:
            by_v5_status[f"locked_after_min{V5_ENTRY_MIN}"] += 1

    total = sum(1 for m in mkts.values() if m["result"] in ("yes", "no"))
    print(f"Total settled markets: {total}")
    print(f"Never reached 80c on winning side: {never_hit_winners} ({never_hit_winners/total:.1%})")
    print()
    print(f"Winner first-hit-80c minute distribution (n={len(first_hit_minutes_winners)}):")
    print(f"  mean   = {statistics.mean(first_hit_minutes_winners):.2f}")
    print(f"  median = {statistics.median(first_hit_minutes_winners)}")
    print(f"  p25    = {statistics.quantiles(first_hit_minutes_winners, n=4)[0]:.1f}")
    print(f"  p75    = {statistics.quantiles(first_hit_minutes_winners, n=4)[2]:.1f}")
    print()
    print("Histogram (minute → markets where winner first hit 80c at that minute):")
    counter = Counter(first_hit_minutes_winners)
    for mi in range(0, 15):
        n = counter.get(mi, 0)
        pct = n / len(first_hit_minutes_winners) * 100 if first_hit_minutes_winners else 0
        bar = "█" * int(pct)
        marker = " <-- V5 entry" if mi == V5_ENTRY_MIN else ""
        print(f"  min {mi:>2}: {n:>4} ({pct:>5.1f}%) {bar}{marker}")
    print()
    print(f"V5 entry-time framing (winner-side):")
    for k, v in sorted(by_v5_status.items()):
        print(f"  {k}: {v} ({v/total:.1%})")
    print()
    print(f"Cumulative % of winners locked-in BY minute X:")
    cum = 0
    for mi in range(0, 15):
        cum += counter.get(mi, 0)
        cumpct = cum / len(first_hit_minutes_winners) * 100 if first_hit_minutes_winners else 0
        marker = " <-- V5 entry minute"
        marker = marker if mi == V5_ENTRY_MIN else ""
        print(f"  by end-of-min {mi:>2}: {cum:>4} ({cumpct:>5.1f}%){marker}")
    print()
    print(f"How many of these losers ALSO touched 80c earlier (head-fakes)?")
    loser_before_winner = 0
    for ticker, m in mkts.items():
        if m["result"] not in ("yes", "no"):
            continue
        result = m["result"]
        first_w = None; first_l = None
        for mi, p_high, p_low in sorted(m["candles"]):
            yes_hit = p_high >= THRESHOLD
            no_hit = p_low > 0 and (1.0 - p_low) >= THRESHOLD
            if (yes_hit if result=="yes" else no_hit) and first_w is None: first_w = mi
            if (no_hit  if result=="yes" else yes_hit) and first_l is None: first_l = mi
        if first_w is not None and first_l is not None and first_l < first_w:
            loser_before_winner += 1
    print(f"  Markets where the LOSING side hit 80c before the winning side: "
          f"{loser_before_winner} ({loser_before_winner/total:.1%})")


if __name__ == "__main__":
    main()
