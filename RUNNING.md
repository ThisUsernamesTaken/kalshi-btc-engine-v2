# Running configuration — 2026-05-13 (NSSM era, LIVE TRADING)

This file records the current trading configuration. As of **2026-05-13 17:15**,
all four stack components run as Windows services managed by **NSSM**, so they
survive PC reboots automatically. The **`KalshiLiveTA`** service is real money
on Kalshi — the other three are paper / capture.

Pre-registered paper variants are in
[`docs/EXPERIMENT_REGISTRY_2026_05_12.md`](docs/EXPERIMENT_REGISTRY_2026_05_12.md).

## Why three parallel signals

After the 4-trade `hold_to_settle_pure` run produced 0/4 winners against the
market consensus, the audit showed that the engine's `mean_revert_dislocation`
regime was 0/5 across all captures, while `info_absorption_trend` was 3/4 —
suggesting the engine is mixing a directional bet with a contract-pricing-
dislocation bet, and the latter is unprofitable. The Pine Script approach
(2026-03-22 confidence-tiers, validated on TradingView) is a pure directional
predictor: it never looks at contract pricing. To compare these two
philosophies on the same data, we now run:

1. **Engine v2 with `hold_to_settle_pure`** — q_cal + regime + fee-aware EV.
2. **TA sidecar (observational only)** — same engine, also logs Pine Script
   score per decision. No behavior change.
3. **Standalone Pine Script paper trader** — pure directional logic,
   independent decisions, independent fills.

After 12–24h of run-time, the three logs answer:
- Does the engine's q_cal beat the TA score on the same decisions?
- Does the TA score on its own beat the engine?
- Where they agree, is the agreement-subset more profitable than either alone?

If one signal source clearly wins at N≥150, that's where to focus. If both
fail, the project's null hypothesis (infrastructure > alpha) is confirmed.

## NSSM services (auto-start on boot, auto-restart on crash)

All four python processes are managed by **NSSM**. They start automatically
when Windows boots and restart with 5-second delay if they exit for any
reason. The `KalshiCapture` service is the dependency for the other three
(NSSM waits for it to start before starting them).

| Service | Role | Stdout/Stderr |
|---|---|---|
| `KalshiCapture` | `engine-v2 capture-burnin --hours 168` — WS subscribe + DB writer | `data/svc_capture.{stdout,stderr}.log` |
| `KalshiPaperEngine` | `live_paper.py --preset hold_to_settle_pure --bankroll 20` | `data/svc_paper_engine.{stdout,stderr}.log` |
| `KalshiPaperTA` | `live_paper_ta.py --venue bitstamp --base-stake 1` | `data/svc_paper_ta.{stdout,stderr}.log` |
| **`KalshiLiveTA`** | **`live_ta.py --venue bitstamp` (REAL MONEY)** | `data/svc_live_ta.{stdout,stderr}.log` |
| `KalshiLadderShadow` | `live_ladder_shadow.py` — confirmation-driven DCA observer (SHADOW only, no Kalshi client) | `data/svc_ladder_shadow.{stdout,stderr}.log` |

The python PIDs change on each restart (NSSM-managed). To find the current
PIDs: `Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Sort CreationDate`.

NSSM CLI is at:
`C:\Users\coleb\AppData\Local\Microsoft\WinGet\Packages\NSSM.NSSM_Microsoft.Winget.Source_8wekyb3d8bbwe\nssm-2.24-101-g897c7ad\win64\nssm.exe`

Common commands (requires elevated shell):
```powershell
$NSSM = "<path above>"
& $NSSM status KalshiCapture          # check one
& $NSSM stop KalshiLiveTA             # graceful stop (sends Ctrl+C)
& $NSSM start KalshiLiveTA            # start
& $NSSM restart KalshiLiveTA          # restart
& $NSSM edit KalshiLiveTA             # GUI editor for service settings
```

Re-install everything cleanly: `powershell -ExecutionPolicy Bypass -File
scripts/install_services.ps1` (requires elevation; idempotent — stops and
re-installs each service).

## LIVE trading constants (in `scripts/live_ta.py`)

Hard-coded in the script per user authorization:

### Per-tier sizing (Pine Script 4x/2x/1x/0.5x ratios)

| Tier | Contracts | Rationale |
|---|---|---|
| `STRONG` | 40 | High-conviction signal (conf ≥ 75) — hasn't fired in live yet |
| `MEDIUM` | 20 | Medium-conviction (conf ≥ 50) — hasn't fired yet |
| `WEAK` | 10 | Standard signal (conf ≥ 20, default `entry_thresh`) |
| `MIMIC` | 5 | Forced or late-phase relaxed entry — half-size for lower conviction |
| (unrecognised tier) | 5 | Fallback = MIMIC level |

