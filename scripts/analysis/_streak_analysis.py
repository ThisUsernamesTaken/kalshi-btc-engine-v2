"""Loss-streak + time-of-day analysis across all Pine Script settled trades.

Combines data/paper_ta_2026_05_12.jsonl (paper) and data/live_ta_trades.jsonl
(live) into a single chronological stream of settled trades, then reports:
  - Loss-streak distribution (1, 2, 3, ... consecutive losses)
  - Hit rate by UTC hour
  - Trades per UTC hour
  - Net P&L by UTC hour
  - Where the worst streaks occurred

Read-only against the JSONL files.
"""
import json
import datetime as dt
from collections import Counter, defaultdict
from pathlib import Path

PAPER_LOG = Path(r"C:\Trading\kalshi-btc-engine-v2\data\paper_ta_2026_05_12.jsonl")
LIVE_LOG  = Path(r"C:\Trading\kalshi-btc-engine-v2\data\live_ta_trades.jsonl")


def load_settles(path: Path, source: str) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("kind") != "settle":
            continue
        if r.get("dry_run"):
            continue
        net = r.get("net_cents")
        if net is None:
            continue
        # Find a usable timestamp — prefer cycle_close_ms (when the trade resolved)
        ts = r.get("cycle_close_ms") or r.get("decided_at_ts_ms") or r.get("ts_ms")
        if ts is None:
            continue
        rows.append({
            "source": source,
            "ts_ms": int(ts),
            "ticker": r.get("ticker"),
            "side": r.get("side"),
            "outcome": r.get("outcome"),
            "net_cents": int(net),
            "contracts": r.get("contracts"),
            "entry_price_cents": r.get("entry_price_cents"),
            "tier_name": r.get("tier_name"),
        })
    return rows


