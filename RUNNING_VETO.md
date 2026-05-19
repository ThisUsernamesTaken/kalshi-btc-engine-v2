# Running the veto-enabled live trader

This document describes how to launch the model-veto layer in production
and what to expect.

## State of play

| | |
|---|---|
| **Source** | `scripts/live/live_v5_unified.py` (patched in commit `171deab`) |
| **CLI flags** | `--veto-mode {off,shadow,skip}` (default `off`); `--veto-threshold N` (default 5) |
| **Veto-enabled watchdog** | `scripts/live/watchdog_v5_unified_veto.cmd` |
| **Veto module** | `src/kalshi_btc_engine_v2/model_veto.py` (tested) |
| **OOS validation** | `analysis/13`, `14`: +$92 swing, CI [+$26, +$172], 99.9% bootstrap-positive |
| **Realistic-fill caveat** | `analysis/10`: standalone maker strategy breaks even at L2 realism; veto layer unaffected (operates on trades engine already places at taker fees) |

## Four modes

| Mode | Behavior | When to use |
|---|---|---|
| `--veto-mode off` | Legacy behavior. No veto computation. | Default. Same as pre-veto-patch behavior. |
| `--veto-mode shadow` | Computes veto decision for every trigger, logs a `model_veto` event with `action` field (KEEP/SKIP/FLIP), but **never skips or flips**. | **Recommended first step.** Run for 24 hours to validate the veto fires sensibly in production. |
| `--veto-mode skip` | Computes + logs + actually **skips** trades where the model disagrees by ≥ `--veto-threshold` cents (default 5c). No flip. | Conservative live veto. After shadow validates. OOS swing +$92 / CI [+$26, +$172]. |
| `--veto-mode flip` | Like `skip`, but disagreements ≥ `--veto-flip-threshold` (default 30c) place an OPPOSITE-side order instead of skipping. | Higher-EV variant. OOS swing +$173 / CI [+$68, +$296]. Requires opposite-side execution path. Validate in shadow ≥24h first. |

## Quick-start: shadow mode

```powershell
# Replace the running KalshiLiveTA / KalshiLiveV5 with a shadow-veto run.
# Direct invocation:
$env:PYTHONPATH = "C:\Trading\kalshi-btc-engine-v2\src"
$env:PYTHONIOENCODING = "utf-8"
$py = "C:\Users\coleb\AppData\Local\Python\bin\python.exe"

& $py C:\Trading\kalshi-btc-engine-v2\scripts\live\live_v5_unified.py `
    --decision-log C:\Trading\kalshi-btc-engine-v2\data\live_v5_unified_trades.jsonl `
    --poll-interval-s 1.5 `
    --status-every-s 30 `
    --no-resting `
    --disable-earlier-moderate --enable-t30-sniper --disable-late --enable-em-upsize `
    --veto-mode shadow --veto-threshold 5
```

Or via the watchdog (auto-restart on crash):
```cmd
C:\Trading\kalshi-btc-engine-v2\scripts\live\watchdog_v5_unified_veto.cmd          (skip-only variant)
C:\Trading\kalshi-btc-engine-v2\scripts\live\watchdog_v5_unified_veto_flip.cmd     (skip+flip variant)
```

Both watchdogs run in `--veto-mode shadow` by default. To activate live veto:
- For the skip-only variant: edit `watchdog_v5_unified_veto.cmd`, change
  `--veto-mode shadow` to `--veto-mode skip`.
- For the skip+flip variant: edit `watchdog_v5_unified_veto_flip.cmd`,
  change `--veto-mode shadow` to `--veto-mode flip`.

The flip variant adds an opposite-side order path that requires its own
24h shadow validation before going live.

## Quick-start: live veto (after shadow validation)

Edit `scripts/live/watchdog_v5_unified_veto.cmd`:
```diff
- set VETO_FLAGS=--veto-mode shadow --veto-threshold 5
+ set VETO_FLAGS=--veto-mode skip   --veto-threshold 5
```

Then restart the watchdog.

## What to watch in the logs

The trader emits new event kinds when the veto runs:

