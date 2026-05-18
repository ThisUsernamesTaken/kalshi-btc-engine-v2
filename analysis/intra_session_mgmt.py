"""Intra-session position management study.

For every settled trade in live_ta_trades.jsonl + paper_ta_2026_05_12.jsonl,
reconstruct the post-entry bid trajectory minute-by-minute from the L2 capture DB
and evaluate active-management rules (trailing stops, fixed stops, early winner exit).
"""

from __future__ import annotations

import json
import os
import sqlite3
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

DATA_DIR = r"C:\Trading\kalshi-btc-engine-v2\data"
DB_PATH = os.path.join(DATA_DIR, "burnin_holdpure_2026_05_12.sqlite")
LIVE_LOG = os.path.join(DATA_DIR, "live_ta_trades.jsonl")
PAPER_LOG = os.path.join(DATA_DIR, "paper_ta_2026_05_12.jsonl")

ENTRY_FEE = 2  # cents per contract (from settle.net = settle.gross - entry_fee)
EXIT_FEE = 2   # estimated symmetric exit fee for early-exit P&L calc

MIN_MS = 60_000


def _to_cents(x) -> Optional[float]:
    """L2 best_yes_bid / best_yes_ask are stored as decimal-dollar strings
    (e.g. "0.6000" = 60 cents).  Return value in cents as float."""
    if x is None:
        return None
    s = str(x).strip()
    if not s:
        return None
    try:
        v = float(s)
    except Exception:
        return None
    return v * 100.0


# ---------- Trade loading ----------------------------------------------------

@dataclass
class Trade:
    source: str           # 'live' or 'paper'
    ticker: str
    entry_ts_ms: int      # ts_minute_ms from fill (and decided_at_ts_ms on settle)
    cycle_close_ms: int   # from settle
    side: str             # 'yes' or 'no'  (our long side)
    entry_price: int      # cents per contract paid
    contracts: int
    outcome: str          # 'yes' / 'no' / other (from settle)
    won: bool
    gross_cents: int
    net_cents: int
    tier_name: str
    confidence: float
    # post-entry trajectory (filled later)
    minute_bids: list[tuple[int, float]] = field(default_factory=list)  # (minute_offset, our_side_bid in cents)
    mfe: Optional[float] = None
    mae: Optional[float] = None
    mfe_minute: Optional[int] = None
    mae_minute: Optional[int] = None
    n_minutes: int = 0


def load_trades() -> list[Trade]:
    trades: list[Trade] = []
    for source, path in (("live", LIVE_LOG), ("paper", PAPER_LOG)):
        fills_by_key: dict[tuple, dict] = {}
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("kind") == "fill":
                    key = (r["ticker"], r["ts_minute_ms"])
                    fills_by_key[key] = r
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("kind") != "settle":
                    continue
                key = (r["ticker"], r["decided_at_ts_ms"])
                fill = fills_by_key.get(key)
                if not fill:
                    continue
                side = r["side"]  # 'yes' or 'no'
                outcome = r["outcome"]
                won = (outcome == side)
                trades.append(Trade(
                    source=source,
                    ticker=r["ticker"],
                    entry_ts_ms=int(r["decided_at_ts_ms"]),
                    cycle_close_ms=int(r["cycle_close_ms"]),
                    side=side,
                    entry_price=int(r["entry_price_cents"]),
                    contracts=int(r["contracts"]),
                    outcome=outcome,
                    won=won,
                    gross_cents=int(r.get("gross_cents") or 0),
                    net_cents=int(r.get("net_cents") or 0),
                    tier_name=str(r.get("tier_name") or ""),
                    confidence=float(r.get("confidence") or 0.0),
                ))
    return trades


# ---------- Trajectory extraction -------------------------------------------

