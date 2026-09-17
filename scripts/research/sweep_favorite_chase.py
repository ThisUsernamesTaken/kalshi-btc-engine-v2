"""Parameter sweep for the favorite-chase rule.

Reads the per-trade JSONL produced by `backtest_favorite_chase.py`,
enriches each trade with:
  - strike (from market_dim.raw_json.floor_strike)
  - BTC spot at entry (latest spot_quote_event.mid for coinbase/BTC-USD <= entry_ts)
  - signed distance: spot - strike  (positive when spot is above strike)

Then sweeps:
  - min |spot - strike| in dollars (entry filter)
  - side filter (all / NO only / YES only)
  - sizing rule (flat 1ct / price-cap / distance-bucket scaling)

Prints the top 20 combinations by net cents.

Run with no args to sweep the standard JSONL. Pure read-only.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from kalshi_btc_engine_v2.policy.edge import kalshi_taker_fee_cents  # noqa: E402

DEFAULT_DB = r"D:\Trading\kalshi-btc-engine-v2\data\burnin_holdpure_2026_05_12.sqlite"
DEFAULT_JSONL = r"C:\Trading\kalshi-btc-engine-v2\data\_favorite_chase_bt.jsonl"


@dataclass
class EnrichedTrade:
    ticker: str
    side: str
    entry_price: float       # dollars
    exit_price: float        # dollars
    exit_reason: str         # 'stop' | 'settle_win' | 'settle_loss'
    entry_ts_ms: int
    exit_ts_ms: int
    strike: float            # USD price
    spot_at_entry: float     # USD mid (coinbase)
    signed_distance: float   # spot - strike
    abs_distance: float


def parse_iso_ms(s: str) -> int:
    import datetime as dt

    s = s.replace(" ", "T")
    if not s.endswith("Z") and "+" not in s:
        s = s + "+00:00"
    elif s.endswith("Z"):
        s = s.replace("Z", "+00:00")
    return int(dt.datetime.fromisoformat(s).timestamp() * 1000)


def load_strikes(con: sqlite3.Connection) -> dict[str, float]:
    out: dict[str, float] = {}
    cur = con.execute(
        "SELECT ticker, raw_json FROM market_dim WHERE ticker LIKE 'KXBTC15M-%'"
    )
    for ticker, raw in cur:
        if not raw:
            continue
        try:
            j = json.loads(raw)
            s = j.get("floor_strike")
            if s is not None:
                out[str(ticker)] = float(s)
        except Exception:
            continue
    return out


def latest_spot(con: sqlite3.Connection, ts_ms: int) -> float | None:
    """Latest BTC mid from coinbase at or before ts_ms."""
    r = con.execute(
        """
        SELECT mid FROM spot_quote_event
        WHERE venue = 'coinbase' AND symbol = 'BTC-USD'
          AND received_ts_ms <= ?
        ORDER BY received_ts_ms DESC
        LIMIT 1
        """,
        (ts_ms,),
    ).fetchone()
    if not r:
        return None
    try:
        return float(r[0])
    except Exception:
        return None


def trade_pnl(
    side: str, entry_price: float, exit_price: float, exit_reason: str, qty: int
) -> int:
    """Cents net PnL for ``qty`` contracts."""
    entry_c = int(round(entry_price * 100))
    exit_c = int(round(exit_price * 100))
    gross = (exit_c - entry_c) * qty
    entry_fee = kalshi_taker_fee_cents(entry_c, count=qty)
    exit_fee = kalshi_taker_fee_cents(exit_c, count=qty) if exit_reason == "stop" else 0
    return gross - entry_fee - exit_fee


def enrich(jsonl_path: Path, db: Path) -> list[EnrichedTrade]:
    con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    strikes = load_strikes(con)
    out: list[EnrichedTrade] = []
    missing_strike = 0
    missing_spot = 0
    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                continue
            ticker = t["ticker"]
            strike = strikes.get(ticker)
            if strike is None:
                missing_strike += 1
                continue
            entry_ts_ms = parse_iso_ms(t["entry_trigger_ts"])
            spot = latest_spot(con, entry_ts_ms)
            if spot is None:
                missing_spot += 1
                continue
            d = spot - strike
            out.append(
                EnrichedTrade(
                    ticker=ticker,
                    side=t["side"],
                    entry_price=float(t["entry_price"]),
                    exit_price=float(t["exit_price"]),
                    exit_reason=t["exit_reason"],
                    entry_ts_ms=entry_ts_ms,
                    exit_ts_ms=parse_iso_ms(t["exit_ts"]),
                    strike=strike,
                    spot_at_entry=spot,
                    signed_distance=d,
                    abs_distance=abs(d),
                )
            )
    con.close()
    print(f"loaded {len(out)} enriched trades  (missing_strike={missing_strike}  missing_spot={missing_spot})", file=sys.stderr)
    return out


# ----- sizing rules -----


def size_flat(et: EnrichedTrade) -> int:
    return 1


def size_skip_above_90c(et: EnrichedTrade) -> int:
    return 0 if et.entry_price >= 0.90 else 1


def size_skip_above_85c(et: EnrichedTrade) -> int:
    return 0 if et.entry_price >= 0.85 else 1


def size_by_distance_bucket(et: EnrichedTrade) -> int:
    """Larger size when farther from strike (stronger directional bias)."""
    d = et.abs_distance
    if d < 25:
        return 1
    if d < 75:
        return 2
    if d < 150:
        return 3
    return 4


def size_inverse_price(et: EnrichedTrade) -> int:
    """Risk-equalise: smaller size on expensive fills."""
    cost = max(0.50, et.entry_price)
    return max(1, int(round(0.50 / cost * 2)))  # ~2 at 0.50, ~1 at 1.00


SIZING_RULES = {
    "flat_1": size_flat,
    "skip>=85c": size_skip_above_85c,
    "skip>=90c": size_skip_above_90c,
    "scale_by_dist": size_by_distance_bucket,
    "scale_inv_price": size_inverse_price,
}


# ----- filter dimensions -----

MIN_DIST_THRESHOLDS = [0.0, 25.0, 100.0, 200.0]
SIDE_FILTERS = ["all", "yes", "no"]
MAX_ENTRY_PRICE = [1.01, 0.95, 0.90, 0.85, 0.80]  # 1.01 = no cap


def evaluate_combo(
    trades: list[EnrichedTrade],
    min_dist: float,
    side: str,
    sizing_name: str,
    max_entry_price: float,
) -> dict:
    sizer = SIZING_RULES[sizing_name]
    n = 0
    wins = 0
    losses = 0
    net = 0
    for t in trades:
        if t.abs_distance < min_dist:
            continue
        if t.entry_price > max_entry_price:
            continue
        if side != "all" and t.side != side:
            continue
        qty = sizer(t)
        if qty <= 0:
            continue
        pnl = trade_pnl(t.side, t.entry_price, t.exit_price, t.exit_reason, qty)
        net += pnl
        n += 1
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1
    return {
        "min_dist": min_dist,
        "side": side,
        "sizing": sizing_name,
        "max_p": max_entry_price,
        "n": n,
        "wins": wins,
        "losses": losses,
        "wr": (wins / n) if n else 0.0,
        "net_cents": net,
        "avg_cents": (net / n) if n else 0.0,
    }


def run_sweep(trades: list[EnrichedTrade]) -> list[dict]:
    out = []
    for d in MIN_DIST_THRESHOLDS:
        for side in SIDE_FILTERS:
            for sizing in SIZING_RULES:
                for max_p in MAX_ENTRY_PRICE:
                    out.append(evaluate_combo(trades, d, side, sizing, max_p))
    out.sort(key=lambda r: -r["net_cents"])
    return out


def print_report(trades: list[EnrichedTrade], rows: list[dict], top_k: int = 25) -> None:
    print()
    print(f"=== sweep over {len(trades)} enriched trades ===")
    print()
    print(f"{'min_d':>6} {'side':>4} {'sizing':>16} {'max_p':>6}  n={'':>4} wins={'':>4} WR={'':>5}  net={'':>7}  avg={'':>6}")
    print("-" * 88)
    for r in rows[:top_k]:
        print(
            f"{r['min_dist']:>6.0f} {r['side']:>4} {r['sizing']:>16} {r['max_p']:>6.2f}  "
            f"n={r['n']:>4} wins={r['wins']:>4} WR={r['wr']:>5.1%}  "
            f"net={r['net_cents']:>+7d}c  avg={r['avg_cents']:>+6.2f}c"
        )
    print()
    print("baseline (min_dist=0, all sides, flat_1, max_p=1.01) for reference:")
    base = next(r for r in rows if r["min_dist"] == 0 and r["side"] == "all" and r["sizing"] == "flat_1" and r["max_p"] == 1.01)
    print(
        f"  n={base['n']}  wins={base['wins']}  WR={base['wr']:.1%}  "
        f"net={base['net_cents']:+d}c  avg={base['avg_cents']:+.2f}c"
    )
    print()
    # Distance distribution to confirm thresholds make sense
    by_bucket: dict[str, list[EnrichedTrade]] = {}
    bucket_edges = [(0, 10), (10, 25), (25, 50), (50, 100), (100, 200), (200, math.inf)]
    for lo, hi in bucket_edges:
        sub = [t for t in trades if lo <= t.abs_distance < hi]
        if not sub:
            continue
        n = len(sub)
        net = sum(
            trade_pnl(t.side, t.entry_price, t.exit_price, t.exit_reason, 1) for t in sub
        )
        w = sum(
            1 for t in sub if trade_pnl(t.side, t.entry_price, t.exit_price, t.exit_reason, 1) > 0
        )
        print(
            f"  |dist| [{lo:>3.0f},{hi:>4.0f})  n={n:>4}  wins={w:>4}  WR={w/n:>5.1%}  "
            f"net={net:>+5d}c  avg={net/n:>+5.2f}c"
        )
    print()
    print("side x distance heatmap (flat_1 contract):")
    print(f"  {'':>14} | {'YES':>16} | {'NO':>16}")
    for lo, hi in bucket_edges:
        line = f"  |d|[{lo:>3.0f},{hi:>4.0f}) | "
        for side in ("yes", "no"):
            sub = [t for t in trades if lo <= t.abs_distance < hi and t.side == side]
            if not sub:
                line += f"{'-':>16} | "
                continue
            n = len(sub)
            net = sum(
                trade_pnl(t.side, t.entry_price, t.exit_price, t.exit_reason, 1) for t in sub
            )
            w = sum(
                1 for t in sub
                if trade_pnl(t.side, t.entry_price, t.exit_price, t.exit_reason, 1) > 0
            )
            line += f"n={n:>3} {w/n:>4.0%} {net:>+5d}c | "
        print(line)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--jsonl", type=Path, default=Path(DEFAULT_JSONL))
    p.add_argument("--db", type=Path, default=Path(DEFAULT_DB))
    p.add_argument("--top-k", type=int, default=25)
    args = p.parse_args()

    trades = enrich(args.jsonl, args.db)
    rows = run_sweep(trades)
    print_report(trades, rows, top_k=args.top_k)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
