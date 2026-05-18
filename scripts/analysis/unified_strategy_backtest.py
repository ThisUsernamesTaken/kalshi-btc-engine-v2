"""
Unified strategy backtest — combines V5 late-favorite (enhanced) + Variant F
early-contrarian, across all 2,819 KXBTC15M markets.

== LEG 1: Late Favorite (V5 enhanced, sizing-tiered) ==

After minute 12 (= last 3 minutes), if either side trades to >=80c (cap 99c):
  Compute BTC velocity over prior 60s (= prior 1m bar move).
  Compute cushion = abs(BTC_now - strike) in dollars.

  Tier MOVING_BIG    : |v60| > $10 AND cushion >= $40 → BUY FAVORITE 10ct
  Tier MOVING_SMALL  : |v60| > $10 AND cushion <  $40 → BUY FAVORITE 5ct
  Tier FLAT_BIG      : |v60| <=$10 AND cushion >= $40 → BUY FAVORITE 5ct
  Tier FLAT_SMALL    : |v60| <=$10 AND cushion <  $40 → FLIP CONTRARIAN
                       → BUY CHEAP SIDE at ~20c, 3ct

  Hold to settlement.

== LEG 2: Early Contrarian (Variant F) ==

In the first 3 minutes (candle minute_idx 0..2):
  If one side hits 80c AND the OTHER side was >= 40c earlier in same window
  (contested), BUY the OTHER (cheap) side at its ask. 2ct.
  One early entry per window max.
  Hold to settlement.

The two legs are independent — both may fire in the same window (but the
late leg gates on "not already entered via early leg" so we don't double-up).
Per spec the user wants both fully evaluated; we'll report independently then
combined.

Fee: ceil(0.07 * P * (1-P) * 100) / 100 per contract (entry). Settlement free.

Data:
  btc-bias-engine/data/kalshi_external_backtest.db
    markets(ticker, open_ts, close_ts, result, raw_json)
    candles(ticker, end_period_ts, yes_bid_close, yes_ask_close, raw_json)
      raw_json holds yes_ask, yes_bid, price each with open/high/low/close
    btc_1m(open_ts, open, high, low, close, volume)
"""
from __future__ import annotations

import json
import math
import sqlite3
import statistics
from dataclasses import dataclass, field
from pathlib import Path

DB = Path("C:/Trading/btc-bias-engine/data/kalshi_external_backtest.db")
OUT_MD = Path("C:/Trading/kalshi-btc-engine-v2/data/unified_strategy_backtest.md")

# Strategy params (mirror spec)
LATE_MIN_MINUTE = 12          # minute_idx >= 12 means last 3 candles (12,13,14)
LATE_TRIGGER = 0.80           # favorite must hit >= 80c
LATE_PRICE_CAP = 0.99         # raise cap from 95c to 99c
EARLY_MAX_MINUTE = 2          # candle minute_idx 0..2 = first 3 minutes
EARLY_TRIGGER = 0.80
EARLY_CONTESTED_MIN = 0.40    # other side must have reached >= 40c earlier
VELOCITY_THRESHOLD = 10.0     # |v60| > $10 → "moving"
CUSHION_THRESHOLD = 40.0      # cushion >= $40 → "big"

# Sizing
N_MOVING_BIG = 10
N_MOVING_SMALL = 5
N_FLAT_BIG = 5
N_FLAT_SMALL_CONTRARIAN = 3   # spec says 2-3; pick 3 to match max stake variant
N_EARLY = 2                   # spec says 2-3; pick 2

# V5 baseline params (for comparison)
V5_MIN_MINUTE = 12
V5_TRIGGER = 0.80
V5_CAP = 0.95
V5_CONTRACTS = 5


# ── helpers ───────────────────────────────────────────────────────────────


def fee(P: float, n: int = 1) -> float:
    """Kalshi taker fee: ceil(0.07 * n * P * (1-P) * 100) / 100. Returns dollars."""
    return math.ceil(0.07 * n * P * (1.0 - P) * 100.0) / 100.0


def fnum(x) -> float:
    return float(x) if x is not None else 0.0


@dataclass
class Candle:
    end_ts: int
    minute_idx: int  # 0..14
    ya_open: float
    ya_high: float
    ya_low: float
    ya_close: float
    yb_open: float
    yb_high: float
    yb_low: float
    yb_close: float
    p_open: float
    p_high: float
    p_low: float
    p_close: float


