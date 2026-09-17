"""Backtest the proposed favorite-chase variants on captured KXBTC15M data.

Two families, run in a single DB pass (per market: load trades + L2 once,
simulate every variant in memory).

FAVORITE family (Variant B + baseline) — enter the first side to reach a
trigger price after T+8min, fill at the next ask:
  fav_t75_stop50   trigger .75, stop .50, else hold-to-settle   [= live baseline]
  fav_t70_stop50   trigger .70
  fav_t65_stop50   trigger .65
  fav_t60_stop50   trigger .60
  fav_t75_hold     trigger .75, no stop, hold-to-settle
  fav_t60_hold     trigger .60, no stop
  fav_t75_tp90     trigger .75, stop .50, take-profit .90
  fav_t60_tp90     trigger .60, stop .50, take-profit .90

FADE family (Variant A) — at T+8min buy the UNDERDOG (cheaper) side at its
ask if that ask is within [floor, cap]; bet on a late inversion:
  fade_cap60_hold  underdog ask <= .60, hold-to-settle
  fade_cap50_hold  underdog ask <= .50
  fade_cap40_hold  underdog ask <= .40
  fade_cap60_tp90  underdog ask <= .60, take-profit .90
  fade_cap60_tp75  underdog ask <= .60, take-profit .75

Favorite entries above 0.95 ask are skipped (no reward room). Read-only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from kalshi_btc_engine_v2.policy.edge import kalshi_taker_fee_cents  # noqa: E402

DEFAULT_DB = r"D:\Trading\kalshi-btc-engine-v2\data\burnin_holdpure_2026_05_12.sqlite"
ENTRY_AFTER_MS = 8 * 60 * 1000
FAVORITE_MAX_ENTRY = 0.95


def parse_iso_to_ms(s: str | None) -> int | None:
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    return int(dt.datetime.fromisoformat(s).timestamp() * 1000)


def coerce_price(x: object) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v > 1.5:
        v /= 100.0
    return v


def ask_for_side(yb: float | None, ya: float | None, side: str) -> float | None:
    if side == "yes":
        return ya
    return None if yb is None else 1.0 - yb


def bid_for_side(yb: float | None, ya: float | None, side: str) -> float | None:
    if side == "yes":
        return yb
    return None if ya is None else 1.0 - ya


def mid_for_side(yb: float | None, ya: float | None, side: str) -> float | None:
    b = bid_for_side(yb, ya, side)
    a = ask_for_side(yb, ya, side)
    if b is None or a is None:
        return None
    return (b + a) / 2.0


def lookup_settlement(con: sqlite3.Connection, ticker: str) -> str | None:
    row = con.execute(
        "SELECT raw_json FROM kalshi_lifecycle_event "
        "WHERE market_ticker = ? AND status = 'determined' "
        "ORDER BY event_id DESC LIMIT 1",
        (ticker,),
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        j = json.loads(row[0])
        msg = j.get("msg", j)
        r = msg.get("result")
        if isinstance(r, str) and r.lower() in ("yes", "no"):
            return r.lower()
    except Exception:
        return None
    return None


def list_markets(con: sqlite3.Connection) -> list[tuple[str, int, int]]:
    rows = con.execute(
        "SELECT ticker, open_time, close_time FROM market_dim "
        "WHERE ticker LIKE 'KXBTC15M-%' AND open_time IS NOT NULL AND close_time IS NOT NULL"
    ).fetchall()
    out = []
    for tk, ot, ct in rows:
        om, cm = parse_iso_to_ms(ot), parse_iso_to_ms(ct)
        if om and cm:
            out.append((str(tk), om, cm))
    out.sort(key=lambda x: x[1])
    return out


def load_market(con: sqlite3.Connection, ticker: str):
    trades = []
    for ts, yp, np_ in con.execute(
        "SELECT COALESCE(exchange_ts_ms,received_ts_ms), yes_price, no_price "
        "FROM kalshi_trade_event WHERE market_ticker=? "
        "ORDER BY COALESCE(exchange_ts_ms,received_ts_ms), event_id",
        (ticker,),
    ):
        yp_c, np_c = coerce_price(yp), coerce_price(np_)
        if yp_c is None and np_c is not None:
            yp_c = 1.0 - np_c
        if np_c is None and yp_c is not None:
            np_c = 1.0 - yp_c
        trades.append((int(ts), yp_c, np_c))
    l2 = []
    for ts, yb, ya in con.execute(
        "SELECT COALESCE(exchange_ts_ms,received_ts_ms), best_yes_bid, best_yes_ask "
        "FROM kalshi_l2_event WHERE market_ticker=? "
        "ORDER BY COALESCE(exchange_ts_ms,received_ts_ms), event_id",
        (ticker,),
    ):
        l2.append((int(ts), coerce_price(yb), coerce_price(ya)))
    return trades, l2


# ---- entry models ----


def favorite_entry(trades, l2, open_ms, trigger, max_entry):
    """First side to print >= trigger after T+8m; fill at next ask. Returns
    (side, fill_ts, fill_price) or None."""
    after = open_ms + ENTRY_AFTER_MS
    trig_side = trig_ts = None
    for ts, yp, np_ in trades:
        if ts < after:
            continue
        if yp is not None and yp >= trigger:
            trig_side, trig_ts = "yes", ts
            break
        if np_ is not None and np_ >= trigger:
            trig_side, trig_ts = "no", ts
            break
    if trig_side is None:
        return None
    for ts, yb, ya in l2:
        if ts <= trig_ts:
            continue
        ask = ask_for_side(yb, ya, trig_side)
        if ask is not None and ask > 0:
            if ask > max_entry:
                return None
            return (trig_side, ts, ask)
    return None


def fade_entry(l2, open_ms, cap, floor):
    """At the first book >= T+8m, buy the cheaper side if its ask in [floor,cap].
    Returns (side, fill_ts, fill_price) or None."""
    after = open_ms + ENTRY_AFTER_MS
    for ts, yb, ya in l2:
        if ts < after:
            continue
        if yb is None or ya is None:
            continue
        yes_mid = (yb + ya) / 2.0
        if yes_mid <= 0.50:
            side, ask = "yes", ya
        else:
            side, ask = "no", 1.0 - yb
        if ask is None or ask <= 0:
            return None
        if floor <= ask <= cap:
            return (side, ts, ask)
        return None
    return None


def simulate_exit(l2, side, fill_ts, close_ms, settlement, stop, tp):
    """Walk L2 after fill. Returns (reason, exit_price, exit_ts)."""
    for ts, yb, ya in l2:
        if ts <= fill_ts:
            continue
        if ts > close_ms:
            break
        mid = mid_for_side(yb, ya, side)
        if mid is None:
            continue
        if tp is not None and mid >= tp:
            bid = bid_for_side(yb, ya, side)
            if bid is not None and bid > 0:
                return ("tp", bid, ts)
        if stop is not None and mid <= stop:
            bid = bid_for_side(yb, ya, side)
            if bid is not None and bid > 0:
                return ("stop", bid, ts)
    if settlement is None:
        return ("no_settle", None, close_ms)
    won = settlement == side
    return ("settle_win" if won else "settle_loss", 1.0 if won else 0.0, close_ms)


def pnl_cents(fill_price, exit_price, reason):
    entry_c = int(round(fill_price * 100))
    exit_c = int(round(exit_price * 100))
    gross = exit_c - entry_c
    entry_fee = kalshi_taker_fee_cents(entry_c, 1)
    exit_fee = kalshi_taker_fee_cents(exit_c, 1) if reason in ("tp", "stop") else 0
    return gross - entry_fee - exit_fee


# ---- variant definitions ----

VARIANTS = [
    # name,            family,    param,  exit: (stop, tp)
    ("fav_t75_stop50", "favorite", 0.75, (0.50, None)),
    ("fav_t70_stop50", "favorite", 0.70, (0.50, None)),
    ("fav_t65_stop50", "favorite", 0.65, (0.50, None)),
    ("fav_t60_stop50", "favorite", 0.60, (0.50, None)),
    ("fav_t75_hold",   "favorite", 0.75, (None, None)),
    ("fav_t60_hold",   "favorite", 0.60, (None, None)),
    ("fav_t75_tp90",   "favorite", 0.75, (0.50, 0.90)),
    ("fav_t60_tp90",   "favorite", 0.60, (0.50, 0.90)),
    ("fade_cap60_hold","fade",     0.60, (None, None)),
    ("fade_cap50_hold","fade",     0.50, (None, None)),
    ("fade_cap40_hold","fade",     0.40, (None, None)),
    ("fade_cap60_tp90","fade",     0.60, (None, 0.90)),
    ("fade_cap60_tp75","fade",     0.60, (None, 0.75)),
]


@dataclass
class Stat:
    n: int = 0
    wins: int = 0
    losses: int = 0
    net: int = 0
    no_entry: int = 0
    no_settle: int = 0
    by_reason: dict = field(default_factory=dict)
    entry_prices: list = field(default_factory=list)


def run(db: Path):
    con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    markets = list_markets(con)
    print(f"[{db.name}] {len(markets)} KXBTC15M markets", file=sys.stderr, flush=True)
    stats = {name: Stat() for name, *_ in VARIANTS}

    for i, (ticker, om, cm) in enumerate(markets, 1):
        if i % 50 == 0:
            print(f"  ... {i}/{len(markets)}", file=sys.stderr, flush=True)
        trades, l2 = load_market(con, ticker)
        if not l2:
            continue
        settlement = lookup_settlement(con, ticker)

        for name, family, param, (stop, tp) in VARIANTS:
            st = stats[name]
            if family == "favorite":
                entry = favorite_entry(trades, l2, om, param, FAVORITE_MAX_ENTRY)
            else:
                entry = fade_entry(l2, om, param, 0.02)
            if entry is None:
                st.no_entry += 1
                continue
            side, fill_ts, fill_price = entry
            reason, exit_price, _ = simulate_exit(l2, side, fill_ts, cm, settlement, stop, tp)
            if reason == "no_settle":
                st.no_settle += 1
                continue
            net = pnl_cents(fill_price, exit_price, reason)
            st.n += 1
            st.net += net
            st.entry_prices.append((fill_price, net))
            if net > 0:
                st.wins += 1
            elif net < 0:
                st.losses += 1
            st.by_reason[reason] = st.by_reason.get(reason, 0) + 1
    con.close()
    return stats


def report(stats):
    print()
    print(f"{'variant':<18} {'n':>4} {'wins':>5} {'WR':>6} {'net':>9} {'avg':>8}  exit-mix")
    print("-" * 92)
    rows = []
    for name, *_ in VARIANTS:
        st = stats[name]
        if st.n == 0:
            print(f"{name:<18}  (no trades)")
            continue
        wr = st.wins / st.n
        avg = st.net / st.n
        mix = " ".join(f"{k}={v}" for k, v in sorted(st.by_reason.items(), key=lambda x: -x[1]))
        rows.append((name, st, wr, avg, mix))
    # print favorites then fades, each sorted by net
    for fam in ("fav", "fade"):
        fam_rows = [r for r in rows if r[0].startswith(fam)]
        fam_rows.sort(key=lambda r: -r[1].net)
        for name, st, wr, avg, mix in fam_rows:
            print(
                f"{name:<18} {st.n:>4} {st.wins:>5} {wr:>5.1%} "
                f"{st.net:>+8d}c {avg:>+7.2f}c  {mix}"
            )
        print()
    # entry-price buckets for the widest fade — shows WHERE (if anywhere) it pays
    wide = stats.get("fade_cap60_hold")
    if wide and wide.entry_prices:
        print("fade_cap60_hold — P&L by underdog entry price (hold-to-settle):")
        buckets = [(0.0, 0.15), (0.15, 0.25), (0.25, 0.35), (0.35, 0.45), (0.45, 0.61)]
        for lo, hi in buckets:
            sub = [(p, net) for p, net in wide.entry_prices if lo <= p < hi]
            if not sub:
                continue
            n = len(sub)
            w = sum(1 for _, net in sub if net > 0)
            tot = sum(net for _, net in sub)
            print(f"  [{lo:.2f},{hi:.2f})  n={n:>4}  wins={w:>4}  WR={w/n:>5.1%}  net={tot:>+6d}c  avg={tot/n:>+6.2f}c")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=Path(DEFAULT_DB))
    args = ap.parse_args()
    stats = run(args.db)
    report(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
