"""Backtest derivatives microstructure signals against KXBTC15M outcomes.

Two horizons:
  H1 = Mar 25 - Apr 24 (full Kalshi DB). Features available: funding_rate, perp_premium.
       → Strategies A, F, (and weak G/H using just these two).
  H2 = recent ~48h (rubik window). Features available: account L/S, top trader L/S,
       OI/volume, taker spot/contract ratios, plus funding + perp_premium.
       But Kalshi outcomes NOT in DB for this window. We compute outcomes from
       OKX 15m perp/index candle direction.
       Entry price assumed neutral $0.50 (no Kalshi book).
       → Strategies B, C, D, E, F, G, H.

Output: kalshi-btc-engine-v2/data/derivatives_backtest_results.md
"""

import json
import math
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KALSHI_DB = Path("C:/Trading/btc-bias-engine/data/kalshi_external_backtest.db")
DER_DB = ROOT / "data" / "derivatives.sqlite"
OUT_MD = ROOT / "data" / "derivatives_backtest_results.md"


def fee_per_contract(p: float) -> float:
    """Kalshi-style fee: ceil(0.07 * P * (1-P) * 100) / 100"""
    return math.ceil(0.07 * p * (1.0 - p) * 100.0) / 100.0


# ----------------------------- H1 dataset (Mar-Apr) -----------------------------

def build_h1_dataset():
    """Per-market row with derivatives features, c1_yes_ask, outcome."""
    k = sqlite3.connect(KALSHI_DB)
    d = sqlite3.connect(DER_DB)

    # Cache funding rates and perp candles into memory
    fr = list(d.execute("SELECT ts, funding_rate FROM funding_rate ORDER BY ts"))
    pc = list(d.execute("SELECT ts, mark_close, idx_close FROM perp_candles_15m WHERE ts < ? ORDER BY ts", (1777048000000,)))
    # Build lookup arrays
    fr_ts = [r[0] for r in fr]
    fr_val = [r[1] for r in fr]
    pc_ts = [r[0] for r in pc]
    pc_mark = [r[1] for r in pc]
    pc_idx = [r[2] for r in pc]

    def last_le(ts_arr, vals, t):
        # binary search: greatest ts <= t
        lo, hi = 0, len(ts_arr) - 1
        ans = -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if ts_arr[mid] <= t:
                ans = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return ans

    rows = []
    markets = k.execute("SELECT ticker, open_ts, close_ts, result FROM markets WHERE status='finalized' ORDER BY open_ts").fetchall()
    for ticker, open_ts, close_ts, result in markets:
        open_ms = open_ts * 1000
        # entry yes_ask: first candle after open_ts (within 120s)
        row = k.execute(
            "SELECT yes_ask_close, yes_bid_close FROM candles WHERE ticker=? AND end_period_ts > ? AND end_period_ts <= ? ORDER BY end_period_ts LIMIT 1",
            (ticker, open_ts, open_ts + 120),
        ).fetchone()
        if not row:
            continue
        yes_ask, yes_bid = float(row[0] or 0), float(row[1] or 0)
        if yes_ask <= 0 or yes_ask >= 1.0:  # untradeable
            continue

        # funding (last <= open_ms)
        i = last_le(fr_ts, fr_val, open_ms)
        funding = fr_val[i] if i >= 0 else None

        # perp candle at or just before open (mark/index)
        i = last_le(pc_ts, pc_mark, open_ms)
        if i < 0:
            continue
        mark, idx = pc_mark[i], pc_idx[i]
        if mark is None or idx is None or idx <= 0:
            continue
        perp_premium_bps = (mark - idx) / idx * 1e4

        # premium delta vs 1h ago
        i_1h = last_le(pc_ts, pc_mark, open_ms - 3600 * 1000)
        if i_1h >= 0 and pc_idx[i_1h] and pc_idx[i_1h] > 0:
            prem_1h_ago = (pc_mark[i_1h] - pc_idx[i_1h]) / pc_idx[i_1h] * 1e4
            premium_delta_bps = perp_premium_bps - prem_1h_ago
        else:
            premium_delta_bps = None

        outcome_yes = (result == "yes")
        rows.append({
            "ticker": ticker,
            "open_ts": open_ts,
            "close_ts": close_ts,
            "yes_ask": yes_ask,
            "yes_bid": yes_bid,
            "no_ask": round(1.0 - yes_bid, 4),
            "no_bid": round(1.0 - yes_ask, 4),
            "funding": funding,
            "perp_premium_bps": perp_premium_bps,
            "premium_delta_bps": premium_delta_bps,
            "outcome_yes": outcome_yes,
        })
    return rows


