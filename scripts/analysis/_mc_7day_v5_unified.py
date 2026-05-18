"""Monte Carlo: project unified V5 engine over 7 days at LIVE sizing.

Bootstraps per-trade P&L from the 30-day unified backtest (2,819 markets) but
rescales MOVING_BIG to 20ct (live) — backtest used 10ct.

Live tiers (per user): MOVING_BIG=20, FLAT_BIG=5, MOVING_SMALL=5. The
FLAT_SMALL contrarian and EARLY leg are assumed OFF in live (both net-losing
or net-flat in the backtest).

Starting balance: $109.
"""
from __future__ import annotations
import math
import statistics
import random
from collections import Counter

# --- backtest stats (30-day, 2817 unified trades) ---
# Each tier has deterministic win / loss P&L per trade because entries are
# at fixed limit prices (0.80 favorites, 0.20 contrarian).
def fee(P, n):
    return math.ceil(0.07 * n * P * (1.0 - P) * 100.0) / 100.0

# (n_contracts, entry_price, n_trades_30d, win_rate, longest_loss_streak)
LIVE_TIERS = {
    "MOVING_BIG":   dict(ct=20, P=0.80, n30=582, wr=562/582, lls=3),
    "MOVING_SMALL": dict(ct=5,  P=0.80, n30=227, wr=195/227, lls=2),
    "FLAT_BIG":     dict(ct=5,  P=0.80, n30=840, wr=793/840, lls=2),
}

# Compute per-trade win/loss P&L for each tier
for name, t in LIVE_TIERS.items():
    n, P = t["ct"], t["P"]
    f = fee(P, n)
    t["win_pnl"]  = n * (1.0 - P) - f
    t["loss_pnl"] = -n * P - f
    t["fee"] = f
    t["per_day"] = t["n30"] / 30.0
    t["ev_per_trade"] = t["wr"] * t["win_pnl"] + (1 - t["wr"]) * t["loss_pnl"]

print("=== LIVE-sizing per-tier economics ===")
print(f"{'Tier':<14} {'ct':>3} {'P':>5} {'fee':>6} {'win':>7} {'loss':>8} {'WR':>6} {'EV/tr':>7} {'/day':>5}")
for name, t in LIVE_TIERS.items():
    print(f"{name:<14} {t['ct']:>3} {t['P']:>5.2f} {t['fee']:>6.2f} "
          f"{t['win_pnl']:>+7.2f} {t['loss_pnl']:>+8.2f} {t['wr']:>6.1%} "
          f"{t['ev_per_trade']:>+7.3f} {t['per_day']:>5.1f}")

ev_per_day = sum(t["per_day"] * t["ev_per_trade"] for t in LIVE_TIERS.values())
trades_per_day = sum(t["per_day"] for t in LIVE_TIERS.values())
print(f"\nExpected per-day EV: ${ev_per_day:+.2f}, {trades_per_day:.1f} trades/day")
print(f"7-day analytic EV:   ${ev_per_day*7:+.2f}")

# --- Build per-tier P&L distributions for bootstrap ---
# Each tier's per-trade P&L is binary: win_pnl with prob wr, loss_pnl else.
# To preserve realistic clustering (loss streaks), we sample trades
# independently — this matches the assumption that market outcomes are
# roughly independent. Backtest's longest_loss_streak was 7 across the whole
# unified strategy, consistent with iid Bernoulli at WR ~93%.

# Trade-count variability: model daily trade count as Poisson around expected.
import random

def sim_one_day(rng):
    """Simulate one trading day. Returns (day_pnl, trade_count)."""
    day_pnl = 0.0
    trade_count = 0
    for name, t in LIVE_TIERS.items():
        # Poisson-ish daily count for this tier
        lam = t["per_day"]
        # use Poisson via random.poisson? stdlib doesn't have it; emulate with
        # a normal approximation rounded (lam is fairly large 8..28, normal is fine).
        # For MOVING_SMALL lam=7.6 also OK.
        n_trades = max(0, int(round(rng.gauss(lam, math.sqrt(lam)))))
        trade_count += n_trades
        for _ in range(n_trades):
            if rng.random() < t["wr"]:
                day_pnl += t["win_pnl"]
            else:
                day_pnl += t["loss_pnl"]
    return day_pnl, trade_count

