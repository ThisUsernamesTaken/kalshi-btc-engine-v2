@echo off
REM Watchdog for live_v5_unified.py running WITH the fair-value-model veto
REM layer in SHADOW MODE.
REM
REM Shadow mode = compute and log model_veto events but NEVER skip trades.
REM This is the safe rollout step: validates that the veto fires sensibly
REM in production conditions before flipping to --veto-mode=skip.
REM
REM To flip to live veto (actually skip flagged trades), change the
REM SMART_V5_FLAGS line below from --veto-mode shadow to --veto-mode skip.
REM
REM OOS validation (analysis/13, 14):
REM   Veto applied to 131 live trades from v5_unified + v5_old + live_ta
REM   on 2026-05-13 to 17: realized -$64 -> +$27 (swing +$92).
REM   95%% bootstrap CI: [+$26, +$172], 99.9%% of resamples positive.
REM
REM See docs/MODEL_VETO_INTEGRATION.md for the rollout plan.

setlocal
set ENGINE_DIR=C:\Trading\kalshi-btc-engine-v2
set PY=C:\Users\coleb\AppData\Local\Python\bin\python.exe
set PYTHONPATH=%ENGINE_DIR%\src
set PYTHONIOENCODING=utf-8
set DECISION_LOG=%ENGINE_DIR%\data\live_v5_unified_trades.jsonl
set LOG_FILE=%ENGINE_DIR%\data\live_v5_unified.combined.log
set WATCHDOG_LOG=%ENGINE_DIR%\data\watchdog_v5_unified_veto.log

REM Strategy flags (mirror watchdog_v5_unified.cmd current production config):
REM   --disable-earlier-moderate : per 2026-05-18 emergency fix
REM   --enable-t30-sniper        : primary edge
REM   --disable-late             : structurally -EV
REM   --enable-em-upsize         : moot (EM disabled) but kept for clarity
REM
REM Veto flags:
REM   --veto-mode shadow         : log decisions but do not skip
REM   --veto-threshold 5         : default 5c disagreement
set SMART_V5_FLAGS=--disable-earlier-moderate --enable-t30-sniper --disable-late --enable-em-upsize
set VETO_FLAGS=--veto-mode shadow --veto-threshold 5

:loop
echo [%date% %time%] starting live_v5_unified (veto-shadow) >> "%WATCHDOG_LOG%"
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
