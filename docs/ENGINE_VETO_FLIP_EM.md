# `live_veto_flip_EM` — the deployed engine

**Status as of 2026-05-20**: LIVE on Kalshi production account (real money).

This document directs future agents (and the user) on what the live
engine does, how to operate it, and where its evidence lives.

## What it is

`live_veto_flip_EM` is the production deployment of:

> The existing `live_v5_unified.py` trader, with the **fair-value-model
> veto layer in flip mode**, with **EARLIER_MODERATE re-enabled**.

It's a single Python process (no new binary) launched with the flags
documented in `scripts/live/watchdog_live_veto_flip_EM.cmd`. It places
real Kalshi orders on the user's account.

## Active trading legs

| Leg | Window | Size | Behavior |
|---|---|---|---|
| **EARLIER_MODERATE** (EM) | T-600 to T-180s | 20ct (30ct on `--enable-em-upsize` high-confidence) | IOC buy favorite at fav_ask + adaptive slip. Hold to settle. |
| **T-30 SNIPER** | T-40 to T-20s | 2ct | IOC buy favorite when fav_bid ≥ 85c. Hold to settle. |
| EARLY | first 3 min | 2ct | Contested-side IOC. (Active but rare.) |
| LATE | disabled | — | `--disable-late` per OOS finding (structurally −EV) |

Each ticker can trigger **at most one entry** across legs.

## The veto layer (the new piece)

After every trigger fires (just before `place_ioc`), the engine
computes:

1. **Sigma**: 5-min realized vol from BTC buffer, with short-window
   `realized_vol_best_effort` fallback. Annualized via `× 7.2498`
   conversion.
