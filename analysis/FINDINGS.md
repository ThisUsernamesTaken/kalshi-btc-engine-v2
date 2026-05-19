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
| Fair-value model (raw, MAKER rest, 5c, 5 days) | n/a | +$129.50 / 275 tr | promising, CI straddles 0 |
| **Fair-value model as VETO layer on live engines** | n/a | **+$91.62 swing / 131 tr / 5 days OOS** | **BEST DEPLOYABLE** — CI fully positive, 100% bootstrap resamples positive |

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

## STRONGEST FINDING: fair-value model as a VETO layer

`13_model_vs_engine.py` and `14_veto_robustness.py` show that the model
disagrees with each live engine's direction on a *much* higher fraction of
losing trades than winning trades. This makes it perfect for a veto layer:

| Engine | Losers flagged | Winners flagged | Original P&L | After-veto P&L |
|---|---|---|---|---|
| v5_unified | 87% (13/15) | 3% (1/36) | −$41.00 | **+$30.33** |
| v5_old | 50% (2/4) | 8% (2/24) | −$1.68 | **+$9.88** |
| live_ta | 36% (5/14) | 5% (2/38) | −$21.65 | −$12.92 |
| **Combined** | — | — | **−$64.33** | **+$27.29** |

**$91.62 swing across 131 trades.** Bootstrap CI [+$26.35, +$172.49] —
100% of resamples positive. Robust to entry-time proxy (30s-600s) and
disagreement threshold (2-20c).

The rule:
```
At entry trigger:
  p_engine_implied = engine_paid_for_this_side / 100
  p_model = settlement_fair_probability(spot, strike, tau, sigma_realized)
  if abs(p_model - p_engine_implied) >= 0.05 and they disagree on direction:
      SKIP TRADE
  else:
      proceed as engine intended
```

The model isn't predicting better on average (Brier 0.1999 model vs 0.1969
market — market actually marginally better at raw probability). Its value
is **directional correctness on disagreements** — when the model and
market disagree, the model is more often right about the realized direction.

Why this works mechanically: the engines (especially v5_unified) chase
exhaustion — they enter the favorite after a big BTC move when implied p
is high (e.g., 89c). The model sees BTC mean-reverting toward the 60s BRTI
window and prices the favorite lower (e.g., 37c). When BTC reverts (often,
because Kalshi settlement is averaged over the final minute), the model
is right.

## Other promising strategy: fair-value model standalone

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
- **Model-veto layer on v5_unified** (highest leverage; CI fully positive).
  Add ~10 lines to the live trigger logic: compute p_model at trigger,
  skip if it disagrees with the trade direction by ≥5c. Even with v5_unified
  currently stopped, the layer can be inserted before resuming live.
- The fair-value model standalone with `|edge| ≥ 5c`, maker-rest orders,
  180-600s entry window (CI straddles zero so lower priority than veto)
- A "watchdog wrapper" that runs the gradient engine 24/7 with
  `observe --paper-fills --capture` to grow the OOS sample beyond 5 days
- The maker-rest fill rate in live conditions (separate experiment)

**ABANDON UNLESS NEW EVIDENCE**:
- The bucket-conditional edge table approach as a trading rule
- RV regime FLIP (already tried; lost 5/5 live)
- LATE leg as currently structured (structurally −EV in v5)

## Veto-shadow operational findings (2026-05-19)

Running `scripts/live/veto_shadow_monitor.py --from-start` on the
v5_unified historical log yields a per-leg breakdown:

| Leg | Triggers | Would skip | Skip rate | By side (skip) |
|---|---|---|---|---|
| EARLIER_MODERATE | 60 | 26 | **43.3%** | 13 YES, 13 NO |
| LATE | 77 | 22 | **28.6%** | 13 YES, 9 NO |
| T-30 SNIPER | 34 | n/a | — | — (missing fields) |

Findings:
- EARLIER_MODERATE has the highest skip rate, consistent with that leg
  being the loss-driver. Deploy veto here first.
- LATE skip rate 28.6% is lower but still meaningful.
- T-30 SNIPER triggers in `live_v5_unified.py` don't currently log
  `btc_now` or `strike` — the veto shadow correctly skips them as
  `missing_required_fields`. To enable veto on snipers, a 2-line patch
  to `live_v5_unified.py` is needed to populate those fields in the
  `t30_sniper_trigger` event.

## Deliverables in this folder

| File | Purpose |
|---|---|
| `FINDINGS.md` | This document |
| `AUTONOMOUS_SESSION_2026_05_19.md` | Chronological log of the autonomous session |
| `01_*.py` through `15_*.py` | Reproducible analysis scripts |
| `fetch_strikes.py` | Strike fetcher (idempotent) |
| `strikes_cache.json` | 351 cached Kalshi REST responses |
| `edge_table.json` | OOS-FAILED bucket table (diagnostic only) |
| `common.py` / `build_edge_table.py` | Shared infrastructure |

## Deployable artifacts (outside `analysis/`)

| File | Purpose |
|---|---|
| `src/kalshi_btc_engine_v2/model_veto.py` | Drop-in veto module |
| `tests/test_model_veto.py` | 8 passing unit tests |
| `docs/MODEL_VETO_INTEGRATION.md` | Where/how to wire it into live_v5_unified |
| `scripts/live/veto_shadow_monitor.py` | Live-tail shadow auditor |
| `scripts/live/check_model_prob.py` | Ad-hoc probability/veto CLI |
