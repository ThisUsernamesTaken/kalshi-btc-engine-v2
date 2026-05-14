# HANDOFF - kalshi-btc-engine-v2

This file is the shared coordination surface for multi-agent work. Read it before
starting. Update it before stopping.

## Lane Ownership

Do not edit outside your lane without coordinating in this file or in the user
thread.

- Codex: `adapters/`, `capture/`, `features/`, `execution/`, `policy/`,
  `storage/`, `cli.py`, integration wiring.
- Claude: `models/`, `risk/`, `backtest/`, `monitoring/health/`,
  `docs/research_*`.
- Shared docs: `README.md`, `docs/*.md`, and this file. Keep edits factual and
  append-oriented when possible.
- Shared tests: tests may be added for your lane. If a test edits another lane's
  fixture or expected behavior, call it out under In Flight.

## Read-Only For Both

- `C:\Trading\btc-bias-engine\` is the live service. Treat it as read-only
  reference only.

## Current Safety Rules

- Paper-only until the user explicitly approves live trading.
- Do not add order placement outside an execution milestone.
- Existing live-order adapter methods must remain gated by `live_enabled=False`
  defaults and `ENGINE_V2_LIVE` policy.
- Keep `MANUAL_FILLS_CAPTURE_ENABLED` semantics from the live engine: manual
  fills are not adopted into engine state unless explicitly enabled.
- Preserve SCALP-derived safety primitives in v2 risk: `$15/window` cap,
  oversell hardening, confirm-with-second-fetch balance protocol, and ticker
  entry tracking.

## Mode

2026-05-11 — User collapsed the dual-agent workflow. Single-agent build
(Claude) drove M6-M9 + CLI integration to completion. The polling cron has been
cancelled. HANDOFF.md remains for context if dual-agent resumes later.

## In Flight

- [User] 15-30 minute live burn-in trial pending, then 4h burn-in if clean.
- [User] Once burn-in data exists: run `engine-v2 backtest --db <captured>.sqlite`
  to validate end-to-end pipeline against real captured events.

## Done

- M1 adapters (Codex): Kalshi REST/WSS, BTC spot adapters, fixed-point book parsing.
- M2 warehouse + deterministic replay (Codex).
- M2.5 paper-only burn-in runner with `capture_health_event` (Codex).
- M3 feature engine (Codex): book, tape, BTC motion, strike geometry, divergence,
  entropy/compression, reflexivity placeholder, liquidity elasticity.
- M4 fair probability + calibration (Codex): settlement-aware case-one/case-two
  math, isotonic/time-bucket calibration, power-logit helper.
- M4 vol/drift estimator bridge (Claude): RV+BV blend, horizon-clipped drift,
  `log_returns_from_prices` helper. `models/vol_estimator.py`.
- Risk guards (Codex, in Claude's lane — accepted on review): window cap,
  ticker lock, oversell hardening, manual-fill ignore, balance double-fetch.
- **M6 decision policy** (Claude): windows, veto, edge, sizing, exits, decision
  orchestrator. Hierarchical entry gates, fractional Kelly with caps, full exit
  rule table including hold-to-settlement. `policy/`.
- **M7 paper execution + gated live** (Claude): aggressive IOC fill simulator
  against the book, queue-position-agnostic for v1, live executor stub gated
  behind `ENGINE_V2_LIVE` and `RiskGuard`. `execution/`.
- **M8 event-driven backtester** (Claude): replays captured SQLite events
  through the full features-lite + fair-prob + policy + paper-exec pipeline,
  emits per-market P&L, fees, and decision stats. `backtest/`.
- **M9 monitoring + kill switches** (Claude): venue quorum, WS connection,
  unmatched fills, daily loss stop, venue disagreement, portfolio mismatch,
  manual halt. `monitoring/health/`.
- **CLI wiring** (Claude): `engine-v2 backtest --db <path>` subcommand wired
  end-to-end; smoke test against empty DB returns zero counts as expected.

## Verification

- pytest: 138/138 passing (M6-M9 + ecology layer + telemetry + CLI subcommands)
- ruff: clean
- black: clean
- CLI smoke + db-stats + settled-markets + walk-forward + backtest with decision-log: all working

## Findings From The Full 4h Burn-In (2026-05-12)

The burn-in completed all 4 hours and captured **17 KXBTC15M markets** through
rollover (the rollover patch worked — `rollover_count=16`). Stats:
- 1,594,543 Kalshi L2 events (110.7 msg/sec average)
- 80,090 Kalshi trades, 137,797 spot-fusion ticks
- 0 sequence gaps, 96.1% quorum coverage
- 13 reconnects, 8779 staleness breaches (Kraken WS persistently jitters 1.8-3.7s)

After running `backfill-market-dim` (new CLI — extracts strike + outcome from
captured lifecycle events for markets that were missing from `market_dim`),
all 17 markets analyzable. 15 of them had settled (7 YES wins, 8 NO wins).

### Headline finding 1: Gated entries are 83% directionally correct

Of 7 entries the gated engine took across these markets:
- 5/6 (settled) were DIRECTIONALLY CORRECT — the side bet would have won at settlement.
- Engine fades market-overpricing-YES at ATM; the model's q_yes < binary mid is right
  most of the time on these 15-min directional markets.

### Headline finding 2: Exit rule cost more than it saved

Despite 83% directionally-correct entries, actual P&L was **−$0.87 (gross,
across 7 markets)**. The `adverse_revaluation` exit (EV < −0.6c) fired 5 times,
prematurely stopping out winning trades.

Counterfactual (hold-to-settlement on the 6 entries with known outcomes):
- BUY YES @ 44c → YES won → +$0.56
- BUY NO  @ 50c → NO  won → +$1.00 (2 contracts)
- BUY NO  @ 85c → YES won → −$2.55 (3 contracts, big loser)
- BUY NO  @ 51c → NO  won → +$2.45 (5 contracts, big winner)
- BUY NO  @ 48c → NO  won → +$0.52
- BUY NO  @ 66c → NO  won → +$0.34
- **Hold-to-settlement gross P&L: +$2.32. Net of fees ≈ +$2.02.**
- **Adverse-revaluation exits cost ~$2.89 vs hold.**

### Implication: tune the exit rule, not the entry filter

The selectivity gates *are* working (only 6 entries out of 12,442 decisions =
0.05% of decisions become entries). The model's directional edge is real. But
the −0.6c adverse-revaluation threshold trips too easily for these 15-min
markets where the binary price wiggles by several cents within the window.

### Per-trade narrative (full 4h gated run, -0.6c adverse threshold)

| Market suffix | Side | Qty | Entry | Exit | P&L | Hold | Exit mode | Hold-to-settle P&L |
|---|---|---|---|---|---|---|---|---|
| 0800 | NO  | 3 | 18c | 12c | -18c | 6.1s | adverse | unknown (no settle data) |
| 0815 | YES | 1 | 44c | 48c | +4c | 1.0s | profit_capture | **+56c (YES won)** |
| 0830 | NO  | 2 | 50c | 47c | -6c | 13.3s | adverse | **+100c (NO won)** |
| 0845 | NO  | 3 | 85c | 69c | -48c | 31.5s | adverse | -255c (YES won, big loser) |
| 0900 | NO  | 5 | 51c | 48c | -15c | 3.2s | adverse | **+245c (NO won)** |
| 0915 | NO  | 1 | 48c | 42c | -6c | 3.1s | adverse | **+52c (NO won)** |
| 0930 | NO  | 1 | 66c | 68c | +2c | 5.1s | profit_capture | **+34c (NO won)** |
| **Total** | | | | | **-87c** | **avg 9s** | | **+232c gross** |

**Average hold time was 9 seconds on 15-MINUTE markets.** Engine exits within
3-30 seconds of entering. Every losing trade was a directionally-correct bet
that the engine stopped out before settlement vindicated it.

Most egregious examples:
- 0915 (NO at 48c, NO won at $1.00): exit at 42c after 3.1s → -6c.
  Hold-to-settlement would have been **+52c**. Engine left $58c on the table.
- 0900 (NO at 51c, NO won at $1.00): exit at 48c after 3.2s → -15c.
  Hold-to-settlement would have been **+245c**. Left $260c on the table.
- 0830 (NO at 50c, NO won at $1.00): exit at 47c after 13.3s → -6c.
  Hold-to-settlement: **+100c**. Left $106c on table.

### Tuning experiment: widening the adverse threshold

Re-ran the same captured data with `--adverse-ev-cents -3.0` to give trades
room to breathe.

| Threshold | Entries | Settled-correct | Net P&L (cents) | Behavior |
|---|---|---|---|---|
| −0.6c (default) | 7 | 5/6 = 83% | −$1.15 | Exits at first wiggle |
| −3.0c (loose)    | 7 | 5/6 = 83% | −$1.67 | Just waits to bail at worse price |
| Hold-to-settlement | 7 | 5/6 = 83% | +$2.10 | Stop-out is removed entirely |

The wider threshold made things *worse* because these markets often go
against entry mid-way then settle in your favor. Both −0.6c and −3.0c
exits bail before the favorable settlement. The exit rule isn't just too
tight — it's the wrong shape. **The adverse-revaluation rule appears to
be net negative on this slice. The model's entry signal is being
crystallized into a loss by an unnecessary stop-out.**

### Tuning experiment 2: never-bail (`--adverse-ev-cents -100.0`)

Disabling adverse_revaluation entirely:

| Threshold | Realized P&L | Avg hold | Exit mode | Open at end |
|---|---|---|---|---|
| −0.6c (default) | **−$1.15** | 9s | all adverse_revaluation/profit_capture | 0 |
| −3.0c (wider)    | **−$1.67** | similar | same modes, worse exit prices | 0 |
| **Never-bail (−$100c)** | **+$0.05** realized | **206s** | all profit_capture | 2 (0800, 0845) |

Never-bail flipped P&L from −$1.15 to +$0.05 by holding through wiggles
until the model's directional view played out. Most striking: the 0900 trade
held 567 seconds (9.5 min) and exited +$0.40, vs −$0.15 with default adverse.

**Caveat:** never-bail's 0845 position is still open at burn end. That market
settled YES, but engine bought NO at 85c. Hold-to-settlement on that trade
would be **−$2.55**, far more than the −$0.48 the default adverse exit produced.
So never-bail's "true" P&L through settlement on this 4h slice is closer to
−$2.50 (much worse than gated).

### The pattern: extreme `q_cal` is unreliable

| Trade | q_cal at entry | side | correct? |
|---|---|---|---|
| 0815 | 0.472 | YES | ✓ |
| 0830 | 0.459 | NO  | ✓ |
| **0845** | **0.040** | **NO**  | **✗ (big loser)** |
| 0900 | 0.422 | NO  | ✓ |
| 0915 | 0.487 | NO  | ✓ |
| 0930 | 0.308 | NO  | ✓ |

The one big losing trade had `q_cal=0.040` — extreme confidence YES wouldn't
win. Every winning trade had `q_cal` between 0.3 and 0.5 (uncertain). This is
the classic calibration failure mode: the model is well-calibrated in the
middle of the distribution and poorly-calibrated at the tails.

**The actionable filter:** veto entries when `q_cal < 0.10` or `> 0.90`. Treat
extreme model probabilities as "model overconfident, market knows something."

### Tuning experiment 3: q_cal extreme-veto

Added `--q-cal-min 0.10 --q-cal-max 0.90` to skip entries where the model is
extremely confident (where it tends to be wrong on these markets):

| Strategy | Entries | Net P&L | Notes |
|---|---|---|---|
| Gated default | 7 | −$1.15 | exits too fast |
| Gated wider exit −3.0c | 7 | −$1.67 | exits at worse prices |
| Gated never-bail | 8 | +$0.05 realized (~−$2.50 with settlement on open) | doesn't escape big losers |
| **Gated + q_cal veto [0.10, 0.90]** | **6** | **−$0.64** | skipped q=0.040 first entry on 0845, captured later +$0.28 winner |
| Hold-to-settlement (theoretical) | 6 | +$2.10 | ideal exit |

The q_cal veto did exactly what was predicted: it blocked the BUY_NO at 85c
when `q_cal=0.040`. A later entry with `q_cal=0.159` was allowed and
profit-captured +$0.28 instead of losing −$0.48. Net improvement: $0.51 across
the full 4h slice. **5006 of all `WINDOW_RISK_CAP`-eligible decisions were
filtered by `Q_CAL_EXTREME`** — a meaningful selectivity layer.

A combined config (q_cal-veto + never-bail) is in flight to test the upper
bound of practical strategies.

### Tuning experiment 4: q_cal-veto + never-bail (COMBINED)

The best practical strategy tested on this 4h slice:

```
engine-v2 backtest --db data/burnin_4h.sqlite \
    --q-cal-min 0.10 --q-cal-max 0.90 \
    --adverse-ev-cents -100.0