def sim_one_week(rng, n_days=7):
    """Returns (cumulative_pnl_by_day_list, total_trades, max_drawdown_in_week,
    best_day, worst_day)."""
    cum = [0.0]
    daily = []
    total_trades = 0
    for d in range(n_days):
        pnl, tc = sim_one_day(rng)
        daily.append(pnl)
        cum.append(cum[-1] + pnl)
        total_trades += tc
    peak = 0.0
    max_dd = 0.0
    for v in cum:
        peak = max(peak, v)
        max_dd = max(max_dd, peak - v)
    return cum, total_trades, max_dd, max(daily), min(daily), daily

# --- Monte Carlo ---
N_SIMS = 10000
START_BAL = 109.0
MILESTONES_UP   = [150, 200, 300]
MILESTONES_DOWN = [75, 50, 25]

rng = random.Random(42)
results = []  # list of dicts
for i in range(N_SIMS):
    cum, tt, dd, best, worst, daily = sim_one_week(rng)
    end_pnl = cum[-1]
    end_bal = START_BAL + end_pnl
    # Path-balance for milestone tracking
    bal_path = [START_BAL + c for c in cum]
    hit_up   = {m: any(b >= m for b in bal_path) for m in MILESTONES_UP}
    hit_down = {m: any(b <= m for b in bal_path) for m in MILESTONES_DOWN}
    results.append(dict(
        end_pnl=end_pnl, end_bal=end_bal,
        total_trades=tt, max_dd=dd,
        best=best, worst=worst,
        daily=daily, bal_path=bal_path,
        hit_up=hit_up, hit_down=hit_down,
    ))

# --- Summary stats ---
def pct(values, p):
    return statistics.quantiles(values, n=100, method="inclusive")[p - 1]

end_pnls = sorted(r["end_pnl"] for r in results)
end_bals = sorted(r["end_bal"] for r in results)
dds      = sorted(r["max_dd"] for r in results)
trades   = sorted(r["total_trades"] for r in results)
bests    = sorted(r["best"] for r in results)
worsts   = sorted(r["worst"] for r in results)

prof = sum(1 for v in end_pnls if v > 0) / N_SIMS

print(f"\n=== Monte Carlo: {N_SIMS} sims, 7 days, start ${START_BAL:.0f} ===")
print(f"\n7-day P&L distribution:")
print(f"  5th  pct : ${pct(end_pnls, 5):+.2f}   (bad week)")
print(f"  10th pct : ${pct(end_pnls, 10):+.2f}")
print(f"  25th pct : ${pct(end_pnls, 25):+.2f}")
print(f"  Median   : ${pct(end_pnls, 50):+.2f}   <-- EXPECTED")
print(f"  75th pct : ${pct(end_pnls, 75):+.2f}")
print(f"  90th pct : ${pct(end_pnls, 90):+.2f}")
print(f"  95th pct : ${pct(end_pnls, 95):+.2f}   (good week)")
print(f"  Mean     : ${statistics.mean(end_pnls):+.2f}")
print(f"  Stdev    : ${statistics.stdev(end_pnls):.2f}")
print(f"  P(profitable after 7d): {prof:.1%}")

print(f"\nEnd-of-week balance (start $109):")
print(f"  5th  pct : ${pct(end_bals, 5):.2f}")
print(f"  Median   : ${pct(end_bals, 50):.2f}")
print(f"  95th pct : ${pct(end_bals, 95):.2f}")

print(f"\nMax drawdown within the 7-day week:")
print(f"  Median   : ${pct(dds, 50):.2f}")
print(f"  75th pct : ${pct(dds, 75):.2f}")
print(f"  95th pct : ${pct(dds, 95):.2f}   (worst case)")

print(f"\nTrades per week:")
print(f"  Median total : {pct(trades, 50):.0f}  ({pct(trades, 50)/7:.1f}/day)")
print(f"  P5..P95      : {pct(trades, 5):.0f}..{pct(trades, 95):.0f}")

print(f"\nBest single day:")
print(f"  Median   : ${pct(bests, 50):+.2f}")
print(f"  95th pct : ${pct(bests, 95):+.2f}")
print(f"\nWorst single day:")
print(f"  Median   : ${pct(worsts, 50):+.2f}")
print(f"  5th  pct : ${pct(worsts, 5):+.2f}")

print(f"\nBalance milestones (probability touched within the 7 days):")
for m in MILESTONES_UP:
    p = sum(1 for r in results if r["hit_up"][m]) / N_SIMS
    print(f"  >= ${m}: {p:.1%}")
