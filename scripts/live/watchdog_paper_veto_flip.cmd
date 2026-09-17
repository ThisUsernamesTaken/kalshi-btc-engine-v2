@echo off
REM Paper-mode veto+flip variant. Runs concurrently with
REM watchdog_paper_baseline.cmd and watchdog_paper_veto_skip.cmd.
REM
REM --veto-mode flip means: skip in mid-disagreement range (5-30c),
REM and FLIP to opposite-side IOC in extreme disagreement (>=30c).
REM In --dry-run, the flip is recorded but no real Kalshi order placed.

setlocal
set ENGINE_DIR=C:\Trading\kalshi-btc-engine-v2
set PY=C:\Users\coleb\AppData\Local\Python\bin\python.exe
set PYTHONPATH=%ENGINE_DIR%\src
set PYTHONIOENCODING=utf-8
set DECISION_LOG=%ENGINE_DIR%\data_local\paper_veto_flip_trades.jsonl
set LOG_FILE=%ENGINE_DIR%\data_local\paper_veto_flip.combined.log
set WATCHDOG_LOG=%ENGINE_DIR%\data_local\watchdog_paper_veto_flip.log
set SMART_V5_FLAGS=--disable-earlier-moderate --enable-t30-sniper --disable-late --enable-em-upsize

:loop
echo [%date% %time%] starting paper-veto-flip >> "%WATCHDOG_LOG%"
"%PY%" "%ENGINE_DIR%\scripts\live\live_v5_unified.py" ^
    --decision-log "%DECISION_LOG%" ^
    --poll-interval-s 1.5 ^
    --status-every-s 60 ^
    --no-resting ^
    --dry-run ^
    %SMART_V5_FLAGS% ^
    --veto-mode flip --veto-threshold 5 --veto-flip-threshold 30 ^
    >> "%LOG_FILE%" 2>&1
echo [%date% %time%] paper-veto-flip exited with %ERRORLEVEL%; restarting in 5s >> "%WATCHDOG_LOG%"
timeout /t 5 /nobreak > nul
goto loop