@dataclass
class Market:
    ticker: str
    open_ts: int
    close_ts: int
    result: str            # 'yes' or 'no'
    floor_strike: float    # BTC strike in $
    candles: list[Candle] = field(default_factory=list)


@dataclass
class Trade:
    ticker: str
    open_ts: int
    leg: str               # 'EARLY' or 'LATE'
    tier: str              # MOVING_BIG / MOVING_SMALL / FLAT_BIG / FLAT_SMALL / EARLY
    side: str              # 'YES' or 'NO'
    entry_minute: int
    entry_price: float
    contracts: int
    settled_yes: bool
    won: bool
    fee_paid: float
    net_pnl: float         # contracts * (1 - entry) win, contracts * (-entry) loss, minus fee


# ── data load ─────────────────────────────────────────────────────────────


def load_markets() -> list[Market]:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT ticker, open_ts, close_ts, result, raw_json FROM markets")
    mkts: dict[str, Market] = {}
    for row in cur.fetchall():
        rj = json.loads(row["raw_json"])
        mkts[row["ticker"]] = Market(
            ticker=row["ticker"],
            open_ts=row["open_ts"],
            close_ts=row["close_ts"],
            result=row["result"],
            floor_strike=float(rj.get("floor_strike", 0.0)),
        )
    cur.execute(
        "SELECT ticker, end_period_ts, raw_json FROM candles "
        "ORDER BY ticker, end_period_ts"
    )
    for row in cur.fetchall():
        m = mkts.get(row["ticker"])
        if m is None:
            continue
        mi = (row["end_period_ts"] - m.open_ts - 60) // 60
        if mi < 0 or mi > 14:
            continue
        d = json.loads(row["raw_json"])
        ya = d.get("yes_ask", {}) or {}
        yb = d.get("yes_bid", {}) or {}
        pr = d.get("price", {}) or {}
        m.candles.append(Candle(
            end_ts=row["end_period_ts"],
            minute_idx=int(mi),
            ya_open=fnum(ya.get("open_dollars")),
            ya_high=fnum(ya.get("high_dollars")),
            ya_low=fnum(ya.get("low_dollars")),
            ya_close=fnum(ya.get("close_dollars")),
            yb_open=fnum(yb.get("open_dollars")),
            yb_high=fnum(yb.get("high_dollars")),
            yb_low=fnum(yb.get("low_dollars")),
            yb_close=fnum(yb.get("close_dollars")),
            p_open=fnum(pr.get("open_dollars")),
            p_high=fnum(pr.get("high_dollars")),
            p_low=fnum(pr.get("low_dollars")),
            p_close=fnum(pr.get("close_dollars")),
        ))
    for m in mkts.values():
        m.candles.sort(key=lambda c: c.minute_idx)
    conn.close()
    return sorted(mkts.values(), key=lambda m: m.open_ts)


def load_btc_1m() -> dict[int, tuple[float, float, float, float]]:
    """Return {open_ts -> (open, high, low, close)} where open_ts is the
    1-min bar's start timestamp."""
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT open_ts, open, high, low, close FROM btc_1m")
    out = {row[0]: (row[1], row[2], row[3], row[4]) for row in cur.fetchall()}
    conn.close()
    return out


# ── strategy ──────────────────────────────────────────────────────────────


def make_trade(
    ticker: str, open_ts: int, leg: str, tier: str, side: str,
    entry_minute: int, entry_price: float, contracts: int, market_result: str,
) -> Trade:
    settled_yes = (market_result == "yes")
    won = (side == "YES" and settled_yes) or (side == "NO" and not settled_yes)
    fp = fee(entry_price, contracts)
    if won:
        gross = contracts * (1.0 - entry_price)
    else:
        gross = -contracts * entry_price
    return Trade(
        ticker=ticker, open_ts=open_ts, leg=leg, tier=tier, side=side,
        entry_minute=entry_minute, entry_price=entry_price,
        contracts=contracts, settled_yes=settled_yes, won=won,
        fee_paid=fp, net_pnl=gross - fp,
    )


def btc_velocity_60s(open_ts: int, candle_idx: int,
                     btc: dict[int, tuple[float, float, float, float]]) -> float | None:
    """BTC close delta over the prior 1-min bar (candle's previous minute).

    The candle with minute_idx=k covers BTC bar starting open_ts + k*60.
    Velocity = bar[k].close - bar[k-1].close. If k=0 or bars missing → None.
    """
    if candle_idx <= 0:
        return None
    cur_bar_ts = open_ts + candle_idx * 60
    prev_bar_ts = cur_bar_ts - 60
    cur = btc.get(cur_bar_ts)
    prev = btc.get(prev_bar_ts)
    if cur is None or prev is None:
        return None
    return cur[3] - prev[3]


