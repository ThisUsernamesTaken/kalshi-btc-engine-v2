"""
Entry-price analysis: winner trajectories, 6-variant backtest, Kelly sizing.
Reads btc-bias-engine/data/kalshi_external_backtest.db (2,819 KXBTC15M markets).
Outputs intermediate JSON for the final markdown report.
"""
from __future__ import annotations
import json, math, sqlite3, statistics, hashlib
from pathlib import Path
from collections import Counter, defaultdict

DB = Path("C:/Trading/btc-bias-engine/data/kalshi_external_backtest.db")
OUT_JSON = Path("C:/Trading/kalshi-btc-engine-v2/data/_entry_price_analysis_out.json")


def fee(P: float, n: int = 1) -> float:
    # Kalshi taker fee, in dollars
    if P <= 0 or P >= 1:
        return 0.0
    return math.ceil(0.07 * n * P * (1.0 - P) * 100.0) / 100.0


def fnum(x):
    return float(x) if x is not None else 0.0


# ── load ──
def load():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    mkts = {}
    for row in cur.execute("SELECT ticker, open_ts, close_ts, result, raw_json FROM markets"):
        rj = json.loads(row["raw_json"])
        mkts[row["ticker"]] = {
            "ticker": row["ticker"],
            "open_ts": row["open_ts"],
            "close_ts": row["close_ts"],
            "result": row["result"],
            "floor_strike": float(rj.get("floor_strike", 0.0)),
            "candles": [],
        }
    for row in cur.execute("SELECT ticker, end_period_ts, raw_json FROM candles ORDER BY ticker, end_period_ts"):
        m = mkts.get(row["ticker"])
        if not m: continue
        mi = (row["end_period_ts"] - m["open_ts"] - 60) // 60
        if mi < 0 or mi > 14: continue
        d = json.loads(row["raw_json"])
        ya = d.get("yes_ask", {}) or {}
        yb = d.get("yes_bid", {}) or {}
        pr = d.get("price", {}) or {}
        m["candles"].append({
            "minute_idx": int(mi),
            "ya_open": fnum(ya.get("open_dollars")), "ya_high": fnum(ya.get("high_dollars")),
            "ya_low": fnum(ya.get("low_dollars")), "ya_close": fnum(ya.get("close_dollars")),
            "yb_open": fnum(yb.get("open_dollars")), "yb_high": fnum(yb.get("high_dollars")),
            "yb_low": fnum(yb.get("low_dollars")), "yb_close": fnum(yb.get("close_dollars")),
            "p_open": fnum(pr.get("open_dollars")), "p_high": fnum(pr.get("high_dollars")),
            "p_low": fnum(pr.get("low_dollars")), "p_close": fnum(pr.get("close_dollars")),
        })
    for m in mkts.values():
        m["candles"].sort(key=lambda c: c["minute_idx"])
    btc = {}
    for row in cur.execute("SELECT open_ts, open, high, low, close FROM btc_1m"):
        btc[row[0]] = (row[1], row[2], row[3], row[4])
    conn.close()
    return list(mkts.values()), btc


def winner_ask(c, result):
    """Winner's ask at close of candle. If result==yes, winner=YES; ask = ya_close.
    Else winner=NO; NO_ask ~ 1 - yes_bid_close (best to-buy-no price)."""
    return c["ya_close"] if result == "yes" else (1.0 - c["yb_close"])


def winner_high(c, result):
    return c["ya_high"] if result == "yes" else (1.0 - c["yb_low"])


def winner_low(c, result):
    return c["ya_low"] if result == "yes" else (1.0 - c["yb_high"])