def hydrate_trajectories(trades: list[Trade]):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    db_min, db_max = next(cur.execute(
        "SELECT MIN(received_ts_ms), MAX(received_ts_ms) FROM kalshi_l2_event"
    ))
    n_in_window = 0
    n_skipped = 0
    for t in trades:
        # Skip trades whose cycle isn't in DB capture window
        if t.entry_ts_ms < db_min or t.cycle_close_ms > db_max + 60_000:
            n_skipped += 1
            continue
        # Query all L2 events for this ticker between entry and cycle close.
        # Use exchange_ts_ms when available, else received_ts_ms (matches index).
        q = """
            SELECT COALESCE(exchange_ts_ms, received_ts_ms) AS ts,
                   best_yes_bid, best_yes_ask
            FROM kalshi_l2_event
            WHERE market_ticker = ?
              AND COALESCE(exchange_ts_ms, received_ts_ms) >= ?
              AND COALESCE(exchange_ts_ms, received_ts_ms) <= ?
            ORDER BY COALESCE(exchange_ts_ms, received_ts_ms) ASC, event_id ASC
        """
        rows = cur.execute(q, (t.ticker, t.entry_ts_ms, t.cycle_close_ms)).fetchall()
        if not rows:
            continue
        n_in_window += 1
        # Bucket by minute-offset from entry; keep LAST observation per minute.
        per_minute_last: dict[int, float] = {}
        last_yb = last_ya = None
        for row in rows:
            yb = _to_cents(row["best_yes_bid"])
            ya = _to_cents(row["best_yes_ask"])
            if yb is not None:
                last_yb = yb
            if ya is not None:
                last_ya = ya
            if t.side == "yes":
                bid = last_yb
            else:
                bid = (100.0 - last_ya) if last_ya is not None else None
            if bid is None:
                continue
            bid = max(0.0, min(100.0, bid))
            minute_off = (row["ts"] - t.entry_ts_ms) // MIN_MS
            if minute_off < 0:
                continue
            per_minute_last[int(minute_off)] = bid
        if not per_minute_last:
            continue
        seq = sorted(per_minute_last.items())
        t.minute_bids = seq
        t.n_minutes = len(seq)
        mfe_minute, mfe = max(seq, key=lambda p: (p[1], -p[0]))
        mae_minute, mae = min(seq, key=lambda p: (p[1], -p[0]))
        # Pick FIRST minute that hit the extreme (max/min may repeat)
        for m, b in seq:
            if b == mfe:
                mfe_minute = m
                break
        for m, b in seq:
            if b == mae:
                mae_minute = m
                break
        t.mfe, t.mae = mfe, mae
        t.mfe_minute, t.mae_minute = mfe_minute, mae_minute
    con.close()
    return n_in_window, n_skipped


# ---------- Simulation: trailing stop & fixed stop --------------------------

def simulate_trailing_stop(seq: list[tuple[int, int]], entry: int, trail_cents: int) -> tuple[int, int, bool]:
    """Return (exit_price, exit_minute, triggered).
    Rule: track running max ('peak'). Exit when bid <= peak - trail_cents.
    If never triggered, return last-observed bid (proxy for settlement) with triggered=False.
    """
    peak = -1
    for m, b in seq:
        if b > peak:
            peak = b
        if peak - b >= trail_cents:
            return b, m, True
    last_m, last_b = seq[-1]
    return last_b, last_m, False


def simulate_fixed_stop(seq: list[tuple[int, int]], entry: int, stop_cents_below_entry: int) -> tuple[int, int, bool]:
    """Stop when bid <= entry - stop_cents_below_entry."""
    threshold = entry - stop_cents_below_entry
    for m, b in seq:
        if b <= threshold:
            return b, m, True
    last_m, last_b = seq[-1]
    return last_b, last_m, False


def early_exit_at(seq: list[tuple[int, int]], target_price: int) -> Optional[tuple[int, int]]:
    """Return (minute, bid) of first time bid >= target_price."""
    for m, b in seq:
        if b >= target_price:
            return m, b
    return None


def pnl_per_contract(entry: int, exit_price: int, settled: bool, won: bool) -> int:
    """Net P&L in cents per contract.

    settled=True means we held to settlement; payoff is 100 if won else 0 minus entry_fee.
    settled=False (early exit) means we sold at `exit_price` minus exit_fee, minus entry_fee.
    """
    if settled:
        payoff = 100 if won else 0
        return payoff - entry - ENTRY_FEE
    return exit_price - entry - ENTRY_FEE - EXIT_FEE


# ---------- Stats helpers ----------------------------------------------------

def pct(n, d):
    return f"{(100.0 * n / d):.1f}%" if d else "-"