def btc_at_minute(open_ts: int, candle_idx: int,
                  btc: dict[int, tuple[float, float, float, float]]) -> float | None:
    """BTC close at the bar starting at open_ts + candle_idx*60."""
    bar = btc.get(open_ts + candle_idx * 60)
    return bar[3] if bar else None


# Late leg
def run_late_leg(
    m: Market,
    btc: dict[int, tuple[float, float, float, float]],
    skip_if_early_entered: bool = True,
    early_entered: bool = False,
) -> Trade | None:
    """Walk candles minute_idx >= 12. First side to hit >=80c (price.high gate)
    triggers; sizing tiers per velocity + cushion. If FLAT_SMALL → flip to the
    cheap side at its ask."""
    if skip_if_early_entered and early_entered:
        return None

    for c in m.candles:
        if c.minute_idx < LATE_MIN_MINUTE:
            continue
        # Determine which side hit (require both quote and trade confirmation,
        # same guard as late_certainty_backtest).
        yes_hit = (c.ya_high >= LATE_TRIGGER) and (c.p_high >= LATE_TRIGGER)
        no_hit = (
            c.yb_low <= 1.0 - LATE_TRIGGER and c.p_low > 0
            and c.p_low <= 1.0 - LATE_TRIGGER
        )
        if not (yes_hit or no_hit):
            continue

        if yes_hit and no_hit:
            yes_excess = c.p_high - LATE_TRIGGER
            no_excess = (1.0 - c.p_low) - LATE_TRIGGER
            fav = "YES" if yes_excess >= no_excess else "NO"
        else:
            fav = "YES" if yes_hit else "NO"

        # Favorite entry price = trigger (limit). Bounded by cap 99c.
        fav_entry = min(max(LATE_TRIGGER, LATE_TRIGGER), LATE_PRICE_CAP)

        # Velocity + cushion at this candle.
        v60 = btc_velocity_60s(m.open_ts, c.minute_idx, btc)
        btc_now = btc_at_minute(m.open_ts, c.minute_idx, btc)
        if v60 is None or btc_now is None or m.floor_strike <= 0:
            # Not enough data — fall back to standard 5ct favorite.
            tier = "MOVING_SMALL"
            return make_trade(
                m.ticker, m.open_ts, "LATE", tier, fav,
                c.minute_idx, fav_entry, N_MOVING_SMALL, m.result,
            )

        moving = abs(v60) > VELOCITY_THRESHOLD
        cushion = abs(btc_now - m.floor_strike)
        big = cushion >= CUSHION_THRESHOLD

        if moving and big:
            tier = "MOVING_BIG"
            n = N_MOVING_BIG
            return make_trade(
                m.ticker, m.open_ts, "LATE", tier, fav,
                c.minute_idx, fav_entry, n, m.result,
            )
        if moving and not big:
            tier = "MOVING_SMALL"
            n = N_MOVING_SMALL
            return make_trade(
                m.ticker, m.open_ts, "LATE", tier, fav,
                c.minute_idx, fav_entry, n, m.result,
            )
        if not moving and big:
            tier = "FLAT_BIG"
            n = N_FLAT_BIG
            return make_trade(
                m.ticker, m.open_ts, "LATE", tier, fav,
                c.minute_idx, fav_entry, n, m.result,
            )
        # FLAT_SMALL: flip contrarian, buy the cheap side at ~20c.
        cheap = "NO" if fav == "YES" else "YES"
        # Cheap side ask ≈ 1 - favorite ask. Use 1 - trigger as the
        # observed proxy (i.e., 20c). This matches "buy CHEAP at ~20c".
        cheap_entry = 1.0 - LATE_TRIGGER  # 0.20
        tier = "FLAT_SMALL"
        return make_trade(
            m.ticker, m.open_ts, "LATE", tier, cheap,
            c.minute_idx, cheap_entry, N_FLAT_SMALL_CONTRARIAN, m.result,
        )

    return None


