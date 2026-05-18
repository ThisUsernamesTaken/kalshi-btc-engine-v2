"""
Contrarian backtest: when V5 (80c+ after minute 12) fires, buy the CHEAP side.

Logic:
- Trigger same as V5: in the first candle at minute_idx >= min_minute where
  one side hits the threshold (ya.high >= 0.80 with price.high confirmation
  for YES, yb.low <= 0.20 with price.low confirmation for NO).
- Buy the OPPOSITE side at the cheapest available price within the trigger
  candle (1 - yb.high for NO contrarian when YES favored;  ya.low for YES
  contrarian when NO favored). Hold to settlement.

Variants:
  A — Pure contrarian
  B — Contrarian only when BTC is within $50 of strike
  C — Contrarian only when cheap-side fill <= 15c
  D — Cheap-side <= 20c AND BTC within $25 of strike
  E — Paired hedge: 10 favorite + 2 contrarian, joint P&L
  F — Min-minute sweep: 10, 12, 13
  G — Strike-distance buckets

Data source: btc-bias-engine/data/kalshi_external_backtest.db
"""
from __future__ import annotations

import json
import math
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

DB = Path("C:/Trading/btc-bias-engine/data/kalshi_external_backtest.db")
OUT_MD = Path("C:/Trading/kalshi-btc-engine-v2/data/contrarian_backtest.md")


def fee(P: float, n: int = 1) -> float:
    """Kalshi fee: ceil(0.07 * n * P * (1-P) * 100) / 100 (total for n contracts)."""
    return math.ceil(0.07 * n * P * (1.0 - P) * 100.0) / 100.0


def fnum(x) -> float:
    return float(x) if x is not None else 0.0


# ---------- data structures ----------

@dataclass
class Candle:
    end_ts: int
    minute_idx: int
    ya_open: float; ya_high: float; ya_low: float; ya_close: float
    yb_open: float; yb_high: float; yb_low: float; yb_close: float
    p_open: float;  p_high: float;  p_low: float;  p_close: float


@dataclass
class Market:
    ticker: str
    open_ts: int
    close_ts: int
    result: str
    floor_strike: float
    candles: list[Candle] = field(default_factory=list)


@dataclass
class CTrade:
    ticker: str
    open_ts: int
    fav_side: str
    cont_side: str
    entry_minute: int
    fav_entry: float
    cont_entry: float
    fav_payout: float
    cont_payout: float
    fav_fee: float
    cont_fee: float
    fav_net_1c: float    # per contract
    cont_net_1c: float
    btc_close: float | None
    strike: float
    strike_dist: float | None  # abs(btc_close - strike) in dollars
    fav_win: bool


# ---------- loaders ----------

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
    cur.execute("SELECT ticker, end_period_ts, raw_json FROM candles ORDER BY ticker, end_period_ts")
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
            end_ts=row["end_period_ts"], minute_idx=int(mi),
            ya_open=fnum(ya.get("open_dollars")), ya_high=fnum(ya.get("high_dollars")),
            ya_low=fnum(ya.get("low_dollars")), ya_close=fnum(ya.get("close_dollars")),
            yb_open=fnum(yb.get("open_dollars")), yb_high=fnum(yb.get("high_dollars")),
            yb_low=fnum(yb.get("low_dollars")), yb_close=fnum(yb.get("close_dollars")),
            p_open=fnum(pr.get("open_dollars")), p_high=fnum(pr.get("high_dollars")),
            p_low=fnum(pr.get("low_dollars")), p_close=fnum(pr.get("close_dollars")),
        ))
    conn.close()
    return sorted(mkts.values(), key=lambda m: m.open_ts)


def load_btc_1m() -> dict[int, tuple[float, float, float, float]]:
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT open_ts, open, high, low, close FROM btc_1m")
    out = {row[0]: (row[1], row[2], row[3], row[4]) for row in cur.fetchall()}
    conn.close()
    return out


# ---------- trigger / entry ----------