# ----------------------------- H2 dataset (recent 48h) --------------------------

def build_h2_dataset():
    """Per 15-min window with rubik features. Outcome from perp 15m candle close direction.
    Entry assumed $0.50 yes_ask (neutral)."""
    d = sqlite3.connect(DER_DB)
    # Use 15m candles in the rubik time window
    # Rubik window: last 48h ending now
    cur_ms = int(time.time() * 1000)
    start_ms = cur_ms - 49 * 3600 * 1000
    pc = list(d.execute(
        "SELECT ts, mark_close, idx_close FROM perp_candles_15m WHERE ts >= ? AND mark_close IS NOT NULL AND idx_close IS NOT NULL ORDER BY ts",
        (start_ms,),
    ))
    pc_ts = [r[0] for r in pc]
    pc_mark = [r[1] for r in pc]
    pc_idx = [r[2] for r in pc]

    # Funding lookup
    fr = list(d.execute("SELECT ts, funding_rate FROM funding_rate ORDER BY ts"))
    fr_ts = [r[0] for r in fr]
    fr_val = [r[1] for r in fr]

    # Rubik tables
    def fetch_table(table, cols):
        return list(d.execute(f"SELECT ts, {','.join(cols)} FROM {table} ORDER BY ts"))

    lsg = fetch_table("rubik_lsratio_global", ["ratio"])
    lsc = fetch_table("rubik_lsratio_contract", ["ratio"])
    lst = fetch_table("rubik_lsratio_toptrader", ["ratio"])
    oiv = fetch_table("rubik_oi_volume", ["oi_usd", "volume_usd"])
    tks = fetch_table("rubik_taker_spot", ["buy_vol", "sell_vol"])
    tkc = fetch_table("rubik_taker_contract", ["buy_vol", "sell_vol"])

    def make_lookup(data, ncols):
        ts = [r[0] for r in data]
        vals = [r[1:] for r in data]
        def fn(t):
            lo, hi = 0, len(ts) - 1
            ans = -1
            while lo <= hi:
                mid = (lo + hi) // 2
                if ts[mid] <= t:
                    ans = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            return vals[ans] if ans >= 0 else None
        def fn_at(t, lookback_s=0):
            tt = t - lookback_s * 1000
            return fn(tt)
        return fn_at, ts

    lsg_get, _ = make_lookup(lsg, 1)
    lsc_get, _ = make_lookup(lsc, 1)
    lst_get, _ = make_lookup(lst, 1)
    oiv_get, oiv_ts = make_lookup(oiv, 2)
    tks_get, _ = make_lookup(tks, 2)
    tkc_get, _ = make_lookup(tkc, 2)
    fr_get, _ = make_lookup(fr, 1)

    # Iterate every 15m candle (each candle ts is the START of a 15-min bar in OKX convention)
    # Outcome: did the NEXT 15m candle close higher than prior 15m close? Use mark_close as proxy.
    rows = []
    for i in range(len(pc_ts) - 1):
        t_open_ms = pc_ts[i]
        t_close_ms = pc_ts[i + 1]
        ref_price = pc_mark[i]
        settle_price = pc_mark[i + 1]
        if ref_price is None or settle_price is None:
            continue
        outcome_yes = settle_price > ref_price

        mark, idx = pc_mark[i], pc_idx[i]
        perp_premium_bps = (mark - idx) / idx * 1e4 if idx and idx > 0 else 0.0
        # 1h ago premium
        # find pc index 4 bars back
        if i >= 4 and pc_mark[i - 4] is not None and pc_idx[i - 4] is not None and pc_idx[i - 4] > 0:
            prem_1h_ago = (pc_mark[i - 4] - pc_idx[i - 4]) / pc_idx[i - 4] * 1e4
            premium_delta_bps = perp_premium_bps - prem_1h_ago
        else:
            premium_delta_bps = None

        funding = fr_get(t_open_ms)
        funding_val = funding[0] if funding else None

        lsg_v = (lsg_get(t_open_ms) or [None])[0]
        lsc_v = (lsc_get(t_open_ms) or [None])[0]
        lst_v = (lst_get(t_open_ms) or [None])[0]
        oi_now = oiv_get(t_open_ms)
        oi_15 = oiv_get(t_open_ms, lookback_s=15 * 60)
        oi_30 = oiv_get(t_open_ms, lookback_s=30 * 60)
        oi_delta_15 = None
        oi_delta_30 = None
        if oi_now and oi_15 and oi_15[0]:
            oi_delta_15 = (oi_now[0] - oi_15[0]) / oi_15[0]
        if oi_now and oi_30 and oi_30[0]:
            oi_delta_30 = (oi_now[0] - oi_30[0]) / oi_30[0]
        # taker spot ratio (avg over last 15m = 3 bars)
        tks_now = tks_get(t_open_ms)
        tkc_now = tkc_get(t_open_ms)
        taker_spot_ratio = (tks_now[0] / tks_now[1]) if tks_now and tks_now[1] else None
        taker_cont_ratio = (tkc_now[0] / tkc_now[1]) if tkc_now and tkc_now[1] else None

        # price delta in last 15m (for OI/price combo)
        if i >= 1 and pc_mark[i - 1]:
            price_change_15m = (pc_mark[i] - pc_mark[i - 1]) / pc_mark[i - 1]
        else:
            price_change_15m = None

        rows.append({
            "open_ms": t_open_ms,
            "close_ms": t_close_ms,
            "ref_price": ref_price,
            "settle_price": settle_price,
            "outcome_yes": outcome_yes,
            "funding": funding_val,
            "perp_premium_bps": perp_premium_bps,
            "premium_delta_bps": premium_delta_bps,
            "lsratio_global": lsg_v,
            "lsratio_contract": lsc_v,
            "lsratio_toptrader": lst_v,
            "oi_delta_15": oi_delta_15,
            "oi_delta_30": oi_delta_30,
            "taker_spot_ratio": taker_spot_ratio,
            "taker_cont_ratio": taker_cont_ratio,
            "price_change_15m": price_change_15m,
        })
    return rows


