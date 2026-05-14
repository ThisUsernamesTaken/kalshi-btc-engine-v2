# Kalshi BTC Engine V2

Standalone, paper-first foundation for a Kalshi BTC 15-minute market-ecology engine.

This project is intentionally separate from `C:\Trading\btc-bias-engine`. Do not import from that
live service, share state files, or reuse live credentials without an explicit follow-up approval.

## For AI agents joining this directory

Start with **[CLAUDE.md](CLAUDE.md)** for orientation, then **[RUNNING.md](RUNNING.md)** for the
current operational state (running processes, configs, PIDs, restart commands), and
**[docs/AGENT_CHEATSHEET.md](docs/AGENT_CHEATSHEET.md)** for copy-pasteable diagnostic commands.
The chronological project history is in [HANDOFF.md](HANDOFF.md). The pre-registered burn-in
design is in [docs/EXPERIMENT_REGISTRY_2026_05_12.md](docs/EXPERIMENT_REGISTRY_2026_05_12.md).

## Safety Defaults

- Paper trading is the default.
- Live order placement requires `ENGINE_V2_LIVE=true`.
- Even with `ENGINE_V2_LIVE=true`, the execution gateway milestone is out of scope until approval.
- Credentials use the `ENGINE_V2_` prefix so they do not collide with any existing bot.
- Market data and replay state live under this project directory by default.

## Current State (2026-05-12)

Milestones 1 through 9 are implemented:

- M1 adapters, M2 warehouse + replay, M2.5 burn-in runner
- M3 features (Codex), M4 fair-prob + vol_estimator bridge
- M5 (deferred — rule-based regime classifier instead of LightGBM)
- M6 decision policy, M7 paper-execution gateway, M8 backtester, M9 health monitor
- Ecology layer: VPIN toxicity, regime classifier, ensemble probabilities, error tracker, cooldowns

Plus analysis CLIs added during cron-cycle iteration:

- `compare-gates`, `hold-counterfactual`, `per-market-report`,
  `trade-patterns`, `divergence-stats`, `backfill-market-dim`,
  `settled-markets`, `db-stats`, `walk-forward`.

154 unit tests passing. Live engine at `C:\Trading\btc-bias-engine\` was never modified.

## Empirical Findings From The 4h Burn-In (Single-Sample Caveat)

See `HANDOFF.md` for the full story. Headlines:

- Engine entries are **83% directionally correct** on settled trades (5/6).
- Default `adverse_revaluation` exit (-0.6c) **strips winners** — average hold
  was 9 seconds on 15-minute markets. Net P&L: −$1.15.
- Two filters each fix this independently:
  - `q_cal` extreme veto `[0.10, 0.90]` blocks overconfident wrong bets.
  - Regime classifier blocking `mean_revert_dislocation` does the same.
- Combined with `--adverse-ev-cents -100.0` (disables the stop):
  **net +$0.31 on the 4h slice** vs default −$1.15.

The CLI ships these as **presets**:

```powershell
python -m kalshi_btc_engine_v2.cli backtest --db .\data\burnin.sqlite --preset qcalveto_neverbail
python -m kalshi_btc_engine_v2.cli backtest --db .\data\burnin.sqlite --preset regimefilter_neverbail
```

**Sample is tiny (5 entries, 4h, one BTC series).** Do not extrapolate.

## CLI Reference

All commands invoke `python -m kalshi_btc_engine_v2.cli <subcommand>`. Run with
`--help` for full args. Common args: `--db <path>` (SQLite warehouse).

### Capture and lifecycle

| Command | Purpose |
|---|---|
| `init-db` | Initialize an empty SQLite warehouse |
| `smoke-replay` | Insert deterministic sample data and replay it |
| `capture-burnin` | Paper-only Kalshi + spot capture for N hours |
| `print-ddl` | Print full schema DDL |

### Inspection

| Command | Purpose |
|---|---|
| `db-stats` | Row counts, health histogram, markets observed |
| `continuity-report` | Sequence gaps / duplicates / runtime |
| `settled-markets` | List settled markets and YES/NO outcomes |
| `backfill-market-dim` | Repair `market_dim` from captured lifecycle events |

### Backtest and analysis

| Command | Purpose |
|---|---|
| `backtest` | Event-driven backtest of captured DB through full pipeline |
| `walk-forward` | Rolling train/validate/test windows (needs days of data) |
| `compare-gates` | Side-by-side gated vs ungated; "is selectivity profitable" |
| `hold-counterfactual` | Per-entry hold-to-settlement vs actual exit |
| `per-market-report` | Per-market entries, exits, hold time, exit modes, settlement delta |
| `trade-patterns` | Detect quick_flip / chase / flip_flop signatures |
| `divergence-stats` | Distribution of `divergence_logit`; suggests regime threshold |

### Backtest tunable flags

- `--preset qcalveto_neverbail` or `--preset regimefilter_neverbail` — apply the proven-profitable config (4h slice).
- `--ungated` — disable regime / cooldown / ticker-lock / veto / min-edge (counterfactual mode).
- `--adverse-ev-cents <N>` — adverse_revaluation threshold (default `-0.6`; `-100` disables).
- `--q-cal-min <P>` / `--q-cal-max <P>` — extreme-confidence veto (default `[0.0, 1.0]`).
- `--regime-divergence-min <X>` — `mean_revert_dislocation` divergence threshold (default `0.5`; 4h data suggests ≈5).
- `--tradeable-regimes <csv>` — restrict tradeable regime labels (default = all 3).
- `--decision-log <path>` — write per-decision JSONL for offline analysis.
- `--min-edge-override <N>` — override per-window minimum edge.

Later milestones should build on this foundation without weakening the paper-first guard.

## Quick Start

From this directory:

```powershell
$env:PYTHONPATH = "src"
python -m kalshi_btc_engine_v2.cli init-db --db .\data\engine_v2.sqlite
python -m kalshi_btc_engine_v2.cli smoke-replay --db .\data\smoke.sqlite
python -m kalshi_btc_engine_v2.cli continuity-report --db .\data\smoke.sqlite
```

The smoke replay inserts a tiny deterministic sample and proves the schema, book reconstruction,
spot fusion, and replay ordering are wired together.

## Environment

Copy `.env.example` to a private location if you need authenticated Kalshi access. Do not commit
private keys.

Required for authenticated Kalshi calls:

- `ENGINE_V2_KALSHI_KEY_ID`
- `ENGINE_V2_KALSHI_PRIVATE_KEY_PATH`

Optional:

- `ENGINE_V2_ENV=prod|demo`
- `ENGINE_V2_DATA_DIR=C:\Trading\kalshi-btc-engine-v2\data`
- `ENGINE_V2_LIVE=false`

## Burn-In

The paper-only capture burn-in runner records Kalshi market data, BTC/USD spot feeds, lifecycle
rollovers, and capture health without placing orders:

```powershell
$env:PYTHONPATH = "src"
python -m kalshi_btc_engine_v2.cli capture-burnin --db .\data\burnin.sqlite --hours 4
```

Add `--market-ticker` to pin the first KXBTC15M market. The runner prints a completion continuity
report when the requested duration elapses or when stopped cleanly with Ctrl+C.
