@echo off
REM Paper-mode baseline (no veto). Used as the control arm alongside
REM watchdog_paper_veto_skip.cmd and watchdog_paper_veto_flip.cmd to
REM compare the three variants on the SAME live market stream.
REM
REM --dry-run means: trigger logic runs normally, BTC poll runs,
REM WS subscribes, but NO Kalshi orders are placed. Settle prices
REM are derived from end-of-cycle book state.

setlocal
set ENGINE_DIR=C:\Trading\kalshi-btc-engine-v2
set PY=C:\Users\coleb\AppData\Local\Python\bin\python.exe
set PYTHONPATH=%ENGINE_DIR%\src
set PYTHONIOENCODING=utf-8
set DECISION_LOG=%ENGINE_DIR%\data\paper_baseline_trades.jsonl
set LOG_FILE=%ENGINE_DIR%\data\paper_baseline.combined.log
set WATCHDOG_LOG=%ENGINE_DIR%\data\watchdog_paper_baseline.log
set SMART_V5_FLAGS=--disable-earlier-moderate --enable-t30-sniper --disable-late --enable-em-upsize

:loop
echo [%date% %time%] starting paper-baseline >> "%WATCHDOG_LOG%"
"%PY%" "%ENGINE_DIR%\scripts\live\live_v5_unified.py" ^
    --decision-log "%DECISION_LOG%" ^
    --poll-interval-s 1.5 ^
    --status-every-s 60 ^
    --no-resting ^
    --dry-run ^
    %SMART_V5_FLAGS% ^
    --veto-mode off ^
    >> "%LOG_FILE%" 2>&1
echo [%date% %time%] paper-baseline exited with %ERRORLEVEL%; restarting in 5s >> "%WATCHDOG_LOG%"
timeout /t 5 /nobreak > nul
goto loop