# ----------------------------- Trading model -----------------------------

def trade_pnl(side: str, yes_ask: float, yes_bid: float, outcome_yes: bool):
    """Compute net P&L for one $1 contract.
    side: 'YES' (buy yes at yes_ask) or 'NO' (buy no at 1-yes_bid)
    Settlement: $1 if correct side; $0 otherwise. Fee = ceil(0.07*P*(1-P)*100)/100 per contract.
    """
    if side == "YES":
        p = yes_ask
        win = outcome_yes
    else:
        p = 1.0 - yes_bid
        win = not outcome_yes
    fee = fee_per_contract(p)
    payoff = 1.0 if win else 0.0
    pnl = payoff - p - fee
    return pnl, fee, p


def trade_pnl_neutral(side: str, outcome_yes: bool, entry=0.50):
    """Neutral $0.50 entry (used when no Kalshi book available — H2 backtest)."""
    win = outcome_yes if side == "YES" else (not outcome_yes)
    fee = fee_per_contract(entry)
    return (1.0 if win else 0.0) - entry - fee, fee, entry


# ----------------------------- Strategies -----------------------------

@dataclass
class StratResult:
    name: str
    horizon: str
    n: int = 0
    wins: int = 0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    total_fees: float = 0.0
    train_n: int = 0
    train_wins: int = 0
    train_net: float = 0.0
    test_n: int = 0
    test_wins: int = 0
    test_net: float = 0.0
    notes: str = ""


