# Integrating the fair-value model veto into the live trader

This is a deployment patch sketch — **do not apply without paper-validating
in shadow mode first.** See `analysis/FINDINGS.md` for the OOS validation
that motivates this layer.

## Where it goes

In `scripts/live/live_v5_unified.py`, the trigger paths emit
`<leg>_trigger` events before calling `place_order`. The veto goes
between the trigger emission and the order placement:

- **EARLIER_MODERATE leg**: insert after line ~1308 (after `log_fp.write(json.dumps(trig_em, ...))`) and before the `place_order` await
- **LATE leg**: insert after the `late_trigger` emission at line ~2269
- **T-30 SNIPER leg**: insert after the `t30_sniper_trigger` emission at line ~1506
- **EARLY leg**: insert after the `early_trigger` emission at line ~1036

The trigger event already carries every field the veto needs.

## Patch sketch (for EARLIER_MODERATE)

```python
from kalshi_btc_engine_v2.model_veto import veto_decision

# ... after trig_em is built and logged ...
log_fp.write(json.dumps(trig_em, default=str) + "\n")
log_fp.flush()

# === MODEL VETO ===
try:
    sigma_ann_for_veto = em_rv5 if em_rv5 is not None else 0.5
    skip, p_model, reason = veto_decision(
        spot_btc=btc_now_em,
        strike=strike_em,
        seconds_to_close=secs_to_close,
        sigma_annualized=sigma_ann_for_veto,
        engine_side=em_side,                # 'yes' or 'no'
        engine_price_cents=em_entry_ask,    # what we intend to pay
        threshold_cents=5,
    )
    log_fp.write(json.dumps({
        "kind": "model_veto",
        "ts_ms": now_ms, "ticker": ticker,
        "stage": "earlier_moderate",
        "skip": skip,
        "p_model_yes": p_model,
        "reason": reason,
    }, default=str) + "\n")
    log_fp.flush()
    if skip:
        log_fp.write(json.dumps({
            "kind": "earlier_moderate_skip",
            "ts_ms": now_ms, "ticker": ticker,
            "reason_code": "MODEL_VETO",
            "detail": reason,
        }, default=str) + "\n")
        log_fp.flush()
        state["earlier_moderate_state"] = "done"
        continue
except Exception as e:
    # Veto must NEVER block trading on its own bugs. Log and proceed.
    log_fp.write(json.dumps({
        "kind": "model_veto_error",
        "ts_ms": now_ms, "ticker": ticker,
        "error": str(e)[:200],
    }, default=str) + "\n")
    log_fp.flush()

# ... continue with order placement ...
```

The same pattern applies to LATE and T-30 SNIPER, with leg-specific
field names (e.g., `late_trigger` uses `btc_now`/`strike`, the T-30
sniper uses `fav_ask_cents` from the trigger).

## Rolling out safely

1. **Shadow mode first.** Add the veto code but DO NOT skip — only log
   the `model_veto` event with `skip` flag. Run for 24 hours, compare
   the veto decisions against actual trade outcomes.

2. **Confirm shadow result.** The veto should fire on ~30-50% of trades
   (averaged across legs). If it fires on <10% or >80%, sigma estimate
   is probably wrong.

3. **Switch to live veto.** Add the `if skip: continue` branch. The
   existing `_skip` event kinds remain so the watchdog can audit.

4. **Monitor for regime change.** The veto's effectiveness depends on
   the BRTI mean-reversion regime persisting. If after 2 weeks of live
   the veto's flagged-loser rate drops below 60% (was 87% in OOS),
   re-validate before continuing.

## Volatility estimation

The veto needs `sigma_annualized`. The trigger event has `rv_5m` (5-min
realized vol from the engine's vol tracker). That's the closest match
to what the OOS backtest used. Available variations:

- `rv_5m` from trigger event: directly usable, already annualized in the
  engine's internal state.
- If not available: pass 0.5 (the OOS default).

The fair-value model has a `sigma_floor_annualized=0.15` cap to prevent
nonsense outputs at extreme/missing vol estimates.

## Expected impact

From `analysis/13_model_vs_engine.py` and `14_veto_robustness.py` on
2026-05-13 to 2026-05-17 data:

- v5_unified: 51 settles → veto would skip 10, keep 41. Realized P&L
  flips from −$41 to +$30 (5-day cumulative).
- v5_old: 28 settles → veto skips 10, keep 18. Realized P&L flips from
  −$1.68 to +$9.88.
- live_ta: 52 settles → veto skips 5, keep 47. Realized P&L improves
  from −$22 to −$13.

Combined: −$64 → +$27 across 131 trades. 95% bootstrap CI on the
swing: [+$26, +$172]. 100% of bootstrap resamples positive.

## What NOT to do

- **Do not flip live without 24h of shadow logging.** The veto might
  fire on trades the engine never actually got filled on; you want to
  see the actual fired/not-fired comparison before going hot.
- **Do not change the threshold to 0c** to be "more aggressive."
  Threshold sensitivity in `14_veto_robustness.py` shows thresholds
  5c-12c are the sweet spot; below 2c the veto starts flagging winners
  it shouldn't.
- **Do not add a veto on T30_SNIPER without re-evaluating.** The sniper
  has only 1 fill in the data; the veto's behavior on snipers is
  untested. Roll out per-leg: EARLIER_MODERATE first (most data),
  LATE second.
