# Pre-registered burn-in: hold-to-settle, May 2026

**Date registered:** 2026-05-12
**Registry author:** engine-v2 (Claude session)
**Burn-in target:** ≥48h continuous, capture + live paper trade

**Status:** Frozen pre-burn. Any change to this file after the burn-in starts
must be recorded with a dated `## Amendment` section below — do not silently
overwrite. The point of pre-registration is to make post-hoc rationalisation
detectable.

---

## Motivation

The 2026-05-12 paper run (`live_paper_qcalveto`) closed 7 trades. Pre-fee
gross was +158¢; after exact taker fees on entry+exit, net was ≈ +101¢. All
seven closed via the `profit_capture` exit branch. The largest winner
(`KXBTC15M-26MAY121430-30`, size=10) exited 14 seconds after entry with
`sec_to_close=209` and carried essentially the entire P&L; the other six
trades (size=1) were each within ±2¢ of break-even net of fees.

Two follow-up analyses (engine-internal critique, then a third-party deep
research report) converged on the same interpretation:

1. Disabling `adverse_revaluation` (the EV-flip stop) did not implement
   hold-to-settlement. The `profit_capture` branch was still doing the same
   kind of damage on the upside — clipping winners that should be held.
2. Round-trip taker fees of ≈4¢ on a 1-contract trade are catastrophic
   relative to typical edge magnitudes. Size-1 entries at off-center prices
   are dominated by fee drag even when directionally correct.

This registry tests whether removing those two pathologies yields a
materially different selected-trade economic profile, **before** any
multi-week residual-model rewrite.

## Hypothesis

> If the engine's apparent paper-trading edge in the 2026-05-12 slice came
> from directionally-correct entries that the current exit logic clips
> prematurely, then disabling both the `profit_capture` and EV-flip
> `adverse_revaluation` branches — leaving only operational rare-bails,
> the structural spot-circuit-breaker, and the mechanical close-out — will
> produce a non-negative selected-trade P&L distribution on a sample of
> ≥150 closed trades.

The null is that the apparent edge was sample-size noise plus survivorship
through the profit-capture filter, and that disabling early exits will not
improve and may worsen net P&L.

## Frozen variants

Exactly the following five presets/configurations will be evaluated. **No
other presets may be added to the comparison post-burn without recording
an amendment.** Hyperparameters not listed are at engine defaults as of
git commit/file-state at registry date.

| Variant | Exit policy | Settlement blackout | Fee-floor veto |
|---|---|---|---|
| `A_baseline_qcalveto_neverbail_safe` | Adverse-revaluation disabled, profit_capture ON, spot 30bp | none | OFF |
| `B_hold_to_settle_pure` | Both EV-flip and profit_capture disabled, spot 30bp | none | ON (defaults) |
| `C_hold_pure_blackout_30s` | Same as B | no new entries when `sec_to_close < 30` | ON |
| `D_hold_pure_blackout_60s` | Same as B | no new entries when `sec_to_close < 60` | ON |
| `E_hold_pure_no_fee_floor` | Same as B | none | OFF |

Variant A is the control (the configuration that produced the 7-trade slice
on 2026-05-12). Variants B–E are interventions. The settlement-blackout
variants (C, D) are conditional on the existing finding that the single
material winner in the 2026-05-12 slice entered at `sec_to_close=223` — a
late-window entry. The blackout variants formally test whether the report's
"no new entries in the last 30–60s" recommendation is on the right side of
the empirical evidence; the prior expectation is that 30s is harmless and
60s may or may not be.

Variant E isolates whether the fee-floor veto carries any of the P&L
delta independent of the exit changes.

## Frozen sample-size rule

Comparison is suspended until **at least one variant has ≥150 closed
round-trip trades**. If a variant has not reached 150 by the end of the
48h capture, the burn-in is extended in 24h increments rather than
analysed at lower N. The threshold is motivated by the simple observation
that one outlier carried the entire 7-trade slice's P&L; at N=150 the
sensitivity of mean P&L to a single trade is approximately 1/150 ≈ 0.7%
per trade, which is the order of magnitude at which signal becomes
distinguishable from a single fee-eater or single windfall.

No early-stopping rule. No interim peeking at variant comparisons before
the threshold is reached. (Aggregate health metrics — feed uptime, event
counts, sequence continuity — may be monitored continuously; the
suspension is specifically on **variant P&L comparison**.)

## Frozen evaluation metrics

Two layers, evaluated separately. **Ship decisions reference only the
trade-policy layer**; the model layer is diagnostic.

### Trade-policy layer (primary)

For each variant, on the closed-trade subset:

