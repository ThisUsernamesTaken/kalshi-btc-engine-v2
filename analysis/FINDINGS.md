# Strategy research — consolidated findings (2026-05-19)

This document consolidates the findings from the analysis pipeline
(`01_*.py` through `11_*.py`). Numbers cited are reproducible from the
data files in `D:\Trading\kalshi-btc-engine-v2\data\` plus the public
Kalshi REST settlement cache (`strikes_cache.json`).

## TL;DR

| Strategy | In-sample | Walk-forward OOS | Verdict |
|---|---|---|---|
| Bucket-conditional edge + dollar sizing + streak halt | +$2,293 / 245 tr | **−$858 / 215 tr** | **DEAD** — overfit |
| Pine bar 5-6 + entry 92c+ filter | +$40 / 42 tr (chrono) | n/a | weak signal at small N |
| Fair-value model (raw, taker, 10c, 1 day) | +$8.29 / 15 tr | n/a | early signal, 1 day |
| **Fair-value model (raw, MAKER rest, 5c, 5 days)** | n/a | **+$129.50 / 275 tr** | **PROMISING** — see caveats |
| Fair-value model + 180-600s window only (maker, 10c) | n/a | +$62.18 / 45 tr | tighter CI, smaller sample |

## Structural facts (verified — independent of strategy choice)

These are observations about the market and the engines that any future
work should respect:

1. **Live engines are all net-negative under realistic execution** (+2c
   slippage per entry). Reported P&Ls are illusory; realistic P&L matches
   the actual account-balance drift.
2. **T-30 sniper fill rate is 2.9%** — 33 of 34 triggers don't execute.
   The validated 44/44 backtest signal is essentially undeployed.
3. **Engines hold to settle** — losses are entry problems, not exit
   problems. The early-exit hypothesis is moot for the loss-driving engines.
4. **Settlement = simple mean of 60 BRTI ticks (NOT VWAP).** Per Kalshi
   contract text and observed settlements.json.
5. **BRTI constituents** are Coinbase, Kraken, Bitstamp, Gemini, itBit,
   LMAX, Bullish, Crypto.com. **Binance is NOT a constituent.** Models
   built on Binance have persistent basis.
6. **Maker fees are 75% cheaper than taker**: `ceil(0.0175·C·P·(1−P))`
   vs `ceil(0.07·...)`. Materially changes breakeven math at small sizes.
7. **The fair-value model is well-calibrated** at n≥500: model and market
   both within ~5pp of bucket midpoints (vs 30pp at n=16).
8. **The model consistently sees the market over-pricing YES.** On the
   2026-05-18 capture every triggered trade was NO. Translates to
   "mean-reverting at settlement" — contracts extrapolate recent BTC
   direction further than the 60s BRTI mean actually goes.

## Failed strategy: bucket-conditional edge

`02_bucket_edge_table.py` finds 12 buckets where engine WR diverges from
breakeven enough to either KEEP or INVERT. Buckets like `v5/EM/cursed-stripe/flat`
(13 trades, 77% engine WR, breakeven needs 89%) suggest inverting at avg
entry 88.7c gives +9.8c/ct edge.

`03_sizing_simulation.py` shows that with bucket-routed trades, dollar-
at-risk discrete tiers, and a 2-loss-streak halt per bucket, projected
in-sample P&L is +$2,293.

**`04_walk_forward.py` kills it.** Across every parameter combination
(min_n=3 to 20, edge threshold 5c to 12c, fold size 1d or 2d, skip-only
or invert-allowed), OOS performance is negative. The most defensible
config (n≥20, edge≥12c) trades only 7 times for −$19.

**Why the failure**: bucket actions are stable across folds (only 1 flip
in 20 high-frequency buckets) but the edge *magnitudes* swing by an order
of magnitude. The strategy picks the right buckets and bets the wrong
sizes. WRs at n=3-15 are inside binomial noise.

## Promising strategy: fair-value model

The gradient engine's `models/probability.py:settlement_fair_probability`
implements the BRTI-averaging-aware log-normal CDF. Variance time is
`τ − 2w/3` pre-window, `remaining/3` inside the 60s window. The formula
is fixed math — no fitting against the data — so OOS generalization is
about the model's calibration, not about sample-size noise.

### Five-day OOS (n=556 markets, `07_full_oos_backtest.py`)

| Threshold | Taker net | Maker (rest at bid+1) | Trades | WR |
|---|---|---|---|---|
| 3c  | −$64 | **+$45** | 362 | 53.0% |
| **5c** | −$7 | **+$129.50** | 275 | 53.8% |
| 8c  | −$21 | +$59 | 162 | 50.6% |
| 10c | −$9  | +$44 | 106 | 50.0% |
| 12c | +$22 | +$52 | 59  | 52.5% |

**Every maker threshold 3-12c is positive.** With taker fees the marginal
edge gets erased; with maker rest, it survives. The 5c threshold has the
most trades and the highest absolute net.

### Bootstrap CIs (`09_bootstrap_ci.py`, B=5000)

| Config | n | Mean | 95% CI |
|---|---|---|---|
| 5c maker, all windows | 275 | +$129.50 | **[−$20, +$276]** |
| 10c maker, 180-600s | 45 | +$62.18 | [−$2, +$126] |
| 12c maker, all windows | 59 | +$51.57 | [−$21, +$122] |

**Every config's 95% lower bound straddles zero.** Promising but not
robust at this N. Need more data (multi-week capture) to tighten CIs.

### Per-day stability (5c maker, all windows)

| Date | n | WR | Net |
|---|---|---|---|
| 2026-05-13 | 19 | 68% | +$31.60 |
| 2026-05-14 | 81 | 67% | +$19.54 |
| 2026-05-15 | 92 | 49% | +$13.35 |
| 2026-05-16 | 79 | 44% | +$68.45 |
| 2026-05-17 | 4  | 25% | −$3.44 |

**Every day positive except the 4-trade tail.** The negative-WR day (05-16)
still produced +$68 — winners were large enough to offset frequent small
losses. That's consistent with a mean-reverting strategy.

## Operational requirement

The maker-fee result is load-bearing. **Deployment requires placing resting
limit orders (price = best_bid + 1c)**, not lifting asks. The current live
trader uses IOC/marketable-limit entries that pay taker fees and erase
the edge.

Fill-rate sensitivity (`08_maker_fill_sim.py`):

| Assumed fill rate | Expected 5-day net |
|---|---|
| 10% | +$13 |
| 30% | +$39 |
| 50% | +$65 |
| 100% (oracle) | +$130 |

Even at 10% fill rate, the strategy is positive over 5 days. But the
gradient engine's 2026-05-18 capture had no temporal overlap with the
556 OOS settles, so we couldn't measure actual fill rates on the
positive-edge trades. That's the next required validation.

## Other findings

### Entry window matters

| Window (sec-to-close) | n | WR | Net (taker, 10c) |
|---|---|---|---|
| 180-600s | 45 | 51% | **+$39.50** |
| 600-900s | 64 | 48% | **−$50.73** |

The model has signal in the mid-cycle (3-10 min to close), noise at
session start (10-15 min). Restricting to 180-600s helps even at taker
fees.

### Edge persistence

`11_edge_persistence.py` tested requiring the model's edge signal to
persist for N seconds before trading. At n=16 the differences are inside
noise; persistence doesn't dramatically help. The model fires from the
start of the cycle and stays consistent — single-tick noise isn't the
problem.

### Model bias is structural, not cherry-picked

On the 2026-05-18 capture, **every triggered trade was NO**. The model
consistently sees the market over-pricing YES. This aligns with the
"momentum exhaustion" / "buying after the move" pattern identified in
the loss-driver analysis. The model captures a behavioral bias of
market makers extrapolating recent BTC direction past the BRTI mean.

## Reproducibility

Run all checks from repo root:

```powershell
$env:PYTHONPATH = "src"
$py = "C:\Users\coleb\AppData\Local\Python\bin\python.exe"

