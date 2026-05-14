# Kalshi BTC Engine v2 — Master Handoff

Comprehensive overview from conception to current state, intended for a fresh
agent or orchestrator who needs the full picture before issuing next steps. If
you only read one document, read this. Read `HANDOFF.md` for chronological
update-log entries.

---

## 0. One-paragraph mission

Build a standalone, paper-first Kalshi BTC 15-minute trading engine
(`kalshi-btc-engine-v2/`) that treats the binary market as a market-ecology
system — participant behavior, liquidity deformation, probability distortion —
not a price-prediction loop. The existing production engine at
`C:\Trading\btc-bias-engine\` is a separate live service (single-tier
MOMENTUM_SCALP) and stays read-only. v2 must remain paper-only until empirical
edge is proven across sufficient data.

---

## 1. Current state snapshot (2026-05-12 EOD)

| Item | State |
|---|---|
| v2 build | Milestones M1-M9 complete, plus ecology layer (toxicity / regime / ensemble / cooldowns / error-tracker) and spot-circuit-breaker exit. |
| Test suite | 160/160 passing. ruff + black clean. |
| Live order placement | Disabled at three layers: `LiveExecutor.config.enabled=False` + `KalshiRestClient.live_enabled=False` + `ENGINE_V2_LIVE` env unset. |
| Kalshi account | $42.44 cash, 0 open positions (last verified via REST). |
| SCALP service (`BTCBiasEngine`) | Stopped, StartType=Disabled. Will not auto-start on reboot. |
| 4h burn-in (`data/burnin_4h.sqlite`) | Complete. 1.6M L2 events, 17 markets, 15 settled. |
| 48h burn-in (`data/burnin_48h.sqlite`) | **Died at user's local-machine reboot.** ~30 min captured. Needs restart. |
| Live engine `C:\Trading\btc-bias-engine\` | Untouched. Read-only constraint preserved. |

---

## 2. What v2 is (architecture)

Source layout under `src/kalshi_btc_engine_v2/`:

| Module | Purpose |
|---|---|
| `adapters/` | Kalshi REST+WSS + Coinbase/Kraken/Bitstamp spot adapters. Public-only data, no order writes when `live_enabled=False`. |
| `core/` | Fixed-point Kalshi book reconstruction (YES bid + inferred YES ask from NO bid), time helpers, event dataclasses. |
| `storage/` | SQLite schema (event-grained warehouse). 10 tables incl. `kalshi_l2_event`, `capture_health_event`. |
| `replay/` | Deterministic event-time replay. |
| `monitoring/` | Continuity stats + `health/` (kill-switch state machine). |
| `capture/burnin.py` | Paper-only N-hour capture runner. WSS to Kalshi + 3 spot venues. Patched mid-session for rollover backoff + market_dim upsert. |
| `models/` | `fair_prob.py` (settlement-aware diffusion), `vol_estimator.py` (RV+BV blend with horizon-clipped drift), `calibration.py` (PAV isotonic + time-bucket), `ensemble.py` (logit blend), `regime.py` (rule-based classifier), `error_tracker.py` (rolling calibration error). |
| `policy/` | `windows.py`, `veto.py`, `edge.py`, `sizing.py` (fractional Kelly), `exits.py`, `decision.py` (orchestrator). |
| `execution/` | `paper.py` (aggressive IOC fill simulator + queue-aware passive fills), `live.py` (gated, never used). |
| `risk/` | `guards.py` ($15/window cap, oversell hardening, manual-fill ignore, balance double-fetch — primitives ported from SCALP catastrophe history), `cooldowns.py` (anti-overtrade state machine). |
| `backtest/` | `runner.py` (event-driven backtester), `state.py`, `walk_forward.py`, `settlement.py`, `backfill.py`, `counterfactual.py`, `per_market_report.py`, `trade_patterns.py`, `divergence_stats.py`. |
| `ecology/toxicity.py` | VPIN-style volume-time imbalance. |
| `cli.py` | 17 subcommands; 2 named presets. |

**Order-of-operations in the decision engine** (per tick):

```
DecisionSnapshot
  → kill_switch?               → KILL_SWITCH
  → ensemble q_cal override    (logit-blend p_spot + power-logit p_binary
                                + divergence + ECR + reflexivity)
  → model_haircut from rolling calibration error
  → regime classifier          → if untradeable (settlement_hazard /
                                  data_fault / illiquid_no_trade) → FLAT
  → if has position            → exit rules: feed_degraded → adverse_revaluation
                                  → spot_circuit_breaker → profit_capture
                                  → time_stop / hold_to_settlement / hold
  → window gate                (warmup / core / precision / freeze)
  → edge ≥ window min          (default 1.2c core / 1.8c precision)
  → veto                       (spread / depth / staleness / venue disagreement
                                / fragility / cooldown)
  → q_cal bounds check         (default [0,1]; preset narrows to [0.10, 0.90])
  → sizing                     (fractional Kelly + market/aggregate/depth caps)
  → cooldown_guard             (same-side gap / exit cooldown / flip-flop / burst)
  → risk_guard                 ($15/window cap + oversell hardening)
  → BUY_YES / BUY_NO