# ── Part 2: trajectories ──
def part2(mkts):
    # For each market, find minute_idx where winner's ask first CLOSES >= X
    # Use close-based detection (avoid the stale ya_open at min 0 from prior market).
    thresholds = [0.60, 0.65, 0.70, 0.75, 0.80]
    first_hit = {x: [] for x in thresholds}
    never_hit = {x: 0 for x in thresholds}
    n_total = 0
    for m in mkts:
        if not m["candles"]: continue
        if m["result"] not in ("yes","no"): continue
        n_total += 1
        for x in thresholds:
            hit_min = None
            for c in m["candles"]:
                if c["minute_idx"] > 14: continue
                # Winner's close-of-minute ask
                w_close = winner_ask(c, m["result"])
                if w_close >= x:
                    hit_min = c["minute_idx"]
                    break
            if hit_min is None:
                never_hit[x] += 1
            else:
                first_hit[x].append(hit_min)

    def pct(arr):
        if not arr: return None
        s = sorted(arr)
        n = len(s)
        return {
            "n": n,
            "median": s[n//2],
            "p25": s[max(0, n//4)],
            "p75": s[min(n-1, (3*n)//4)],
            "mean": round(sum(s)/n, 2),
        }

    table = []
    for x in thresholds:
        d = pct(first_hit[x])
        nh = never_hit[x]
        table.append({
            "threshold_c": int(x*100),
            "n_hit": d["n"] if d else 0,
            "n_never": nh,
            "pct_never": round(nh / n_total * 100, 1) if n_total else 0,
            "median_min": d["median"] if d else None,
            "p25_min": d["p25"] if d else None,
            "p75_min": d["p75"] if d else None,
            "mean_min": d["mean"] if d else None,
        })

    # Conditional: in sessions where winner first hits 80c LATE (min >= 12),
    # what was winner's price at earlier minutes? Use close-based first-hit detection.
    late_80 = []
    for m in mkts:
        if not m["candles"]: continue
        if m["result"] not in ("yes","no"): continue
        hit80 = None
        for c in m["candles"]:
            if c["minute_idx"] > 14: continue
            if winner_ask(c, m["result"]) >= 0.80:
                hit80 = c["minute_idx"]; break
        if hit80 is None or hit80 < 12:
            continue
        # collect winner_ask at min 3, 5, 7, 10, 12
        snap = {}
        cmap = {c["minute_idx"]: c for c in m["candles"]}
        for mi in (3, 5, 7, 10, 11):
            if mi in cmap:
                snap[mi] = winner_ask(cmap[mi], m["result"])
        late_80.append(snap)

    def mean_at(mi):
        vals = [s[mi] for s in late_80 if mi in s]
        if not vals: return None
        s = sorted(vals)
        return {
            "n": len(vals),
            "mean": round(sum(vals)/len(vals), 3),
            "median": round(s[len(s)//2], 3),
            "p25": round(s[len(s)//4], 3),
            "p75": round(s[(3*len(s))//4], 3),
        }

    late_80_summary = {f"min_{mi}": mean_at(mi) for mi in (3,5,7,10,11)}
    late_80_summary["n_sessions"] = len(late_80)

    return {"thresholds": table, "n_markets": n_total, "late_80_climb": late_80_summary}


# ── Part 3: 6 variants backtest ──
def btc_at(open_ts, mi, btc):
    bar = btc.get(open_ts + mi * 60)
    return bar[3] if bar else None


def btc_velocity_60s(open_ts, mi, btc):
    if mi <= 0: return None
    cur = btc.get(open_ts + mi*60)
    prev = btc.get(open_ts + (mi-1)*60)
    if cur is None or prev is None: return None
    return cur[3] - prev[3]


def trade_pnl(side, entry_price, contracts, result):
    """Returns (won, gross, net) where gross = contracts*(1-entry) if win else -contracts*entry."""
    won = (side == "YES" and result == "yes") or (side == "NO" and result == "no")
    if won:
        gross = contracts * (1.0 - entry_price)
    else:
        gross = -contracts * entry_price
    f = fee(entry_price, contracts)
    return won, gross, gross - f


def split_train_test(mkts, seed_str="entry_price_analysis_v1"):
    """Deterministic 50/50 split by md5(ticker+seed)."""
    train, test = [], []
    for m in mkts:
        h = hashlib.md5((m["ticker"] + seed_str).encode()).hexdigest()
        bucket = int(h, 16) & 1
        (train if bucket == 0 else test).append(m)
    return train, test


# Variant runners — each returns list of trades, where trade = dict with entry_price, contracts, side, won, gross, net.

def variant_v5(m, btc):
    """V5 baseline: min>=12, ask>=80c, 10ct at actual ask, hold."""
    for c in m["candles"]:
        if c["minute_idx"] < 12: continue
        yes_hit = c["ya_high"] >= 0.80 and c["p_high"] >= 0.80
        no_hit = (c["yb_low"] <= 0.20 and c["p_low"] > 0 and c["p_low"] <= 0.20)
        if not (yes_hit or no_hit): continue
        if yes_hit and no_hit:
            fav = "YES" if (c["p_high"] - 0.80) >= ((1-c["p_low"]) - 0.80) else "NO"
        else:
            fav = "YES" if yes_hit else "NO"
        # entry price: use 0.80 limit, capped at 0.99 (as harness does)
        entry = 0.80
        won, gross, net = trade_pnl(fav, entry, 10, m["result"])
        return {"variant":"v5", "minute":c["minute_idx"], "side":fav, "entry":entry, "contracts":10, "won":won, "gross":gross, "net":net}
    return None


def _side_trigger(c, X):
    """Return (yes_hit, no_hit) requiring traded-price confirmation: high>=X AND traded
    price reached X. We require p_high >= X (or p_low <= 1-X) to avoid pure quote ghosts.
    Also require minute_idx > 0 since min-0 quote opens are stale (carry over)."""
    if c["minute_idx"] == 0:
        # only allow min-0 trigger if close ALSO confirms (real price action)
        yes_hit = c["ya_close"] >= X and c["p_close"] >= X
        no_hit = c["yb_close"] <= (1 - X) and c["p_close"] > 0 and c["p_close"] <= (1 - X)
    else:
        yes_hit = c["ya_high"] >= X and c["p_high"] >= X
        no_hit = c["yb_low"] <= (1 - X) and c["p_low"] > 0 and c["p_low"] <= (1 - X)
    return yes_hit, no_hit


def _pick_side(c, yes_hit, no_hit):
    if yes_hit and no_hit:
        # both touched X — use close to decide direction
        return "YES" if c["ya_close"] >= (1 - c["yb_close"]) else "NO"
    return "YES" if yes_hit else "NO"


def variant_earlier_aggressive(m, btc):
    """min>=7, first ask>=65c, 15ct."""
    for c in m["candles"]:
        if c["minute_idx"] < 7: continue
        if c["minute_idx"] > 14: continue
        yh, nh = _side_trigger(c, 0.65)
        if not (yh or nh): continue
        fav = _pick_side(c, yh, nh)
        entry = 0.65
        won, gross, net = trade_pnl(fav, entry, 15, m["result"])
        return {"variant":"earlier_aggressive", "minute":c["minute_idx"], "side":fav, "entry":entry, "contracts":15, "won":won, "gross":gross, "net":net}
    return None


def variant_earlier_moderate(m, btc):
    """min>=5, ask>=60c AND BTC gap (|btc_now - strike|)/strike >= 10bps, 20ct."""
    BPS = 10
    for c in m["candles"]:
        if c["minute_idx"] < 5: continue
        if c["minute_idx"] > 14: continue
        yh, nh = _side_trigger(c, 0.60)
        if not (yh or nh): continue
        btc_now = btc_at(m["open_ts"], c["minute_idx"], btc)
        if btc_now is None or m["floor_strike"] <= 0:
            continue
        gap_bps = abs(btc_now - m["floor_strike"]) / m["floor_strike"] * 1e4
        if gap_bps < BPS: continue
        fav = _pick_side(c, yh, nh)
        entry = 0.60
        won, gross, net = trade_pnl(fav, entry, 20, m["result"])
        return {"variant":"earlier_moderate", "minute":c["minute_idx"], "side":fav, "entry":entry, "contracts":20, "won":won, "gross":gross, "net":net}
    return None


def variant_committed_move(m, btc):
    """min>=3, BTC moved >=$50 from session open AND ask>=60c, 20ct."""
    btc_open = btc_at(m["open_ts"], 0, btc)
    if btc_open is None: return None
    for c in m["candles"]:
        if c["minute_idx"] < 3: continue
        if c["minute_idx"] > 14: continue
        btc_now = btc_at(m["open_ts"], c["minute_idx"], btc)
        if btc_now is None: continue
        move = btc_now - btc_open
        if abs(move) < 50: continue
        yh, nh = _side_trigger(c, 0.60)
        if not (yh or nh): continue
        side = "YES" if move > 0 else "NO"
        # side must have closed >=60c (otherwise we'd have entered the wrong direction)
        side_close = c["ya_close"] if side == "YES" else (1.0 - c["yb_close"])
        if side_close < 0.60: continue
        entry = 0.60
        won, gross, net = trade_pnl(side, entry, 20, m["result"])
        return {"variant":"committed_move", "minute":c["minute_idx"], "side":side, "entry":entry, "contracts":20, "won":won, "gross":gross, "net":net}
    return None


def variant_ladder(m, btc):
    """5ct at min5 (ask>=60c), +5ct at min7 (ask>=70c), +10ct at min12 (ask>=80c). Accumulate same side."""
    legs = []
    chosen_side = None
    for trigger_min, trig_price, n, leg_name in [(5, 0.60, 5, "leg1"), (7, 0.70, 5, "leg2"), (12, 0.80, 10, "leg3")]:
        for c in m["candles"]:
            if c["minute_idx"] < trigger_min: continue
            if c["minute_idx"] > 14: continue
            yh, nh = _side_trigger(c, trig_price)
            if not (yh or nh): continue
            if chosen_side is None:
                chosen_side = _pick_side(c, yh, nh)
            # must trigger on chosen side
            side_close = c["ya_close"] if chosen_side == "YES" else (1.0 - c["yb_close"])
            if side_close < trig_price: continue
            entry = trig_price
            won, gross, net = trade_pnl(chosen_side, entry, n, m["result"])
            legs.append({"leg":leg_name, "minute":c["minute_idx"], "entry":entry, "contracts":n, "won":won, "gross":gross, "net":net})
            break
    if not legs: return None
    total_contracts = sum(l["contracts"] for l in legs)
    weighted_entry = sum(l["entry"]*l["contracts"] for l in legs) / total_contracts
    total_gross = sum(l["gross"] for l in legs)
    total_net = sum(l["net"] for l in legs)
    won = legs[0]["won"]
    return {"variant":"ladder", "minute":legs[0]["minute"], "side":chosen_side, "entry":weighted_entry, "contracts":total_contracts, "won":won, "gross":total_gross, "net":total_net, "n_legs": len(legs)}


def variant_adaptive(m, btc):
    """10ct at first ask>=65c. If by min 10, position not underwater AND ask>=80c, add 10ct."""
    leg1 = None
    side = None
    for c in m["candles"]:
        if c["minute_idx"] > 14: continue
        yh, nh = _side_trigger(c, 0.65)
        if not (yh or nh): continue
        side = _pick_side(c, yh, nh)
        leg1 = {"minute":c["minute_idx"], "entry":0.65, "contracts":10}
        break
    if leg1 is None:
        return None
    # check at min 10: side's bid (mark-to-market) > entry price → not underwater
    # find candle at min 10 (or closest after leg1)
    cmap = {c["minute_idx"]: c for c in m["candles"]}
    leg2 = None
    if 10 in cmap:
        c10 = cmap[10]
        side_bid = c10["yb_close"] if side == "YES" else (1.0 - c10["ya_close"])
        if side_bid > 0.65:
            # not underwater; look for ask>=80c at min 10 or later
            for c in m["candles"]:
                if c["minute_idx"] < 10: continue
                side_ask = c["ya_high"] if side == "YES" else (1.0 - c["yb_low"])
                if side_ask >= 0.80:
                    leg2 = {"minute":c["minute_idx"], "entry":0.80, "contracts":10}
                    break
    legs = [leg1] + ([leg2] if leg2 else [])
    total_contracts = sum(l["contracts"] for l in legs)
    weighted_entry = sum(l["entry"]*l["contracts"] for l in legs) / total_contracts
    won = (side == "YES" and m["result"] == "yes") or (side == "NO" and m["result"] == "no")
    total_gross = 0.0
    total_fee = 0.0
    for l in legs:
        if won: total_gross += l["contracts"] * (1.0 - l["entry"])
        else:   total_gross -= l["contracts"] * l["entry"]
        total_fee += fee(l["entry"], l["contracts"])
    total_net = total_gross - total_fee
    return {"variant":"adaptive", "minute":leg1["minute"], "side":side, "entry":weighted_entry, "contracts":total_contracts, "won":won, "gross":total_gross, "net":total_net, "n_legs":len(legs)}


VARIANTS = {
    "v5_baseline": variant_v5,
    "earlier_aggressive": variant_earlier_aggressive,
    "earlier_moderate": variant_earlier_moderate,
    "committed_move": variant_committed_move,
    "ladder": variant_ladder,
    "adaptive": variant_adaptive,
}


def summarize(trades):
    if not trades:
        return {"n": 0, "wr": 0.0, "avg_entry": 0.0, "gross": 0.0, "net": 0.0, "edge_c_per_ct": 0.0, "total_contracts": 0, "max_dd": 0.0}
    n = len(trades)
    wins = sum(1 for t in trades if t["won"])
    total_contracts = sum(t["contracts"] for t in trades)
    gross = sum(t["gross"] for t in trades)
    net = sum(t["net"] for t in trades)
    avg_entry = sum(t["entry"]*t["contracts"] for t in trades) / total_contracts
    edge_c = (net / total_contracts) * 100  # cents per contract
    # max drawdown on cumulative net (in trade order)
    cum = 0.0
    peak = 0.0
    mdd = 0.0
    for t in trades:
        cum += t["net"]
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    return {
        "n": n,
        "wr_pct": round(wins/n*100, 1),
        "avg_entry_c": round(avg_entry*100, 1),
        "gross": round(gross, 2),
        "net": round(net, 2),
        "edge_c_per_ct": round(edge_c, 2),
        "total_contracts": total_contracts,
        "max_dd": round(mdd, 2),
    }


def part3(mkts, btc):
    train, test = split_train_test(mkts)
    out = {"split": {"train_n": len(train), "test_n": len(test), "seed": "md5(ticker+entry_price_analysis_v1)"}}
    results = {}
    for vname, vfn in VARIANTS.items():
        train_trades = [t for t in (vfn(m, btc) for m in train) if t is not None]
        test_trades  = [t for t in (vfn(m, btc) for m in test)  if t is not None]
        all_trades = train_trades + test_trades
        results[vname] = {
            "train": summarize(train_trades),
            "test":  summarize(test_trades),
            "all":   summarize(all_trades),
        }
    out["variants"] = results

    # Part 4: Kelly by entry price bucket using ALL trades from a price-bucketed sweep.
    # We need WR(p) per bucket — sweep across all markets, recording entries at each ask threshold (60/65/70/75/80/85/90/95).
    # For each threshold X: find the first candle (any minute) where some side's high >= X. Pick that side, enter at X (limit), hold.
    # Kelly per entry-price bucket. We compute TWO regimes:
    #  (a) "first-trigger": enter at first minute price hits X (low WR at low minutes)
    #  (b) "late-trigger" (min>=12): only fire if X is reached in the last 3 minutes
    # The user wants empirical WR per bucket from the backtest — both views are useful.
    # For the table, present BOTH so they can see whether the trade-off favors size-at-low-price
    # vs late-certainty.
    kelly_data = []
    for X in [0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]:
        # (a) first-trigger
        w_a = n_a = 0
        # (b) late-trigger
        w_b = n_b = 0
        for m in mkts:
            if m["result"] not in ("yes","no"): continue
            fired_a = False
            fired_b = False
            for c in m["candles"]:
                if c["minute_idx"] > 14: continue
                yh, nh = _side_trigger(c, X)
                if not (yh or nh): continue
                side = _pick_side(c, yh, nh)
                won = (side == "YES" and m["result"]=="yes") or (side=="NO" and m["result"]=="no")
                if not fired_a:
                    n_a += 1
                    if won: w_a += 1
                    fired_a = True
                if c["minute_idx"] >= 12 and not fired_b:
                    n_b += 1
                    if won: w_b += 1
                    fired_b = True
                if fired_a and fired_b: break
        # Build a record per regime
        def kelly_row(label, n, wins):
            if n == 0:
                return {"regime": label, "X_c": int(X*100), "n":0}
            p = wins / n
            b = (1.0 - X) / X
            q = 1.0 - p
            kelly = (p*b - q) / b if b > 0 else 0.0
            ev_gross = p - X
            f1 = fee(X, 1)
            ev_net = ev_gross - f1
            bankroll = 100.0
            ct_full = max(0.0, bankroll * kelly / X) if X > 0 else 0
            ct_quarter = ct_full / 4
            return {
                "regime": label, "X_c": int(X*100), "n": n, "wins": wins,
                "p": round(p, 4), "b": round(b, 4),
                "ev_gross_c": round(ev_gross*100, 2),
                "ev_net_c": round(ev_net*100, 2),
                "kelly": round(kelly, 4),
                "ct_full_100bk": round(ct_full, 1),
                "ct_quarter_100bk": round(ct_quarter, 1),
            }
        kelly_data.append(kelly_row("first_trigger", n_a, w_a))
        kelly_data.append(kelly_row("late_trigger_min12", n_b, w_b))
    out["kelly"] = kelly_data
    return out


def main():
    print("Loading...")
    mkts, btc = load()
    print(f"Loaded {len(mkts)} markets, {len(btc)} BTC bars")
    p2 = part2(mkts)
    p3 = part3(mkts, btc)
    out = {"part2": p2, "part3": p3}
    OUT_JSON.write_text(json.dumps(out, indent=2))
    print(f"Wrote {OUT_JSON}")
    print("\n--- PART 2 trajectory ---")
    print(json.dumps(p2, indent=2))
    print("\n--- PART 3 split ---")
    print(json.dumps(p3["split"], indent=2))
    print("\n--- PART 3 variants ---")
    for v, r in p3["variants"].items():
        print(f"\n{v}")
        for k, sub in r.items():
            print(f"  {k}: {sub}")
    print("\n--- KELLY ---")
    for row in p3["kelly"]:
        print(row)


if __name__ == "__main__":
    main()
