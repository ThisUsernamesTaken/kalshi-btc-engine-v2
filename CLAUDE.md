# CLAUDE.md — Orientation for AI agents

If you're a Claude or other AI assistant joining this directory, read this first.
You are joining a **paper-only research project** with live processes already running.
Do not assume nothing is happening — there is active state to respect.

## Project in one paragraph

`kalshi-btc-engine-v2` is a Kalshi BTC 15-minute binary trading research stack.
It is **separate** from the live Polymarket engine in `C:\Trading\btc-bias-engine`
(do not import from it, do not share credentials except via the existing
`_resolve_kalshi_creds_into_env` helper in `cli.py`). The stack is **paper-only**:
`live_enabled: false` in `configs/default.json`, and the user has not authorized
live execution. Do not flip that flag.

## Right now (load-bearing operational state)

**Four Windows services are running under NSSM as of 2026-05-13 17:15.**
They auto-start on boot and auto-restart on crash. One is **LIVE TRADING
REAL MONEY** on Kalshi.

```powershell
# Status check (no admin needed):
$NSSM = "C:\Users\coleb\AppData\Local\Microsoft\WinGet\Packages\NSSM.NSSM_Microsoft.Winget.Source_8wekyb3d8bbwe\nssm-2.24-101-g897c7ad\win64\nssm.exe"
foreach ($s in "KalshiCapture","KalshiPaperEngine","KalshiPaperTA","KalshiLiveTA") { Write-Host "$s : $(& $NSSM status $s)" }
```

| Service | Role | Output |
|---|---|---|
| `KalshiCapture` | WS subscribe + DB write (`burnin_holdpure_2026_05_12.sqlite`) | `data/svc_capture.{stdout,stderr}.log` |
| `KalshiPaperEngine` | Engine v2 paper with `hold_to_settle_pure` preset | `data/svc_paper_engine.{stdout,stderr}.log` |
| `KalshiPaperTA` | Pine Script PAPER trader | `data/svc_paper_ta.{stdout,stderr}.log` |
| **`KalshiLiveTA`** | **Pine Script LIVE trader (REAL MONEY)** | `data/svc_live_ta.{stdout,stderr}.log` |

Decision logs (where the actual trade data lives):
- `data/paper_holdpure_2026_05_12.jsonl` — engine v2 paper decisions + TA sidecar
- `data/paper_ta_2026_05_12.jsonl` — Pine Script paper fills/settles
- **`data/live_ta_trades.jsonl`** — **LIVE trader: orders, fills, settles, halts**

`RUNNING.md` has the full operational details, NSSM commands, restart
procedures, hard-cap constants, and reboot history. **Read it before
touching anything.**

## Two competing strategies being A/B-tested

| Strategy | Where | Premise |
|---|---|---|
| Engine v2 `hold_to_settle_pure` | `scripts/live_paper.py` | Contract pricing has fair-value gaps; trade them. q_cal + regime + fee-aware EV |
| Pine Script directional | `scripts/live_paper_ta.py` | BTC itself is directional; predict up/down with TA score (EMA + RSI + cycle return) and buy ATM YES/NO accordingly |

At small N so far (6 closed trades each on overlapping markets) the Pine Script
is +$1.23 / 5–6 wins, the engine is −$1.19 / 0–6 wins. The user is letting both
run to gather more N. **Do not change either strategy's logic without explicit
direction.**

## Critical context the user will assume you remember

From the user's project memory:
- **Never leave unauthorized strategies running.** Verify before claiming a
  strategy is "disabled" — check what placed the last trade.
- **Never trust take-profit fills — track actual balance.** Don't measure
  P&L by counting "wins"; settle against the lifecycle data.
- **Never oversell.** One trade per session per ticker. Reconcile residuals.

These apply broadly across all the user's trading work. The engine v2 already
has guards in `risk/guards.py` and `risk/cooldowns.py` reflecting these.

## Python and environment

- **Python interpreter:** `C:\Users\coleb\AppData\Local\Python\bin\python.exe`
  (non-standard install; system PATH points to Windows Store stub).
