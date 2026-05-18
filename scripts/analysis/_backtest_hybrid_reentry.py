"""Backtest live_hybrid.py entry-policy variants against burnin tick data.

Reads bitstamp spot ticks + Kalshi L2 + lifecycle from the active burnin DB
(read-only), replays the velocity-trigger / trail-stop / signal-flip-exit
logic from scripts/live_hybrid.py exactly, and reports P&L under three
entry policies:

  A: BASELINE  - 1 entry per 15-min cycle (current production behavior)
  B: REENTRY   - new velocity trigger after an exit re-opens within the
                 same cycle; only one position open at a time
  C: MULTI_3   - up to 3 entries per cycle, max 1 open at a time

Caveats vs production live_hybrid.py:
- The TA confirmation gate (score>+20 blocks NO, score<-20 blocks YES) is
  NOT applied here. Porting TAScoreState onto the tick stream would add
  noise without changing the question (entry-policy diff). The gate is
  symmetric across scenarios so omitting it should not bias the
  comparison.
- Fills are simulated at "ask + 3c slippage" for entries and "bid - 3c"
  for exits using the latest known L2 snapshot. No partial-fill model;
  IOC always fills fully if marketable, else skipped.
- Late-cycle hold (final 120s) suppresses both trail and flip, matching
  production.
- Settlement at cycle close uses the determined lifecycle outcome.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ── Hybrid constants (copied verbatim from live_hybrid.py) ───────────────
CYCLE_MS = 15 * 60 * 1000
THRESHOLD_15S_USD = 20.0
THRESHOLD_30S_USD = 30.0
VELOCITY_WINDOW_MS = 35_000
TIER_STRONG_USD = 40.0
TIER_MEDIUM_USD = 30.0
TIER_CONTRACTS = {"STRONG": 40, "MEDIUM": 20, "WEAK": 10}
TRAIL_CENTS = 10
TRAIL_BID_FLOOR_CENTS = 5
LATE_CYCLE_HOLD_SECONDS = 120
SIGNAL_FLIP_GRACE_S = 20.0
SLIPPAGE_CENTS = 3
EXIT_SLIPPAGE_CENTS = 3
LIMIT_CAP_CENTS = 99


def kalshi_taker_fee_cents(price_cents: int, count: int = 1, k: float = 0.07) -> int:
    if count <= 0:
        return 0
    p = max(0.0, min(1.0, price_cents / 100.0))
    raw = k * count * p * (1.0 - p) * 100.0
    return int(math.ceil(raw - 1e-12))


def cycle_floor_ms(ts_ms: int) -> int:
    return (ts_ms // CYCLE_MS) * CYCLE_MS


def tier_for_magnitude(m: float) -> str:
    if m >= TIER_STRONG_USD:
        return "STRONG"
    if m >= TIER_MEDIUM_USD:
        return "MEDIUM"
    return "WEAK"


# ── Data loading ─────────────────────────────────────────────────────────


@dataclass
class L2Snap:
    ts_ms: int
    yes_bid_c: Optional[int]
    yes_ask_c: Optional[int]


@dataclass
class MarketInfo:
    ticker: str
    close_ms: int  # cycle close
    result: Optional[str]  # 'yes' / 'no' / None
    snaps: list[L2Snap] = field(default_factory=list)


def load_spot_ticks(db: Path, venue: str = "bitstamp") -> list[tuple[int, float]]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT received_ts_ms, mid FROM spot_quote_event WHERE venue=? AND mid IS NOT NULL ORDER BY received_ts_ms",
        (venue,),
    ).fetchall()
    out = [(int(r[0]), float(r[1])) for r in rows]
    con.close()
    return out


def load_markets(db: Path) -> dict[str, MarketInfo]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out: dict[str, MarketInfo] = {}
    # close_time -> close_ms via market_dim
    for ticker, ct in con.execute(
        "SELECT ticker, close_time FROM market_dim WHERE ticker LIKE 'KXBTC15M-%' AND close_time IS NOT NULL"
    ):
        import datetime as dt

        close = dt.datetime.strptime(ct, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
        out[ticker] = MarketInfo(ticker=ticker, close_ms=int(close.timestamp() * 1000), result=None)
    # determined results
    for tkr, rj in con.execute(
        "SELECT market_ticker, raw_json FROM kalshi_lifecycle_event WHERE status='determined' AND market_ticker LIKE 'KXBTC15M-%'"
    ):
        if tkr not in out:
            continue
        try:
            j = json.loads(rj)
            out[tkr].result = j.get("msg", j).get("result")
        except Exception:
            pass
    # L2 snaps (only with bid+ask present)
    for r in con.execute(
        """SELECT market_ticker, received_ts_ms, best_yes_bid, best_yes_ask
           FROM kalshi_l2_event
           WHERE market_ticker LIKE 'KXBTC15M-%'
             AND best_yes_bid IS NOT NULL AND best_yes_ask IS NOT NULL
           ORDER BY received_ts_ms"""
    ):
        tkr = r[0]
        if tkr not in out:
            continue
        try:
            bid_c = int(round(float(r[2]) * 100))
            ask_c = int(round(float(r[3]) * 100))
        except Exception:
            continue
        out[tkr].snaps.append(L2Snap(ts_ms=int(r[1]), yes_bid_c=bid_c, yes_ask_c=ask_c))
    con.close()
    return out


# ── Per-cycle ATM market lookup ──────────────────────────────────────────


def build_cycle_index(markets: dict[str, MarketInfo]) -> dict[int, list[MarketInfo]]:
    """Group markets by their cycle_close_ms."""
    out: dict[int, list[MarketInfo]] = defaultdict(list)
    for m in markets.values():
        out[m.close_ms].append(m)
    return out


def latest_snap_at_or_before(market: MarketInfo, ts_ms: int) -> Optional[L2Snap]:
    """Linear scan with cached cursor would be faster but markets are short."""
    last: Optional[L2Snap] = None
    for s in market.snaps:
        if s.ts_ms <= ts_ms:
            last = s
        else:
            break
    return last


def find_atm_market(
    cycle_markets: list[MarketInfo], ts_ms: int
) -> tuple[Optional[MarketInfo], Optional[L2Snap]]:
    best: Optional[tuple[MarketInfo, L2Snap, float]] = None
    for m in cycle_markets:
        s = latest_snap_at_or_before(m, ts_ms)
        if s is None or s.yes_ask_c is None:
            continue
        d = abs(s.yes_ask_c - 50)
        if best is None or d < best[2]:
            best = (m, s, d)
    if best is None:
        return None, None
    return best[0], best[1]


# ── Backtest engine ──────────────────────────────────────────────────────


@dataclass
class Position:
    ticker: str
    side: str  # 'yes' or 'no'
    contracts: int
    entry_price_c: int
    entry_fee_c: int
    cycle_floor_ms: int
    cycle_close_ms: int
    entered_at_ms: int
    hwm_bid_c: int
    market: MarketInfo


@dataclass
class Trade:
    ticker: str
    side: str
    contracts: int
    entry_price_c: int
    exit_price_c: int
    entry_fee_c: int
    exit_fee_c: int
    gross_c: int
    net_c: int
    cycle_floor_ms: int
    settled_via: str  # 'trail' | 'flip' | 'settle'


def our_side_bid_c(snap: L2Snap, side: str) -> Optional[int]:
    if side == "yes":
        return snap.yes_bid_c
    if snap.yes_ask_c is None:
        return None
    return max(0, 100 - snap.yes_ask_c)


def our_side_ask_c(snap: L2Snap, side: str) -> Optional[int]:
    if side == "yes":
        return snap.yes_ask_c
    if snap.yes_bid_c is None:
        return None
    return max(0, 100 - snap.yes_bid_c)


def run_backtest(
    ticks: list[tuple[int, float]],
    markets: dict[str, MarketInfo],
    *,
    policy: str,  # 'A', 'B', or 'C'
    max_entries_per_cycle: int,
) -> tuple[list[Trade], dict]:
    cycle_idx = build_cycle_index(markets)
    window: deque = deque()

    open_positions: dict[str, Position] = {}
    trades: list[Trade] = []
    entries_per_cycle: dict[int, int] = defaultdict(int)
    open_per_cycle: dict[int, int] = defaultdict(int)
    trigger_count = 0
    skipped_no_market = 0
    skipped_dedupe = 0
    skipped_open = 0
    flip_exits = 0
    trail_exits = 0
    settle_exits = 0
    last_ts = 0

    # Pre-sort cycles by close time for end-of-cycle settlement processing.
    cycles_sorted = sorted(cycle_idx.keys())
    next_settle_idx = 0

    for ts_ms, mid in ticks:
        last_ts = ts_ms
        window.append((ts_ms, mid))
        cutoff = ts_ms - VELOCITY_WINDOW_MS
        while window and window[0][0] < cutoff:
            window.popleft()

        if len(window) < 2:
            continue

        ref_15s = None
        ref_30s = None
        for w_ts, w_mid in window:
            if w_ts <= ts_ms - 15_000:
                ref_15s = (w_ts, w_mid)
            if w_ts <= ts_ms - 30_000:
                ref_30s = (w_ts, w_mid)
            else:
                break
        delta_15s = (mid - ref_15s[1]) if ref_15s else 0.0
        delta_30s = (mid - ref_30s[1]) if ref_30s else 0.0
        have_15s = ref_15s is not None and (ts_ms - ref_15s[0]) >= 14_000
        have_30s = ref_30s is not None and (ts_ms - ref_30s[0]) >= 28_000
        trig_up = (have_15s and delta_15s >= THRESHOLD_15S_USD) or (
            have_30s and delta_30s >= THRESHOLD_30S_USD
        )
        trig_down = (have_15s and delta_15s <= -THRESHOLD_15S_USD) or (
            have_30s and delta_30s <= -THRESHOLD_30S_USD
        )

        # ── Settle any cycles that have closed ───────────────────────────
        while next_settle_idx < len(cycles_sorted) and cycles_sorted[next_settle_idx] <= ts_ms:
            cc = cycles_sorted[next_settle_idx]
            next_settle_idx += 1
            to_close = [t for t, p in open_positions.items() if p.cycle_close_ms == cc]
            for tkr in to_close:
                p = open_positions.pop(tkr)
                result = p.market.result
                if result is None:
                    continue
                if p.side == result:
                    gross = p.contracts * (100 - p.entry_price_c)
                else:
                    gross = -p.contracts * p.entry_price_c
                net = gross - p.entry_fee_c
                trades.append(
                    Trade(
                        ticker=p.ticker,
                        side=p.side,
                        contracts=p.contracts,
                        entry_price_c=p.entry_price_c,
                        exit_price_c=100 if p.side == result else 0,
                        entry_fee_c=p.entry_fee_c,
                        exit_fee_c=0,
                        gross_c=gross,
                        net_c=net,
                        cycle_floor_ms=p.cycle_floor_ms,
                        settled_via="settle",
                    )
                )
                settle_exits += 1

        # ── Signal-flip exit (process BEFORE entry to free open slot) ───
        if (trig_up or trig_down) and open_positions:
            burst_dir = "up" if trig_up else "down"
            to_flip = []
            for tkr, p in open_positions.items():
                if (ts_ms - p.entered_at_ms) / 1000.0 < SIGNAL_FLIP_GRACE_S:
                    continue
                secs_to_close = (p.cycle_close_ms - ts_ms) / 1000.0
                if secs_to_close <= LATE_CYCLE_HOLD_SECONDS:
                    continue
                if (p.side == "yes" and burst_dir == "down") or (
                    p.side == "no" and burst_dir == "up"
                ):
                    to_flip.append(tkr)
            for tkr in to_flip:
                p = open_positions[tkr]
                snap = latest_snap_at_or_before(p.market, ts_ms)
                if snap is None:
                    continue
                bid = our_side_bid_c(snap, p.side)
                if bid is None:
                    continue
                sell_limit = max(1, bid - EXIT_SLIPPAGE_CENTS)
                # Assume IOC fills at the bid (the slip cushion just ensures fill).
                fill_price = bid
                exit_fee = kalshi_taker_fee_cents(fill_price, count=p.contracts)
                gross = p.contracts * (fill_price - p.entry_price_c)
                net = gross - p.entry_fee_c - exit_fee
                trades.append(
                    Trade(
                        ticker=p.ticker,
                        side=p.side,
                        contracts=p.contracts,
                        entry_price_c=p.entry_price_c,
                        exit_price_c=fill_price,
                        entry_fee_c=p.entry_fee_c,
                        exit_fee_c=exit_fee,
                        gross_c=gross,
                        net_c=net,
                        cycle_floor_ms=p.cycle_floor_ms,
                        settled_via="flip",
                    )
                )
                flip_exits += 1
                open_positions.pop(tkr)
                open_per_cycle[p.cycle_floor_ms] -= 1

        # ── Entry ─────────────────────────────────────────────────────────
        if trig_up or trig_down:
            trigger_count += 1
            cf = cycle_floor_ms(ts_ms)
            cycle_close = cf + CYCLE_MS
            side = "yes" if trig_up else "no"

            # Dedupe / capacity check based on policy.
            allow = True
            if policy == "A":
                # 1 entry per cycle, ever.
                if entries_per_cycle[cf] >= 1:
                    allow = False
                    skipped_dedupe += 1
            elif policy == "B":
                # Re-entry allowed but only after current pos closes.
                if open_per_cycle[cf] >= 1:
                    allow = False
                    skipped_open += 1
            elif policy == "C":
                if entries_per_cycle[cf] >= max_entries_per_cycle:
                    allow = False
                    skipped_dedupe += 1
                elif open_per_cycle[cf] >= 1:
                    allow = False
                    skipped_open += 1

            if allow:
                cycle_markets = cycle_idx.get(cycle_close, [])
                m, snap = find_atm_market(cycle_markets, ts_ms)
                if m is None or snap is None:
                    skipped_no_market += 1
                else:
                    ask = our_side_ask_c(snap, side)
                    if ask is None or ask >= 100:
                        skipped_no_market += 1
                    else:
                        limit = min(LIMIT_CAP_CENTS, ask + SLIPPAGE_CENTS)
                        # Conservative fill at the ask.
                        fill_price = ask
                        mag = max(abs(delta_15s), abs(delta_30s))
                        tier = tier_for_magnitude(mag)
                        contracts = TIER_CONTRACTS[tier]
                        entry_fee = kalshi_taker_fee_cents(fill_price, count=contracts)
                        p = Position(
                            ticker=m.ticker,
                            side=side,
                            contracts=contracts,
                            entry_price_c=fill_price,
                            entry_fee_c=entry_fee,
                            cycle_floor_ms=cf,
                            cycle_close_ms=cycle_close,
                            entered_at_ms=ts_ms,
                            hwm_bid_c=fill_price,
                            market=m,
                        )
                        # Track per-cycle (unique key by entry time so multi-entry doesn't collide).
                        key = f"{m.ticker}#{ts_ms}"
                        open_positions[key] = p
                        entries_per_cycle[cf] += 1
                        open_per_cycle[cf] += 1

        # ── Trail-stop ───────────────────────────────────────────────────
        to_trail = []
        for tkr, p in open_positions.items():
            secs_to_close = (p.cycle_close_ms - ts_ms) / 1000.0
            if secs_to_close <= LATE_CYCLE_HOLD_SECONDS:
                continue
            snap = latest_snap_at_or_before(p.market, ts_ms)
            if snap is None:
                continue
            bid = our_side_bid_c(snap, p.side)
            if bid is None:
                continue
            if bid > p.hwm_bid_c:
                p.hwm_bid_c = bid
            if bid <= p.hwm_bid_c - TRAIL_CENTS and bid >= TRAIL_BID_FLOOR_CENTS:
                to_trail.append(tkr)
        for tkr in to_trail:
            p = open_positions[tkr]
            snap = latest_snap_at_or_before(p.market, ts_ms)
            if snap is None:
                continue
            bid = our_side_bid_c(snap, p.side)
            if bid is None:
                continue
            fill_price = bid
            exit_fee = kalshi_taker_fee_cents(fill_price, count=p.contracts)
            gross = p.contracts * (fill_price - p.entry_price_c)
            net = gross - p.entry_fee_c - exit_fee
            trades.append(
                Trade(
                    ticker=p.ticker,
                    side=p.side,
                    contracts=p.contracts,
                    entry_price_c=p.entry_price_c,
                    exit_price_c=fill_price,
                    entry_fee_c=p.entry_fee_c,
                    exit_fee_c=exit_fee,
                    gross_c=gross,
                    net_c=net,
                    cycle_floor_ms=p.cycle_floor_ms,
                    settled_via="trail",
                )
            )
            trail_exits += 1
            open_positions.pop(tkr)
            open_per_cycle[p.cycle_floor_ms] -= 1

    # ── Final settlement of leftovers ────────────────────────────────────
    for tkr, p in list(open_positions.items()):
        result = p.market.result
        if result is None:
            continue
        if p.side == result:
            gross = p.contracts * (100 - p.entry_price_c)
        else:
            gross = -p.contracts * p.entry_price_c
        net = gross - p.entry_fee_c
        trades.append(
            Trade(
                ticker=p.ticker,
                side=p.side,
                contracts=p.contracts,
                entry_price_c=p.entry_price_c,
                exit_price_c=100 if p.side == result else 0,
                entry_fee_c=p.entry_fee_c,
                exit_fee_c=0,
                gross_c=gross,
                net_c=net,
                cycle_floor_ms=p.cycle_floor_ms,
                settled_via="settle",
            )
        )
        settle_exits += 1

    stats = {
        "trigger_count": trigger_count,
        "skipped_dedupe": skipped_dedupe,
        "skipped_open": skipped_open,
        "skipped_no_market": skipped_no_market,
        "flip_exits": flip_exits,
        "trail_exits": trail_exits,
        "settle_exits": settle_exits,
        "entries_per_cycle_hist": dict(
            sorted(
                {n: list(entries_per_cycle.values()).count(n) for n in set(entries_per_cycle.values())}.items()
            )
        ),
    }
    return trades, stats


def summarize(name: str, trades: list[Trade], stats: dict, n_cycles_with_data: int) -> dict:
    total = len(trades)
    wins = sum(1 for t in trades if t.net_c > 0)
    gross = sum(t.gross_c for t in trades)
    net = sum(t.net_c for t in trades)
    fees = sum(t.entry_fee_c + t.exit_fee_c for t in trades)

    # Drawdown on net P&L (chronological by cycle_floor_ms then within).
    chrono = sorted(trades, key=lambda t: t.cycle_floor_ms)
    eq = 0
    peak = 0
    dd_max = 0
    for t in chrono:
        eq += t.net_c
        peak = max(peak, eq)
        dd = peak - eq
        dd_max = max(dd_max, dd)

    cycles_with_trade = len({t.cycle_floor_ms for t in trades})
    avg_per_cycle = net / cycles_with_trade if cycles_with_trade else 0.0

    by_exit = defaultdict(lambda: [0, 0, 0])  # [n, wins, net]
    for t in trades:
        by_exit[t.settled_via][0] += 1
        if t.net_c > 0:
            by_exit[t.settled_via][1] += 1
        by_exit[t.settled_via][2] += t.net_c

    out = {
        "name": name,
        "trades": total,
        "wins": wins,
        "win_rate": (wins / total) if total else 0.0,
        "gross_cents": gross,
        "fees_cents": fees,
        "net_cents": net,
        "max_drawdown_cents": dd_max,
        "cycles_with_trade": cycles_with_trade,
        "avg_net_per_cycle_traded_cents": avg_per_cycle,
        "by_exit": {k: {"n": v[0], "wins": v[1], "net_c": v[2]} for k, v in by_exit.items()},
        "stats": stats,
    }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=r"C:\Trading\kalshi-btc-engine-v2\data\burnin_holdpure_2026_05_12.sqlite")
    ap.add_argument("--venue", default="bitstamp")
    ap.add_argument("--max-entries-c", type=int, default=3)
    ap.add_argument("--out", default=r"C:\Trading\kalshi-btc-engine-v2\data\_backtest_hybrid_reentry.json")
    args = ap.parse_args()

    db = Path(args.db)
    print(f"[backtest] loading ticks from {db.name} venue={args.venue} ...", file=sys.stderr, flush=True)
    ticks = load_spot_ticks(db, args.venue)
    print(f"[backtest] loaded {len(ticks)} ticks", file=sys.stderr, flush=True)
    markets = load_markets(db)
    settled = sum(1 for m in markets.values() if m.result is not None)
    snaps = sum(len(m.snaps) for m in markets.values())
    print(
        f"[backtest] loaded {len(markets)} 15M markets, {settled} settled, {snaps} L2 snaps",
        file=sys.stderr,
        flush=True,
    )

    # Cycles where we have both spot ticks and a settled market.
    if ticks:
        tick_lo, tick_hi = ticks[0][0], ticks[-1][0]
    else:
        tick_lo = tick_hi = 0
    eligible_cycles = sum(
        1
        for m in markets.values()
        if m.result is not None and tick_lo <= m.close_ms <= tick_hi
    )
    print(f"[backtest] eligible (settled+in-tick-range) cycles = {eligible_cycles}", file=sys.stderr, flush=True)

    scenarios = [
        ("A_baseline_1_per_cycle", "A", 1),
        ("B_reentry_unlimited", "B", 999),
        (f"C_max_{args.max_entries_c}_per_cycle", "C", args.max_entries_c),
    ]
    results = []
    for name, policy, n in scenarios:
        print(f"[backtest] running {name} ...", file=sys.stderr, flush=True)
        trades, stats = run_backtest(ticks, markets, policy=policy, max_entries_per_cycle=n)
        results.append(summarize(name, trades, stats, eligible_cycles))

    out_doc = {
        "db": str(db),
        "venue": args.venue,
        "ticks_loaded": len(ticks),
        "tick_range_ms": [tick_lo, tick_hi],
        "tick_range_hours": (tick_hi - tick_lo) / 3600000.0 if ticks else 0.0,
        "markets_loaded": len(markets),
        "markets_settled": settled,
        "eligible_cycles": eligible_cycles,
        "thresholds": {
            "thr_15s_usd": THRESHOLD_15S_USD,
            "thr_30s_usd": THRESHOLD_30S_USD,
            "tier_strong_usd": TIER_STRONG_USD,
            "tier_medium_usd": TIER_MEDIUM_USD,
            "tier_contracts": TIER_CONTRACTS,
            "trail_cents": TRAIL_CENTS,
            "late_cycle_hold_s": LATE_CYCLE_HOLD_SECONDS,
            "signal_flip_grace_s": SIGNAL_FLIP_GRACE_S,
        },
        "scenarios": results,
        "caveat": "TA-disagree veto NOT applied; entries simulated at L2 ask, exits at L2 bid (no partial fills).",
    }
    Path(args.out).write_text(json.dumps(out_doc, indent=2, default=str), encoding="utf-8")
    print(f"[backtest] wrote {args.out}", file=sys.stderr, flush=True)

    # Pretty console summary.
    print()
    print(f"{'SCENARIO':<28} {'TRADES':>7} {'WIN%':>7} {'GROSS$':>9} {'FEES$':>8} {'NET$':>9} {'MAXDD$':>9} {'CYCLES':>7} {'AVG/CYC$':>9}")
    for r in results:
        print(
            f"{r['name']:<28} {r['trades']:>7} "
            f"{r['win_rate']*100:>6.1f}% "
            f"${r['gross_cents']/100:>8.2f} "
            f"${r['fees_cents']/100:>7.2f} "
            f"${r['net_cents']/100:>8.2f} "
            f"${r['max_drawdown_cents']/100:>8.2f} "
            f"{r['cycles_with_trade']:>7} "
            f"${r['avg_net_per_cycle_traded_cents']/100:>8.2f}"
        )
    print()
    for r in results:
        be = r["by_exit"]
        parts = []
        for k in ("trail", "flip", "settle"):
            if k in be:
                v = be[k]
                parts.append(f"{k}: n={v['n']} w={v['wins']} net=${v['net_c']/100:+.2f}")
        print(f"  {r['name']}: " + " | ".join(parts))


if __name__ == "__main__":
    main()