def find_contrarian_entry(
    m: Market,
    threshold: float = 0.80,
    min_minute: int = 12,
    target_cheap: float | None = None,   # limit-order price on the cheap side
    btc_by_ts: dict | None = None,
    entry_model: str = "limit",          # "limit" or "snapshot"
) -> CTrade | None:
    """
    Trigger: same as V5 (first candle at minute_idx >= min_minute where a side
    hits `threshold` with trade-price confirmation).

    Entry models for the cheap side:
      - "limit": place a limit order at `target_cheap` (default 1-threshold = 0.20).
        Fills only if the favorite's bid reached (1 - target_cheap) within the
        trigger candle (book-gap noise filtered via the same p.high/p.low
        confirmation used for the favorite). Symmetric to V5's "limit at
        threshold" methodology for the favorite.
      - "snapshot": pay end-of-trigger-candle ask on cheap side
        (1 - yb.close for YES fav; ya.close for NO fav). Always fills given a
        clean quote. Approximates a market-buy at trigger moment.
    """
    if target_cheap is None:
        target_cheap = 1.0 - threshold
    fav_bid_required = 1.0 - target_cheap

    for c in m.candles:
        if c.minute_idx >= min_minute:
            yes_hit = (c.ya_high >= threshold) and (c.p_high >= threshold)
            no_hit  = (c.yb_low  <= 1.0 - threshold) and (c.p_low > 0 and c.p_low <= 1.0 - threshold)

            if not (yes_hit or no_hit):
                continue

            if yes_hit and no_hit:
                yes_excess = c.p_high - threshold
                no_excess = (1.0 - c.p_low) - threshold
                fav_side = "YES" if yes_excess >= no_excess else "NO"
            else:
                fav_side = "YES" if yes_hit else "NO"

            cont_side = "NO" if fav_side == "YES" else "YES"

            if entry_model == "limit":
                # Need the corresponding side of the book to have touched the limit price.
                # YES fav (buy NO at target): need yes_bid to reach fav_bid_required,
                #   AND a trade to have confirmed at that level (p.high >= fav_bid_required).
                # NO fav (buy YES at target): need yes_ask to drop to target_cheap,
                #   AND a trade at that level (p.low <= target_cheap).
                if cont_side == "NO":
                    if c.yb_high < fav_bid_required:
                        return None
                    if c.p_high < fav_bid_required:
                        return None
                else:
                    if c.ya_low > target_cheap:
                        return None
                    if c.p_low <= 0 or c.p_low > target_cheap:
                        return None
                cont_entry = target_cheap
            elif entry_model == "snapshot":
                if cont_side == "NO":
                    cont_entry = 1.0 - c.yb_close
                else:
                    cont_entry = c.ya_close
                if cont_entry < 0.01 or cont_entry > 0.99:
                    continue
            else:
                raise ValueError(f"unknown entry_model: {entry_model}")

            # BTC context for strike-distance analysis
            btc_close = None
            strike_dist = None
            if btc_by_ts is not None and m.floor_strike >= 1000:
                # bar covering the entry minute closes at open_ts + (mi+1)*60.
                # btc_1m is keyed by open_ts, which = end_ts - 60. So bar key = open_ts + mi*60.
                bar = btc_by_ts.get(m.open_ts + c.minute_idx * 60)
                if bar is not None:
                    btc_close = bar[3]
                    strike_dist = abs(btc_close - m.floor_strike)

            fav_win = (m.result == "yes") if fav_side == "YES" else (m.result == "no")
            fav_payout = 1.0 if fav_win else 0.0
            cont_payout = 0.0 if fav_win else 1.0

            fav_entry = threshold  # consistent w/ V5 methodology
            fav_fee_1c = fee(fav_entry, 1)
            cont_fee_1c = fee(cont_entry, 1)
            fav_net_1c = fav_payout - fav_entry - fav_fee_1c
            cont_net_1c = cont_payout - cont_entry - cont_fee_1c

            return CTrade(
                ticker=m.ticker, open_ts=m.open_ts,
                fav_side=fav_side, cont_side=cont_side,
                entry_minute=c.minute_idx,
                fav_entry=fav_entry, cont_entry=cont_entry,
                fav_payout=fav_payout, cont_payout=cont_payout,
                fav_fee=fav_fee_1c, cont_fee=cont_fee_1c,
                fav_net_1c=fav_net_1c, cont_net_1c=cont_net_1c,
                btc_close=btc_close, strike=m.floor_strike,
                strike_dist=strike_dist, fav_win=fav_win,
            )
    return None


# ---------- summarisers ----------