```

---

## 3. Empirical findings — ordered by confidence

### CONFIDENT (large-N evidence from parallel session)

A dispatch-instance backtest over **2,815 KXBTC15M markets**:

- **Simple structural rules lose −$4.53/trade.** Buy-the-favorite + hold has no
  edge. The market correctly prices 15-min BTC direction on average.
- **Profit-taking is strictly worse than hold-to-settlement.** TP at +5c
  produces 78% win rate but tiny wins plus full losses; net −$26,741 vs hold's
  −$13,211 on the same sample.
- **Mid-window entries (minutes 8-12) are the only timing bucket near
  breakeven** (+$0.07/trade). Edge concentrates after market reveals direction
  but with enough time for the binary to converge.

**Implication:** any profit must come from the model identifying *specific
windows where the market is wrong*, not from structural/rule-based behavior.

### HIGH-CONFIDENCE within our 4h slice (small N — 5 to 7 entries)

- **Engine entries are 83% directionally correct** (5 of 6 settled trades the
  side bet won at settlement).
- **Default `adverse_revaluation` exit (-0.6c) strips winners.** Average hold
  time 9 seconds on 15-minute markets. Net P&L: −$1.15.
- **Widening the exit threshold to -3.0c made things worse** (-$1.67). Engine
  just waits longer to bail at worse prices.
- **Two filters independently fix the dominant losing pattern:**
  - `q_cal` extreme-confidence veto `[0.10, 0.90]` blocks the q=0.040 entry
    on KXBTC15M-26MAY120845-45 (the −$0.48 loser).
  - Regime classifier blocking `mean_revert_dislocation` catches the same
    entry via structural divergence.
- **Combined with `--adverse-ev-cents -100.0` (never-bail):** the
  `qcalveto_neverbail` preset produces **net +$0.31 on the 4h slice**.
  Equivalent `regimefilter_neverbail` produces identical P&L.
- **`qcalveto_neverbail_safe` (30bp spot circuit breaker added) produces the
  same +$0.31** — the breaker did not fire on this slice. Inert tail-risk
  insurance.

### MODERATE-CONFIDENCE / mechanism findings

- **The losing trade had `q_cal=0.040`** (extreme confidence). All winners had
  `q_cal ∈ [0.30, 0.50]` (uncertain). Model is well-calibrated mid-distribution,
  poorly calibrated at the tails.
- **Calibration MAE 0.43** on the small sample with filtered decisions.
  Significant absolute miscalibration; profitable only via smart filtering.
- **Median |divergence_logit| = 4.95** in the captured 4h (p90 = 12.74). The
  blueprint default `mean_revert_min_divergence=0.5` is crossed by 100% of
  decisions — useless as a filter. Tuned threshold ~5.0 would split the regime
  classifier 50/50.
- **Regime threshold tuning alone is cosmetic.** Same 7 entries either threshold
  — because `info_absorption_trend` and `mean_revert_dislocation` are both
  tradeable. Filtering happens behaviorally (which regimes get to trade), not
  just by label.
- **Ungated mode chases markets.** Removing q_cal + regime + cooldown gates,
  the engine took 10 BUY_NO trades in a row as a market rose 50c → 99c. 8 of
  10 stopped out. Confirms the gates' value.

### LOW-CONFIDENCE / TINY-SAMPLE-ONLY

- The +$0.31 result is based on 5 entries across 4h on one market series. Could
  be noise. Need the 48h burn-in to validate at N≥100.

### UNRECONCILED / OPEN

- **Dispatch instance reported "13 live paper trades, 100% win rate,
  +$2.21 gross."** This session never built a live paper-trade loop. Source of
  that data is unclear — possibly a SCALP-era run labeled as v2, possibly a
  parallel harness the dispatch instance built. Do not cite this number until
  reconciled.

---

## 4. Bugs found and fixed this session

1. **Kalshi WS requires authentication even for public channels.** v2 capture
   was returning 401 on first run. Patched: `_resolve_kalshi_creds_into_env`
   auto-loads from `btc-bias-engine/credentials/kalshi.env` if `ENGINE_V2_*`
   env vars aren't set. Three-layer order-placement gate still intact.
2. **`floor_strike` lives in `market_dim.raw_json`, not the dedicated column.**
   Patched `default_strike_provider` to dig into raw_json after the column
   lookup fails.
3. **Rollover discovery silently gave up.** When the next KXBTC15M market
   wasn't immediately open, `_rollover` returned with `current_market_ticker`
   still pointing at the dead market. Patched with backoff retries (2/5/10/20/30s)
   + idle-loop discovery fallback.
4. **`market_dim` not upserted on rollover.** Only the first market got
   inserted; rolled markets only had `kalshi_lifecycle_event` rows. Patched.
   Built `backfill-market-dim` CLI to repair historical captures.

---

## 5. Tooling shipped

### Capture / lifecycle

- `init-db` `smoke-replay` `capture-burnin` `print-ddl`

### Inspection

- `db-stats` `continuity-report` `settled-markets` `backfill-market-dim`

### Backtest + analysis

- `backtest` `walk-forward` `compare-gates` `hold-counterfactual`
  `per-market-report` `trade-patterns` `divergence-stats`

### Backtest tunable flags (on `backtest`)

| Flag | Default | Notes |
|---|---|---|
| `--preset {qcalveto_neverbail \| regimefilter_neverbail \| *_safe}` | none | Bakes proven configs. |
| `--ungated` | False | Disables regime/cooldown/ticker-lock/veto/min-edge for counterfactual. |
| `--adverse-ev-cents <N>` | -0.6 | EV-based stop. `-100.0` disables. |
| `--q-cal-min/-max <P>` | 0.0 / 1.0 | Extreme-confidence veto. |
| `--regime-divergence-min <X>` | 0.5 | `mean_revert` threshold (empirically too low at default). |
| `--tradeable-regimes <csv>` | all 3 | Per-regime behavioral filter. |
| `--spot-circuit-breaker-bp <bp>` | 0.0 | Spot-confirmation stop. `0` disables. |
| `--decision-log <path>` | none | Write per-decision JSONL for offline analysis. |
| `--min-edge-override <N>` | window-default | Override per-window min edge. |
| `--regime-divergence-min <X>` | 0.5 | Regime classifier threshold. |

### Presets (all in `cli.py:_BACKTEST_PRESETS`)

| Preset | Configuration | 4h result |
|---|---|---|
| `qcalveto_neverbail` | q∈[0.10,0.90] + adverse=-100c | **+$0.31** |
| `regimefilter_neverbail` | block `mean_revert_dislocation` + adverse=-100c | **+$0.31** (same trades) |
| `qcalveto_neverbail_safe` | above + 30bp spot circuit breaker | **+$0.31** (breaker inert) |
| `regimefilter_neverbail_safe` | above + 30bp spot circuit breaker | **+$0.31** (breaker inert) |

---

## 6. The actionable headline

**The engine's entries are good. Its default exits are bad. Filter the
overconfident wrong bets at entry, hold the rest until settlement, and have a
spot-confirmation circuit breaker for tail risk that ignores binary-mid noise.**

**Concrete recommended config for the next paper run:**

```powershell
python -m kalshi_btc_engine_v2.cli backtest `
    --db .\data\burnin_48h.sqlite `
    --preset qcalveto_neverbail_safe `
    --decision-log .\data\paper.decisions.jsonl
```