def run_strategy(name, horizon, rows, decide_fn, use_neutral=False):
    """decide_fn(row) -> ('YES' | 'NO' | None)
    Train/test split is chronological 50/50.
    """
    # sort by open
    rows = sorted(rows, key=lambda r: r.get("open_ts", r.get("open_ms", 0)))
    half = len(rows) // 2
    res = StratResult(name=name, horizon=horizon)
    for i, r in enumerate(rows):
        side = decide_fn(r)
        if side not in ("YES", "NO"):
            continue
        if use_neutral:
            pnl, fee, _p = trade_pnl_neutral(side, r["outcome_yes"])
        else:
            pnl, fee, _p = trade_pnl(side, r["yes_ask"], r["yes_bid"], r["outcome_yes"])
        win = pnl > 0
        res.n += 1
        res.wins += int(win)
        res.gross_pnl += pnl + fee
        res.net_pnl += pnl
        res.total_fees += fee
        if i < half:
            res.train_n += 1
            res.train_wins += int(win)
            res.train_net += pnl
        else:
            res.test_n += 1
            res.test_wins += int(win)
            res.test_net += pnl
    return res


# H1 strategies (Mar-Apr)

def strat_A_funding(threshold_high=0.00005, threshold_low=-0.00005, fade=True):
    """Strategy A: Funding rate extremes.
    fade=True (default): positive funding → fade longs → buy NO; negative → buy YES
    fade=False: follow funding (just to test)
    Threshold values are PER-INTERVAL funding rate (8h), expressed as decimal.
    Default 5bps (0.00005 = 0.005%) per 8h is roughly "highly positive" though not Binance's 0.01% threshold.
    """
    def decide(r):
        f = r.get("funding")
        if f is None:
            return None
        if fade:
            if f > threshold_high:
                return "NO"
            elif f < threshold_low:
                return "YES"
        else:
            if f > threshold_high:
                return "YES"
            elif f < threshold_low:
                return "NO"
        return None
    return decide


def strat_F_premium(thresh_bps=2.0, fade=False):
    """Strategy F: Perp premium / discount.
    fade=False: premium → buy YES; discount → buy NO
    fade=True: fade direction.
    """
    def decide(r):
        p = r.get("perp_premium_bps")
        if p is None:
            return None
        if not fade:
            if p > thresh_bps:
                return "YES"
            elif p < -thresh_bps:
                return "NO"
        else:
            if p > thresh_bps:
                return "NO"
            elif p < -thresh_bps:
                return "YES"
        return None
    return decide


def strat_F_premium_delta(thresh_bps=2.0, fade=False):
    """Strategy F': premium DELTA (change in last hour). Rising premium = building bullish bias.
    """
    def decide(r):
        p = r.get("premium_delta_bps")
        if p is None:
            return None
        if not fade:
            if p > thresh_bps:
                return "YES"
            elif p < -thresh_bps:
                return "NO"
        else:
            if p > thresh_bps:
                return "NO"
            elif p < -thresh_bps:
                return "YES"
        return None
    return decide


# H2 strategies (recent 48h, rubik)

def strat_B_taker(threshold_hi=1.10, threshold_lo=0.90, fade=False, source="contract"):
    """Strategy B: Taker buy/sell ratio."""
    key = "taker_cont_ratio" if source == "contract" else "taker_spot_ratio"
    def decide(r):
        v = r.get(key)
        if v is None:
            return None
        if not fade:
            if v > threshold_hi: return "YES"
            if v < threshold_lo: return "NO"
        else:
            if v > threshold_hi: return "NO"
            if v < threshold_lo: return "YES"
        return None
    return decide


