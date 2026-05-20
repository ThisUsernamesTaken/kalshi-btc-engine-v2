@echo off
REM LIVE trader: veto+flip with EARLIER_MODERATE re-enabled.
REM
REM This is the highest-OOS-alpha configuration. EM is re-enabled
REM (was disabled per the 2026-05-18 emergency fix after a $22 bleed),
REM with the model-veto layer in flip mode as the proposed safeguard.
REM
REM SAFETY:
REM   - Default MIN_BALANCE_CENTS = $1; trader balance-halts below this
REM   - Daily loss cap $27 (DAILY_LOSS_CAP_CENTS in code)
REM   - --no-resting: no resting orders; IOC only
REM   - Veto in flip mode: skip on disagreement >=5c, flip on >=30c
REM
REM OOS evidence (analysis/17, 18):
REM   On 131 OOS trades with this config: -$64 -> +$108 swing,
REM   95%% bootstrap CI [+$68, +$296], 99.9%% positive resamples.
REM
REM Co-runs alongside 4 paper variants (paper_baseline / paper_veto_skip /
REM paper_veto_flip / paper_veto_flip_EM) on the same account. Paper
REM instances run --dry-run so they don't place real orders.

setlocal
set ENGINE_DIR=C:\Trading\kalshi-btc-engine-v2
set PY=C:\Users\coleb\AppData\Local\Python\bin\python.exe
set PYTHONPATH=%ENGINE_DIR%\src
set PYTHONIOENCODING=utf-8
set DECISION_LOG=%ENGINE_DIR%\data\live_veto_flip_EM_trades.jsonl
set LOG_FILE=%ENGINE_DIR%\data\live_veto_flip_EM.combined.log
set WATCHDOG_LOG=%ENGINE_DIR%\data\watchdog_live_veto_flip_EM.log

REM EM ENABLED + T-30 sniper + EM upsize. No --disable-earlier-moderate.
REM No --dry-run (this is LIVE).
set ENGINE_FLAGS=--enable-t30-sniper --disable-late --enable-em-upsize

set VETO_FLAGS=--veto-mode flip --veto-threshold 5 --veto-flip-threshold 30 --veto-flip-slip 2

:loop
echo [%date% %time%] starting live_veto_flip_EM (LIVE) >> "%WATCHDOG_LOG%"
"%PY%" "%ENGINE_DIR%\scripts\live\live_v5_unified.py" ^
    --decision-log "%DECISION_LOG%" ^
    --poll-interval-s 1.5 ^
    --status-every-s 30 ^
    --no-resting ^
    %ENGINE_FLAGS% ^
    %VETO_FLAGS% ^
    >> "%LOG_FILE%" 2>&1
echo [%date% %time%] live_veto_flip_EM exited with %ERRORLEVEL%; restarting in 5s >> "%WATCHDOG_LOG%"
timeout /t 5 /nobreak > nul
goto loop
