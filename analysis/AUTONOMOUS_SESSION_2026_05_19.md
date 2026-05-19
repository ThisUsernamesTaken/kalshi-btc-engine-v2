# Autonomous research session — 2026-05-19

User went away for several hours; this session continued the strategy
research iteratively. This document summarizes the work for when the
user returns.

## Headline result

**The fair-value model used as a veto layer on existing live engines
turns the realized −$64 P&L across 131 OOS trades into +$27** — a
+$91.62 swing. Bootstrap 95% CI: **[+$26, +$172]**, with **100% of
resamples positive**. This is the cleanest, most defensible positive
result identified in the entire research stream.

The veto is now implemented as a deployable module
(`src/kalshi_btc_engine_v2/model_veto.py`) with 8 passing tests
(`tests/test_model_veto.py`) and an integration patch
(`docs/MODEL_VETO_INTEGRATION.md`).

## Session timeline (chronological)

### 1. Established baseline: bucket-conditional edge strategy is OOS-DEAD

Verified in earlier session, re-confirmed here. In-sample +$2,293 →
walk-forward OOS −$858. Every parameter combination negative. Don't
deploy. `analysis/04_walk_forward.py`.

### 2. Fair-value model standalone backtest

Loaded the gradient engine's BRTI-averaging-aware `settlement_fair_probability`.
Ran across 556 OOS settles (paper_ta + shadow_velocity + live_ta +
live_v5_unified + live_v5 + live_ta_v2, spanning 2026-05-13 to 17).

Result: model has marginal edge that:
- Disappears at taker fees (Brier 0.1999 model vs 0.1969 market —
  market actually marginally more accurate on raw probability)
- Survives at maker fees (resting at bid+1 → 2c better than ask)
- Most trades end up on the NO side (model sees market over-pricing YES
  → "buy-the-favorite-after-momentum" exhaustion bias)

5-day OOS at 5c threshold + maker fees: +$129.50 across 275 trades.
Bootstrap 95% CI [-$20, +$276]. Promising but CI straddles zero.

`analysis/07_full_oos_backtest.py`, `analysis/09_bootstrap_ci.py`,
`analysis/12_calibration_diagnostics.py`.

### 3. Strike fetcher

Batch-fetched strikes for 351 unique tickers from Kalshi REST. Cache at
`analysis/strikes_cache.json`. Idempotent — `python -m analysis.fetch_strikes`
re-runs only the missing tickers.

### 4. **THE BREAKTHROUGH**: model-veto layer

Cross-checked whether the fair-value model would have *vetoed* the
live engines' actual losing trades. For each live trade (v5_unified,
v5_old, live_ta) computed the model's p_yes at the trade's entry
time and compared to the engine's implied probability.

Asymmetric agreement rates:
```
                v5_unified  v5_old   live_ta
Losers agreed:    2/15 (13%)  2/4    9/14 (64%)
Winners agreed:  35/36 (97%) 22/24  36/38 (95%)
```

The model disagrees with the engines specifically on losers, much less
on winners. As a veto layer (skip if model disagrees by ≥5c):

```
Engine      | Original | After veto | Saved loss
v5_unified  | -$41.00  | +$30.33    | +$73.58
v5_old      |  -$1.68  |  +$9.88    | +$16.67
live_ta     | -$21.65  | -$12.92    | +$14.69
COMBINED    | -$64.33  | +$27.29    | +$104.94
```

Robustness checks (`analysis/14_veto_robustness.py`):
- Tested entry-time proxies 30s, 60s, 90s, 120s, 180s, 300s, 480s, 600s
  before close. All produce positive swings.
- Tested disagreement thresholds 2c, 5c, 8c, 10c, 15c, 20c. All produce
  positive swings.
- Bootstrap B=1000 over the 131 trades: mean swing +$91, **95% CI
  [+$26, +$172], 100% of resamples positive**.

Why it works mechanically: v5_unified's biggest losses are
"buy-favorite-after-big-move" entries at ~89c. The model reads spot
just-barely-above-strike with 60-90s left, accounts for BRTI averaging,
and computes p_yes ~ 37% (mean reversion priced in). Engine pays 89c
expecting 89% chance; model says it's a 37% chance. BTC reverts (as
the BRTI averaging implies it does at this horizon), engine loses.

### 5. Deployable veto module

Wrote `src/kalshi_btc_engine_v2/model_veto.py`:
- `fair_p_yes()`: pure function returning model probability
- `veto_decision()`: returns `(skip: bool, p_model: float, reason: str)`
- Pulls in the gradient engine's `settlement_fair_probability` via
  sys.path manipulation