Tier comes from `kalshi_btc_engine_v2.features.ta_score.TADecision.tier_name`,
set per the Pine Script's confidence-tier classification.

### Other hard caps

| Constant | Value | Effect |
|---|---|---|
| `DAILY_LOSS_CAP_CENTS` | 999999 | Effectively disabled |
| `MIN_BALANCE_CENTS` | 500 | Halts trading if Kalshi balance < $5 |
| `STALE_DATA_TIMEOUT_MS` | 30_000 | Skips entry if last spot tick > 30s old |
| `SLIPPAGE_CENTS` | 3 | IOC limit = ask + 3¢ |
| `LIMIT_CAP_CENTS` | 99 | Limit capped at 99¢ (can't pay $1.00) |

Per-cycle dedupe persists across restarts via decision-log replay. A
restarted service will NOT re-enter cycles it already attempted.

## KalshiLadderShadow (confirmation-driven add-ladder simulator)

**Read-only against the capture DB + `live_ta_trades.jsonl`. Writes to
`data/ladder_shadow.jsonl`. Has NO Kalshi client — cannot place real
orders even if buggy.**

Logic (in `scripts/live_ladder_shadow.py`):

1. Tails new `fill` records from `live_ta_trades.jsonl` and starts
   tracking each as an open position.
2. While the position is open, polls the capture DB every 2s for the
   latest L2 ask/bid for that ticker, the latest spot mid, and the
   top-5 depth on the position's side.
3. Evaluates four conditions for each open position:
   - **Thin liquidity** — `top5_depth < THIN_DEPTH_CAP (200)`
   - **Near strike** — `|spot − strike| / strike < NEAR_STRIKE_FRAC (0.05%)`
   - **Stabilized** — stddev of contract mid over last 10s < `STABILIZED_STD_CENTS (2.0)`
   - **Adverse trigger** — current ask must be ≥ `ADVERSE_TRIGGER_CENTS (5)` below entry
4. When all four fire AND the ladder is `idle`, "places" a shadow rung at
   `current_ask + LADDER_LIFT_CENTS (2)`, logs `would_add`, transitions to `waiting`.
5. After `LADDER_HOLD_SECONDS (10)`, checks the current ask:
   - If `current_ask ≥ rung_price` → log `rung_held`, return to `idle` (can add another rung if conditions persist).
   - Else → log `rung_failed`, transition to `stopped` (no more rungs for this position).
6. Hard cap: `MAX_RUNGS_PER_POSITION = 3` rungs per open position.
7. At settlement (lifecycle determined), emits `settle_with_ladder` with:
   - `actual_net_cents` — live trader's actual outcome
   - `ladder_net_cents` — counterfactual P&L if every shadow rung had been a real fill
   - `combined_net_cents = actual + ladder`
   - `delta_vs_actual_cents = ladder_net_cents` — how much the ladder would have improved (+) or worsened (−) the live position

Adjusting thresholds: edit the module constants at the top of
`scripts/live_ladder_shadow.py` and restart the service:
`& $NSSM restart KalshiLadderShadow`.

Session-bias reversal heuristic is NOT yet gating — needs more data on
predictive value before being added to the four-condition AND.

## Reboot / outage history

- 2026-05-12 20:15 → 2026-05-13 09:27 — first hold-pure paper session.
- 2026-05-13 ~08:00 — second Claude instance built `live_ta.py` + watchdog.
- 2026-05-13 08:08 → 09:27 — first LIVE trading session (5 trades, −$2.61 realized, +1 win that settled while offline).
- 2026-05-13 09:27 — PC locked (power flicker per user). All processes died.
- 2026-05-13 16:10 — paper-only restart (this session, before live-wiring was rediscovered).
- 2026-05-13 17:15 — full NSSM service installation. Live trader resumed.

## Decision logs

All three are JSONL, append-only, one record per decision/snapshot/fill.

- `data/paper_holdpure_2026_05_12.jsonl` — Engine v2 decisions under
  `hold_to_settle_pure`. As of this file's commit, each record now also
  includes `ta_score`, `ta_bull_conf`, `ta_bear_conf`, `ta_bull_tier`,
  `ta_bear_tier`, `ta_score_velocity`, `ta_bar_in_cycle` (the sidecar fields).
  This is the **engine + sidecar** stream.
- `data/paper_ta_2026_05_12.jsonl` — Standalone Pine Script paper trader.
  Records `kind ∈ {snapshot, fill, decision_no_market, settle}` per row.
- `data/burnin_holdpure.combined.log` and `data/paper_holdpure.combined.log`
  and `data/paper_ta.combined.log` — stdout/stderr of the three processes.

## Engine v2 configuration (PID 14584)

```
--preset hold_to_settle_pure
--bankroll 20
```

Which expands to (see `cli.py:_BACKTEST_PRESETS`):
- `adverse_ev_cents = -100`   (EV-flip stop disabled; feed-degraded path preserved)
- `spot_circuit_breaker_bp = 30`   (structural rare-bail kept)
- `profit_capture_enabled = False`   (no profit_capture branch — true hold-to-settle)
- `q_cal_min = 0.10, q_cal_max = 0.90`   (extreme-conviction veto)
- Fee-floor veto active at defaults: `fee_floor_max_contracts = 3`,
  `fee_floor_off_center_band = 0.10`, `fee_floor_min_edge_cents = 4.0`
- Decision interval: **250ms** (was 1000ms — see Codex's latency fix)

## Pine Script paper trader configuration (under watchdog PID 9880)

```
--venue bitstamp --base-stake 1 --start-at-tail --stale-venue-timeout-s 600
```

Launched by `scripts/watchdog_paper_ta.cmd` which loops the python script
indefinitely. If the inner script exits for any reason (crash, stale-venue
self-exit at 10 min of no events, manual signal), the watchdog logs the
exit code and restarts after 5 seconds. To stop indefinitely: kill PID 9880
from an elevated shell (the child python will exit on stdin/stdout closure).

Why bitstamp: the original launch used `coinbase` but the capture's coinbase
WS feed silently stopped writing to `spot_quote_event` at 2026-05-13 05:21 UTC,
causing the TA trader's watermark to stall at event_id 106549. Bitstamp is
still flowing reliably (event_id growing past 123,000). If coinbase comes
back, switch by killing the watchdog and relaunching with `bitstamp` → `coinbase`
in the cmd arg (or `--venue coinbase` if launching directly).

- Aggregates bitstamp spot mids into 1-min OHLC bars.
- Score: `120·cycleReturn% + 200·emaSpread% + 25·rsiBias + 15·candlePressure + 10·(relVol−1)`,
  smoothed by `ema(rawScore, 2)`. Volume term defaults to 1.0 (we don't
  capture BTC spot trade events).
- Three-phase entry: bars 3–6 strict, 7–12 relaxed (vel-align + bad-hour
  filters bypassed), 13+ forced (any non-zero score lean fires).
- Tier sizing: STRONG (4x), MEDIUM (2x), WEAK (1x), MIMIC (0.5x).
- Bad-hour filter: UTC 9, 15, 16 blocked in phase 1.
- Market selection: for the cycle's close time, pick the strike whose
  current `yes_ask` is closest to 50¢ (ATM). BUY_YES on CALL, BUY_NO on PUT.
- Settlement reconciliation: when `kalshi_lifecycle_event.status = 'determined'`
  appears for the position's ticker, mark closed with the reported result.

## How to inspect progress

```powershell
# Status lines from each process
Get-Content "data\paper_holdpure.combined.log" -Tail 5
Get-Content "data\paper_ta.combined.log" -Tail 5

# Engine v2 decisions
python -c "import json; [print(json.loads(l).get('action'), json.loads(l).get('market_ticker'), json.loads(l).get('ta_score')) for l in open('data/paper_holdpure_2026_05_12.jsonl').readlines()[-20:]]"

# TA standalone fills only
python -c "import json; [print(json.loads(l)) for l in open('data/paper_ta_2026_05_12.jsonl') if json.loads(l).get('kind')=='fill']"

# Live-paper status tail (most useful one)
Get-Content "data\paper_ta.combined.log" -Tail 3
```

## How to stop

```powershell
# Elevated PowerShell required.
$NSSM = "C:\Users\coleb\AppData\Local\Microsoft\WinGet\Packages\NSSM.NSSM_Microsoft.Winget.Source_8wekyb3d8bbwe\nssm-2.24-101-g897c7ad\win64\nssm.exe"

# Pause LIVE trading only (real-money emergency stop):
& $NSSM stop KalshiLiveTA

# Stop everything in dependency order:
& $NSSM stop KalshiLiveTA
& $NSSM stop KalshiPaperTA
& $NSSM stop KalshiPaperEngine
& $NSSM stop KalshiCapture

# To prevent auto-start on next boot:
& $NSSM set KalshiLiveTA Start SERVICE_DEMAND_START
```

NSSM sends a graceful Ctrl+C and waits up to 10s for the python signal
handlers to checkpoint. Stopping does NOT corrupt the SQLite — WAL is
replayed on next open. Decision-log JSONLs are append-mode so restarts
continue them. The live trader rebuilds its per-cycle dedupe set from
the trade log on startup, so duplicate entries are not possible.

## How to restart

NSSM auto-restarts on crash (5s delay) and auto-starts on boot. Manual
restart is rarely needed but works via:

```powershell
& $NSSM restart KalshiLiveTA          # single service
& $NSSM start KalshiCapture           # if disabled
```

For a clean reinstall (e.g. after editing service args or `live_ta.py`):
```powershell
powershell -ExecutionPolicy Bypass -File C:\Trading\kalshi-btc-engine-v2\scripts\install_services.ps1
# Then:
foreach ($s in "KalshiCapture","KalshiPaperEngine","KalshiPaperTA","KalshiLiveTA") { & $NSSM start $s }
```

### Legacy manual launch (if NSSM is unavailable):

```powershell
$d = "C:\Trading\kalshi-btc-engine-v2"
$py = "C:\Users\coleb\AppData\Local\Python\bin\python.exe"
$db = "$d\data\burnin_holdpure_2026_05_12.sqlite"

# Capture (will resume; WS reconnects, watermarks advance)
$captureCmd = "& '$py' -m kalshi_btc_engine_v2.cli capture-burnin --db '$db' --hours 48 *> '$d\data\burnin_holdpure.combined.log'"
Start-Process powershell -ArgumentList "-NoProfile","-WindowStyle","Hidden","-Command",$captureCmd -WindowStyle Hidden

# Engine v2 paper (hold_to_settle_pure)
$paperCmd = "`$env:PYTHONPATH='$d\src'; & '$py' '$d\scripts\live_paper.py' --db '$db' --decision-log '$d\data\paper_holdpure_2026_05_12.jsonl' --preset hold_to_settle_pure --bankroll 20 *> '$d\data\paper_holdpure.combined.log'"
Start-Process powershell -ArgumentList "-NoProfile","-WindowStyle","Hidden","-Command",$paperCmd -WindowStyle Hidden

# Pine Script paper, under indefinite-restart watchdog
Start-Process -FilePath "cmd.exe" -ArgumentList "/c","$d\scripts\watchdog_paper_ta.cmd","bitstamp" -WindowStyle Hidden
```

## Preserved data from prior runs

- `data/burnin_pure_capture_2026_05_12.sqlite` (3.4 GB) — the 2h capture-only
  run before this configuration. Has lifecycle data on 10 settled BTC markets.
  Used for the calibration audit that produced the 75%/0% regime split.
- `data/paper_continuous_qcalveto.gated_full.jsonl` and
  `data/burnin_4h.qcalveto_neverbail_safe.current.jsonl` — earlier decision
  logs from the `qcalveto_neverbail_safe` preset. Audit drew on these.

## Operational note: capture-burnin venue resilience

On 2026-05-13 the coinbase and kraken spot-quote feeds silently stopped
writing to the DB at ~05:21 UTC (after ~9h of capture). Bitstamp kept
flowing. The capture process itself (PID 11352) did not crash and the
heartbeat log kept printing mps averages, but those averages decay over
time without firing any alarm. There is no current alert for a per-venue
silent dropout. Treat the TA watchdog's `--stale-venue-timeout-s` as a
band-aid, not a fix. A proper solution would either reconnect the venue
WS on staleness inside `BurnInRunner`, or have the TA trader auto-fail-
over to whichever venue is freshest at the moment of query.

## Open items at this snapshot

- **Paper executor doesn't reconcile on settlement.** The engine's
  `live_paper.py` tail doesn't ingest `kalshi_lifecycle_event`, so settled
  positions stay marked `is_flat=False` and `open_positions` in the status
  line is stale. Doesn't affect P&L computation (which happens offline via
  `settled-markets` + the decision log), but the live status line lies.
  Fix: extend `TAIL_TABLES` in `scripts/live_paper.py` to include lifecycle
  events, and wire a settlement handler in `PaperExecutor`. Not done.
- **`market_dim.status` not refreshed on settle.** Same root cause —
  `backfill-market-dim` only upserts new tickers, never updates existing
  ones with their settlement result. Means `engine-v2 settled-markets`
  shows 0 even when the lifecycle table has them. Workaround: query
  `kalshi_lifecycle_event` directly with `status='determined'`.
- **Dispatch reconciliation** ("13 live paper trades, 100% WR, +$2.21")
  still unresolved per HANDOFF.md.
- **Throughput problem.** ~10 settled BTC markets/day × ~25% engine
  participation rate = ~2-3 engine entries/day. The registry's N≥150
  target implies 50–80 days of capture, far beyond the 48h target. Either
  extend, or admit a smaller-N analysis with wider confidence bounds.