---

## 7. Recommended priority queue

1. **Restart the 48h burn-in.** It died at the local-machine reboot earlier
   today; only 30 min captured. Same command as before:
   ```powershell
   Remove-Item data\burnin_48h.sqlite*
   python -m kalshi_btc_engine_v2.cli capture-burnin `
       --db .\data\burnin_48h.sqlite --hours 48
   ```
2. **Wait for ~100-200 markets** before drawing conclusions. The 4h slice has
   only 5 entries; 48h should give 100+ for statistical claims.
3. **Run `--preset qcalveto_neverbail_safe` on the captured 48h.** Check:
   - Net P&L ≥ 0 → preset survives larger N → can claim filtered edge.
   - Spot circuit breaker actually fires on at least one trade → validates
     the rail behaves as designed.
   - Per-market-report shows hold-to-settlement delta near zero or positive
     → confirms exit logic is working with the filter.
4. **Build the automated post-capture report** Codex proposed: one CLI that
   runs all relevant analyses and emits a single decision-grade document
   (preset comparison + per-market P&L + counterfactual + spot-circuit events
   + q_cal bucket performance + timing bucket). Don't build before the 48h
   data exists — risk of fitting to noise.
5. **Reconcile the dispatch instance's "13 live paper trades" data** before
   citing it in any decision.
6. **Replace the regime classifier from telemetry to behavior.** Either
   shrink size in `mean_revert_dislocation` rather than block, or block but
   only when divergence is structurally confirmed (e.g., persistent for N
   seconds).
7. **Calibrator fitting.** The `IsotonicCalibrator` exists but isn't being
   fit against realized outcomes during runs. After 48h there will be 100+
   settled markets — fit a per-time-bucket calibrator from those and have
   live decisions use the corrected `q_cal`.
8. **Only after 1-7:** consider a tiny live shadow ($10/trade for ~50
   contracts of headroom) running beside the (still-stopped) SCALP engine.
   Never via NSSM auto-start.

---

## 8. Hard constraints (load-bearing, do not relax)

- `C:\Trading\btc-bias-engine\` — read-only. Never modify, import, or invoke.
  That is the live production service that has had real-money catastrophes
  (PAPER_FVG_LIVE_MODE −19%, DIRECTION 90% backtest vs 28% live).
- Live order placement gated at three layers:
  1. `LiveExecutor.config.enabled=False`
  2. `KalshiRestClient.live_enabled=False`
  3. `ENGINE_V2_LIVE` env unset
  All three must be true to send a real order. Do not bypass.
- `$15/window` risk cap (`RiskConfig.max_risk_per_window_dollars=15.0`) is
  the load-bearing safety net. Don't relax for any reason short of explicit
  user direction.
- SCALP v1 NSSM service (`BTCBiasEngine`) — currently Disabled startup.
  Re-enabling requires `sc config BTCBiasEngine start= demand` + explicit
  intent.
- `capture/burnin.py` contains no `create_order` code path — it cannot
  place orders even if a bug tried. Keep it that way.
- 154→160 unit tests must stay green. If a change breaks them, fix the
  underlying regression rather than mask with skips.

---

## 9. Repo navigation

| Want to know | Read |
|---|---|
| Where each thing is | This document section 2 |
| Update log (chronological) | `HANDOFF.md` "Update log" |
| CLI reference | `README.md` "CLI Reference" |
| The MD that started it all | `C:\Trading\deep-research-report.md` |
| Exit rules behavior | `policy/exits.py` |
| Entry orchestration | `policy/decision.py` |
| Risk primitives | `risk/guards.py` |
| Why a finding exists | `HANDOFF.md` "Findings From The Full 4h Burn-In" |

---

## 10. Open questions

1. **Does +$0.31 on 4h survive at N=100-200 markets?** (Tests if the q_cal
   filter is real edge or noise.)
2. **Does the spot circuit breaker ever fire on a long-horizon capture?**
   (Tests if the rail is correctly tuned at 30bp or if it needs widening.)
3. **What does per-time-bucket calibration look like on settled markets?**
   (Tests whether `q_cal` is honest at all `seconds_to_close` or only late
   in the window when the market has converged.)
4. **Where did the dispatch instance's 13-trade live-paper data come from?**
   (Unreconciled.)
5. **Is `mean_revert_dislocation` a real "fade overshoot" regime, or just
   "binary mid is slightly off from model" noise?** (Tests if regime
   classification has any independent signal beyond q_cal extremity.)

---

## 11. What "good" looks like from here

- 48h burn-in captures 100+ markets cleanly. Quorum coverage ≥ 95%.
- `qcalveto_neverbail_safe` preset produces non-negative net P&L on the 48h.
- Spot circuit breaker fires at least once and demonstrably prevents a loss.
- Calibrator fit from settled outcomes is used live; `q_cal` improves.
- A post-capture report becomes the single source of truth for "ship/don't
  ship" decisions, not ad-hoc analysis scripts.
- Then, and only then, a tiny live shadow $10/trade for a few days.
- If live shadow tracks paper P&L within an honest margin: scale slowly.

If at any step the empirical edge disappears at scale, the project's
conclusion is "the v2 architecture is honest but the model alpha isn't strong
enough at 15-min binaries" — and v2 becomes infrastructure for a future
strategy rather than a strategy itself. That outcome is also acceptable; it's
better than running an unverified system live with real capital.