```jsonl
{"kind": "model_veto", "ts_ms": ..., "ticker": "...", "stage": "earlier_moderate|late|t30_sniper", "mode": "shadow|skip", "would_skip": true|false, "p_model_yes": 0.421, "engine_side": "yes", "engine_price_cents": 89, "sigma_used": 0.45, "threshold_cents": 5, "reason": "model_says_yes_overpriced p_model=0.421 engine_implied=0.890 diff=-46.9c"}
{"kind": "earlier_moderate_skip", "reason_code": "MODEL_VETO", "detail": "model_says_yes_overpriced ..."}
{"kind": "model_veto_error", "error": "..."}    # never blocks trading
```

Quick triage:
```powershell
# Count veto decisions per leg
Select-String 'model_veto' C:\Trading\kalshi-btc-engine-v2\data\live_v5_unified_trades.jsonl | Measure-Object

# What % would-skip?
$py = "C:\Users\coleb\AppData\Local\Python\bin\python.exe"
& $py -c "import json; n=s=0
for L in open(r'C:\Trading\kalshi-btc-engine-v2\data\live_v5_unified_trades.jsonl', encoding='utf-8'):
    try: e=json.loads(L)
    except: continue
    if e.get('kind')=='model_veto':
        n+=1
        if e.get('would_skip'): s+=1
print(f'{s}/{n} would-skip = {s/max(n,1)*100:.0f}%')"
```

Expected ranges from OOS:
- EARLIER_MODERATE: 40-50% would-skip
- LATE: 25-35% would-skip
- T-30 SNIPER: low single digits would-skip (sniper agrees with model)

## Veto rollout plan

### Step 1: 24h shadow validation (DAY 1)

Launch with `--veto-mode shadow`. Goals:
1. Confirm the trader boots cleanly with the new module
2. Verify veto fires on a meaningful fraction of triggers (~30-50%)
3. Watch for any `model_veto_error` events (should be zero in stable conditions)
4. Cross-check would-skip decisions against actual trade outcomes (manual review)

### Step 2: Per-leg live veto (DAY 2-3)

Once shadow validates, switch to `--veto-mode skip` but stage by leg:
- **First**: re-enable EARLIER_MODERATE (`--enable-earlier-moderate`) WITH veto.
  This is the highest-value combination — EM has the biggest historical
  losses, and the veto's strongest signal is on EM.
- **Second** (after 1 week clean): enable LATE with veto.
- **T-30 SNIPER veto**: optional. The sniper rarely fails the veto (it
  enters when the favorite is decisively priced, which the model also
  reads as high-prob). Net effect on sniper P&L should be small.

### Step 3: Monitor regime stability (WEEK 2+)

The veto's effectiveness depends on the BRTI mean-reversion regime
persisting. Audit weekly:
- Loser-flag-rate on EM: target ≥80%
- Winner-flag-rate on EM: target ≤10%

If these drift below the OOS-observed pattern, pause the veto and
re-derive the bucket statistics before continuing.

## Safety guarantees

- **Veto errors never block trading.** Every veto call is wrapped in
  try/except. Errors log `model_veto_error` and the trade proceeds.
- **`--veto-mode off` = pre-veto behavior.** Zero code path change.
- **Module-import failure is also handled.** If `model_veto` can't load
  (missing gradient engine path, etc.), startup logs a warning and
  forces `--veto-mode off`.
- **Veto operates after the trigger event is logged** — full audit
  trail of "engine wanted to trade X, then veto said skip" is preserved.

## When NOT to deploy

- During Kalshi market reopening / outage recovery (model probabilities
  may be unreliable while quotes are stale).
- If the gradient engine `models/probability.py` has been modified
  (re-validate via `tests/test_model_veto.py` first).
- If `live_v5_unified_trades.jsonl` shows balance halt or daily-loss-cap
  status (resolve the underlying issue first).

## Rollback

To disable the veto entirely:
```diff
- set VETO_FLAGS=--veto-mode shadow --veto-threshold 5
+ set VETO_FLAGS=
```

Or just stop the veto watchdog and restart the original `watchdog_v5_unified.cmd`.