def strat_C_oi(threshold_pct=0.003, fade=False, window="15"):
    """Strategy C: OI delta + price direction.
    If OI ↑ AND price ↑ in last 15m → new longs opening → continuation up = YES (follow)
    If OI ↑ AND price ↓ → new shorts opening → continuation down = NO
    If OI ↓ → unwinding → reversal of last 15m (fade)
    fade flag inverts.
    """
    key = "oi_delta_15" if window == "15" else "oi_delta_30"
    def decide(r):
        oi = r.get(key)
        px = r.get("price_change_15m")
        if oi is None or px is None:
            return None
        if abs(oi) < threshold_pct:
            return None
        oi_up = oi > 0
        px_up = px > 0
        if oi_up:
            # continuation
            signal = "YES" if px_up else "NO"
        else:
            # reversal
            signal = "NO" if px_up else "YES"
        return signal if not fade else ("YES" if signal == "NO" else "NO")
    return decide


def strat_D_toptrader(threshold=0.55, fade=False):
    """Strategy D: Top trader long/short ratio.
    Note: OKX ratio is long/short, so >1 means more longs. Translating "55% long" → ratio = 0.55/0.45 ≈ 1.222
    We'll use the ratio directly with a threshold.
    """
    # 0.55/0.45 = 1.222; 0.45/0.55 = 0.818
    hi = threshold / (1 - threshold)
    lo = (1 - threshold) / threshold
    def decide(r):
        v = r.get("lsratio_toptrader")
        if v is None:
            return None
        if not fade:
            if v > hi: return "YES"
            if v < lo: return "NO"
        else:
            if v > hi: return "NO"
            if v < lo: return "YES"
        return None
    return decide


def strat_E_global(threshold=0.55, fade=True):
    """Strategy E: Global retail long/short. fade=True means fade the crowd."""
    hi = threshold / (1 - threshold)
    lo = (1 - threshold) / threshold
    def decide(r):
        v = r.get("lsratio_global")
        if v is None:
            return None
        if fade:
            if v > hi: return "NO"
            if v < lo: return "YES"
        else:
            if v > hi: return "YES"
            if v < lo: return "NO"
        return None
    return decide


def strat_G_composite(rows_h2, train_split=True):
    """Strategy G: Composite. Weight each signal by its train-period win rate, then vote."""
    # Build sub-signals as decision functions
    signals = [
        ("taker_cont", strat_B_taker(1.10, 0.90, fade=False, source="contract")),
        ("taker_spot", strat_B_taker(1.10, 0.90, fade=False, source="spot")),
        ("oi_dir", strat_C_oi(0.003, fade=False, window="15")),
        ("toptrader", strat_D_toptrader(0.53, fade=False)),
        ("global_fade", strat_E_global(0.53, fade=True)),
        ("premium", strat_F_premium(1.0, fade=False)),
    ]
    # Compute train win rates
    rows = sorted(rows_h2, key=lambda r: r["open_ms"])
    half = len(rows) // 2
    weights = {}
    for name, fn in signals:
        wins, n = 0, 0
        for r in rows[:half]:
            side = fn(r)
            if not side:
                continue
            win = (side == "YES" and r["outcome_yes"]) or (side == "NO" and not r["outcome_yes"])
            n += 1
            wins += int(win)
        weights[name] = (wins / n) if n > 0 else 0.5
    # Decision: weighted vote >= threshold
    def decide(r):
        yes_w, no_w = 0.0, 0.0
        n_voted = 0
        for name, fn in signals:
            side = fn(r)
            if side == "YES":
                yes_w += weights[name] - 0.5
                n_voted += 1
            elif side == "NO":
                no_w += weights[name] - 0.5
                n_voted += 1
        if n_voted < 3:
            return None
        if yes_w > no_w and (yes_w - no_w) > 0.10:
            return "YES"
        if no_w > yes_w and (no_w - yes_w) > 0.10:
            return "NO"
        return None
    return decide


# ----------------------------- Main runner -----------------------------

