# Veto-enabled live trader — handoff (2026-05-19)

## State

**The veto-enabled live trader is built and tested. Ready to launch in
shadow mode immediately.**

| Component | File | Status |
|---|---|---|
| Veto module | `src/kalshi_btc_engine_v2/model_veto.py` | tested, 8/8 |
| Live trader (patched) | `scripts/live/live_v5_unified.py` | tested, 187/187 |
| Watchdog (veto-enabled) | `scripts/live/watchdog_v5_unified_veto.cmd` | new |
| Shadow monitor | `scripts/live/veto_shadow_monitor.py` | works on historical |
| Ad-hoc CLI | `scripts/live/check_model_prob.py` | works |
| Runbook | [`RUNNING_VETO.md`](RUNNING_VETO.md) | written |
| Integration patch doc | `docs/MODEL_VETO_INTEGRATION.md` | written |
| Consolidated findings | `analysis/FINDINGS.md` | updated |

## How to launch

**Shadow mode (safe, recommended first step):**

```cmd
C:\Trading\kalshi-btc-engine-v2\scripts\live\watchdog_v5_unified_veto.cmd
```

This launches the trader with `--veto-mode shadow`: every trigger gets
a model_veto event logged with `would_skip` flag, but **no trades are
skipped**. Run for 24h to validate the veto fires sensibly in production
before flipping to live veto.

**To flip to live veto** (after 24h shadow):

Edit `scripts/live/watchdog_v5_unified_veto.cmd`:
```diff
- set VETO_FLAGS=--veto-mode shadow --veto-threshold 5
+ set VETO_FLAGS=--veto-mode skip   --veto-threshold 5
```

Restart the watchdog. Now `MODEL_VETO`-flagged trades will be skipped.

**To roll back entirely**: stop the veto watchdog and resume the original
`watchdog_v5_unified.cmd`.

## What was validated

**24 commits this session. 8 new tests pass. 187/187 full suite passes.**

The veto layer was OOS-validated on 131 live trades across 5 days from
3 independent engines (v5_unified + v5_old + live_ta):

| Strategy | Realized swing | 95% Bootstrap CI | % positive resamples |
|---|---|---|---|
| Veto-only (skip ≥5c) | **+$92** | [+$26, +$172] | **99.9%** |
| Veto + flip (skip ≥8c, flip ≥30c) | +$173 | [+$68, +$296] | 99.9% |

Robustness:
- Every sigma 0.15–1.50 produces positive swing ($60-$87)
- Every entry-time proxy 30s–600s produces positive swing
- Every disagreement threshold 2c–20c produces positive swing
- 13/15 v5_unified losers caught by veto (87%); only 1/36 winners (3%)

## Caveats

1. **The standalone maker-rest strategy is more fragile than the proxy
   backtest suggested.** Realistic L2 simulation (`analysis/10`, 305
   trades, 4-day SQLite tape) shows it breaks even (−$0.98 / 27% WR).
   The veto layer is **unaffected** because it operates on trades
   placed at taker fees regardless of fill model. **Do not deploy the
   standalone maker-rest strategy without further validation.**

2. **The flip layer has small N (11 trades, 8W/3L).** Bootstrap CI is
   strong but the absolute sample is thin. Deploy veto-only first;
   evaluate adding flip after 2+ weeks of live veto data.

3. **The bucket-conditional strategy is DEAD.** `analysis/edge_table.json`
   has `oos_validation_status: FAILED`. Do not deploy any strategy
   that consults the bucket table as a trading rule.

## Files added this session

