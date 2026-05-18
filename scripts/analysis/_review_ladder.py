"""Review KalshiLadderShadow performance since deployment.

Walks data/ladder_shadow.jsonl and produces:
  - Counts per event kind (track_open, would_add, rung_held, rung_failed, settle_with_ladder)
  - Per-ticker ladder trajectory (rungs, hold/fail outcomes)
  - Aggregate counterfactual: how much P&L would the ladder have added/subtracted
  - Condition breakdown — which of the four conditions are most/least often met
  - Hour-of-day distribution of would_add events
"""
import json
import datetime as dt
from collections import Counter, defaultdict
from pathlib import Path

LOG = Path(r"C:\Trading\kalshi-btc-engine-v2\data\ladder_shadow.jsonl")


def main() -> None:
    if not LOG.exists():
        print(f"no ladder shadow log at {LOG}")
        return

    by_kind: Counter = Counter()
    startups: list[dict] = []
    track_opens: list[dict] = []
    would_adds: list[dict] = []
    rung_helds: list[dict] = []
    rung_faileds: list[dict] = []
    settles: list[dict] = []

    for line in LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        k = r.get("kind")
        by_kind[k] += 1
        if k == "startup":
            startups.append(r)
        elif k == "track_open":
            track_opens.append(r)
        elif k == "would_add":
            would_adds.append(r)
        elif k == "rung_held":
            rung_helds.append(r)
        elif k == "rung_failed":
            rung_faileds.append(r)
        elif k == "settle_with_ladder":
            settles.append(r)

    print(f"=== Ladder shadow review — log: {LOG.name} ===")
    print(f"Total events: {sum(by_kind.values())}")
    print(f"Event kinds: {dict(by_kind)}")
    print()

    if startups:
        s0 = startups[0]
        ts = dt.datetime.fromtimestamp(s0["ts_ms"]/1000, tz=dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"First startup: {ts}")
        print(f"Config: thin_depth_cap={s0.get('thin_depth_cap')} "
              f"near_strike_frac={s0.get('near_strike_frac')} "
              f"stabilized_std_cents={s0.get('stabilized_std_cents')} "
              f"adverse_trigger_cents={s0.get('adverse_trigger_cents')} "
              f"max_rungs={s0.get('max_rungs_per_position')}")
        print(f"Startups in log: {len(startups)}")
        print()

    print(f"Positions tracked: {len(track_opens)}")
    print(f"Would-add events: {len(would_adds)}")
    print(f"Rung-held: {len(rung_helds)}")
    print(f"Rung-failed: {len(rung_faileds)}")
    print(f"Settled-with-ladder: {len(settles)}")
    print()

    # ── Trade-level per-ticker ladder summary ───────────────────────────
    per_ticker: defaultdict = defaultdict(lambda: {"opens": 0, "adds": [], "holds": 0, "fails": 0})
    for t in track_opens:
        per_ticker[t["ticker"]]["opens"] += 1
    for a in would_adds:
        per_ticker[a["ticker"]]["adds"].append(a)
    for h in rung_helds:
        per_ticker[h["ticker"]]["holds"] += 1
    for fl in rung_faileds:
        per_ticker[fl["ticker"]]["fails"] += 1

    if per_ticker:
        print(f"=== Per-ticker activity ({len(per_ticker)} tickers) ===")
        print(f"{'ticker':<28} {'opens':>5} {'adds':>4} {'held':>4} {'fail':>4}  add_details")
        print("-" * 100)
        for ticker, d in per_ticker.items():
            add_detail = ", ".join(
                f"r{a.get('rung_index')}@{a.get('rung_price_cents')}c"
                for a in d["adds"]
            )
            print(f"{ticker:<28} {d['opens']:>5} {len(d['adds']):>4} {d['holds']:>4} {d['fails']:>4}  {add_detail}")
        print()

    # ── Settlement counterfactual ───────────────────────────────────────
    if settles:
        print(f"=== Settlement counterfactual ({len(settles)} settled positions w/ ladder events) ===")
        print(f"{'ticker':<28} {'side':<4} {'out':<4} {'rungs':>5} {'actual':>7} {'ladder':>7} {'combined':>9} {'delta':>7}")
        print("-" * 100)
        total_actual = 0
        total_ladder = 0
        total_combined = 0
        delta_improvements = 0
        delta_harms = 0
        for s in settles:
            actual = s.get("actual_net_cents", 0)
            ladder = s.get("ladder_net_cents", 0)
            combined = s.get("combined_net_cents", 0)
            delta = s.get("delta_vs_actual_cents", 0)
            total_actual += actual
            total_ladder += ladder
            total_combined += combined
            if delta > 0:
                delta_improvements += 1
            elif delta < 0:
                delta_harms += 1
            print(f"{s['ticker']:<28} {s['side']:<4} {s['outcome']:<4} "
                  f"{s.get('ladder_rungs',0):>5} {actual:+7d} {ladder:+7d} {combined:+9d} {delta:+7d}")
        print("-" * 100)
        print(f"Totals:                                       {total_actual:+7d} {total_ladder:+7d} {total_combined:+9d}")
        print()
        print(f"Ladder improvement count: {delta_improvements}")
        print(f"Ladder harm count: {delta_harms}")
        print(f"Net ladder delta: {sum(s.get('delta_vs_actual_cents',0) for s in settles):+d}c "
              f"(${sum(s.get('delta_vs_actual_cents',0) for s in settles)/100:+.2f})")
        print()
    else:
        print("=== No settle_with_ladder events yet ===")
        print("Need positions with ladder activity AND lifecycle settlement to fire.")
        print()

    # ── Condition breakdown for would_add events ────────────────────────
    if would_adds:
        print(f"=== Condition values at would_add events ===")
        # All 4 conditions must fire for would_add; show distribution of values
        depths = [a.get("depth_top5") for a in would_adds if a.get("depth_top5") is not None]
        spreads = [(a.get("side_ask_cents",0) - a.get("side_bid_cents",0)) for a in would_adds if a.get("side_bid_cents")]
        std_window = [a.get("window_std_cents") for a in would_adds if a.get("window_std_cents") is not None]
        if depths:
            print(f"  top5_depth at trigger:        min={min(depths):.0f}  max={max(depths):.0f}  mean={sum(depths)/len(depths):.0f}  (cap={200})")
        if std_window:
            print(f"  10s contract-mid std (cents): min={min(std_window):.2f}  max={max(std_window):.2f}  mean={sum(std_window)/len(std_window):.2f}  (cap=2.0)")
        if spreads:
            print(f"  side spread at trigger:       min={min(spreads)}c  max={max(spreads)}c  mean={sum(spreads)/len(spreads):.1f}c")

        # Hour of day for would_add events
        hour_n = Counter()
        for a in would_adds:
            h = dt.datetime.fromtimestamp(a["ts_ms"]/1000, tz=dt.UTC).hour
            hour_n[h] += 1
        if hour_n:
            print(f"  would_add UTC hours: {dict(sorted(hour_n.items()))}")
        print()


if __name__ == "__main__":
    main()
