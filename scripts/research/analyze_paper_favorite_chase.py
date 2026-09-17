"""Per-series rollup of paper_favorite_chase.jsonl decisions.

Reads the live paper-forward log and reports:
  - entries/exits/settles per series
  - net P&L per series
  - skip rate (skip_side_filter / skip_max_price / skip_max_slip / trigger_no_book)
  - average slippage per series

Use to sanity-check the running strategy and to compare per-asset bias.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

DEFAULT_LOG = Path(r"C:\Trading\kalshi-btc-engine-v2\data\paper_favorite_chase.jsonl")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--log", type=Path, default=DEFAULT_LOG)
    p.add_argument("--since-ms", type=int, default=0, help="Only events with log_ts_ms >= this")
    args = p.parse_args()

    by_series_n: collections.Counter[str] = collections.Counter()
    by_series_wins: collections.Counter[str] = collections.Counter()
    by_series_losses: collections.Counter[str] = collections.Counter()
    by_series_net: dict[str, int] = collections.defaultdict(int)
    by_series_slip: dict[str, list[float]] = collections.defaultdict(list)
    by_series_entry_price: dict[str, list[float]] = collections.defaultdict(list)
    by_series_skips: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    open_positions: dict[str, dict] = {}  # ticker -> entry info
    by_series_side: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)

    total_events = 0
    with args.log.open(encoding="utf-8") as f:
        for line in f:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("log_ts_ms", 0) < args.since_ms:
                continue
            total_events += 1
            kind = ev.get("kind")
            ticker = ev.get("ticker", "")
            series = ev.get("series") or (ticker.split("-", 1)[0] if "-" in ticker else ticker)

            if kind == "entry":
                open_positions[ticker] = ev
                by_series_slip[series].append(float(ev.get("slip", 0.0)))
                by_series_entry_price[series].append(float(ev.get("fill_price", 0.0)))
                by_series_side[series][ev.get("side", "?")] += 1
            elif kind == "exit":
                by_series_n[series] += 1
                net = int(ev.get("net_cents", 0))
                by_series_net[series] += net
                if net > 0:
                    by_series_wins[series] += 1
                elif net < 0:
                    by_series_losses[series] += 1
                open_positions.pop(ticker, None)
            elif kind in ("skip_side_filter", "skip_max_price", "skip_max_slip", "trigger_no_book"):
                by_series_skips[series][kind] += 1

    print(f"events scanned: {total_events}")
    print(f"open positions still in-flight: {len(open_positions)}")
    print()
    series_order = sorted(set(list(by_series_n) + list(by_series_skips) + list(by_series_side)))
    print(f"{'series':<10} {'closed':>6} {'wins':>5} {'WR':>6} {'net_c':>8} {'avg_c':>7}  {'YES':>4} {'NO':>4}  {'avg_p':>5} {'avg_slip':>8}  {'skips':>8}")
    print("-" * 100)
    for s in series_order:
        n = by_series_n[s]
        w = by_series_wins[s]
        net = by_series_net[s]
        avg = (net / n) if n else 0.0
        wr = (w / n) if n else 0.0
        yes_n = by_series_side[s].get("yes", 0)
        no_n = by_series_side[s].get("no", 0)
        avg_p = sum(by_series_entry_price[s]) / len(by_series_entry_price[s]) if by_series_entry_price[s] else 0.0
        avg_slip = sum(by_series_slip[s]) / len(by_series_slip[s]) if by_series_slip[s] else 0.0
        skips_total = sum(by_series_skips[s].values())
        print(
            f"{s:<10} {n:>6} {w:>5} {wr:>5.1%} {net:>+7d}c {avg:>+6.2f}c  "
            f"{yes_n:>4} {no_n:>4}  {avg_p:>5.2f} {avg_slip:>+7.3f}  {skips_total:>8}"
        )
    print()
    print("skip breakdown (per series):")
    for s in series_order:
        if not by_series_skips[s]:
            continue
        breakdown = ", ".join(f"{k}={v}" for k, v in sorted(by_series_skips[s].items()))
        print(f"  {s}: {breakdown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