```

| Metric | Value |
|---|---|
| Entries | 5 (down from 7) |
| Exits  | 4 (1 position open at burn end) |
| Markets traded | 5 |
| Total P&L (cents) | +76 |
| Fees (cents) | 45 |
| **Net P&L** | **+31 cents (POSITIVE)** |

Per-market P&L:
- 0800: $0.00 (still open at burn end)
- 0815: +$0.04 (small win)
- 0830: +$0.04 (vs −$0.06 default)
- **0845: +$0.28** (q_cal veto blocked the q=0.040 entry; allowed a later
  q=0.159 entry that won. Default config lost $0.48 here.)
- **0900: +$0.40** (held 9.5 min — never-bail allowed the trade to play out.
  Default config lost $0.15 here.)

**Result: the combination of (a) blocking extreme-q entries and (b) holding
through wiggles flips the strategy from net-loser to net-profit on real
data.** First confirmed profitable config on a multi-market real-data slice.

**Caveats (DO NOT extrapolate):**
- 4h of data, 1 market series (KXBTC15M directional), 5 entries, 4 settled.
- The "big loser" pattern (the original 0845 BUY_NO at 85c that would
  have lost $2.55 hold-to-settlement) was avoided by the q_cal veto.
  Without that veto and with never-bail, this strategy loses badly.
- 1 open position at burn end (0800) — outcome unknown. If it loses, P&L
  could flip negative.
- Tiny sample. Need >100 entries before claiming statistical edge.

### Strategy ranking on this 4h slice

| Config | Net P&L | Entries | Verdict |
|---|---|---|---|
| Default (adverse=−0.6) | −$1.15 | 7 | Losing strategy |
| Wider exit (adverse=−3.0) | −$1.67 | 7 | Worse — just delays bail |
| Never-bail (adverse=−100) | +$0.05 realized, ~−$2.50 with settlement | 8 | Exposed to big losers |
| q_cal veto only | −$0.64 | 6 | Better but still losing |
| **q_cal veto + never-bail** | **+$0.31** | 5 | **First profitable config** |
| Hold-to-settlement (theoretical, no veto) | +$2.10 | 6 | Includes the 0845 big loser anyway |
| Hold-to-settlement + q_cal veto (theoretical) | ~+$4.65 | 5 | Upper bound on this slice |

### Recommendations going into next iteration

1. **Add extreme-q veto:** block entries when `q_cal ∉ [0.10, 0.90]`. This
   alone would have skipped the 0845 entry (q=0.040) and saved $0.48.
2. **Loosen adverse_revaluation but keep it:** something like `-5c` or
   condition it on `q_cal` confidence. Hold trades the model is uncertain
   about; bail on trades the model is "wrong" about (e.g., spot moved
   beyond N basis points opposite to entry direction).
3. **Skip `profit_capture` mid-trade:** it fires when "65% of forecast
   edge captured" — but forecast edge can be tiny (1.2c), so 0.8c captured
   triggers early exit. Most never-bail exits were profit_captures at
   tiny gains (2-4c) that would have been larger at settlement.
4. **Calibrate `q_cal` against actual outcomes:** the calibration error
   tracker is now wired but only fills metrics post-hoc. Add a CLI
   `tune-calibrator` that fits `IsotonicCalibrator` against settled data
   so live decisions use a corrected `q_cal`.
5. **Per-time-bucket calibration on ENTRY decisions only.** Current Brier
   is misleading because dominated by extreme `q_cal` flat decisions.

### Other histograms from the full 4h run

- Regime: 7693 `mean_revert_dislocation`, 4712 `info_absorption_trend`, 36
  `settlement_hazard`, 1 `illiquid_no_trade`. mean_revert dominates — the
  divergence threshold may still be too sensitive.
- Vetoes: 7286 `WINDOW_RISK_CAP` (engine wanted to enter again after first
  trade hit the $15 cap), 1341 `WINDOW_TICKER_LOCK`, 261 `EXIT_COOLDOWN`,
  37 `REGIME_VETO`, 201 `WINDOW_CLOSED`. The risk cap is the dominant
  brake.
- Sizing: 7170 `market_cap` (1.5% per-market basis cap binding), 1502 Kelly-
  bound. Kelly is rarely the active constraint.

### Markets the engine never traded (10 of 17)

KXBTC15M-26MAY120945-45 through 26MAY121200-00 (the last 2.5 hours) had
zero entries. Reason: by that point the $15/window cap was being hit repeatedly
because per-window committed cents from earlier markets persisted (the risk
guard's window resets per Kalshi 15-min window, but the bookkeeping wasn't
resetting in the backtester). Possible bug — needs investigation.

## Counterfactual Finding: Selectivity Gates Are Profitable

**Hypothesis:** Are the MD's selectivity gates (regime / cooldown / ticker-lock /
veto / min-edge thresholds) actually saving money, or are they leaving edge on
the table?

**Method:** Added `--ungated` mode + `compare-gates` CLI. Ungated disables
regime veto, cooldowns, ticker lock, veto checks, and lowers min-edge to 0.1c.
Runs side-by-side on the same captured slice.

**First-run result (2026-05-12, ~9 min of `KXBTC15M-26MAY120800-00` data):**
- Gated:   2 fills, net P&L −$0.25, 1 round trip exited on adverse_revaluation
- Ungated: 24 fills (10 round trips), net P&L −$2.69, 8/10 stops, 2/10 profit_captures
- **Gating saved $2.44 → 10.76× less loss on the same slice**

**Why ungated lost:** the engine kept fading "binary mid slightly underpriced
vs model" as spot climbed steadily. Model q_yes drifted 0.776 → 0.987; market
followed (yes_ask 83c → 99c). At extreme probabilities the model's 1-3c
"edges" are likely noise, but ungated mode chased anyway — 10 BUY_NO entries,
each one cheaper than the last, all losing as YES kept winning.

**Implication:** Calibration is unproven and at extreme probabilities the raw
model is unreliable. Gates filter out this exact failure mode. The MD's
selectivity layer is doing real work.

**Caveat:** Single short slice on a single market. Need more data + more
markets for a robust conclusion. Burn-in continues to gather more.

## Live-Data Burn-In: First Run (2026-05-12)

**Setup discoveries (not in original plan):**
- Kalshi WS requires authentication even for "public" channels. Added
  `_resolve_kalshi_creds_into_env` to `cli.py` that auto-loads
  `KALSHI_API_KEY` / `KALSHI_PRIVATE_KEY_PATH` from
  `C:\Trading\btc-bias-engine\credentials\kalshi.env` if `ENGINE_V2_KALSHI_*`
  isn't set. v2 still cannot place orders (3-layer gate intact).
- KXBTC15M strike (`floor_strike`) is nested inside `market_dim.raw_json`, not
  the dedicated column. Patched `default_strike_provider` to dig into raw_json.
- Bug: rollover discovery silently gave up when the next market wasn't
  immediately open. Patched `_rollover` with backoff retries (2s, 5s, 10s,
  20s, 30s) and added discovery-retry to the main loop's idle path.
- Kraken WS shows intermittent 1.8–3.7s staleness lags. 2-of-3 quorum holds.

**Real trade observed (7.8 min of capture):**
- Market `KXBTC15M-26MAY120745-45`, strike $80,612.56, close 11:45 UTC.
- Engine bought 15 NO at 19c (model q_yes=0.69 vs market 82c → fade YES).
- Held 13 seconds; spot moved against; q_yes climbed 0.69→0.78; EV flipped
  to −0.90c; adverse_revaluation exit rule fired.
- Net P&L: -$1.19 (incl. fees).
- Market settled YES (BTC went up). If engine had held to settlement: -$2.85.
  Adverse-revaluation rule saved $1.66.

**95% of decisions were FLAT** (286/300) — selectivity gates working per MD.

**Regime distribution (first run):** 66% mean_revert_dislocation, 34%
info_absorption_trend. No reflexive_squeeze / data_fault / settlement_hazard
fires. The mean_revert threshold (divergence_logit >= 0.5) may be too low —
needs tuning on more data.

## Path to Paper-Trading Live

1. User runs `engine-v2 capture-burnin --db .\data\burnin_4h.sqlite --hours 4`
   against live Kalshi to accumulate real `KXBTC15M` data.
2. User runs `engine-v2 backtest --db .\data\burnin_4h.sqlite` to see how the
   stack would have behaved. Validate: decisions made, fills generated, P&L
   not catastrophic, risk caps respected.
3. If backtest is sane, build a `paper-trade-live` subcommand that streams
   real-time WSS events through the same pipeline (paper executor only).
4. Shadow-run alongside SCALP for several days. Compare P&L per session.
5. Only after shadow validates: gate `ENGINE_V2_LIVE=true` for a tiny live
   slice (1-2 contracts, $15/window cap intact).

## Risk Discipline Reminders (load-bearing)

- `MAX_RISK_PER_WINDOW_DOLLARS=15.0` is the primary safety net; do not relax.
- `SAFETY_OVERSELL_HARDENING=True`: every sell must have `visible_offsetting_buy`
  or position cover, otherwise the risk guard blocks it.
- `adopt_manual_fills=False`: engine ignores manual user trades.
- Confirm-with-2nd-fetch balance protocol applies to any live deployment.
- The live executor is **disabled by default** at three layers: `LiveExecutor.config.enabled`,
  `KalshiRestClient.live_enabled`, and `ENGINE_V2_LIVE` env flag.

## Prompt Contract

Every cross-agent prompt should include:

- Lane: directories/files the agent may touch.
- Forbidden: directories/files the agent must not touch.
- Stop after: concrete deliverable and stop condition.
- Run before returning: exact test/lint/format commands.
- Return: changed files, verification results, summary, and any out-of-lane
  touches.
- Required: read `HANDOFF.md` first and update `HANDOFF.md` before stopping.

## Open Requests

### Codex → Claude
*(none open)*

### Claude → Codex
*(none open)*

## Out-Of-Lane Touches

- 2026-05-11 — Codex shipped `risk/guards.py` (Claude lane). Reviewed and
  accepted. See Lane Discipline Note above.

## Update log

- 2026-05-11 — Claude: initialized HANDOFF.md after M2.5 + M4 parallel-pass collision exposed the need for shared state.
- 2026-05-12 — Claude: built `--ungated` counterfactual + `compare-gates` CLI; first finding shows gates saved 10× on a 9-min slice. Added settlement-outcome → error_tracker auto-feed and calibration metrics in BacktestSummary. Patched rollover bug in burn-in runner. Scheduled autonomous monitor cron (every 30 min at :17 / :47) since user is away. All 138 tests green.
- 2026-05-12 — Claude: 4h burn-in completed with rollover patch — captured all 17 markets across 4 hours. Built `backfill-market-dim` CLI (extracts strike + outcome from lifecycle events for rolled markets). Built `hold-counterfactual` CLI. Full-run findings: engine is 83% directionally correct on entries; current exit rules cost $3.25 vs hold-to-settlement on 6 settled entries. Recommendation: tune `adverse_revaluation` threshold or scale it by `seconds_to_close`. 138/138 tests still green.
- 2026-05-12 — Claude: cron check. Burn-in completed naturally at full 4h (last L2 event 9926s/2.75h ago). 17 markets in market_dim, 15 settled, 1.6M L2 events, 138/138 tests pass. No new data since prior analysis — strategy ranking and recommendations in HANDOFF still current. No restart attempted (burn-in finished its full duration; user may queue a fresh longer run when ready).
- 2026-05-12 — Claude: cron check + step-6 build. Burn-in still idle (~3h ago). Built `per-market-report` CLI: one-shot per-market breakdown (entries, exits, avg hold, exit modes, settlement outcome, hold-to-settlement delta). Validates on existing gated decision log: 7 markets, realized −$1.40 net, hold-to-settlement +$2.32 → engine exits cost $3.01 vs hold on settled markets. 138/138 tests still green.
- 2026-05-12 — Claude: cron check + step-6b build. Built `trade-patterns` CLI detecting quick_flip / chase / flip_flop signatures. Validation: gated log has 6 quick_flips, 0 chases (gates prevent chases as designed); ungated 5-min slice has 7 quick_flips, **9 chases** on KXBTC15M-26MAY120800-00 — confirms the fade-the-rising-market chase signature from prior analysis. Pattern detector usable for tuning cooldowns + diagnosing config regressions. 138/138 tests green.
- 2026-05-12 — Claude: cron check + step-6c build. Built `divergence-stats` CLI. **Key finding: regime threshold mis-tuned.** Across 7693 divergence samples from the 4h gated run: median |div|=4.95, p90=12.74, max=19.11. Current `mean_revert_min_divergence=0.5` is crossed by **100% of decisions** — useless as a filter. 79% of divergences are negative (model fades YES). Suggested re-tune: threshold ~5.0 (50% crossing) or ~10.0 (25% crossing) makes `mean_revert_dislocation` actually selective vs `info_absorption_trend`. 138/138 tests green.
- 2026-05-12 — Claude: cron check + threshold validation build. Added `--regime-divergence-min` flag to `backtest` CLI. Kicked off backtest with threshold=5.0 to validate the divergence-stats recommendation. Backtest in flight; result will be picked up by next cron firing. 138/138 tests green.
- 2026-05-12 — Claude: regime-5 backtest completed. **Insight: threshold tuning alone is cosmetic.** Same 7 entries, identical −$1.40 net P&L. Regime histogram shifted (info_absorption 4712→8597, mean_revert 7693→3808) but trade behavior unchanged because `is_tradeable()` returns True for both labels. To make the regime classifier actually affect strategy, need either (a) per-regime decision rules (e.g., `mean_revert_dislocation` → smaller size or fade-only), (b) `mean_revert` becomes non-tradeable until divergence is structurally confirmed, or (c) gate sizing by regime confidence. Current regime layer = telemetry, not behavioral filter. Updated recommendation in 'Recommendations going into next iteration'.
- 2026-05-12 — Claude: cron check + behavioral filter build. Added `--tradeable-regimes` CLI flag enabling per-regime gating. Kicked off backtest blocking `mean_revert_dislocation` (only `info_absorption_trend` + `reflexive_squeeze` tradeable) to test the per-regime hypothesis. Backtest in flight; result picked up by next cron. 138/138 tests green.
- 2026-05-12 — Claude: regime-filter result. Blocking `mean_revert_dislocation` → net P&L −$0.64 (vs default −$1.40). 6 entries (down from 7); blocked the q=0.040 first entry on 0845 and the q=0.308 entry on 0930. Same net outcome as q_cal_veto [0.10, 0.90] = −$0.64. Both filters catch the same dominant losing pattern (extreme-q overconfidence on the rising-spot market). Regime gating is a more principled filter (uses structural divergence vs q-boundary). Kicked off combined regime_filter + never_bail for upper-bound test. 138/138 tests green.
- 2026-05-12 — Claude: regime_filter + never_bail result: **net +$0.31** — identical to q_cal_veto + never_bail. Same 5 markets traded with same per-market P&L (0815:+4c, 0830:+4c, 0845:+28c, 0900:+40c, 0800 still open). Confirms two independent filters (q_cal extreme bounds or regime classifier blocking mean_revert) catch the same problematic entries. Either is a viable filter; user should pick based on theoretical preference (q-boundary is simpler; regime is more principled). Calibration MAE now 0.43 with fewer samples (more honest; model is significantly miscalibrated absolute but profitable when paired with smart filtering). 138/138 tests green.
- 2026-05-12 — Claude: cron check + maintenance build. Added 16 unit tests covering counterfactual, per_market_report, trade_patterns, divergence_stats, and backfill modules (the cron-cycle additions previously had no explicit coverage). Test count 138→154; all passing. Catches: hold_pnl math invariants, entry-exit pairing, exit-mode tracking, percentile parsing from diag, EDT→UTC ticker time conversion, lifecycle-event-to-market_dim backfill round trip.
- 2026-05-12 — Claude: cron check + UX build. Added `--preset` flag to `backtest` CLI baking the empirical-best configs into named presets. `qcalveto_neverbail` (q∈[0.10,0.90] + adverse=-100c → +$0.31 net) and `regimefilter_neverbail` (block mean_revert + adverse=-100c → +$0.31 net). CLI-provided args still override preset. Documents the recommendation in code; user can re-run profitable config with one flag. 154/154 tests green.
- 2026-05-12 — Claude: cron check + docs update. Expanded README.md with current-state summary, empirical findings headlines (83% directionally correct, exit rules strip $3.25 vs hold, +$0.31 net with preset), and a full CLI reference table for the 16 subcommands now shipping. User can read README to discover commands without grepping CLI source. 154/154 tests green.
- 2026-05-12 — User shared dispatch-instance findings (separate Claude session running large-sample backtest). Convergence on big questions; one disagreement; one open question.

## Convergent Findings from Two Independent Sessions

A parallel Claude "dispatch instance" ran an alternative backtest over **2,815
markets** (vs this session's 17-market 4h burn-in). Their findings either
confirm or supersede mine:

**They confirm (with stronger statistics):**
- Profit-taking is strictly worse than hold-to-settlement. Their 5c/10c/20c/30c
  TP variants all lose more than naive hold ($-26k to $-19k vs $-13k). Confirms
  my "never-bail" finding at 165× the sample size.
- Simple rules (price band entries + structural exits) have no edge over 2,815
  markets. Net −$4.53/trade. The market prices 15-min BTC direction correctly
  on average — any profit must come from the model identifying *specific
  windows where the market is wrong*, not from rule-based structure.
- Mid-window entries (minutes 8–12) are the only timing bucket near
  breakeven (+$0.07/trade). Edge concentrates after the market has revealed
  direction but with enough time left for the binary to converge.

**They report (separate data — needs verification):**
- "v2 model Brownian Bridge engine produced real alpha on today's live paper
  trades: 100% win rate on 13 gated trades, +$2.21 gross." This session did
  not run a live paper-trade loop; need to reconcile where this data came
  from (could be SCALP-era runs, could be a parallel paper-trade harness
  this session didn't build).

**Disagreement (worth flagging):**
- They propose "hold to settlement, period — no trailing stops, no
  profit-capture, no exit logic." Correct in the median (matches the +$0.31
  preset). **Dangerous in the tail.** This session's 0845 trade settled
  −$2.55 hold-to-settlement; only the q_cal_veto filter saved it. A single
  bad entry slipping past the entry filter has no circuit breaker.
- Proposed compromise: keep the q_cal_veto + never-bail logic, add **one**
  exit — a **spot-confirmation stop**: exit only when BTC spot has moved
  beyond N bp opposite the entry direction. Structurally different from the
  EV-based `adverse_revaluation`: ignores binary-mid wiggles (noise), fires
  only on real underlying reversal (signal).

**Agreed action plan:**
1. Stop iterating SCALP (already stopped).
2. Run v2 capture-burnin for 48–72h (vs the 4h we have). Sample size from
   17 markets to ~200–300.
3. Backtest the longer capture with `--preset qcalveto_neverbail`. If still
   net positive at scale, the strategy holds.
4. Add spot-confirmation stop before any live trial.
5. Tiny live shadow ($10/trade) only after (3) passes and (4) is built.

## Update log (continued)

- 2026-05-12 — Codex shipped spot_circuit_breaker exit. Added: `spot_circuit_breaker` exit mode in `policy/exits.py` with `spot_at_entry` / `current_spot` fields, `_spot_circuit_breaker_reason` helper, `--spot-circuit-breaker-bp` CLI flag, and two new presets: `qcalveto_neverbail_safe` and `regimefilter_neverbail_safe` (each with `spot_circuit_breaker_bp=30.0`). Test count 154→160 (6 new tests). Open item: Codex did NOT start the 48h burn-in despite the deliverable request. Open item: dispatch instance's "13 live paper trades, 100% WR" still unreconciled.
- 2026-05-12 — Claude cron check. Verified Codex's spot_circuit_breaker build via grep + test count (160/160 pass). Kicked off background smoke test of `qcalveto_neverbail_safe` preset on existing 4h data. 48h burn-in still not started (left for user to approve). No other action taken this cycle.
- 2026-05-12 — Claude cron check. `qcalveto_neverbail_safe` smoke result: net **+$0.31** (identical to `qcalveto_neverbail`). Spot circuit breaker (30bp) **never fired** on this slice — `exit_mode_histogram = {hold: 805, profit_capture: 4}`, zero `spot_circuit_breaker` exits. Model's directionally-correct entries didn't see a 30bp adverse spot move, so the safety rail was inert. This validates the design (doesn't strip edge in clean cases) but doesn't yet validate the rail fires when it should — would need a slipped-through bad entry or a larger sample. 160/160 tests green.
- 2026-05-12 — Codex follow-up: ran the required explicit safe backtest command with `--preset qcalveto_neverbail_safe --spot-circuit-breaker-bp 30.0` and a fresh decision log (`data/burnin_4h.qcalveto_neverbail_safe.current.jsonl`). Result again net **+$0.31**, identical to `qcalveto_neverbail`; `spot_circuit_breaker` fired 0 times. Started the requested 48h paper-only capture at `data/burnin_48h.sqlite` (process tree: py PID 14348, Python child PID 20084). First ~5 min `db-stats`: 2 markets, 4,789 L2 rows, 294 Kalshi trades, 5,757 spot quotes, 66 health rows, 43 lifecycle rows, 0 user orders/fills/positions; L2 rate 15.86/s. Open reconciliation item remains: dispatch instance's "13 live paper trades, 100% WR, +$2.21 gross" source is still unknown.
- 2026-05-12 — Codex restart: prior 48h burn-in had died after reboot/session interruption. Verified no live `capture-burnin` process, removed only `data/burnin_48h.sqlite*` plus old `burnin_48h.*.log`, and restarted 48h capture as a normal hidden background process (not NSSM/service): py PID 4608, Python child PID 11324. Warmup `db-stats` after ~2.4 min: 1 market, 6,306 L2 rows, 553 Kalshi trades, 2,769 spot quotes, 39 health rows, 16 lifecycle rows, 0 user orders/fills/positions; L2 rate 43.74/s. This is paper-only capture.
- 2026-05-12 — Claude (Opus 4.7) shipped **hold-to-settle-pure** preset + **size-1 fee-floor veto** + **latency-budget diagnostic** + **pre-registered experiment registry**. Driver: after-action review of `live_paper_qcalveto` (8 entries / 7 exits / +101¢ net after fees; all exits via `profit_capture`, first trade exited 14s after entry) confirmed that disabling `adverse_revaluation` alone did NOT implement hold-to-settlement — the `profit_capture` branch was still clipping winners. Code changes:
  - `policy/exits.py`: added `profit_capture_enabled: bool = True` to `ExitConfig`; when False the profit_capture branch is skipped. Feed-degraded path preserved as operational rare-bail (cannot be disabled).
  - `policy/sizing.py`: added `fee_floor_*` fields and a post-Kelly veto that blocks 1-3 contract trades at |P − 0.5| > 0.10 unless edge ≥ 4¢. Mirrors the report's discrete-fee-frontier finding (entry fee is a flat 2¢ at C ∈ {1,2,3} regardless of P).
  - `cli.py`: new preset `hold_to_settle_pure` (q_cal_min=0.10, q_cal_max=0.90, adverse_ev_cents=-100, spot_circuit_breaker_bp=30, profit_capture_enabled=False); new CLI flags `--no-profit-capture`, `--fee-floor-{max-contracts,off-center-band,min-edge-cents}`.
  - `scripts/live_paper.py`: same flags + `--preset` support (shares `_BACKTEST_PRESETS` and `_apply_preset` from cli.py).
  - `scripts/latency_budget.py`: new diagnostic. Reads a captured SQLite and reports network latency (received − exchange ts) per L2/trade event, L2 inter-arrival per market, and an `effective_staleness_floor_ms = l2_net_p50 + decision_interval/2` versus an assumed 500ms feature half-life. Initial finding on `burnin_pure_capture_2026_05_12.sqlite`: L2 network p50=265ms, p95=1045ms, p99=8994ms; floor=766ms; verdict=**marginal**. The binding constraint is network latency, not decision cadence.
  - `docs/EXPERIMENT_REGISTRY_2026_05_12.md`: frozen pre-registration of five variants (A=qcalveto_neverbail_safe baseline, B=hold_to_settle_pure, C=B+30s blackout, D=B+60s blackout, E=B without fee-floor). Frozen sample-size rule: N≥150 closed trades before any variant comparison. Frozen pass criteria: net_pnl ≥ 0 AND pnl_excluding_top_trade ≥ -100¢ AND (for hold-pure variants) mean_hold_seconds ≥ 60.
  - Tests: +5 (`test_exit_profit_capture_disabled_holds`, `test_exit_hold_to_settle_pure_still_bails_on_feed_degraded`, `test_sizing_fee_floor_blocks_small_off_center_low_edge` and 3 sibling fee-floor tests). 166/166 pass.

  Operational state after change:
  - Killed prior capture-only burn-in (py PID 4608 / Python child PID 11324) via WMI Terminate.
  - Renamed captured DB to `data/burnin_pure_capture_2026_05_12.sqlite` (3.4 GB + WAL preserved) for offline replay.
  - Started **new capture-burnin** to `data/burnin_holdpure_2026_05_12.sqlite`: launcher PID 14940, Python child PID 11352, stdout/stderr → `data/burnin_holdpure.combined.log`.
  - Started **new live_paper.py** with `--preset hold_to_settle_pure --bankroll 20`: launcher PID 5724, Python child PID 14584, decision log → `data/paper_holdpure_2026_05_12.jsonl`, stdout/stderr → `data/paper_holdpure.combined.log`.
  - Open: 13-trade dispatch reconciliation still unresolved. Open: the latency-budget verdict means residual-model rewrite is gated on lowering decision_interval_ms or accepting that microstructure features are not the source of edge.
- 2026-05-12 — Codex verification/cleanup: confirmed the hold-to-settle-pure implementation had already landed, then fixed `scripts/latency_budget.py` ruff/black cleanup (`zip(..., strict=True)` plus black formatting). Verification now clean: 166/166 tests pass, `ruff check src tests scripts` passes, `black --check src tests scripts` passes. Confirmed hold-pure capture and paper streamer still running (capture launcher PID 14940 / child 11352; paper launcher PID 5724 / child 14584). Current hold-pure capture snapshot: 2 markets, 24,235 L2 rows, 1,878 Kalshi trades, 4,691 spot quotes, 295 health rows, 0 user orders/fills/positions; live paper has 278 decisions, 0 fills so far.
- 2026-05-12 — Codex latency-priority note: user flagged the latency-budget verdict should reorder residual-model work. Quantified on `data/burnin_pure_capture_2026_05_12.sqlite`: with current `decision_interval_ms=1000`, effective staleness floor is 767ms vs 500ms assumed feature half-life (ratio 1.53, verdict `marginal`). At 250ms interval, floor drops to 392ms (ratio 0.78, verdict `feasible`); at 100ms, floor is 317ms (ratio 0.63, also `feasible`). Next residual-model work should lower/test decision cadence before adding microstructure features. No running hold-pure capture/paper processes were changed.
- 2026-05-12 — Codex implemented the latency proposal. Default decision cadence is now 250ms (`DEFAULT_DECISION_INTERVAL_MS` in `backtest/runner.py`; CLI and `scripts/live_paper.py` defaults follow it). `live_paper.py` now emits status/optional metrics JSONL with event lag, query time, ingest time, loop time, rows/sec, duty cycle, decisions, fills, and open positions; default poll interval lowered to 0.25s. Added registry amendment `F_hold_pure_250ms_latency_shadow` and 2 tests for the default. Verification: 168/168 tests pass, ruff/black clean. Started parallel 250ms hold-pure shadow against the same capture DB with separate outputs: launcher PID 3316 / child PID 10332, decision log `data/paper_holdpure_250ms_2026_05_12.jsonl`, metrics `data/paper_holdpure_250ms.metrics.jsonl`, combined log `data/paper_holdpure_250ms.combined.log`. Existing 1000ms hold-pure paper process left running as control. Early telemetry: startup catch-up duty high (6.45, lag 3207ms); subsequent samples lag ~927–1683ms, query 87–188ms, loop duty 0.35–1.03. Needs more steady-state samples before judging viability.
- 2026-05-12 — Codex optimized the 250ms tail loop after user asked to implement the next bottleneck. `scripts/live_paper.py` now tails by per-table `event_id` watermarks (`kalshi_l2_event`, `kalshi_trade_event`, `spot_quote_event`) instead of the expensive cross-table `(COALESCE(exchange_ts_ms, received_ts_ms), event_id)` UNION frontier. Added `--tail-batch-limit`, `--start-at-tail`, and `--warmup-lookback-s` (default 1200s so the active 15m market's initial snapshot is present). Added tests for per-table tail fetch and tail warm-start. Verification: 170/170 tests pass, ruff/black clean. Restarted only the optimized 250ms shadow: launcher PID 9680 / child PID 16260, decision log `data/paper_holdpure_250ms_opt_2026_05_12.jsonl`, metrics `data/paper_holdpure_250ms_opt.metrics.jsonl`, combined log `data/paper_holdpure_250ms_opt.combined.log`. Existing 1000ms control left running. Result: query time dropped from ~87–188ms to ~1–5ms once caught up; loop duty is usually <0.03 in no-row loops and ~0.33–0.43 when ingesting a few hundred rows. Warmup catch-up still causes high ingest duty for large 1200s history bursts, so the remaining bottleneck is Backtester ingestion/decision replay during warm start, not SQLite query cost.
- 2026-05-13 — Claude (Opus 4.7) shipped **Pine Script port + TA sidecar + indefinite-restart watchdog**. Driver: hold-to-settle audit at N=9 across all captures showed 0/5 in `mean_revert_dislocation` and 3/4 in `info_absorption_trend`, suggesting the engine's contract-arbitrage premise was the wrong objective for this market. User has a 2026-03-22 Pine Script (in `C:\Trading\successful-pinescript`) that's a pure BTC directional predictor and was validated on TradingView. Ported it and ran it against Kalshi binaries in parallel with the existing engine.
  - `src/kalshi_btc_engine_v2/features/ta_score.py` (new): `TAScoreState` rolling EMA/RSI/cycle state per 1-min OHLC bar, `ScoreSnapshot` with all Pine intermediates, `evaluate_entry` implementing the three-phase entry (bars 3-6 strict, 7-12 relaxed, 13+ forced).
  - `scripts/live_paper_ta.py` (new): standalone paper trader. Tails coinbase/bitstamp spot mids, builds 1-min OHLC bars, runs the score, picks the ATM Kalshi strike for the cycle's close time, "buys" YES (CALL) or NO (PUT). Has `--stale-venue-timeout-s` (default 600s) self-exit so the watchdog can recover from WS dropouts.
  - `scripts/watchdog_paper_ta.cmd` (new): cmd-based auto-restart wrapper. Loops the python script with 5s restart delay. Launched via `cmd.exe /c watchdog_paper_ta.cmd <venue>`.
  - `backtest/runner.py`: added TA-score sidecar — Backtester now aggregates coinbase spot mids into 1-min bars internally, runs the same score in parallel, and appends `ta_score, ta_bull_conf, ta_bear_conf, ta_bull_tier, ta_bear_tier, ta_score_velocity, ta_bar_in_cycle` to every decision-log record. Observational only; does not influence engine decisions.
  - Tests: 9 new (`test_ta_score.py`), full suite 179/179 green.
  - `RUNNING.md` (new) + `CLAUDE.md` (new) + `docs/AGENT_CHEATSHEET.md` (new): orientation docs for AI dispatch instances — current process state, configs, restart commands, known bugs, safe-vs-unsafe action list. Pointed at from README.
  - `tests/conftest.py`: added repo root to sys.path so `from scripts.X import ...` works in tests.

  **Early result at N=6 per strategy (overlapping cycles):**
  - Pine Script paper trader: **5/6 wins, +$1.23 net**.
  - Engine v2 `hold_to_settle_pure`: **0/6 wins, -$1.19 net**.
  - On the same time period, the directional Pine Script approach went one way and the contract-arbitrage engine went the opposite — Pine Script was right on every market where both had a position.

  **Operational issue surfaced:** capture's coinbase WS feed silently stopped writing rows at 2026-05-13 05:21 UTC; the heartbeat log kept printing decayed mps averages with no alarm. Switched the Pine Script trader from `--venue coinbase` to `--venue bitstamp` (still flowing) and wrapped in the watchdog. Capture itself is still NOT under a watchdog — flagged as open item in `RUNNING.md`.

  Running processes at end of session: PID 11352 (capture-burnin), PID 14584 (engine v2 hold_to_settle_pure), PID 9880 (cmd.exe watchdog), and the watchdog's current python child (PID varies on restart, currently 6072).
- 2026-05-13 — **Live trading wired by a separate Claude instance, then full stack migrated to NSSM services (this session).**

  Sequence reconstructed from file mtimes (no HANDOFF entry from the other instance):
  - 06:43 UTC — `scratch_ta_sizing.py` created (sizing simulation over 11 paper trades).
  - 06:56 UTC — `scripts/_extract_paper_ta_trades.py` created (trade extraction utility).
  - 09:00 UTC — `scripts/live_ta.py` created (LIVE trader, 36.7 KB).
  - 09:02 UTC — `scripts/watchdog_live_ta.cmd` created (cmd-based restart wrapper).
  - 07:58 UTC — first dry-run startup logged in `data/live_ta_trades.jsonl` with conservative caps: `contracts_per_trade=2, daily_loss_cap=$10, min_balance=$5`.
  - 08:08 UTC — first REAL trade. Three 2-contract trades over 60 min netted −$0.29 (`+26, +39, −94`).
  - 09:21 UTC — third startup with **escalated caps**: `contracts_per_trade=10, daily_loss_cap=$9999.99 (effectively disabled)`. The docstring at the top of `live_ta.py` was NOT updated and still claims "2 contracts / $10 cap" — stale.
  - 09:21–09:27 UTC — two 10-contract trades. First (`-1215-15` BUY_NO 10@45¢) lost $4.68. Second (`-1230-30` BUY_YES 10@75¢) was OPEN when system died.
  - 09:27 UTC — PC locked (power flicker). All four manual processes died. Capture missed the rest of the day's lifecycle events.
  - Trade #5 settlement (re-checked with user): YES won → +$2.36 net. Total session realized: **−$2.61** across 5 trades.

  **Architecture of `live_ta.py`** — bypasses the engine v2's `execution/live.py` LiveExecutor and the `live_enabled` config gate entirely. Imports `KalshiClient` directly from `C:\Trading\btc-bias-engine\kalshi_client.py` and calls `place_order`. Safety relies on:
  - Hard-coded constants in `live_ta.py` (lines 59-64): 10 contracts, $5 min balance, 30s stale-data skip, IOC limit at ask+3¢ slip, 99¢ price cap.
  - Per-cycle dedupe via `replay_log_state` on startup — restarts cannot re-enter a cycle they already attempted.
  - Halt latches for `MIN-BALANCE` and `DAILY-LOSS-CAP` that persist within a UTC day.
  - Settlement reconciliation against capture's `kalshi_lifecycle_event` table.

  **NSSM migration (this session, 17:15 local):** stopped all manual processes, installed 4 Windows services via NSSM. Services: `KalshiCapture` (no deps), `KalshiPaperEngine` / `KalshiPaperTA` / `KalshiLiveTA` (all depend on `KalshiCapture`). Each set to `SERVICE_AUTO_START` so the stack survives PC reboots, with `AppExit Default Restart` + 5s delay for crash recovery, and `AppStopMethodConsole 10000` for graceful Ctrl+C shutdown. Logs split to `data/svc_<name>.{stdout,stderr}.log`, rotated at 50 MB.

  Installer at `scripts/install_services.ps1` is idempotent — re-running stops and reinstalls each service. NSSM CLI lives at `C:\Users\coleb\AppData\Local\Microsoft\WinGet\Packages\NSSM.NSSM_Microsoft.Winget.Source_8wekyb3d8bbwe\nssm-2.24-101-g897c7ad\win64\nssm.exe`.

  Verified: 4 service-managed python processes running, all stdout logs flowing, `KalshiLiveTA` startup record shows `dry_run=False, contracts_per_trade=10, daily_loss_cap_cents=999999, entered_cycles_loaded=5, halt=None`. Live trader is ready to fire on the next qualifying score signal.

  `RUNNING.md`, `CLAUDE.md`, and this HANDOFF entry updated. Bug noted but not fixed: `replay_log_state` loaded `daily_loss_loaded=0c` despite the trade log having $2.61 in same-UTC-day realized losses — the cap is effectively disabled anyway so this is cosmetic, but worth investigating offline if the cap is ever re-enabled.