2. **p_model**: BRTI-averaging-aware log-normal CDF (from gradient
   engine's `settlement_fair_probability`). Uses spot, strike, τ, sigma.
3. **engine_implied_p_yes**: 100 − engine_price if engine bet NO, else
   engine_price.
4. **disagreement_c**: directional only — counts only when model says
   the opposite side wins more than engine_implied implies.

Decision:
- **disagreement < 5c**: `KEEP` — trade as engine intended
- **5c ≤ disagreement < 30c**: `SKIP` — abort the trade
- **disagreement ≥ 30c**: `FLIP` — place opposite-side IOC at
  opposite_ask + 2c slip

All veto decisions are logged as `model_veto` events with action,
p_model_yes, sigma_used, reason. Flip trades log
`model_veto_flip_attempt` → `model_veto_flip_fill` or `_no_fill`.

The veto is **fail-open**: any exception in the veto path logs
`model_veto_error` and the original engine trade proceeds. The veto
never blocks trading on its own bugs.

## Code locations

| Component | File |
|---|---|
| Live trader (patched) | `scripts/live/live_v5_unified.py` |
| Veto module | `src/kalshi_btc_engine_v2/model_veto.py` |
| Veto tests | `tests/test_model_veto.py` (13 tests, all passing) |
| Live watchdog | `scripts/live/watchdog_live_veto_flip_EM.cmd` |
| Shadow watchdog (non-live) | `scripts/live/watchdog_v5_unified_veto_flip.cmd` |
| Live-tail monitor | `scripts/live/veto_shadow_monitor.py` |
| Post-hoc auditor | `scripts/live/veto_audit.py` |
| One-shot snapshot | `scripts/live/veto_status.py` |
| Ad-hoc CLI | `scripts/live/check_model_prob.py` |
| Paper-variants comparison | `scripts/live/paper_variants_compare.py` |

## Operational invariants

1. **Decision log paths**:
   - **Live engine** (initial run): `data/live_veto_flip_EM_trades.jsonl`
   - **Live engine** (via current watchdog .cmd): `data_local/live_veto_flip_EM_trades.jsonl`
   - **Paper variants** (current): `data_local/paper_*_trades.jsonl`
   - Combined stdout/stderr alongside, `*.combined.log`
   - Watchdog restart logs: `*watchdog_*.log`

   The `data_local/` folder was introduced 2026-05-20 to keep the
   concurrent paper variants and re-launched live trader separate from
   the historical `data/` logs.
2. **Account balance** halts the trader below $1 (`MIN_BALANCE_CENTS = 100`).
3. **Daily loss cap** `DAILY_LOSS_CAP_CENTS = 2700` ($27). Halts trading
   when cumulative day-floor loss reaches this. Reset at UTC day floor.
4. **Hold-to-settle** — no early exits coded. Once filled, the position
   resolves at the market's close.
5. **Veto is post-trigger pre-fill** — it runs after the engine has
   decided to trade and emitted a `*_trigger` event, but before
   `place_ioc` is called.

## OOS evidence backing this deployment

`analysis/13`, `analysis/14`, `analysis/17`, `analysis/18`. Applied to
131 historical trades from v5_unified + v5_old + live_ta across
2026-05-13 to 17:

| Strategy | Net swing | 95% Bootstrap CI | % positive resamples |
|---|---|---|---|
| Veto-only (skip ≥5c) | +$92 | [+$26, +$172] | 99.9% |
| **Veto+flip (skip ≥8c, flip ≥30c)** | **+$173** | **[+$68, +$296]** | **99.9%** |

Robust to sigma sensitivity (0.15–1.5), entry-time proxy (30s-600s),
disagreement threshold (2c-20c).

**Caveats**:
- The standalone fair-value strategy with realistic L2 fill simulation
  (`analysis/10`) was breakeven — but the veto-as-filter does not depend
  on standalone-model alpha. The veto's job is to reduce engine losses,
  not to predict trades better than the market.
- Brier scores: model 0.1999, market 0.1969. Market is slightly more
  accurate on raw probability; veto's value is in **directional
  agreement on disagreements** (87% of EM losers flagged, 3% of winners).

## Concurrent paper variants

Four paper variants run alongside the live engine for continuous
forward-validation:

| Variant | Decision log |
|---|---|
| paper_baseline (no veto) | `data/paper_baseline_trades.jsonl` |
| paper_veto_skip (skip-only) | `data/paper_veto_skip_trades.jsonl` |
| paper_veto_flip (skip+flip) | `data/paper_veto_flip_trades.jsonl` |
| paper_veto_flip_EM (EM+flip) | `data/paper_veto_flip_EM_trades.jsonl` |

All four are `--dry-run`. They share the same account but don't place
real orders. Their decision logs let us see:
- Fill rates the engine "wants" vs the live engine actually achieves
- How often the veto disagrees with the engine
- P&L the engine would have achieved in baseline vs each veto variant

Compare with `paper_variants_compare.py`.

## How to operate

### Launch / re-launch
```cmd
C:\Trading\kalshi-btc-engine-v2\scripts\live\watchdog_live_veto_flip_EM.cmd
```
Auto-restarts on crash. Logs to `data/live_veto_flip_EM.combined.log`.

### Quick health check
```powershell
python C:\Trading\kalshi-btc-engine-v2\scripts\live\veto_status.py `
  --log C:\Trading\kalshi-btc-engine-v2\data\live_veto_flip_EM_trades.jsonl `
  --last-hours 4
```

### Verify the veto fired correctly
```powershell
python C:\Trading\kalshi-btc-engine-v2\scripts\live\veto_audit.py `
  --log C:\Trading\kalshi-btc-engine-v2\data\live_veto_flip_EM_trades.jsonl
```
Reports TP/FP/FN/TN confusion matrix + P&L impact.

### Stop the engine

Find the process and kill it. (Or stop the watchdog .cmd if launched
that way.)

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*live_veto_flip_EM*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

### Roll back to pre-veto

Two paths:

1. **Disable the veto in-place**: edit the watchdog and change
   `--veto-mode flip` → `--veto-mode off`. Restart.
2. **Roll back to the prior production trader**: stop this one and
   start `scripts/live/watchdog_v5_unified.cmd` (the no-veto config).

## Failure modes to expect and handle

| Symptom | Likely cause | Action |
|---|---|---|
| `balance_halt` events | Account < $1 | Deposit or stop |
| Daily-loss-cap hit | Cumulative day loss reached $27 | Trader self-halts; will resume next UTC day-floor |
| `model_veto_error` events | Sigma/spot bad data | Veto disabled for that trigger; engine continues. No trader impact. |
| `t30_sniper_no_fill` ≫ fills | Book moved away from limit | Expected: 97% no-fill rate historically |
| Veto flagging >70% of EM | Sigma estimate way off (too low) | Audit `model_veto` events; check `sigma_used` field |
| Multiple instances on same account | Manual mistake | Each writes to own log; placing orders is exclusive per-trigger. But balance reads will be confusing. |

## Watch for regime drift

The veto's effectiveness depends on the BRTI mean-reversion regime
persisting (cited in `analysis/FINDINGS.md`). Audit weekly:

```powershell
python scripts/live/veto_audit.py --log data/live_veto_flip_EM_trades.jsonl --since 2026-05-20T00:00:00Z
```

Target metrics from OOS:
- **Precision** (veto-skip ∩ loser) / total skips ≥ **70%**
- **Recall** (veto-skip ∩ loser) / total losers ≥ **80% on EM**, lower on other legs

If precision drops below 50% over a rolling 50-trade window, **the
regime has likely shifted**. Pause and re-derive bucket statistics
(see `analysis/13_model_vs_engine.py`).

## What this engine does NOT do

- **Place resting (maker) orders.** All entries are IOC takers. The
  standalone maker-rest strategy was evaluated (`analysis/07`) and
  proved fragile under realistic fill simulation (`analysis/10`).
- **Trade outside the v5/Pine-style trigger logic.** No new entry
  mechanism. The veto just gates and occasionally flips existing
  engine decisions.
- **Use Brownian Bridge directionally.** The model is used for *
  veto and flip*, not for placing new trades the engine wouldn't have
  considered.
- **Trade based on bucket-conditional edge tables.** Those were
  OOS-validated as overfit (`analysis/edge_table.json` carries
  `oos_validation_status: FAILED`).

## When in doubt

1. Read `analysis/FINDINGS.md` for the consolidated evidence base.
2. Read `analysis/AUTONOMOUS_SESSION_2026_05_19.md` for the iteration
   history that led here.
3. Read `RUNNING_VETO.md` for the safe rollout plan (shadow → live
   skip → live flip).
4. Read `HANDOFF_VETO_2026_05_19.md` for the standalone-deliverable
   summary written before EM was re-enabled.
5. If `live_veto_flip_EM_trades.jsonl` shows behavior you don't
   understand, run `veto_audit.py` on it before changing anything.

## Authorship trail

- Built and tested across the 2026-05-18 → 2026-05-20 autonomous
  research session.
- Patches committed in: `171deab`, `b4fb624`, `281e12a`, `b477860`,
  `636b46a`, `c539601` (live watchdog).
- User authorization for live launch: explicit verbal in the chat
  session on 2026-05-20 ("It's worth letting the paper engine
  continue... Deposit complete, get it trading").