# Early leg
def run_early_leg(m: Market) -> Trade | None:
    """Walk candles in minute_idx 0..2. Track max yes/no traded so far.
    When one side hits 80c AND OTHER reached >= 40c in earlier candles
    (contested), buy the OTHER side at its ask. 2ct."""
    max_yes_traded = 0.0
    max_no_traded = 0.0

    for c in m.candles:
        if c.minute_idx > EARLY_MAX_MINUTE:
            break

        yes_hit = (c.ya_high >= EARLY_TRIGGER) and (c.p_high >= EARLY_TRIGGER)
        no_hit = (
            c.yb_low <= 1.0 - EARLY_TRIGGER and c.p_low > 0
            and c.p_low <= 1.0 - EARLY_TRIGGER
        )

        if yes_hit or no_hit:
            if yes_hit and no_hit:
                yes_ex = c.p_high - EARLY_TRIGGER
                no_ex = (1.0 - c.p_low) - EARLY_TRIGGER
                fav = "YES" if yes_ex >= no_ex else "NO"
            else:
                fav = "YES" if yes_hit else "NO"

            # contested check on the OTHER side from earlier candles
            other_max = max_no_traded if fav == "YES" else max_yes_traded
            if other_max >= EARLY_CONTESTED_MIN:
                # Buy the OTHER (cheap) side at ~ 1 - trigger ≈ 20c.
                cheap = "NO" if fav == "YES" else "YES"
                cheap_entry = 1.0 - EARLY_TRIGGER
                return make_trade(
                    m.ticker, m.open_ts, "EARLY", "EARLY", cheap,
                    c.minute_idx, cheap_entry, N_EARLY, m.result,
                )
            # not contested → keep watching (don't fire)

        # update max-traded tracker AFTER evaluating (uses prior candles only)
        if c.p_high > 0:
            max_yes_traded = max(max_yes_traded, c.p_high)
        if c.p_low > 0:
            max_no_traded = max(max_no_traded, 1.0 - c.p_low)

    return None


# Baseline V5
def run_v5_baseline(m: Market) -> Trade | None:
    for c in m.candles:
        if c.minute_idx < V5_MIN_MINUTE:
            continue
        yes_hit = (c.ya_high >= V5_TRIGGER) and (c.p_high >= V5_TRIGGER)
        no_hit = (
            c.yb_low <= 1.0 - V5_TRIGGER and c.p_low > 0
            and c.p_low <= 1.0 - V5_TRIGGER
        )
        if not (yes_hit or no_hit):
            continue
        if yes_hit and no_hit:
            yes_ex = c.p_high - V5_TRIGGER
            no_ex = (1.0 - c.p_low) - V5_TRIGGER
            fav = "YES" if yes_ex >= no_ex else "NO"
        else:
            fav = "YES" if yes_hit else "NO"
        # V5 entry at trigger price (limit), capped at 95c → trigger price is 80c.
        entry_price = V5_TRIGGER
        return make_trade(
            m.ticker, m.open_ts, "LATE_V5", "V5", fav,
            c.minute_idx, entry_price, V5_CONTRACTS, m.result,
        )
    return None


# ── metrics ───────────────────────────────────────────────────────────────


def trade_metrics(trades: list[Trade], label: str) -> dict:
    if not trades:
        return {
            "label": label, "n": 0, "wins": 0, "win_rate": 0.0,
            "total_net": 0.0, "avg_net_per_trade": 0.0,
            "avg_entry": 0.0, "max_dd": 0.0, "longest_loss_streak": 0,
            "yes_count": 0, "no_count": 0,
        }
    wins = sum(1 for t in trades if t.won)
    total_net = sum(t.net_pnl for t in trades)
    avg_entry = statistics.mean(t.entry_price for t in trades)
    eq = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in sorted(trades, key=lambda x: x.open_ts):
        eq += t.net_pnl
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
    cur_streak = 0
    longest = 0
    for t in sorted(trades, key=lambda x: x.open_ts):
        if not t.won:
            cur_streak += 1
            longest = max(longest, cur_streak)
        else:
            cur_streak = 0
    return {
        "label": label,
        "n": len(trades),
        "wins": wins,
        "win_rate": wins / len(trades),
        "total_net": total_net,
        "avg_net_per_trade": total_net / len(trades),
        "avg_entry": avg_entry,
        "max_dd": max_dd,
        "longest_loss_streak": longest,
        "yes_count": sum(1 for t in trades if t.side == "YES"),
        "no_count": sum(1 for t in trades if t.side == "NO"),
    }


def chrono_split(trades: list[Trade]) -> tuple[list[Trade], list[Trade]]:
    s = sorted(trades, key=lambda t: t.open_ts)
    mid = len(s) // 2
    return s[:mid], s[mid:]


# ── main ──────────────────────────────────────────────────────────────────


