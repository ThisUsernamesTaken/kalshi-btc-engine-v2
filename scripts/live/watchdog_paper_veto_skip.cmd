@echo off
REM Paper-mode veto-skip variant. Runs concurrently with
REM watchdog_paper_baseline.cmd and watchdog_paper_veto_flip.cmd.
REM
REM --veto-mode skip means: when model disagrees with engine direction
REM by >= --veto-threshold cents, SKIP the trade entirely. Logs to a
REM separate decision-log so we can compare against baseline + flip.

setlocal
set ENGINE_DIR=C:\Trading\kalshi-btc-engine-v2
set PY=C:\Users\coleb\AppData\Local\Python\bin\python.exe
set PYTHONPATH=%ENGINE_DIR%\src
set PYTHONIOENCODING=utf-8
set DECISION_LOG=%ENGINE_DIR%\data_local\paper_veto_skip_trades.jsonl
set LOG_FILE=%ENGINE_DIR%\data_local\paper_veto_skip.combined.log
set WATCHDOG_LOG=%ENGINE_DIR%\data_local\watchdog_paper_veto_skip.log
set SMART_V5_FLAGS=--disable-earlier-moderate --enable-t30-sniper --disable-late --enable-em-upsize

:loop
echo [%date% %time%] starting paper-veto-skip >> "%WATCHDOG_LOG%"
"%PY%" "%ENGINE_DIR%\scripts\live\live_v5_unified.py" ^
    --decision-log "%DECISION_LOG%" ^
    --poll-interval-s 1.5 ^
    --status-every-s 60 ^
    --no-resting ^
    --dry-run ^
    %SMART_V5_FLAGS% ^
    --veto-mode skip --veto-threshold 5 ^
    >> "%LOG_FILE%" 2>&1
echo [%date% %time%] paper-veto-skip exited with %ERRORLEVEL%; restarting in 5s >> "%WATCHDOG_LOG%"
timeout /t 5 /nobreak > nul
goto loop