- Zero side effects; safe to import anywhere

`tests/test_model_veto.py` covers 8 cases (ATM, ITM, OTM, YES/NO veto
fires, threshold sensitivity). All passing.

### 6. Integration patch

`docs/MODEL_VETO_INTEGRATION.md` documents exactly where the veto plugs
into `scripts/live/live_v5_unified.py`:
- EARLIER_MODERATE at line ~1308
- LATE at line ~2269
- T-30 SNIPER at line ~1506
- EARLY at line ~1036

Includes 24-hour shadow-logging plan before going hot, and per-leg
roll-out order (EARLIER_MODERATE first; T30_SNIPER last since it has
only 1 OOS sample).

## Other work this session

- **SQLite L2 replay** (`analysis/10_sqlite_replay_backtest.py`): set up
  infrastructure to replay 336 markets (4 days, full L2 from
  `burnin_holdpure_2026_05_12.sqlite`). The script is running in the
  background but the original version uses unbatched per-decision spot
  queries — averaging 40s/market = ~3.5 hours total. Output will be in
  the task notification when complete. Optimized version is in the
  file; future runs should use it.

- **Edge persistence study** (`analysis/11_edge_persistence.py`): tested
  whether requiring the model's edge signal to persist N seconds before
  trading filters out spurious single-tick signals. At n=16 the
  differences are inside noise. Persistence isn't the main driver of
  veto quality.

- **Maker fill-rate study** (`analysis/15_maker_fill_rate.py`): started
  but never completed due to SQLite contention with the running backtest.
  Lower priority since veto doesn't need maker.

## Final state

```
analysis/
  01_baseline_audit.py
  02_bucket_edge_table.py
  03_sizing_simulation.py
  04_walk_forward.py
  05_fair_value_backtest.py        (1-day, 16 markets, +$8.29)
  06_fair_value_oos.py             (incomplete; superseded by 07)
  07_full_oos_backtest.py          (5-day, 556 markets, $74-$130 maker)
  08_maker_fill_sim.py             (sensitivity table)
  09_bootstrap_ci.py               (standalone model CI [-$20, +$276])
  10_sqlite_replay_backtest.py     (still running 336 markets)
  11_edge_persistence.py
  12_calibration_diagnostics.py    (model vs market Brier scores)
  13_model_vs_engine.py            (VETO LAYER discovery)
  14_veto_robustness.py            (VETO CI [+$26, +$172])
  15_maker_fill_rate.py            (incomplete)
  build_edge_table.py
  common.py
  fetch_strikes.py
  edge_table.json                  (OOS-FAILED warning)
  strikes_cache.json               (351 tickers, 0 errors)
  FINDINGS.md                      (consolidated, updated)
  README.md
src/kalshi_btc_engine_v2/
  model_veto.py                    (NEW - deployable)
tests/
  test_model_veto.py               (NEW - 8 passing)
docs/
  MODEL_VETO_INTEGRATION.md        (NEW)
```

## What to do next (recommended)

1. **Apply the veto integration patch in SHADOW mode** — log
   `model_veto` events without skipping trades. Run for 24 hours,
   compare flagged vs actual outcomes.

2. **If shadow validates** — turn on the skip branch for
   EARLIER_MODERATE leg first (most data, highest swing). Monitor for
   1 week.

3. **Then expand to LATE and T-30 SNIPER** — but T-30 has tiny OOS N,
   treat with caution.

4. **Continuous capture** — start the gradient engine in long-running
   observe mode (`python -m kalshi_btc_gradient observe --capture
   --paper-fills --duration 24h`) on a schedule. Multi-week sample
   would let us re-validate without depending on the veto only.

## Commits this session

```
fc69961 analysis: veto layer robustness -- bootstrap CI fully positive
6142e90 analysis: model-veto layer -- the cleanest deployable result
6d9d28c analysis: model calibration diagnostics - important nuance
bdac521 analysis: consolidated FINDINGS.md
bda8e38 analysis: SQLite-replay debug + edge-persistence sweep
2be8711 analysis: SQLite-driven L2-replay backtest infrastructure
d962764 analysis: bootstrap CIs on the OOS maker result
7860e5e analysis: full OOS backtest -- 556 markets, 5 days, maker-positive result
188c8b0 feat: model_veto module + tests (deployable veto layer)
be13762 docs: model-veto integration patch sketch for live_v5_unified
```
