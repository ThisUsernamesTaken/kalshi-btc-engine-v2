# Installing on another machine

These instructions reproduce the exact production setup running on the
author's workstation as of 2026-05-14. Designed for another AI agent or
human operator who needs to set up the live + paper Kalshi BTC trading
stack on a fresh Windows machine.

**Read `CLAUDE.md` first if you are an AI agent.** It explains what each
component does and the safety rules. This file is the mechanical setup.

## Prerequisites

| Requirement | Why |
|---|---|
| Windows 10/11 (admin access) | NSSM is Windows-only; live services need admin |
| Python 3.11 or newer | Tests run on 3.14 currently; 3.11+ confirmed working |
| ~50 GB free disk on `C:` | Capture DB grows ~1.5 GB/day; logs ~50 MB/service |
| Kalshi production account + API key + RSA-PSS private key | For `KalshiLiveTA` live order placement |
| Internet (low-latency to Kalshi US-East endpoints) | Latency budget: 250ms decision interval, p50 net latency ≈ 265ms |

Optional but recommended:
- `gh` (GitHub CLI) for managing the repo
- `winget` for installing NSSM
- A second machine or VM if you want to test paper-only before live

## Step 1 — Directory layout

The codebase has hardcoded paths in a few places. Match them exactly to
avoid editing files. Required layout:

```
C:\Trading\
    kalshi-btc-engine-v2\        # this repo (clone target)
    btc-bias-engine\             # provides KalshiClient — see Step 2
        credentials\
            kalshi.env           # Kalshi API key path + path to PEM
        kalshi_client.py         # RSA-PSS auth client
```

If you must install elsewhere, the following references will need editing:
- `scripts/live_ta.py` line ~47 (`_V1_ROOT`)
- `scripts/live_ta.py` line ~66 (`KALSHI_CREDS_PATH`)
- `src/kalshi_btc_engine_v2/cli.py` line ~23 (`LIVE_ENGINE_CREDS_PATH`)
- `scripts/install_services.ps1` `$ENGINE` and `$DB` variables
- `scripts/watchdog_paper_ta.cmd` and `scripts/watchdog_live_ta.cmd` `ENGINE_DIR` variable

## Step 2 — Clone repos

```powershell
mkdir C:\Trading
cd C:\Trading
git clone https://github.com/ThisUsernamesTaken/kalshi-btc-engine-v2.git
# btc-bias-engine is private and contains the live engine source.
# If you don't have access, you'll need a stand-in for kalshi_client.py
# that provides:
#   class KalshiClient(key_id, private_key_pem, demo=False) — async context manager
#       .get_balance() -> .balance (cents)
#       .place_order(ticker, side, count, price, order_type, action, time_in_force) -> Order
#       .order_id, .filled_count, .average_price, .status fields on Order
#   class KalshiAPIError(Exception) — with .status, .body, .path
# See scripts/live_ta.py for exact usage.
git clone https://github.com/ThisUsernamesTaken/yuh.git btc-bias-engine
```

## Step 3 — Install Python

The author's setup uses Python at:
`C:\Users\<USER>\AppData\Local\Python\bin\python.exe`

If your Python is somewhere else, edit `scripts/install_services.ps1`'s
`$PY` variable, all `*.cmd` watchdogs' `PY=` line, and the
`AGENT_CHEATSHEET.md` constants.

Quick install via winget:
```powershell
winget install Python.Python.3.11
```

## Step 4 — Install NSSM

```powershell
winget install NSSM.NSSM
```

After install, find the binary:
```powershell
where.exe nssm.exe
# or:
Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\NSSM.NSSM_*" -Recurse -Filter nssm.exe
```

The author's path is:
```
C:\Users\coleb\AppData\Local\Microsoft\WinGet\Packages\NSSM.NSSM_Microsoft.Winget.Source_8wekyb3d8bbwe\nssm-2.24-101-g897c7ad\win64\nssm.exe
```

Edit `scripts/install_services.ps1`'s `$NSSM` variable to point at the
binary location on your machine.

## Step 5 — Install GitHub CLI (optional)

```powershell
winget install GitHub.cli
& "C:\Program Files\GitHub CLI\gh.exe" auth login
```

Required only if you plan to push commits or create new repos from this
machine.

## Step 6 — Place Kalshi credentials

Create `C:\Trading\btc-bias-engine\credentials\kalshi.env` with:

```
KALSHI_API_KEY=<your kalshi API key UUID>
KALSHI_PRIVATE_KEY_PATH=C:\Trading\btc-bias-engine\credentials\kalshi_private.pem
```

Place your RSA PEM private key at the path above. The PEM must be in
PKCS#8 format readable by `cryptography.hazmat.primitives.serialization.load_pem_private_key`.