def main() -> None:
    paper = load_settles(PAPER_LOG, "paper")
    live  = load_settles(LIVE_LOG, "live")
    trades = sorted(paper + live, key=lambda t: t["ts_ms"])

    if not trades:
        print("no settled trades found")
        return

    n = len(trades)
    wins = sum(1 for t in trades if t["net_cents"] > 0)
    losses = n - wins

    print(f"=== Pine Script strategy: settled-trade analysis ===")
    print(f"Sources: paper={len(paper)}  live={len(live)}  total={n}")
    print(f"Time span: {dt.datetime.fromtimestamp(trades[0]['ts_ms']/1000, tz=dt.UTC).strftime('%Y-%m-%d %H:%M UTC')} "
          f"to {dt.datetime.fromtimestamp(trades[-1]['ts_ms']/1000, tz=dt.UTC).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"W/L: {wins}/{losses}  hit-rate={100*wins/n:.1f}%")
    print(f"Net (sum across paper+live): {sum(t['net_cents'] for t in trades):+d}c (${sum(t['net_cents'] for t in trades)/100:+.2f})")
    print()

    # ── Streak analysis ──────────────────────────────────────────────────
    streak_lengths = []          # all loss-streak lengths observed
    win_streak_lengths = []
    streak_details = []          # (start_idx, end_idx, length, ticker_first, ticker_last)
    current_len = 0
    current_start = None
    current_kind = None  # 'L' or 'W'

    for i, t in enumerate(trades):
        kind = "W" if t["net_cents"] > 0 else "L"
        if current_kind is None:
            current_kind = kind
            current_len = 1
            current_start = i
        elif kind == current_kind:
            current_len += 1
        else:
            # streak ended
            if current_kind == "L":
                streak_lengths.append(current_len)
                if current_len >= 3:
                    streak_details.append((
                        current_start, current_start + current_len - 1,
                        current_len, trades[current_start]['ts_ms'],
                        trades[current_start + current_len - 1]['ts_ms'],
                    ))
            else:
                win_streak_lengths.append(current_len)
            current_kind = kind
            current_len = 1
            current_start = i
    # flush
    if current_kind == "L":
        streak_lengths.append(current_len)
        if current_len >= 3:
            streak_details.append((
                current_start, current_start + current_len - 1,
                current_len, trades[current_start]['ts_ms'],
                trades[current_start + current_len - 1]['ts_ms'],
            ))
    elif current_kind == "W":
        win_streak_lengths.append(current_len)

    streak_hist = Counter(streak_lengths)
    print(f"=== Loss-streak distribution ===")
    print(f"{'length':<10}{'count':<10}{'P (observed)':<14}{'P (if i.i.d. at {loss_rate:.2f})'.format(loss_rate=losses/n)}")
    p_loss = losses / n
    for L in sorted(streak_hist):
        count = streak_hist[L]
        # P(streak of EXACTLY length L) ≈ (loss_rate)^L * (win_rate) — i.i.d. baseline
        p_iid = (p_loss ** L) * (1 - p_loss)
        print(f"{L:<10}{count:<10}{count/sum(streak_hist.values()):<14.3f}{p_iid:<14.3f}")
    print(f"longest loss-streak: {max(streak_lengths)} trades" if streak_lengths else "no losses")
    print(f"longest win-streak:  {max(win_streak_lengths)} trades" if win_streak_lengths else "no wins")
    print()

    if streak_details:
        print(f"=== Loss-streaks of length >= 3 (timing) ===")
        for start, end, L, ts_first, ts_last in streak_details:
            t1 = dt.datetime.fromtimestamp(ts_first/1000, tz=dt.UTC).strftime('%m-%d %H:%M UTC')
            t2 = dt.datetime.fromtimestamp(ts_last/1000, tz=dt.UTC).strftime('%m-%d %H:%M UTC')
            span_minutes = (ts_last - ts_first) / 1000 / 60
            net_streak = sum(t["net_cents"] for t in trades[start:end+1])
            print(f"  L={L}  {t1} -> {t2}  ({span_minutes:.0f} min)  net={net_streak:+d}c")
        print()

    # ── Hour-of-day analysis ─────────────────────────────────────────────
    hour_n = Counter()
    hour_wins = Counter()
    hour_losses = Counter()
    hour_net = defaultdict(int)
    for t in trades:
        h = dt.datetime.fromtimestamp(t["ts_ms"]/1000, tz=dt.UTC).hour
        hour_n[h] += 1
        if t["net_cents"] > 0:
            hour_wins[h] += 1
        else:
            hour_losses[h] += 1
        hour_net[h] += t["net_cents"]

    print(f"=== Hit rate / P&L by UTC hour ===")
    print(f"{'hour UTC':>10}{'trades':>8}{'wins':>6}{'losses':>8}{'hit-rate':>10}{'net cents':>12}{'  dist':>5}")
    for h in sorted(hour_n):
        rate = 100 * hour_wins[h] / hour_n[h] if hour_n[h] else 0
        bar_len = min(40, int(hour_n[h] * 2))
        bar = "#" * bar_len
        print(f"{h:>10d}{hour_n[h]:>8d}{hour_wins[h]:>6d}{hour_losses[h]:>8d}{rate:>9.1f}%{hour_net[h]:>+12d}  {bar}")

    print()
    print(f"=== Trades concentrated by ET clock-equivalent (UTC hour - 4) ===")
    # 4-hour buckets in ET: 0-3 (overnight), 4-7 (early), 8-11 (morning), 12-15 (midday), 16-19 (afternoon), 20-23 (evening)
    buckets = {
        "ET 00-03 (overnight US)":   range(4, 8),    # UTC 04-07
        "ET 04-07 (pre-market Asia)":range(8, 12),   # UTC 08-11
        "ET 08-11 (US/EU overlap)":  range(12, 16),  # UTC 12-15
        "ET 12-15 (US afternoon)":   range(16, 20),  # UTC 16-19
        "ET 16-19 (US close + Asia)":range(20, 24),  # UTC 20-23
        "ET 20-23 (Asia)":           range(0, 4),    # UTC 00-03
    }
    for label, hours in buckets.items():
        n_b = sum(hour_n[h] for h in hours)
        w_b = sum(hour_wins[h] for h in hours)
        l_b = sum(hour_losses[h] for h in hours)
        net_b = sum(hour_net[h] for h in hours)
        rate = 100 * w_b / n_b if n_b else 0
        print(f"  {label:<30} n={n_b:>3} W={w_b:>2} L={l_b:>2} hit={rate:>5.1f}%  net={net_b:+d}c")


if __name__ == "__main__":
    main()