def summarise_contrarian(trades: list[CTrade], label: str, total_windows: int) -> dict:
    if not trades:
        return {"label": label, "n": 0, "windows": total_windows}
    wins = [t for t in trades if t.cont_payout > 0]
    win_rate = len(wins) / len(trades)
    avg_entry = statistics.mean(t.cont_entry for t in trades)
    avg_win_payout = statistics.mean(1.0 - t.cont_entry for t in wins) if wins else 0.0
    gross = sum(t.cont_payout - t.cont_entry for t in trades)
    fees = sum(t.cont_fee for t in trades)
    net = sum(t.cont_net_1c for t in trades)
    return {
        "label": label,
        "n": len(trades),
        "windows": total_windows,
        "trade_rate": len(trades) / total_windows if total_windows else 0.0,
        "win_rate": win_rate,
        "avg_entry": avg_entry,
        "avg_win_payout": avg_win_payout,  # avg $ profit per WIN before fees
        "gross_pnl": gross,
        "fees": fees,
        "net_pnl": net,
        "net_per_contract": net / len(trades),
        "ev_per_contract_vs_skip": net / len(trades),  # skipping has EV=0
    }


def summarise_paired(trades: list[CTrade], fav_n: int, cont_n: int, label: str) -> dict:
    """Paired position: fav_n contracts favorite + cont_n contrarian. P&L per session."""
    if not trades:
        return {"label": label, "n": 0}
    nets = []
    for t in trades:
        # Per contract: net_1c already includes fee_1c. But fees scale non-linearly with n.
        fav_fee_n = fee(t.fav_entry, fav_n)
        cont_fee_n = fee(t.cont_entry, cont_n)
        net = (fav_n * (t.fav_payout - t.fav_entry) - fav_fee_n
               + cont_n * (t.cont_payout - t.cont_entry) - cont_fee_n)
        nets.append(net)
    avg = statistics.mean(nets)
    wins = sum(1 for n in nets if n > 0)
    return {
        "label": label,
        "n": len(trades),
        "fav_n": fav_n, "cont_n": cont_n,
        "win_rate_positive": wins / len(trades),
        "avg_net_per_session": avg,
        "total_net": sum(nets),
        "stdev_net": statistics.stdev(nets) if len(nets) > 1 else 0.0,
        "min_net": min(nets),
        "max_net": max(nets),
        "median_net": statistics.median(nets),
    }


def chrono_split(trades: list[CTrade]) -> tuple[list[CTrade], list[CTrade]]:
    s = sorted(trades, key=lambda t: t.open_ts)
    mid = len(s) // 2
    return s[:mid], s[mid:]


def filter_trades(
    trades: list[CTrade],
    max_cheap: float | None = None,
    max_strike_dist: float | None = None,
    min_strike_dist: float | None = None,
) -> list[CTrade]:
    out = []
    for t in trades:
        if max_cheap is not None and t.cont_entry > max_cheap:
            continue
        if max_strike_dist is not None:
            if t.strike_dist is None or t.strike_dist > max_strike_dist:
                continue
        if min_strike_dist is not None:
            if t.strike_dist is None or t.strike_dist < min_strike_dist:
                continue
        out.append(t)
    return out


# ---------- Kelly ----------