def quantiles(vals: list[int]) -> dict:
    if not vals:
        return {}
    s = sorted(vals)
    n = len(s)
    def q(p):
        if n == 1:
            return s[0]
        k = (n - 1) * p
        f = int(k)
        c = min(f + 1, n - 1)
        return s[f] + (s[c] - s[f]) * (k - f)
    return {
        "n": n,
        "min": s[0],
        "p10": q(0.10),
        "p25": q(0.25),
        "p50": q(0.50),
        "p75": q(0.75),
        "p90": q(0.90),
        "max": s[-1],
        "mean": statistics.fmean(s),
    }


def fmt_q(qd):
    if not qd:
        return "(empty)"
    return (
        f"n={qd['n']:>3}  min={qd['min']:>+5.0f}  p10={qd['p10']:>+5.1f}  "
        f"p25={qd['p25']:>+5.1f}  p50={qd['p50']:>+5.1f}  p75={qd['p75']:>+5.1f}  "
        f"p90={qd['p90']:>+5.1f}  max={qd['max']:>+5.0f}  mean={qd['mean']:>+5.1f}"
    )


# ---------- Main -------------------------------------------------------------

def main():
    print("=" * 100)
    print("INTRA-SESSION POSITION MANAGEMENT STUDY")
    print("Capture DB:", DB_PATH)
    print("=" * 100)

    trades = load_trades()
    print(f"\nLoaded {len(trades)} settled trades  "
          f"({sum(1 for t in trades if t.source=='live')} live, "
          f"{sum(1 for t in trades if t.source=='paper')} paper).")
    print(f"Wins: {sum(1 for t in trades if t.won)}  "
          f"Losses: {sum(1 for t in trades if not t.won)}")

    n_hyd, n_skip = hydrate_trajectories(trades)
    have_traj = [t for t in trades if t.minute_bids]
    print(f"Trajectory hydrated for {len(have_traj)} / {len(trades)} trades "
          f"(skipped {n_skip} outside capture window).")

    wins = [t for t in have_traj if t.won]
    losses = [t for t in have_traj if not t.won]
    print(f"  Wins with trajectory:   {len(wins)}")
    print(f"  Losses with trajectory: {len(losses)}")

    # ===== Per-trade table (compact) ==========================================
    print("\n" + "=" * 100)
    print("PER-TRADE DETAIL  (entry / MFE / MAE / outcome)")
    print("=" * 100)
    header = (f"{'src':<5}{'ticker':<28}{'side':<4}{'tier':<7}"
              f"{'entry':>6}{'MFE':>7}{'MFE@m':>7}{'MAE':>7}{'MAE@m':>7}"
              f"{'#min':>5}{'res':>5}{'netc':>6}")
    print(header)
    print("-" * len(header))
    for t in sorted(have_traj, key=lambda x: (x.source, x.entry_ts_ms)):
        res = "WIN" if t.won else "LOSS"
        print(f"{t.source:<5}{t.ticker[:27]:<28}{t.side.upper():<4}{t.tier_name:<7}"
              f"{t.entry_price:>6}{t.mfe:>7.1f}{t.mfe_minute:>7}"
              f"{t.mae:>7.1f}{t.mae_minute:>7}"
              f"{t.n_minutes:>5}{res:>5}{t.net_cents:>6}")

    # ===== Excursion distributions ============================================
    print("\n" + "=" * 100)
    print("MFE / MAE DISTRIBUTIONS  (cents vs entry_price; +ve favourable)")
    print("=" * 100)
    def excursions(group):
        mfe_c = [t.mfe - t.entry_price for t in group]
        mae_c = [t.mae - t.entry_price for t in group]
        return mfe_c, mae_c

    mfe_w, mae_w = excursions(wins)
    mfe_l, mae_l = excursions(losses)
    print(f"WINS   MFE: {fmt_q(quantiles(mfe_w))}")
    print(f"WINS   MAE: {fmt_q(quantiles(mae_w))}")
    print(f"LOSSES MFE: {fmt_q(quantiles(mfe_l))}")
    print(f"LOSSES MAE: {fmt_q(quantiles(mae_l))}")

    # Time-to-extremes
    print("\nTime-to-MFE / Time-to-MAE  (minutes after entry, capped at cycle close ~10-13m)")
    print(f"WINS   t->MFE: {fmt_q(quantiles([t.mfe_minute for t in wins]))}")
    print(f"WINS   t->MAE: {fmt_q(quantiles([t.mae_minute for t in wins]))}")
    print(f"LOSSES t->MFE: {fmt_q(quantiles([t.mfe_minute for t in losses]))}")
    print(f"LOSSES t->MAE: {fmt_q(quantiles([t.mae_minute for t in losses]))}")

    # ===== Loser-side analysis ================================================
    print("\n" + "=" * 100)
    print("LOSER ANALYSIS  (n = {})".format(len(losses)))
    print("=" * 100)
    n_ever_above = sum(1 for t in losses if t.mfe > t.entry_price)
    n_ever_above_fee_break = sum(1 for t in losses if t.mfe >= t.entry_price + ENTRY_FEE + EXIT_FEE)
    print(f"Losers whose bid was ever ABOVE entry (raw):                 "
          f"{n_ever_above}/{len(losses)}  ({pct(n_ever_above, len(losses))})")
    print(f"Losers whose bid was ever >= entry + roundtrip fees (4c):     "
          f"{n_ever_above_fee_break}/{len(losses)}  "
          f"({pct(n_ever_above_fee_break, len(losses))})")

    # Irreversibility: minute bid first dropped below 20c (deep OTM)
    irrev = []
    for t in losses:
        m_below20 = None
        for m, b in t.minute_bids:
            if b < 20:
                m_below20 = m
                break
        irrev.append((t, m_below20))
    n_below20 = sum(1 for _, m in irrev if m is not None)
    if n_below20:
        irrev_minutes = [m for _, m in irrev if m is not None]
        print(f"Losers reaching bid<20c (effectively dead):                 "
              f"{n_below20}/{len(losses)}  median minute={statistics.median(irrev_minutes):.1f}")
    else:
        print("No losers reached bid<20c within their cycle.")

    # Trailing-stop sims (per-contract)
    print("\nTrailing-stop simulation - exit when bid drops Xc below running peak.")
    print("Realized payoff per contract assumed: settle->{100 if won else 0}, "
          "early->exit_price, both minus entry_fee 2c, early also minus exit_fee 2c.\n")
    print(f"{'trail':<7}{'#triggered':>11}{'#saved':>8}{'#worse':>8}"
          f"{'avg dc/trade':>16}{'sum dc (losses)':>18}{'sum dc (all)':>15}")
    for trail in (5, 10, 15, 20):
        deltas_all = []
        deltas_loss = []
        n_trig = n_saved = n_worse = 0
        for t in have_traj:
            if not t.minute_bids:
                continue
            exit_p, exit_m, triggered = simulate_trailing_stop(t.minute_bids, t.entry_price, trail)
            baseline = pnl_per_contract(t.entry_price, 0, settled=True, won=t.won)
            alt = pnl_per_contract(t.entry_price, exit_p, settled=False, won=t.won) if triggered \
                else baseline
            delta = alt - baseline
            deltas_all.append(delta)
            if triggered:
                n_trig += 1
                if not t.won:
                    deltas_loss.append(delta)
                    if delta > 0:
                        n_saved += 1
                else:
                    if delta < 0:
                        n_worse += 1
        avg_all = statistics.fmean(deltas_all) if deltas_all else 0
        print(f"{trail:<7}{n_trig:>11}{n_saved:>8}{n_worse:>8}"
              f"{avg_all:>+16.1f}{sum(deltas_loss):>+18.1f}{sum(deltas_all):>+15.1f}")

    # Fixed stop at entry - 15c
    print("\nFixed stop-loss at entry - 15c (per contract):")
    print(f"{'rule':<22}{'#triggered':>11}{'#saved (losers)':>17}"
          f"{'#hurt (winners)':>17}{'sum dc (all)':>14}")
    for stop_c in (10, 15, 20, 25):
        deltas = []
        n_trig = n_saved = n_hurt = 0
        for t in have_traj:
            if not t.minute_bids:
                continue
            exit_p, exit_m, trig = simulate_fixed_stop(t.minute_bids, t.entry_price, stop_c)
            baseline = pnl_per_contract(t.entry_price, 0, settled=True, won=t.won)
            alt = pnl_per_contract(t.entry_price, exit_p, settled=False, won=t.won) if trig \
                else baseline
            delta = alt - baseline
            deltas.append(delta)
            if trig:
                n_trig += 1
                if not t.won and delta > 0:
                    n_saved += 1
                if t.won and delta < 0:
                    n_hurt += 1
        print(f"entry-{stop_c:>2}c            {n_trig:>11}{n_saved:>17}"
              f"{n_hurt:>17}{sum(deltas):>+14.1f}")

    # ===== Winner-side analysis ===============================================
    print("\n" + "=" * 100)
    print("WINNER ANALYSIS  (n = {})".format(len(wins)))
    print("=" * 100)
    targets = (80, 90, 95)
    for target in targets:
        hit_minutes = []
        n_hit = 0
        for t in wins:
            r = early_exit_at(t.minute_bids, target)
            if r:
                n_hit += 1
                hit_minutes.append(r[0])
        print(f"Bid >= {target}c before settlement: {n_hit}/{len(wins)} "
              f"({pct(n_hit, len(wins))})  "
              f"median minute reached: "
              f"{(statistics.median(hit_minutes) if hit_minutes else float('nan')):.1f}")

    # Early-exit-at-90 capital efficiency simulation
    print("\nEarly-exit-at-90c vs hold-to-settle (per contract, all winners with trajectories):")
    held_pnl = []
    early_pnl = []
    held_minutes = []
    early_minutes = []
    for t in wins:
        baseline = pnl_per_contract(t.entry_price, 0, settled=True, won=True)
        held_pnl.append(baseline)
        # Use full cycle length as hold time
        held_minutes.append(t.minute_bids[-1][0] if t.minute_bids else 13)
        r = early_exit_at(t.minute_bids, 90)
        if r:
            m, b = r
            alt = pnl_per_contract(t.entry_price, b, settled=False, won=True)
            early_pnl.append(alt)
            early_minutes.append(m)
        else:
            early_pnl.append(baseline)
            early_minutes.append(t.minute_bids[-1][0] if t.minute_bids else 13)
    if held_pnl:
        sum_held = sum(held_pnl)
        sum_early = sum(early_pnl)
        avg_held_m = statistics.fmean(held_minutes)
        avg_early_m = statistics.fmean(early_minutes)
        print(f"  hold-to-settle: sumc/contract = {sum_held:+.1f}, "
              f"avg hold-minutes = {avg_held_m:.1f}, "
              f"c per minute of capital = {sum_held/sum(held_minutes):.2f}")
        print(f"  exit-at-90c:    sumc/contract = {sum_early:+.1f}, "
              f"avg hold-minutes = {avg_early_m:.1f}, "
              f"c per minute of capital = {sum_early/sum(early_minutes):.2f}")
        diff = sum_held - sum_early
        print(f"  d if we exit at 90 instead of holding: {-diff:+.1f}c (negative = leaves money on table)")

    # ===== Recommendation =====================================================
    print("\n" + "=" * 100)
    print("OBSERVED PATTERNS")
    print("=" * 100)
    # MFE > 0 in losses, MAE clustering, etc.
    if losses:
        rec_mfe = [t.mfe - t.entry_price for t in losses]
        rec_mae = [t.mae - t.entry_price for t in losses]
        n_round_trip = sum(1 for t in losses
                           if (t.mfe - t.entry_price) >= 4 and t.mfe_minute < t.n_minutes - 1)
        print(f"- {n_round_trip}/{len(losses)} losers traded through a >=4c profit window before reversing.")
        print(f"- Median MFE excursion among losers: {statistics.median(rec_mfe):+.1f}c  "
              f"(mean {statistics.fmean(rec_mfe):+.1f}c).")
        print(f"- Median MAE excursion among losers: {statistics.median(rec_mae):+.1f}c.")
    if wins:
        n_drawdown_5 = sum(1 for t in wins if (t.entry_price - t.mae) >= 5)
        n_drawdown_10 = sum(1 for t in wins if (t.entry_price - t.mae) >= 10)
        print(f"- {n_drawdown_5}/{len(wins)} winners drew down >=5c from entry before recovering.")
        print(f"- {n_drawdown_10}/{len(wins)} winners drew down >=10c from entry before recovering.")

    print("\nDone.")


if __name__ == "__main__":
    main()