def fmt_pct(n, d):
    return f"{(n/d*100):.1f}%" if d > 0 else "n/a"


def summarize(res: StratResult) -> str:
    return (
        f"- **{res.name}** ({res.horizon}): n={res.n}, "
        f"wr={fmt_pct(res.wins, res.n)}, net=${res.net_pnl:.2f}, fees=${res.total_fees:.2f}, "
        f"train wr={fmt_pct(res.train_wins, res.train_n)} net=${res.train_net:.2f}, "
        f"test wr={fmt_pct(res.test_wins, res.test_n)} net=${res.test_net:.2f}"
        + (f"  ({res.notes})" if res.notes else "")
    )


def main():
    print("Building H1 dataset (Mar-Apr Kalshi)...")
    h1 = build_h1_dataset()
    print(f"  H1 rows: {len(h1)}")
    print("Building H2 dataset (recent 48h, perp-based outcomes, neutral $0.50 entry)...")
    h2 = build_h2_dataset()
    print(f"  H2 rows: {len(h2)}")

    # ---------- H1 strategies ----------
    h1_results = []
    # A: funding fade at varied thresholds
    for thresh in [0.00001, 0.00005, 0.0001]:
        r = run_strategy(f"A.funding-fade(±{thresh:.5f})", "H1", h1, strat_A_funding(thresh, -thresh, fade=True))
        h1_results.append(r)
    # A follow
    r = run_strategy("A.funding-FOLLOW(±0.00005)", "H1", h1, strat_A_funding(0.00005, -0.00005, fade=False))
    h1_results.append(r)
    # F: premium follow / fade
    for thresh in [0.5, 1.0, 2.0, 5.0]:
        r = run_strategy(f"F.premium-follow(±{thresh}bps)", "H1", h1, strat_F_premium(thresh, fade=False))
        h1_results.append(r)
    for thresh in [1.0, 2.0]:
        r = run_strategy(f"F.premium-fade(±{thresh}bps)", "H1", h1, strat_F_premium(thresh, fade=True))
        h1_results.append(r)
    # F': premium delta
    for thresh in [0.5, 1.0, 2.0]:
        r = run_strategy(f"F'.premium-delta-follow(±{thresh}bps)", "H1", h1, strat_F_premium_delta(thresh, fade=False))
        h1_results.append(r)
    # H1 baseline buy YES at low yes_ask
    def baseline_buy_yes_if_cheap(r):
        return "YES" if r["yes_ask"] < 0.50 else "NO"
    r = run_strategy("Baseline.buy-cheap-side", "H1", h1, baseline_buy_yes_if_cheap)
    r.notes = "buys whichever side has lower implied prob"
    h1_results.append(r)
    # H1 baseline: always YES
    r = run_strategy("Baseline.always-YES", "H1", h1, lambda r: "YES")
    h1_results.append(r)

    # ---------- H2 strategies ----------
    h2_results = []
    if h2:
        # B
        for thi, tlo in [(1.05, 0.95), (1.10, 0.90), (1.20, 0.80)]:
            for src in ("contract", "spot"):
                for fade in (False, True):
                    name = f"B.taker-{src}-{'fade' if fade else 'follow'}({thi}/{tlo})"
                    r = run_strategy(name, "H2", h2, strat_B_taker(thi, tlo, fade=fade, source=src), use_neutral=True)
                    h2_results.append(r)
        # C
        for thresh in [0.001, 0.003, 0.005]:
            for window in ("15", "30"):
                for fade in (False, True):
                    name = f"C.oi-{'fade' if fade else 'follow'}({thresh}, w={window})"
                    r = run_strategy(name, "H2", h2, strat_C_oi(thresh, fade=fade, window=window), use_neutral=True)
                    h2_results.append(r)
        # D
        for thresh in [0.53, 0.55, 0.58]:
            for fade in (False, True):
                name = f"D.toptrader-{'fade' if fade else 'follow'}({thresh})"
                r = run_strategy(name, "H2", h2, strat_D_toptrader(thresh, fade=fade), use_neutral=True)
                h2_results.append(r)
        # E
        for thresh in [0.53, 0.55, 0.58]:
            for fade in (True, False):
                name = f"E.global-{'fade' if fade else 'follow'}({thresh})"
                r = run_strategy(name, "H2", h2, strat_E_global(thresh, fade=fade), use_neutral=True)
                h2_results.append(r)
        # F on H2
        for thresh in [0.5, 1.0, 2.0]:
            for fade in (False, True):
                name = f"F.premium-{'fade' if fade else 'follow'}({thresh}bps)"
                r = run_strategy(name, "H2", h2, strat_F_premium(thresh, fade=fade), use_neutral=True)
                h2_results.append(r)
        # G: composite
        decide_g = strat_G_composite(h2)
        r = run_strategy("G.composite-weighted-vote", "H2", h2, decide_g, use_neutral=True)
        r.notes = "weights from train-half win rates; min 3 votes; margin>0.10"
        h2_results.append(r)
        # H2 baseline: always YES
        r = run_strategy("Baseline.always-YES", "H2", h2, lambda r: "YES", use_neutral=True)
        h2_results.append(r)
        r = run_strategy("Baseline.always-NO", "H2", h2, lambda r: "NO", use_neutral=True)
        h2_results.append(r)

    # Ranking by test_net (then test_n)
    all_results = h1_results + h2_results
    ranked = sorted(all_results, key=lambda r: (-(r.test_net), -r.n))

    # ---------- Write markdown ----------
    lines = []
    lines.append("# Derivatives Microstructure Shadow Backtest\n")
    lines.append(f"Generated: 2026-05-14\n")
    lines.append("\n## Data sources and constraints\n")
    lines.append("- **Binance futures API is US-IP blocked (HTTP 451).** Pivoted to OKX + Deribit.\n")
    lines.append("- **OKX rubik endpoints** (taker, OI, top-trader L/S, account L/S) cap at the most-recent ~48 hours and cannot be paginated further back.\n")
    lines.append("  - This means Strategies **B, C, D, E** (rubik-dependent) can only be backtested on a fresh 48h window (H2), NOT against the existing Kalshi DB (Mar 25 - Apr 24).\n")
    lines.append("- **OKX funding-rate-history** and **history-mark/index-candles** paginate freely.\n")
    lines.append("  - Strategies **A** (funding) and **F** (perp premium) backtested on the full Kalshi window (H1) with real `yes_ask` / `yes_bid`.\n")
    lines.append("\n## Horizons\n")
    lines.append(f"- **H1: 2026-03-25 → 2026-04-24** ({len(h1)} markets, real Kalshi prices, real Kalshi `result` field as outcome).\n")
    lines.append(f"- **H2: recent ~48h ending 2026-05-14** ({len(h2)} 15-min windows, outcome computed from OKX 15m mark-price direction, entry = neutral $0.50, fee = ceil(0.07·P·(1-P)·100)/100 = $0.02 at P=0.50).\n")
    lines.append("\n## P&L model\n")
    lines.append("- Entry: yes_ask for YES side, (1 - yes_bid) for NO side\n")
    lines.append("- Settlement: $1 if correct, $0 otherwise (hold to settle, free)\n")
    lines.append("- Fee: ceil(0.07 · P · (1-P) · 100) / 100 per contract\n")
    lines.append("- 50/50 chronological train/test split (no peek-ahead in Strategy G; weights learned on train half only)\n")
    lines.append("\n## H1 results (Mar-Apr, real Kalshi)\n")
    for r in h1_results:
        lines.append(summarize(r) + "\n")
    lines.append("\n## H2 results (recent 48h, neutral entry)\n")
    for r in h2_results:
        lines.append(summarize(r) + "\n")
    lines.append("\n## Ranked by out-of-sample (test-half) net P&L\n")
    lines.append("| # | Strategy | Horizon | n | Train wr | Train $ | Test wr | Test $ |\n")
    lines.append("|---|----------|---------|---|----------|---------|---------|--------|\n")
    for i, r in enumerate(ranked, 1):
        lines.append(
            f"| {i} | {r.name} | {r.horizon} | {r.n} | {fmt_pct(r.train_wins, r.train_n)} | ${r.train_net:.2f} | {fmt_pct(r.test_wins, r.test_n)} | ${r.test_net:.2f} |\n"
        )

    # Append interpretation / caveats section
    lines.append("\n## Interpretation\n")
    lines.append(
        "### H1 (Mar-Apr, 2809 markets)\n"
        "- **No funding/premium strategy beats break-even on Kalshi at scale.** Both follow and fade variants of\n"
        "  funding-rate and perp-premium produce net losses ranging from -$15 to -$106 over ~2,800 markets.\n"
        "- The market is well-calibrated: `Baseline.always-YES` loses -$2.16 in the test half (just fee bleed).\n"
        "- Funding-FOLLOW (51.4% wr) and premium-FADE (51.5% wr) both clear baseline by a hair, but the test-half\n"
        "  edge is statistically indistinguishable from noise (n≈500-1400, std err ≈ 1.3-2.2pp on win rate).\n"
        "- **Conclusion**: funding rate and perp-spot premium appear to be priced into KXBTC15M quickly. They are\n"
        "  not a free lunch.\n"
        "\n"
        "### H2 (recent 48h, 195 windows)\n"
        "- Several strategies clear baseline in this window, led by `B.taker-contract-follow(1.2/0.8)` with 60.5%\n"
        "  test win rate and +$6.88 over 76 trades.\n"
        "- **Major caveat**: BTC trended up modestly in the test half, so any strategy that biases toward YES looks\n"
        "  good. Notice that `Baseline.always-YES` and four `premium-fade` variants all share the EXACT same\n"
        "  numbers (test wr 54.1%, +$2.04) — they're all firing YES on every window. The fact that\n"
        "  taker-contract-follow at the 1.2/0.8 threshold pulls another +$4.84 above always-YES on 76 trades is\n"
        "  promising but still small in absolute terms (≈1.6pp lift, σ ≈ 5.7pp).\n"
        "- We do NOT have Kalshi pricing for H2, so the $0.50 neutral entry overstates edge: real Kalshi `yes_ask`\n"
        "  is typically NOT 0.50 when a directional signal fires (the book leans the way price is moving).\n"
        "- Top-trader FADE and OI FADE at restrictive thresholds also test-half-positive, but n<30 — pure noise.\n"
        "- **Strategy G composite collapsed in test** (64.7% → 10.0%), classic overfit to a tiny train half.\n"
        "\n"
        "### Recommendation\n"
        "- **Do NOT trust H2 alpha as established.** The 48h sample is too short and biased to draw conclusions.\n"
        "- **The shadow engine should be data-collection-first**: poll OKX rubik features every 15 minutes,\n"
        "  log them alongside the actual Kalshi outcome, and rerun this backtest in 4-6 weeks with a real\n"
        "  ~1000-window dataset. Only then will we have power to detect a 2-3pp directional edge.\n"
        "- For the prototype, use the most-promising candidate `B.taker-contract-follow(1.2/0.8)` as the primary\n"
        "  signal (best test win rate among signals with n>50), but log all features for future analysis.\n"
        "- Strategy H (combo with GBM edge) was not run — H1 lacks GBM edge data linkage, and H2 lacks Kalshi\n"
        "  book to compute GBM-implied edge. Defer until shadow engine has accumulated ≥2 weeks of paired data.\n"
    )

    OUT_MD.write_text("".join(lines), encoding="utf-8")
    print(f"Wrote {OUT_MD}")

    # Also dump JSON for downstream tooling
    OUT_MD.with_suffix(".json").write_text(
        json.dumps({
            "h1_n": len(h1),
            "h2_n": len(h2),
            "results": [r.__dict__ for r in all_results],
        }, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