```
analysis/
  01-04: bucket strategy (proven OOS-dead)
  05-07: fair-value model OOS (1-day -> 5-day)
  08-09: maker fill / bootstrap CIs
  10:    SQLite L2 replay (305 trades, realistic, breakeven)
  11:    edge persistence study
  12:    calibration diagnostics (model vs market Brier)
  13:    MODEL VETO discovery (87% vs 3% loser-vs-winner flag asymmetry)
  14:    veto robustness (sigma, threshold, proxy)
  15:    maker fill rate (incomplete, low priority)
  16:    veto sigma sensitivity (0.15-1.50 all positive)
  17:    veto + flip hybrid (+$173 swing)
  18:    flip bootstrap (99.9% positive CIs)
  AUTONOMOUS_SESSION_2026_05_19.md
  FINDINGS.md (consolidated)
  fetch_strikes.py, strikes_cache.json (351 cached)
  build_edge_table.py, edge_table.json (OOS-FAILED warning)

src/kalshi_btc_engine_v2/
  model_veto.py  (drop-in)

scripts/live/
  live_v5_unified.py  (patched: --veto-mode flag at 4 trigger sites)
  watchdog_v5_unified_veto.cmd  (new watchdog)
  veto_shadow_monitor.py  (live-tail auditor)
  check_model_prob.py  (ad-hoc CLI)

docs/
  MODEL_VETO_INTEGRATION.md

tests/
  test_model_veto.py  (8 tests)

(repo root)
  RUNNING_VETO.md
  HANDOFF_VETO_2026_05_19.md  (this file)
```

## Next actions for the user

1. **Read `RUNNING_VETO.md`.** Three-step rollout plan and what to watch in logs.
2. **Launch shadow mode.** `scripts\live\watchdog_v5_unified_veto.cmd`.
3. **Audit veto events for 24 hours.** Goal: confirm ~30-50% of triggers
   get `would_skip: true`, matching the OOS pattern.
4. **If shadow looks good**: flip to `--veto-mode skip`. Re-enable
   EARLIER_MODERATE (since the veto's strongest signal is on EM losers)
   by removing `--disable-earlier-moderate`.
5. **After 1 week clean**: consider re-enabling LATE with veto, or
   evaluating the flip layer.

## Bugs found and fixed during integration testing

1. **Unit bug (commit `281e12a`)**: the engine's `realized_vol_5m`
   returns `sigma_per_sec_log × sqrt(60) × 100` (% per √min), NOT
   annualized vol. The original integration passed it directly as
   `sigma_annualized` to the model, treating BTC as ~10% vol when
   it's ~50%. Fixed with a `_rv5m_to_sigma_ann` helper applying the
   `× 7.2498` conversion. Post-fix audit shows swing improved from
   +$19 → +$30 on historical shadow data.

2. **RV-availability gap (commit `b477860`)**: `realized_vol_5m`
   requires ≥30 samples spanning ≥2.5 min, so ~70% of EM triggers in
   the live log have `rv_5m=None` (shortly after watchdog restarts).
   Added `realized_vol_best_effort` which falls back to 120s and
   then 60s windows. After 60s of BTC buffer fill, the veto has a
   usable RV estimate at every trigger.

## Honest assessment

The veto layer is the **first non-overfit, OOS-validated, deployment-
ready positive-EV finding** in the analysis pipeline. The math (BRTI
60s averaging mean-reversion captured by the gradient engine's
settlement_fair_probability) is structurally sound. The bootstrap CIs
say the swing is real, not noise. The integration is minimal (~180
lines), safe-by-default (`--veto-mode off`), and reversible.

It's not a money printer. Even at +$92 OOS swing over 131 trades, the
EV is +$0.70/trade. Scaling depends on the engine's trigger rate (~10
trades/day visible in the historical log). At full deployment with
current production flags (T-30 sniper only), the veto rarely fires
because sniper entries usually agree with the model. The veto's value
lands when EARLIER_MODERATE re-enables.

**Realistic production expectations** (after fixing the rv_5m unit bug):
the historical shadow re-run shows ~+$30 swing on 46 settled+vetoed
trades — less than the OOS analysis's +$92 because the historical
trigger events sometimes lacked rv_5m. With the new best-effort RV
fallback in the live code, the LIVE veto should get closer to the
OOS +$92 since it has the live BTC buffer. Audit weekly.

The biggest open question: does the BRTI mean-reversion regime persist?
If the loser-flag-rate on EM drops below 80%, pause and re-derive.