def joint_kelly(p_fav: float, fav_entry: float, cont_entry: float,
                fav_fee: float, cont_fee: float, fav_n: int = 10) -> dict:
    """
    Given fav_n contracts of favorite, find contrarian h that maximises E[log(1+r)].
    Perfectly anti-correlated: when fav wins, cont loses (and vice versa).

    Per-session P&L:
      fav wins (p_fav):     fav_n*(1-fav_entry) - fav_fee*scale_f
                            + h*(-cont_entry) - cont_fee*scale_h
      fav loses (1-p_fav):  -fav_n*fav_entry - fav_fee*scale_f
                            + h*(1-cont_entry) - cont_fee*scale_h

    We approximate fees as fixed (1c-scale) for the optimisation since the
    integer ceil makes the closed form messy. Use unit capital = fav_n*fav_entry
    (the at-risk stake) as the bankroll denominator.
    """
    fav_fee_n = fee(fav_entry, fav_n)
    bankroll = fav_n * fav_entry + fav_fee_n  # cost of taking the favorite alone

    def session_net(h):
        cont_fee_n = fee(cont_entry, int(round(h))) if h >= 1 else 0.0
        if h < 0.5:
            cont_fee_n = 0.0
            h_eff = 0.0
        else:
            h_eff = h
        if h_eff == 0:
            win = fav_n * (1.0 - fav_entry) - fav_fee_n
            lose = -fav_n * fav_entry - fav_fee_n
            return win, lose
        win = fav_n * (1.0 - fav_entry) - fav_fee_n + h_eff * (-cont_entry) - cont_fee_n
        lose = -fav_n * fav_entry - fav_fee_n + h_eff * (1.0 - cont_entry) - cont_fee_n
        return win, lose

    # EV(h)
    def ev(h):
        w, l = session_net(h)
        return p_fav * w + (1 - p_fav) * l

    # Find h that maximises E[log(1 + net/bankroll)] over discrete h in 0..2*fav_n
    best_h = 0
    best_growth = -1e9
    growth_by_h = {}
    for h in range(0, 2 * fav_n + 1):
        w, l = session_net(h)
        cap = bankroll + (h * cont_entry + fee(cont_entry, h) if h > 0 else 0.0)
        if cap <= 0:
            continue
        # log-growth requires 1 + r > 0; clip losses
        r_w = w / cap
        r_l = l / cap
        if r_w <= -1 or r_l <= -1:
            growth_by_h[h] = float("-inf")
            continue
        g = p_fav * math.log(1 + r_w) + (1 - p_fav) * math.log(1 + r_l)
        growth_by_h[h] = g
        if g > best_growth:
            best_growth = g
            best_h = h

    return {
        "p_fav": p_fav,
        "fav_n": fav_n,
        "cont_entry": cont_entry,
        "bankroll_base": bankroll,
        "ev_h0": ev(0),
        "ev_h2": ev(2),
        "ev_h5": ev(5),
        "ev_h10": ev(10),
        "best_h": best_h,
        "best_log_growth": best_growth,
        "log_growth_h0": growth_by_h.get(0, 0),
        "ratio_best_to_fav": best_h / fav_n if fav_n else 0,
    }


# ---------- run ----------

def fmt_pct(x: float) -> str:
    return f"{x*100:.1f}%"


def fmt_money(x: float) -> str:
    return f"${x:+.2f}"


def gather_trades(markets, min_minute, target_cheap, btc, entry_model):
    trades = []
    elig = 0
    for m in markets:
        if not any(c.minute_idx >= min_minute for c in m.candles):
            continue
        elig += 1
        t = find_contrarian_entry(
            m, threshold=0.80, min_minute=min_minute,
            target_cheap=target_cheap, btc_by_ts=btc, entry_model=entry_model,
        )
        if t:
            trades.append(t)
    return trades, elig