print()
for m in MILESTONES_DOWN:
    p = sum(1 for r in results if r["hit_down"][m]) / N_SIMS
    print(f"  <= ${m}: {p:.1%}")

# --- Risk-of-ruin / margin analysis ---
# Each trade requires margin = contracts * entry_price + fee.
# MOVING_BIG margin: 20*0.80 + 0.23 = $16.23 per trade
# If account drops below ~$16 we can't place MOVING_BIG. ~$5 minimum for the others.
mb_margin = 20 * 0.80 + LIVE_TIERS["MOVING_BIG"]["fee"]
sm_margin = 5 * 0.80 + LIVE_TIERS["MOVING_SMALL"]["fee"]
print(f"\nMargin per trade: MOVING_BIG ${mb_margin:.2f}, FLAT/MOVING_SMALL ${sm_margin:.2f}")
print(f"Min cash to support concurrent MB trades is the real constraint.")

# What % of sims see balance dip below $16.23 (lose MOVING_BIG capability)?
p_under_mb = sum(1 for r in results if any(b < mb_margin for b in r["bal_path"])) / N_SIMS
print(f"P(balance dips below ${mb_margin:.2f} -> MOVING_BIG margin-capped): {p_under_mb:.1%}")


# ===========================================================================
# STRESS SCENARIOS: haircut WR to model fill slippage, regime drift, missed
# trades. The backtest assumes you always get filled at exactly 0.80 — in
# live, real fills are noisier and the realized WR may be a few pp lower.
# ===========================================================================

def run_stress(label, wr_haircut_pp, fill_rate=1.0, n_sims=10000):
    """Re-run MC with each tier's WR reduced by wr_haircut_pp and trade count
    multiplied by fill_rate."""
    tiers = {}
    for name, t in LIVE_TIERS.items():
        tiers[name] = {**t}
        tiers[name]["wr"] = max(0.0, t["wr"] - wr_haircut_pp / 100.0)
        tiers[name]["per_day"] = t["per_day"] * fill_rate

    rng = random.Random(7)
    end_pnls = []
    dds = []
    for _ in range(n_sims):
        cum = [0.0]
        for d in range(7):
            for nm, t in tiers.items():
                lam = t["per_day"]
                n_tr = max(0, int(round(rng.gauss(lam, math.sqrt(max(lam, 0.01))))))
                day_p = 0.0
                for _ in range(n_tr):
                    if rng.random() < t["wr"]:
                        day_p += t["win_pnl"]
                    else:
                        day_p += t["loss_pnl"]
                cum.append(cum[-1] + day_p)
        # collapse: cum has 1 + 7*3 = 22 points, but day-end is every 3 entries
        # we just want week-end and intra-week DD on the trade-grouped path
        end_pnls.append(cum[-1])
        peak = 0.0; dd = 0.0
        for v in cum:
            peak = max(peak, v); dd = max(dd, peak - v)
        dds.append(dd)
    end_pnls.sort(); dds.sort()
    prof = sum(1 for v in end_pnls if v > 0) / n_sims
    print(f"\n--- STRESS: {label} ---")
    print(f"  WR haircut: -{wr_haircut_pp} pp;  fill rate: {fill_rate:.0%}")
    print(f"  Median 7d P&L : ${pct(end_pnls, 50):+.2f}")
    print(f"  5th pct       : ${pct(end_pnls, 5):+.2f}")
    print(f"  10th pct      : ${pct(end_pnls, 10):+.2f}")
    print(f"  95th pct      : ${pct(end_pnls, 95):+.2f}")
    print(f"  P(profitable) : {prof:.1%}")
    print(f"  Median max DD : ${pct(dds, 50):.2f}")
    print(f"  95th pct DD   : ${pct(dds, 95):.2f}")

print("\n" + "=" * 60)
print("STRESS SCENARIOS — apply WR haircut + fill-rate haircut")
print("=" * 60)
# WR haircut: realized live WR is typically lower than backtest due to fill
# slippage, partial fills, latency-driven misses.
run_stress("Mild stress     (~5 pp WR haircut, 90% fill rate)", 5,  0.90)
run_stress("Moderate stress (10 pp WR haircut, 75% fill rate)", 10, 0.75)
run_stress("Severe stress   (15 pp WR haircut, 60% fill rate)", 15, 0.60)
run_stress("Worst-case      (20 pp WR haircut, 50% fill rate)", 20, 0.50)