- **PYTHONPATH:** `C:\Trading\kalshi-btc-engine-v2\src`
- **Encoding:** set `PYTHONIOENCODING=utf-8` before any CLI that prints
  non-ASCII (some presets contain `−` U+2212 minus signs).
- **Tests:** `python -m pytest tests/ -q` — should report **179 passed**.
- **Credentials:** auto-resolved by `_resolve_kalshi_creds_into_env` from
  `C:\Trading\btc-bias-engine\credentials\kalshi.env`. Do not copy or commit.

## File map

```
RUNNING.md                  # Current operational state (PIDs, configs, commands)
HANDOFF.md                  # Dated change log; chronological project history
README.md                   # Quick-start and CLI reference
CLAUDE.md                   # This file

docs/
  EXPERIMENT_REGISTRY_2026_05_12.md   # Pre-registered burn-in design
  MASTER_HANDOFF.md                   # Higher-level milestone summary

src/kalshi_btc_engine_v2/
  cli.py                              # All `engine-v2 <subcmd>` commands; _BACKTEST_PRESETS lives here
  config.py                           # Pydantic-style config schema
  adapters/{kalshi.py, spot.py}       # WS payload → typed records
  capture/                            # BurnInRunner + WS subscribe layer
  storage/{schema.py, sqlite.py}      # DDL + connection helper
  core/{events.py, time.py}           # ReplayEvent type, ts helpers
  models/{ensemble.py, fair_prob.py, regime.py, vol_estimator.py, error_tracker.py}
  policy/{decision.py, edge.py, exits.py, sizing.py, veto.py, windows.py}
  features/{engine.py, ta_score.py}   # ta_score.py = Pine Script port
  execution/{paper.py, live.py, types.py}   # paper executor; live UNUSED
  risk/{guards.py, cooldowns.py}
  monitoring/continuity.py
  backtest/{runner.py, ...}           # Event-driven backtester; sidecar TA score lives here
  replay/engine.py                    # Deterministic event replay

scripts/
  live_paper.py                       # Engine v2 tail-paper-trader
  live_paper_ta.py                    # Pine Script standalone paper trader
  watchdog_paper_ta.cmd               # cmd-based auto-restart wrapper
  latency_budget.py                   # Network-latency diagnostic
  run_smoke.ps1                       # Wires init-db + smoke-replay

tests/                                # 179 tests; run with -q for green/red only
configs/default.json                  # live_enabled: false; venue list

data/
  burnin_holdpure_2026_05_12.sqlite   # ACTIVE capture DB; do not delete
  burnin_pure_capture_2026_05_12.sqlite  # Preserved pre-restart capture
  paper_holdpure_2026_05_12.jsonl     # Engine decisions + TA sidecar fields
  paper_ta_2026_05_12.jsonl           # Pine Script decisions + fills + settles
  paper_holdpure.combined.log         # Engine stdout/stderr
  paper_ta.combined.log               # Pine Script stdout/stderr + watchdog log
  burnin_holdpure.combined.log        # Capture stdout/stderr
```

## Safe inspection commands (do these before touching anything)

```powershell
# Latest engine + TA status lines
Get-Content data\paper_holdpure.combined.log -Tail 3
Get-Content data\paper_ta.combined.log -Tail 5

# Count Pine Script fills + settles + outcomes
$py = "C:\Users\coleb\AppData\Local\Python\bin\python.exe"
& $py -c "import json; fills=[json.loads(l) for l in open(r'data\paper_ta_2026_05_12.jsonl') if json.loads(l).get('kind')=='settle']; print(f'settled={len(fills)} net={sum(t[\"net_cents\"] for t in fills):+d}c wins={sum(1 for t in fills if t[\"net_cents\"]>0)}')"

# DB row counts (cheap)
& $py -m kalshi_btc_engine_v2.cli db-stats --db data\burnin_holdpure_2026_05_12.sqlite

# What's settled (relies on market_dim — broken; use lifecycle for truth)
& $py -m kalshi_btc_engine_v2.cli settled-markets --db data\burnin_holdpure_2026_05_12.sqlite
```