def run_for_model(markets, btc, entry_model: str):
    print(f"\n###### entry_model = {entry_model} ######")
    triggers_by_min: dict[int, list[CTrade]] = {}
    eligible_windows_by_min: dict[int, int] = {}
    for min_minute in (10, 12, 13):
        trades, elig = gather_trades(markets, min_minute, 0.20, btc, entry_model)
        triggers_by_min[min_minute] = trades
        eligible_windows_by_min[min_minute] = elig
        print(f"  min={min_minute}: {len(trades)} fills (of {elig} eligible windows)")

    base = triggers_by_min[12]
    base_windows = eligible_windows_by_min[12]

    # A: pure contrarian on V5
    A = summarise_contrarian(base, "A: pure contrarian", base_windows)
    At, Av = chrono_split(base)
    A_train = summarise_contrarian(At, "A train", base_windows // 2)
    A_test = summarise_contrarian(Av, "A test", base_windows - base_windows // 2)

    # B: BTC within $50
    B_trades = filter_trades(base, max_strike_dist=50.0)
    B = summarise_contrarian(B_trades, "B: BTC<=$50", base_windows)
    Bt, Bv = chrono_split(B_trades)
    B_train = summarise_contrarian(Bt, "B train", len(Bt))
    B_test = summarise_contrarian(Bv, "B test", len(Bv))

    # C: cheap <= 15c. Limit model: re-run with target=0.15. Snapshot: filter base.
    if entry_model == "limit":
        C_trades, _ = gather_trades(markets, 12, 0.15, btc, entry_model)
    else:
        C_trades = filter_trades(base, max_cheap=0.15)
    C = summarise_contrarian(C_trades, "C: cheap<=15c", base_windows)
    Ct, Cv = chrono_split(C_trades)
    C_train = summarise_contrarian(Ct, "C train", len(Ct))
    C_test = summarise_contrarian(Cv, "C test", len(Cv))

    # D: cheap <= 20c AND BTC within $25
    if entry_model == "limit":
        D_trades = filter_trades(base, max_strike_dist=25.0)  # target already 0.20
    else:
        D_trades = filter_trades(base, max_cheap=0.20, max_strike_dist=25.0)
    D = summarise_contrarian(D_trades, "D: cheap<=20c & BTC<=$25", base_windows)
    Dt, Dv = chrono_split(D_trades)
    D_train = summarise_contrarian(Dt, "D train", len(Dt))
    D_test = summarise_contrarian(Dv, "D test", len(Dv))

    # E: paired 10+2 etc
    E_10_0  = summarise_paired(base, 10, 0,  "E0: 10 fav + 0 cont (V5 baseline)")
    E_10_2  = summarise_paired(base, 10, 2,  "E: 10 fav + 2 cont")
    E_10_5  = summarise_paired(base, 10, 5,  "E': 10 fav + 5 cont")
    E_10_10 = summarise_paired(base, 10, 10, "E'': 10 fav + 10 cont (arb)")

    # F: min-minute sweep
    F = {mm: summarise_contrarian(triggers_by_min[mm], f"F-min{mm}", eligible_windows_by_min[mm])
         for mm in (10, 12, 13)}

    # G: strike distance buckets
    buckets = [("0-25",0,25), ("25-50",25,50), ("50-100",50,100), ("100+",100,10**9)]
    G = {}
    for name, lo, hi in buckets:
        sub = [t for t in base if t.strike_dist is not None and lo <= t.strike_dist < hi]
        G[name] = summarise_contrarian(sub, f"G-{name}", len(sub))

    # Kelly (joint with 10-contract favorite). Use A's win-rate / entry.
    if A.get("n", 0) > 0:
        kelly = joint_kelly(
            p_fav=1.0 - A["win_rate"],
            fav_entry=0.80,
            cont_entry=A["avg_entry"],
            fav_fee=fee(0.80, 1),
            cont_fee=fee(A["avg_entry"], 1),
            fav_n=10,
        )
    else:
        kelly = None

    # ----- print to chat -----
    print()
    print(f"=== entry_model = {entry_model.upper()} ===")
    def line(s):
        if s.get("n", 0) == 0:
            return f"{s['label']:32s} n=0"
        return (f"{s['label']:32s} n={s['n']:>4d}  WR={fmt_pct(s['win_rate']):>6s}  "
                f"avgEntry={s['avg_entry']:.3f}  netPerCt={s['net_per_contract']:+.4f}  "
                f"tot1c={fmt_money(s['net_pnl'])}")

    for s in (A, B, C, D):
        print(line(s))
    print("-- E (paired sizing) --")
    for s in (E_10_0, E_10_2, E_10_5, E_10_10):
        if s.get("n", 0):
            print(f"{s['label']:32s} avgNet/sess={fmt_money(s['avg_net_per_session'])}  "
                  f"std={s['stdev_net']:.2f}  min={fmt_money(s['min_net'])}  "
                  f"max={fmt_money(s['max_net'])}  +sess%={fmt_pct(s['win_rate_positive']):>6s}")
    print("-- F (entry time sweep) --")
    for mm in (10, 12, 13):
        s = F[mm]; s["label"] = f"F-min{mm}"
        print(line(s))
    print("-- G (strike distance, pure contrarian) --")
    for name, _, _ in buckets:
        s = G[name]; s["label"] = f"G ${name}"
        print(line(s))
    print("-- train/test 50/50 --")
    for tr, te in ((A_train, A_test), (B_train, B_test), (C_train, C_test), (D_train, D_test)):
        if tr["n"] or te["n"]:
            tag = tr["label"].split()[0]
            print(f"  {tag}  TRAIN n={tr['n']:>4d} WR={fmt_pct(tr.get('win_rate',0)):>6s} "
                  f"netPerCt={tr.get('net_per_contract',0):+.4f}   "
                  f"TEST  n={te['n']:>4d} WR={fmt_pct(te.get('win_rate',0)):>6s} "
                  f"netPerCt={te.get('net_per_contract',0):+.4f}")
    if kelly:
        print("-- Kelly (joint w/ 10-ct favorite) --")
        print(f"  p(fav) = {kelly['p_fav']:.4f}, cont_entry = {A['avg_entry']:.3f}")
        print(f"  EV/sess: h=0 {fmt_money(kelly['ev_h0'])}  h=2 {fmt_money(kelly['ev_h2'])}  "
              f"h=5 {fmt_money(kelly['ev_h5'])}  h=10 {fmt_money(kelly['ev_h10'])}")
        print(f"  Kelly-optimal h = {kelly['best_h']}  ratio {kelly['ratio_best_to_fav']:.2f}")

    return {
        "model": entry_model, "base_windows": base_windows,
        "A": A, "B": B, "C": C, "D": D,
        "A_train": A_train, "A_test": A_test,
        "B_train": B_train, "B_test": B_test,
        "C_train": C_train, "C_test": C_test,
        "D_train": D_train, "D_test": D_test,
        "E_10_0": E_10_0, "E_10_2": E_10_2, "E_10_5": E_10_5, "E_10_10": E_10_10,
        "F": F, "G": G, "buckets": buckets, "kelly": kelly,
    }


def write_combined_report(results: dict, total_markets: int) -> None:
    """results: {'limit': {...}, 'snapshot': {...}}"""
    L = []
    L.append("# Contrarian Backtest — V5 Reversal Trade")
    L.append("")
    L.append(f"Universe: {total_markets} KXBTC15M markets from `kalshi_external_backtest.db`.")
    L.append("")
    L.append("## Strategy")
    L.append("")
    L.append("**Trigger:** Same as V5 favorite — first candle at minute_idx >= min_minute where")
    L.append("one side hits 80c with trade-price confirmation (`p.high >= 0.80` for YES, ")
    L.append("`p.low <= 0.20` for NO). Buy the OPPOSITE (cheap) side. Hold to settlement.")
    L.append("Fee = ceil(0.07·n·P·(1-P)·100)/100 total for n contracts.")
    L.append("")
    L.append("**Two entry models** are reported in parallel, because the realistic fill")
    L.append("price for the cheap side is methodology-sensitive:")
    L.append("")
    L.append("- **LIMIT** (mirror of V5 favorite methodology): place a limit at `1 - threshold`")
    L.append("  (0.20 for variants A/B/D, 0.15 for C). Fills only if the corresponding side")
    L.append("  of the book reached the limit level within the trigger candle (yes_bid.high")
    L.append("  ≥ 0.80 for NO contrarian; yes_ask.low ≤ 0.20 for YES contrarian), with the")
    L.append("  same trade-price confirmation as the V5 trigger.")
    L.append("- **SNAPSHOT** (market-buy approximation): pay end-of-trigger-candle ask")
    L.append("  (`1 - yb.close` for NO contrarian; `ya.close` for YES contrarian). Always")
    L.append("  fills given a clean quote. Captures cases where the cheap side has already")
    L.append("  extended below the 0.20 limit by the time you hit the button.")
    L.append("")
    L.append("Both models share the V5 trigger; they differ only in the cheap-side fill price.")
    L.append("")
    for model in ("limit", "snapshot"):
        r = results[model]
        L.append(f"## Model: {model.upper()}")
        L.append("")
        L.append(f"Eligible V5 windows (min 12 trigger): {r['base_windows']}.")
        L.append("")
        L.append("### Variant summary (pure contrarian)")
        L.append("")
        L.append("| Variant | n_fills | WinRate | AvgEntry | AvgWinPay | Net/Contract | Total 1c | Total 10c | EV vs skip |")
        L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for s in (r["A"], r["B"], r["C"], r["D"]):
            if s.get("n", 0):
                L.append(f"| {s['label']} | {s['n']} | {fmt_pct(s['win_rate'])} | "
                         f"{s['avg_entry']:.4f} | {s['avg_win_payout']:.4f} | "
                         f"{s['net_per_contract']:+.4f} | {fmt_money(s['net_pnl'])} | "
                         f"{fmt_money(s['net_pnl']*10)} | {s['ev_per_contract_vs_skip']:+.4f} |")
            else:
                L.append(f"| {s['label']} | 0 | — | — | — | — | — | — | — |")
        L.append("")
        L.append("### E — Paired hedge sizing (fav size = 10)")
        L.append("")
        L.append("| Sizing | n | +sess% | AvgNet/sess | StDev | Min | Max | Median |")
        L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for s in (r["E_10_0"], r["E_10_2"], r["E_10_5"], r["E_10_10"]):
            if s.get("n", 0):
                L.append(f"| {s['label']} | {s['n']} | {fmt_pct(s['win_rate_positive'])} | "
                         f"{fmt_money(s['avg_net_per_session'])} | {s['stdev_net']:.2f} | "
                         f"{fmt_money(s['min_net'])} | {fmt_money(s['max_net'])} | "
                         f"{fmt_money(s['median_net'])} |")
        L.append("")
        L.append("### F — Entry-time sweep")
        L.append("")
        L.append("| min_minute | n | WinRate | AvgEntry | Net/Contract | Total 1c |")
        L.append("|---|---:|---:|---:|---:|---:|")
        for mm in (10, 12, 13):
            s = r["F"][mm]
            if s.get("n", 0):
                L.append(f"| min {mm} | {s['n']} | {fmt_pct(s['win_rate'])} | "
                         f"{s['avg_entry']:.4f} | {s['net_per_contract']:+.4f} | "
                         f"{fmt_money(s['net_pnl'])} |")
        L.append("")
        L.append("### G — Strike-distance buckets (BTC distance from strike at entry)")
        L.append("")
        L.append("| Bucket | n | WinRate | AvgEntry | Net/Contract | EV vs skip |")
        L.append("|---|---:|---:|---:|---:|---:|")
        for name, _, _ in r["buckets"]:
            s = r["G"][name]
            if s.get("n", 0):
                L.append(f"| ${name} | {s['n']} | {fmt_pct(s['win_rate'])} | "
                         f"{s['avg_entry']:.4f} | {s['net_per_contract']:+.4f} | "
                         f"{s['ev_per_contract_vs_skip']:+.4f} |")
            else:
                L.append(f"| ${name} | 0 | — | — | — | — |")
        L.append("")
        L.append("### 50/50 train/test split")
        L.append("")
        L.append("| Variant | Half | n | WinRate | Net/Contract |")
        L.append("|---|---|---:|---:|---:|")
        for tr, te, name in (
            (r["A_train"], r["A_test"], "A"),
            (r["B_train"], r["B_test"], "B"),
            (r["C_train"], r["C_test"], "C"),
            (r["D_train"], r["D_test"], "D"),
        ):
            if tr["n"]:
                L.append(f"| {name} | TRAIN | {tr['n']} | {fmt_pct(tr['win_rate'])} | "
                         f"{tr['net_per_contract']:+.4f} |")
            if te["n"]:
                L.append(f"| {name} | TEST | {te['n']} | {fmt_pct(te['win_rate'])} | "
                         f"{te['net_per_contract']:+.4f} |")
        L.append("")
        if r["kelly"]:
            k = r["kelly"]
            L.append("### Kelly hedge sizing (joint with 10-contract favorite)")
            L.append("")
            L.append(f"- p(favorite wins) = {k['p_fav']:.4f}")
            L.append(f"- avg contrarian entry = {r['A']['avg_entry']:.4f}")
            L.append("")
            L.append("| h (cont contracts) | EV per session |")
            L.append("|---|---:|")
            L.append(f"| h=0 (no hedge) | {fmt_money(k['ev_h0'])} |")
            L.append(f"| h=2 | {fmt_money(k['ev_h2'])} |")
            L.append(f"| h=5 | {fmt_money(k['ev_h5'])} |")
            L.append(f"| h=10 (arb) | {fmt_money(k['ev_h10'])} |")
            L.append("")
            L.append(f"**Kelly-optimal h = {k['best_h']}** (ratio to favorite size = "
                     f"{k['ratio_best_to_fav']:.2f}).")
            L.append("")

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(L), encoding="utf-8")
    print(f"\nWrote: {OUT_MD}")


def run():
    print("Loading markets + candles...")
    markets = load_markets()
    print(f"  {len(markets)} markets")
    print("Loading BTC 1m...")
    btc = load_btc_1m()
    print(f"  {len(btc)} BTC 1m bars")

    results = {}
    for model in ("limit", "snapshot"):
        results[model] = run_for_model(markets, btc, model)

    write_combined_report(results, len(markets))


if __name__ == "__main__":
    run()
