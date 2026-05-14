# Install all five kalshi-btc-engine-v2 components as Windows services
# via NSSM. Run from an elevated PowerShell.
#
# Services installed:
#   KalshiCapture       - capture-burnin (WS subscriber + DB writer)
#   KalshiPaperEngine   - live_paper.py with hold_to_settle_pure preset
#   KalshiPaperTA       - live_paper_ta.py with bitstamp venue
#   KalshiLiveTA        - live_ta.py REAL MONEY (10 contracts/trade)
#   KalshiLadderShadow  - live_ladder_shadow.py SHADOW DCA observer
#                         (reads live trade log, observes contract prices,
#                          logs what a confirmation-driven add ladder would
#                          have done — never places real orders)
#
# Each service auto-restarts on exit with a 5-second delay. Paper/live
# traders depend on KalshiCapture so they start in the right order.
# Ladder shadow depends on KalshiLiveTA so it has fills to observe.
#
# To uninstall: `nssm remove <service> confirm` for each.

param(
    [switch]$DryRun
)

$ErrorActionPreference = "Continue"   # NSSM emits stderr on "service not found" which is benign here

$NSSM   = "C:\Users\coleb\AppData\Local\Microsoft\WinGet\Packages\NSSM.NSSM_Microsoft.Winget.Source_8wekyb3d8bbwe\nssm-2.24-101-g897c7ad\win64\nssm.exe"
$PY     = "C:\Users\coleb\AppData\Local\Python\bin\python.exe"
$ENGINE = "C:\Trading\kalshi-btc-engine-v2"
$DATA   = "$ENGINE\data"
$DB     = "$DATA\burnin_holdpure_2026_05_12.sqlite"
$ENV_EXTRA = "PYTHONPATH=$ENGINE\src`0PYTHONIOENCODING=utf-8"

function Install-NssmService {
    param(
        [string]$Name,
        [string]$Exe,
        [string]$AppArgs,
        [string]$Stdout,
        [string]$Stderr,
        [string]$DependsOn = "",
        [string]$Description
    )
    Write-Host "==> Installing $Name"
    if ($DryRun) {
        Write-Host "    (dry run) nssm install $Name $Exe $AppArgs"
        return
    }
    # Remove any existing service of the same name first (idempotent).
    # NSSM emits "Can't open service!" on missing services; benign.
    cmd /c "`"$NSSM`" stop $Name 2>nul 1>nul"
    cmd /c "`"$NSSM`" remove $Name confirm 2>nul 1>nul"

    & $NSSM install $Name $Exe
    & $NSSM set $Name AppParameters $AppArgs
    & $NSSM set $Name AppDirectory $ENGINE
    & $NSSM set $Name AppEnvironmentExtra $ENV_EXTRA
    & $NSSM set $Name AppStdout $Stdout
    & $NSSM set $Name AppStderr $Stderr
    & $NSSM set $Name AppStdoutCreationDisposition 4    # OPEN_ALWAYS (append)
    & $NSSM set $Name AppStderrCreationDisposition 4
    & $NSSM set $Name AppRotateFiles 1
    & $NSSM set $Name AppRotateBytes 52428800           # 50 MB
    & $NSSM set $Name Start SERVICE_AUTO_START
    & $NSSM set $Name AppExit Default Restart
    & $NSSM set $Name AppRestartDelay 5000              # 5 s
    & $NSSM set $Name AppStopMethodConsole 10000        # 10s for graceful Ctrl+C
    & $NSSM set $Name Description $Description
    if ($DependsOn) {
        & $NSSM set $Name DependOnService $DependsOn
    }
    Write-Host "    installed $Name"
}

# ---- KalshiCapture: WS subscriber + DB writer ----
Install-NssmService `
    -Name "KalshiCapture" `
    -Exe $PY `
    -AppArgs "-m kalshi_btc_engine_v2.cli capture-burnin --db `"$DB`" --hours 168" `
    -Stdout "$DATA\svc_capture.stdout.log" `
    -Stderr "$DATA\svc_capture.stderr.log" `
    -Description "Kalshi BTC engine v2 capture-burnin (paper-only WS capture)"

# ---- KalshiPaperEngine: engine v2 hold-to-settle paper trader ----
Install-NssmService `
    -Name "KalshiPaperEngine" `
    -Exe $PY `
    -AppArgs "$ENGINE\scripts\live_paper.py --db `"$DB`" --decision-log `"$DATA\paper_holdpure_2026_05_12.jsonl`" --preset hold_to_settle_pure --bankroll 20 --start-at-tail" `
    -Stdout "$DATA\svc_paper_engine.stdout.log" `
    -Stderr "$DATA\svc_paper_engine.stderr.log" `
    -DependsOn "KalshiCapture" `
    -Description "Kalshi BTC engine v2 paper trader (hold_to_settle_pure)"

# ---- KalshiPaperTA: Pine Script paper trader ----
Install-NssmService `
    -Name "KalshiPaperTA" `
    -Exe $PY `
    -AppArgs "$ENGINE\scripts\live_paper_ta.py --db `"$DB`" --decision-log `"$DATA\paper_ta_2026_05_12.jsonl`" --venue bitstamp --base-stake 1 --start-at-tail --stale-venue-timeout-s 600" `
    -Stdout "$DATA\svc_paper_ta.stdout.log" `
    -Stderr "$DATA\svc_paper_ta.stderr.log" `
    -DependsOn "KalshiCapture" `
    -Description "Kalshi BTC engine v2 Pine Script PAPER trader"

# ---- KalshiLiveTA: Pine Script LIVE trader (REAL MONEY) ----
# NOTE: 10 contracts/trade, daily loss cap effectively disabled, $5 min balance.
# Caps are hard-coded in scripts/live_ta.py and confirmed intentional by user.
Install-NssmService `
    -Name "KalshiLiveTA" `
    -Exe $PY `
    -AppArgs "$ENGINE\scripts\live_ta.py --db `"$DB`" --decision-log `"$DATA\live_ta_trades.jsonl`" --venue bitstamp --start-at-tail --stale-venue-timeout-s 600" `
    -Stdout "$DATA\svc_live_ta.stdout.log" `
    -Stderr "$DATA\svc_live_ta.stderr.log" `
    -DependsOn "KalshiCapture" `
    -Description "Kalshi BTC engine v2 Pine Script LIVE trader (REAL MONEY)"

# ---- KalshiLadderShadow: confirmation-driven DCA observer (shadow only) ----
# Tails KalshiLiveTA's trade log, observes contract prices in the capture
# DB, simulates a 4-condition rung ladder, and logs would-add events plus
# counterfactual settlement P&L. Has NO Kalshi client — cannot place orders.
Install-NssmService `
    -Name "KalshiLadderShadow" `
    -Exe $PY `
    -AppArgs "$ENGINE\scripts\live_ladder_shadow.py --db `"$DB`" --live-trade-log `"$DATA\live_ta_trades.jsonl`" --shadow-log `"$DATA\ladder_shadow.jsonl`" --poll-interval-s 2 --status-every-s 60" `
    -Stdout "$DATA\svc_ladder_shadow.stdout.log" `
    -Stderr "$DATA\svc_ladder_shadow.stderr.log" `
    -DependsOn "KalshiLiveTA" `
    -Description "Kalshi BTC engine v2 ladder-DCA shadow observer (no orders)"

Write-Host ""
Write-Host "All five services installed."
Write-Host ""
Write-Host "Status check:"
& $NSSM status KalshiCapture
& $NSSM status KalshiPaperEngine
& $NSSM status KalshiPaperTA
& $NSSM status KalshiLiveTA
& $NSSM status KalshiLadderShadow
