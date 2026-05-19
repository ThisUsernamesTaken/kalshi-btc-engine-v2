@echo off
REM Watchdog for live_v5_unified.py running with VETO+FLIP mode in SHADOW.
REM
REM Higher-alpha variant of the veto layer: when the model disagrees with
REM the engine by less than veto-flip-threshold (default 30c) but more than
REM veto-threshold (default 5c), SKIP. When disagreement is >= flip-threshold,
REM place an OPPOSITE-SIDE order at the opposite ask + slip.
REM
REM Shadow mode = compute and log model_veto events (with action=KEEP/SKIP/FLIP)
REM but NEVER actually skip or flip. Required validation step before flipping
REM to --veto-mode flip live.
REM
REM To flip to LIVE veto+flip (actually skip/flip): change --veto-mode shadow
REM to --veto-mode flip below.
REM
REM CAUTION: --veto-mode flip places NEW opposite-side IOC orders. This is
REM a different trade path from the engine's normal flow. Always validate in
REM SHADOW for at least 24h first.
REM
REM OOS validation (analysis/17, 18):
REM   Veto+flip applied to 131 OOS live trades: realized -$64 -> +$108
REM   (swing +$173). 95%% bootstrap CI: [+$68, +$296], 99.9%% positive.
REM
REM See RUNNING_VETO.md and HANDOFF_VETO_2026_05_19.md for the rollout plan.

setlocal
set ENGINE_DIR=C:\Trading\kalshi-btc-engine-v2
set PY=C:\Users\coleb\AppData\Local\Python\bin\python.exe
set PYTHONPATH=%ENGINE_DIR%\src
set PYTHONIOENCODING=utf-8
set DECISION_LOG=%ENGINE_DIR%\data\live_v5_unified_trades.jsonl
set LOG_FILE=%ENGINE_DIR%\data\live_v5_unified.combined.log
set WATCHDOG_LOG=%ENGINE_DIR%\data\watchdog_v5_unified_veto_flip.log

REM Strategy flags (mirror watchdog_v5_unified.cmd current production config)
set SMART_V5_FLAGS=--disable-earlier-moderate --enable-t30-sniper --disable-late --enable-em-upsize

REM Veto+flip flags (start in SHADOW mode)
set VETO_FLAGS=--veto-mode shadow --veto-threshold 5 --veto-flip-threshold 30 --veto-flip-slip 2

:loop
echo [%date% %time%] starting live_v5_unified (veto+flip shadow) >> "%WATCHDOG_LOG%"
"%PY%" "%ENGINE_DIR%\scripts\live\live_v5_unified.py" ^
    --decision-log "%DECISION_LOG%" ^
    --poll-interval-s 1.5 ^
    --status-every-s 30 ^
    --no-resting ^
    %SMART_V5_FLAGS% ^
    %VETO_FLAGS% ^
    >> "%LOG_FILE%" 2>&1
echo [%date% %time%] live_v5_unified exited with %ERRORLEVEL%; restarting in 5s >> "%WATCHDOG_LOG%"
timeout /t 5 /nobreak > nul
goto loop