## Known bugs — do NOT "fix" without explicit direction

1. **PaperExecutor doesn't reconcile on settlement.** The engine v2 tail
   loop (`live_paper.py`) doesn't ingest `kalshi_lifecycle_event`, so
   settled positions stay marked `is_flat=False`. `open_positions` in the
   status line is stale. The Pine Script trader has its own reconciliation
   that DOES work via the `lookup_settlement` function. Documented in
   `RUNNING.md`.
2. **`market_dim.status` not refreshed by `backfill-market-dim`.** Existing
   rows are skipped (line ~166 of `backtest/backfill.py`). Result: `settled-markets`
   returns 0 for already-discovered tickers. Workaround: query
   `kalshi_lifecycle_event` directly with `status='determined'`.
3. **Capture has no per-venue stall detection.** Coinbase WS dropped silently
   at 2026-05-13 05:21 UTC; the heartbeat log kept printing decayed mps
   averages with no alarm. The Pine Script trader uses bitstamp via
   `--venue bitstamp` and has a `--stale-venue-timeout-s` self-exit so the
   watchdog can recover.
4. **Capture itself is not under a watchdog.** If `capture-burnin` dies, the
   whole stack is dead but the watchdog'd Pine Script will restart-loop
   reading a frozen DB. Worth wrapping but the user hasn't asked.

## Memory and prior incidents

The user's project memory at `C:\Users\coleb\.claude\projects\C--Trading\memory\`
contains feedback memories from real losses on the live Polymarket engine
(2026-03 to 2026-04). Read `feedback_balance_tracking.md`, `feedback_no_oversell_no_reentry.md`,
and `feedback_no_unauthorized_strategies.md` before touching anything
trading-adjacent. The Kalshi engine is paper-only but the same discipline
applies — verify, don't extrapolate, never claim a strategy is off without
confirming what placed the last trade.

## What changes are safe vs need approval

**Safe without asking** (additive, paper-only, reversible):
- Adding analysis scripts under `scripts/` or as `_*.py` temp files (clean up after)
- Reading any data file
- Running tests
- Querying SQLite read-only (`mode=ro` URI)
- Inspecting decision logs

**Ask first**:
- Stopping any of the four running processes
- Restarting with different config
- Modifying `live_paper.py` or `live_paper_ta.py` while running
- Changing presets, exit logic, sizing, or veto thresholds
- Deleting any `data/` file (decision logs and DBs are load-bearing)

**Never without explicit user-confirmed authorization**:
- Setting `live_enabled: true` in `configs/default.json` (currently `false`, but `scripts/live_ta.py` bypasses this gate)
- Setting `ENGINE_V2_LIVE=true`
- Placing actual orders manually
- Modifying `scripts/live_ta.py` hard-cap constants (`CONTRACTS_PER_TRADE=10`, `DAILY_LOSS_CAP_CENTS=999999`, `MIN_BALANCE_CENTS=500`) — these are intentional per user but changes require explicit re-authorization
- Touching `C:\Trading\btc-bias-engine\` (used as the KalshiClient library source; do NOT modify, the live trader depends on it)

**Note about `live_ta.py`'s architecture**: it does NOT route through
`execution/live.py` (the engine v2's LiveExecutor with built-in safety
gates). Instead it imports `KalshiClient` directly from
`C:\Trading\btc-bias-engine\kalshi_client.py` and calls `place_order`
on its own. Safety relies entirely on the hard-coded constants in
`live_ta.py` (lines 59-64) and the per-cycle dedupe in the trade log.
The `live_enabled` flag in `configs/default.json` does NOT gate this code path.

## When in doubt

Read `RUNNING.md` for current state, `HANDOFF.md` for history,
`docs/EXPERIMENT_REGISTRY_2026_05_12.md` for the burn-in design. Ask the
user before taking irreversible actions. The next dispatch instance should
be able to pick up exactly where this one left off using these three docs
plus this file.