**`KalshiLiveTA` will refuse to start if the credentials file or PEM is missing.**

## Step 7 — Install Python dependencies

```powershell
cd C:\Trading\kalshi-btc-engine-v2
pip install -e .[dev]
```

This installs the engine package in editable mode plus dev dependencies
(pytest, ruff, black).

## Step 8 — Verify tests pass

```powershell
$env:PYTHONPATH = "C:\Trading\kalshi-btc-engine-v2\src"
python -m pytest tests -q
# Expected: 179 passed (as of 2026-05-14 commit 488c932)
```

If tests fail, do NOT proceed to live deployment. Investigate first.

## Step 9 — Create data directory

```powershell
mkdir C:\Trading\kalshi-btc-engine-v2\data
```

The `data/` directory is gitignored. All captured market data, decision
logs, and service stdout/stderr go here. Plan for ~50 GB of growth per
month at current capture rates.

## Step 10 — First run as paper-only

Before installing services, run the paper components manually for a few
cycles to confirm WS connectivity and credential resolution:

```powershell
$env:PYTHONPATH = "C:\Trading\kalshi-btc-engine-v2\src"
$env:PYTHONIOENCODING = "utf-8"
$PY = "C:\Users\<USER>\AppData\Local\Python\bin\python.exe"
$DB = "C:\Trading\kalshi-btc-engine-v2\data\burnin_holdpure_2026_05_12.sqlite"

# Capture (foreground, Ctrl+C to stop after a minute):
& $PY -m kalshi_btc_engine_v2.cli capture-burnin --db $DB --hours 1
```

If you see L2 events accumulating and no Python tracebacks, the WS and
credentials are good. Stop and proceed.

## Step 11 — Install all five NSSM services

From an **elevated** PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File "C:\Trading\kalshi-btc-engine-v2\scripts\install_services.ps1"
```

This installs:
- `KalshiCapture` — WS subscribe + DB writer
- `KalshiPaperEngine` — engine v2 paper trader (hold_to_settle_pure preset)
- `KalshiPaperTA` — Pine Script paper trader
- `KalshiLiveTA` — Pine Script **LIVE trader (REAL MONEY)**
- `KalshiLadderShadow` — confirmation-driven DCA observer (no orders)

Each service is configured to auto-start on boot with a 5-second restart
delay on crash. Each writes to its own stdout/stderr in `data/svc_*.log`
with 50 MB log rotation.

Start them:
```powershell
$NSSM = "<your nssm path>"
foreach ($s in "KalshiCapture","KalshiPaperEngine","KalshiPaperTA","KalshiLiveTA","KalshiLadderShadow") {
    & $NSSM start $s
    Start-Sleep 3
}
```

Verify:
```powershell
foreach ($s in "KalshiCapture","KalshiPaperEngine","KalshiPaperTA","KalshiLiveTA","KalshiLadderShadow") {
    "$s : $(& $NSSM status $s)"
}
# All five should report SERVICE_RUNNING
```

## Step 12 — Tail logs to confirm health

```powershell
Get-Content C:\Trading\kalshi-btc-engine-v2\data\svc_capture.stderr.log -Tail 5 -Wait
# Expect periodic "heartbeat elapsed=..." lines

Get-Content C:\Trading\kalshi-btc-engine-v2\data\svc_live_ta.stdout.log -Tail 5 -Wait
# Expect "[live-ta] starting tail=..." then periodic "[live-ta] decisions=..." lines
```

## Step 13 — Run the audit scripts

```powershell
$PY = "<your python>"
$env:PYTHONIOENCODING = "utf-8"

# Pine Script paper P&L:
& $PY C:\Trading\kalshi-btc-engine-v2\scripts\_audit_ta_pnl.py

# Engine v2 paper P&L:
& $PY C:\Trading\kalshi-btc-engine-v2\scripts\_audit_engine_pnl.py

# Live trader review (per-trade table):
& $PY C:\Trading\kalshi-btc-engine-v2\scripts\_review_live.py