- `net_pnl_cents` — sum of `(exit_price - entry_price) * contracts` minus
  exact rounded fees on entry and (where applicable) exit. Settlement
  carries zero exit fee.
- `trade_count` — closed round-trips.
- `pnl_excluding_top_trade` — net P&L if the single largest-|P&L| trade is
  dropped. Detects single-trade dominance.
- `pnl_excluding_size_one` — net P&L from trades with `contracts > 1` only.
  Detects whether the fee-eater tail is contributing meaningfully.
- `mean_hold_seconds` — average time between entry and exit. For
  hold-to-settle-pure this should be substantially longer than the
  2026-05-12 baseline (which had a 14-second worst case).
- `exit_mode_histogram` — distribution over `{settlement, time_stop,
  hold_to_settlement, spot_circuit_breaker, adverse_revaluation:feed_degraded}`.
  Hold-to-settle-pure variants must show zero `profit_capture` and zero
  `adverse_revaluation:ev_flip`.
- `max_session_drawdown_cents` — worst rolling intra-day net P&L drawdown.

### Probability-model layer (diagnostic only)

For each variant, on **all decision snapshots** (not just trades):

- `brier_score` against settled outcomes (`predicted_q_yes` vs realised
  YES/NO).
- `calibration_mae` per the existing `CalibrationErrorTracker`.
- `reliability_by_phase` — Brier score binned by `seconds_to_close`
  quartile, to detect settlement-phase model degradation.

These are diagnostic. The model layer does not gate ship decisions in this
registry — only the trade-policy layer does.

## Frozen pass/fail criteria

A variant is considered to have **passed** the burn-in if **all** of the
following hold on the closed-trade subset at N≥150:

1. `net_pnl_cents ≥ 0`.
2. `pnl_excluding_top_trade ≥ -100¢`. (i.e. the variant cannot be relying
   on a single outlier to be net-positive — one trade can be worth at most
   100¢ above the rest.)
3. `mean_hold_seconds ≥ 60` for any hold-to-settle-pure variant (B, C, D).
   This is a sanity check that the exit changes actually took effect.

A variant **fails** if any criterion is violated. There is no concept of
"partial pass" or "pass conditional on excluding bad trades" — the
post-hoc-rationalisation defence depends on this being binary.

A variant being eligible to proceed to **live shadowing** additionally
requires:

4. `max_session_drawdown_cents ≥ -300¢`.
5. Probability-model layer: no statistically significant degradation
   (≥10% relative increase in Brier score) in any phase quartile
   relative to variant A.

## What is explicitly not promised by this registry

- This registry does not preregister any decision about a **residual /
  shrinkage entry rewrite**. That rewrite is contingent on observing
  evidence that the entry layer's directional accuracy is meaningfully
  better than the market-price baseline on the traded subset. If the
  burn-in shows that hold-to-settle-pure produces non-negative net P&L
  but does so via roughly 50% directional accuracy, the right
  interpretation is "no entry alpha; exit was the only working part" —
  and the rewrite is not justified.
- This registry does not promise live deployment regardless of outcome.
  All variants are paper-only. Live shadowing requires the
  drawdown/Brier criteria above AND a separate sign-off.
- Variant comparisons are **per-variant pass/fail**, not pairwise
  significance tests. We do not have N for pairwise inference and we
  do not pretend to.

## Amendments

(Append dated subsections here if any frozen parameter must change after
the burn-in starts. Do not silently edit the body of this document.)

### 2026-05-12 — Decision-cadence latency shadow

The latency-budget diagnostic changed the engineering priority before any
residual-model work. On `data/burnin_pure_capture_2026_05_12.sqlite`, the
1000ms decision cadence produced an effective staleness floor of 767ms versus
the 500ms assumed microstructure feature half-life (`marginal`). A 250ms
cadence produced a 392ms floor (`feasible`).

This amendment adds a parallel operational shadow, not a replacement for the
frozen A-E policy comparison:

| Variant | Difference from B |
|---|---|
| `F_hold_pure_250ms_latency_shadow` | Same as `B_hold_to_settle_pure`, but `decision_interval_ms=250` and `poll_interval_s=0.25`; live-paper telemetry logs event lag, query time, ingest time, loop time, duty cycle, decisions, and fills. The live shadow starts near the tail with a 1200s warmup lookback so the current market's initial book snapshot is present without replaying the full DB. |

Variant F is evaluated first for system viability: event lag must remain below
1000ms p95 on the live-paper telemetry, and loop duty cycle should remain
below 0.50 p95. Trade P&L comparisons remain suspended under the same N≥150
closed-trade rule used above.