# Baseline + bucket-edge diagnostics
& $py -m analysis.01_baseline_audit
& $py -m analysis.02_bucket_edge_table

# Bucket-strategy in-sample simulation (DO NOT DEPLOY)
& $py -m analysis.03_sizing_simulation

# Walk-forward OOS validation (the strategy-killer)
& $py -m analysis.04_walk_forward

# Fair-value model OOS validation
& $py -m analysis.05_fair_value_backtest      # 1-day, 16 markets
& $py -m analysis.fetch_strikes               # populate strikes_cache.json
& $py -m analysis.07_full_oos_backtest        # 5-day, 556 markets
& $py -m analysis.09_bootstrap_ci             # bootstrap CIs
& $py -m analysis.10_sqlite_replay_backtest   # 4-day SQLite L2 replay
& $py -m analysis.11_edge_persistence         # edge-persistence sensitivity
```

## What to deploy / do not deploy

**DO NOT DEPLOY**:
- `analysis/edge_table.json` — flagged `oos_validation_status: FAILED`
- Any "bucket-conditional INVERT" logic — overfit at this N
- Any sizing scheme that scales above ~10ct without paper validation

**WORTH PAPER-VALIDATING**:
- The fair-value model (gradient engine) with `|edge| ≥ 5c`, maker-rest
  orders, 180-600s entry window
- A "watchdog wrapper" that runs the gradient engine 24/7 with
  `observe --paper-fills --capture` to grow the OOS sample beyond 5 days
- The maker-rest fill rate in live conditions (separate experiment)

**ABANDON UNLESS NEW EVIDENCE**:
- The bucket-conditional edge table approach as a trading rule
- RV regime FLIP (already tried; lost 5/5 live)
- LATE leg as currently structured (structurally −EV in v5)