# Loss-streak + time-of-day analysis:
& $PY C:\Trading\kalshi-btc-engine-v2\scripts\_streak_analysis.py
```

All are read-only.

## Step 14 — Configure tier sizing (LIVE only)

The live trader (`scripts/live_ta.py`) sizes contracts by Pine Script
tier. Defaults as of 2026-05-14:

```python
TIER_CONTRACTS = {
    "STRONG": 40,  # 4x
    "MEDIUM": 20,  # 2x
    "WEAK":   10,  # 1x
    "MIMIC":   5,  # 0.5x
}
```

These are tuned to a ~$70-80 Kalshi balance. **If your balance is materially
different, scale TIER_CONTRACTS proportionally** before starting `KalshiLiveTA`.

For paper-only operation, this doesn't matter — only the four LIVE service
constants do (TIER_CONTRACTS, MIN_BALANCE_CENTS, STALE_DATA_TIMEOUT_MS,
LIMIT_CAP_CENTS).

## Step 15 — Verify live trader respects all guards before allowing any fill

The live trader has the following hard caps; the install is not safe to
trade real money on unless you confirm all of these are correct:

- `TIER_CONTRACTS` (see Step 14) — per-trade size by Pine Script tier
- `DAILY_LOSS_CAP_CENTS = 999999` — effectively disabled per user authorization
- `MIN_BALANCE_CENTS = 500` — halts if balance < $5
- `STALE_DATA_TIMEOUT_MS = 30000` — skips entries if last spot tick > 30s old
- `SLIPPAGE_CENTS = 3` — IOC limit at ask + 3¢
- `LIMIT_CAP_CENTS = 99` — limit capped at 99¢
- Per-cycle dedupe via `replay_log_state` on startup — restarts cannot re-enter cycles they already attempted

Read `scripts/live_ta.py` lines 1-30 for the design rationale.

## Step 16 — Stop services

```powershell
# Pause LIVE only:
& $NSSM stop KalshiLiveTA

# Stop everything in dependency order:
foreach ($s in "KalshiLadderShadow","KalshiLiveTA","KalshiPaperTA","KalshiPaperEngine","KalshiCapture") {
    & $NSSM stop $s
}

# Disable auto-start on boot (e.g., before maintenance):
& $NSSM set KalshiLiveTA Start SERVICE_DEMAND_START
# Re-enable:
& $NSSM set KalshiLiveTA Start SERVICE_AUTO_START
```

## Step 17 — Uninstall everything

```powershell
foreach ($s in "KalshiLadderShadow","KalshiLiveTA","KalshiPaperTA","KalshiPaperEngine","KalshiCapture") {
    & $NSSM stop $s
    & $NSSM remove $s confirm
}
```

The `data/` directory persists. Delete manually if desired.

---

## Common gotchas

### "Can't open service!" from NSSM
Service doesn't exist (yet) — benign. The install script handles this.

### Python process crashes immediately in REPL loop
NSSM `AppParameters` is empty. Re-run `install_services.ps1` and verify
with `& $NSSM get <service> AppParameters` — should show the script
path and CLI flags.

### "Access is denied" when stopping services
Run from an elevated PowerShell. NSSM services run as LocalSystem; only
admins can control them.

### `live_ta.py` exits with `FileNotFoundError: Kalshi creds not found`
The credentials file path is hardcoded. Either place creds at the expected
path (Step 6) or edit `KALSHI_CREDS_PATH` in `live_ta.py`.

### `live_ta.py` exits with `ModuleNotFoundError: No module named 'kalshi_client'`
The `_V1_ROOT` import path can't find `kalshi_client.py`. Either clone
`btc-bias-engine` to `C:\Trading\btc-bias-engine\` (Step 2) or edit
`_V1_ROOT` in `live_ta.py`.

### Coinbase WS feed stops writing rows mid-day
Known issue — flagged in `RUNNING.md`. The current Pine Script paper
trader and live trader both default to `--venue bitstamp` for this
reason. If bitstamp also stalls, the trader's `--stale-venue-timeout-s`
fires a self-exit after 600s and NSSM restarts it.

### Capture process is not under a watchdog
The capture-burnin process IS managed by NSSM but its WS reconnect logic
is not bulletproof. If WS connection silently breaks, NSSM doesn't see a
crash and won't restart. Monitor the heartbeat mps in
`data/svc_capture.stderr.log`; if a venue's mps drops to 0 for >5 min,
restart the service manually: `& $NSSM restart KalshiCapture`.

---

## For Claude or other AI agents picking up after install

After Step 17 verifies all services are healthy:

1. Read `CLAUDE.md` for project orientation
2. Read `RUNNING.md` for current operational state
3. Read `docs/FINDINGS_2026_05_14.md` for trading-data analysis
4. Read `docs/AGENT_CHEATSHEET.md` for diagnostic commands
5. Read `HANDOFF.md` for chronological history

The data audit scripts (`_audit_*.py`, `_review_live.py`, `_streak_analysis.py`)
will all work after install since they only depend on Python + the engine's
own decision logs. Use them to verify the strategy is producing similar
results to the originating machine.

**Do not enable live trading on this install without explicit user
authorization.** The default `live_enabled: false` in
`configs/default.json` does NOT gate `live_ta.py` (which has its own
hard caps). The user must explicitly approve before starting
`KalshiLiveTA`. If installing for paper-only operation, simply do NOT
start that one service; the other four are safe.