def run() -> str:
    print("Loading markets + candles ...", flush=True)
    markets = load_markets()
    print(f"  {len(markets)} markets", flush=True)
    print("Loading BTC 1m bars ...", flush=True)
    btc = load_btc_1m()
    print(f"  {len(btc)} btc 1m bars", flush=True)

    # Run each leg independently across all markets, then combine.
    early_trades: list[Trade] = []
    late_trades: list[Trade] = []
    v5_trades: list[Trade] = []

    for m in markets:
        # Need at least some late candles to be eligible.
        has_late = any(c.minute_idx >= LATE_MIN_MINUTE for c in m.candles)
        has_early = any(c.minute_idx <= EARLY_MAX_MINUTE for c in m.candles)

        et: Trade | None = None
        if has_early:
            et = run_early_leg(m)
            if et:
                early_trades.append(et)

        if has_late:
            lt = run_late_leg(
                m, btc, skip_if_early_entered=True, early_entered=(et is not None),
            )
            if lt:
                late_trades.append(lt)
            v5 = run_v5_baseline(m)
            if v5:
                v5_trades.append(v5)

    unified_trades = early_trades + late_trades

    # Per-tier breakdowns for late leg
    by_tier: dict[str, list[Trade]] = {}
    for t in late_trades:
        by_tier.setdefault(t.tier, []).append(t)

    # Metrics
    early_m = trade_metrics(early_trades, "EARLY (Variant F)")
    late_m = trade_metrics(late_trades, "LATE (V5 enhanced, all tiers)")
    unified_m = trade_metrics(unified_trades, "UNIFIED total")
    v5_m = trade_metrics(v5_trades, "V5 baseline (80c, min12, 5ct, cap 95c)")

    tier_metrics = {
        tier: trade_metrics(ts, f"LATE / {tier}") for tier, ts in by_tier.items()
    }

    # Train/test chronological split for unified
    train, test = chrono_split(unified_trades)
    train_m = trade_metrics(train, "UNIFIED train (older 50%)")
    test_m = trade_metrics(test, "UNIFIED test (newer 50%)")

    # Train/test for V5
    v5_train, v5_test = chrono_split(v5_trades)
    v5_train_m = trade_metrics(v5_train, "V5 train")
    v5_test_m = trade_metrics(v5_test, "V5 test")

    # Build markdown
    lines: list[str] = []
    lines.append("# Unified Strategy Backtest\n")
    lines.append(
        "**Source data**: `btc-bias-engine/data/kalshi_external_backtest.db`  "
        f"(2,819 KXBTC15M markets, {len(btc):,} BTC 1m bars)\n"
    )
    lines.append("## Strategy spec\n")
    lines.append(
        "- **LATE (Leg 1)**: minute_idx >= 12. First side to hit "
        f">={int(LATE_TRIGGER*100)}c (cap {int(LATE_PRICE_CAP*100)}c) triggers.\n"
        f"  - MOVING_BIG    (|v60|>${VELOCITY_THRESHOLD:.0f} & cushion>=${CUSHION_THRESHOLD:.0f}) → buy favorite {N_MOVING_BIG}ct\n"
        f"  - MOVING_SMALL  (|v60|>${VELOCITY_THRESHOLD:.0f} & cushion<${CUSHION_THRESHOLD:.0f})  → buy favorite {N_MOVING_SMALL}ct\n"
        f"  - FLAT_BIG      (|v60|<=${VELOCITY_THRESHOLD:.0f} & cushion>=${CUSHION_THRESHOLD:.0f}) → buy favorite {N_FLAT_BIG}ct\n"
        f"  - FLAT_SMALL    (|v60|<=${VELOCITY_THRESHOLD:.0f} & cushion<${CUSHION_THRESHOLD:.0f})  → FLIP to cheap @ ~{int((1-LATE_TRIGGER)*100)}c, {N_FLAT_SMALL_CONTRARIAN}ct\n"
        f"- **EARLY (Leg 2)**: candle minute_idx 0..{EARLY_MAX_MINUTE}. One side hits "
        f">={int(EARLY_TRIGGER*100)}c AND other side was >="
        f"{int(EARLY_CONTESTED_MIN*100)}c earlier → buy cheap @ ~{int((1-EARLY_TRIGGER)*100)}c, {N_EARLY}ct.\n"
        "- **Fee**: `ceil(0.07 * P * (1-P) * 100) / 100` per contract; settlement free.\n"
        "- **Hold**: every trade held to settlement.\n"
        "- Velocity proxy = BTC 1m close[k] - close[k-1] at the candle's minute.\n"
        "- Cushion = abs(BTC close - floor_strike).\n"
        "- If the early leg enters, the late leg skips (no double-up per window).\n"
    )

    def md_block(m: dict) -> str:
        if m["n"] == 0:
            return f"- **{m['label']}**: 0 trades\n"
        return (
            f"- **{m['label']}**: n={m['n']}, "
            f"wins={m['wins']} ({m['win_rate']*100:.1f}%), "
            f"net=**${m['total_net']:+,.2f}**, "
            f"avg/trade=${m['avg_net_per_trade']:+.4f}, "
            f"avg_entry={m['avg_entry']:.3f}, "
            f"yes/no={m['yes_count']}/{m['no_count']}, "
            f"max_dd=${m['max_dd']:.2f}, "
            f"longest_loss_streak={m['longest_loss_streak']}\n"
        )

    lines.append("## Top-line results (all markets)\n")
    lines.append(md_block(unified_m))
    lines.append(md_block(early_m))
    lines.append(md_block(late_m))
    lines.append(md_block(v5_m))

    lines.append("\n## Late-leg per-tier breakdown\n")
    for tier in ("MOVING_BIG", "MOVING_SMALL", "FLAT_BIG", "FLAT_SMALL"):
        if tier in tier_metrics:
            lines.append(md_block(tier_metrics[tier]))
        else:
            lines.append(f"- **LATE / {tier}**: 0 trades\n")

    lines.append("\n## 50/50 chronological train/test split (UNIFIED)\n")
    lines.append(md_block(train_m))
    lines.append(md_block(test_m))

    lines.append("\n## V5 baseline same split (for comparison)\n")
    lines.append(md_block(v5_train_m))
    lines.append(md_block(v5_test_m))

    # Per-tier in train+test
    lines.append("\n## Late-leg per-tier — train half\n")
    train_late = [t for t in train if t.leg == "LATE"]
    train_by_tier: dict[str, list[Trade]] = {}
    for t in train_late:
        train_by_tier.setdefault(t.tier, []).append(t)
    for tier in ("MOVING_BIG", "MOVING_SMALL", "FLAT_BIG", "FLAT_SMALL"):
        if tier in train_by_tier:
            lines.append(md_block(trade_metrics(train_by_tier[tier], f"train / {tier}")))
    lines.append("\n## Late-leg per-tier — test half\n")
    test_late = [t for t in test if t.leg == "LATE"]
    test_by_tier: dict[str, list[Trade]] = {}
    for t in test_late:
        test_by_tier.setdefault(t.tier, []).append(t)
    for tier in ("MOVING_BIG", "MOVING_SMALL", "FLAT_BIG", "FLAT_SMALL"):
        if tier in test_by_tier:
            lines.append(md_block(trade_metrics(test_by_tier[tier], f"test / {tier}")))

    # Comparison summary
    lines.append("\n## Unified vs V5 baseline summary\n")
    diff = unified_m["total_net"] - v5_m["total_net"]
    lines.append(
        f"- Unified total net **${unified_m['total_net']:+,.2f}** "
        f"({unified_m['n']} trades, {unified_m['win_rate']*100:.1f}% WR)\n"
    )
    lines.append(
        f"- V5 baseline net   **${v5_m['total_net']:+,.2f}** "
        f"({v5_m['n']} trades, {v5_m['win_rate']*100:.1f}% WR)\n"
    )
    lines.append(f"- **Unified - V5 = ${diff:+,.2f}**\n")

    # Sample trades for sanity check
    lines.append("\n## Sample trades (first 5 of each leg)\n")
    lines.append("\n### Early leg samples\n")
    for t in early_trades[:5]:
        lines.append(
            f"- {t.ticker}  side={t.side} entry={t.entry_price:.3f} "
            f"ct={t.contracts} won={t.won} net={t.net_pnl:+.4f}\n"
        )
    lines.append("\n### Late leg samples\n")
    for t in late_trades[:5]:
        lines.append(
            f"- {t.ticker}  tier={t.tier} side={t.side} m={t.entry_minute} "
            f"entry={t.entry_price:.3f} ct={t.contracts} won={t.won} "
            f"net={t.net_pnl:+.4f}\n"
        )

    text = "".join(lines)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text(text, encoding="utf-8")
    print(f"Wrote {OUT_MD}", flush=True)
    return text


if __name__ == "__main__":
    out = run()
    print()
    print(out)
