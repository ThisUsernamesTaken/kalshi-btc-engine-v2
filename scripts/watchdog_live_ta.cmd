@echo off
REM Indefinite auto-restart watchdog for the LIVE TA directional trader.
REM Runs live_ta.py in a loop, restarting on any exit.

setlocal
set ENGINE_DIR=C:\Trading\kalshi-btc-engine-v2
set PY=C:\Users\coleb\AppData\Local\Python\bin\python.exe
set PYTHONPATH=%ENGINE_DIR%\src
set PYTHONIOENCODING=utf-8
set DB=%ENGINE_DIR%\data\burnin_holdpure_2026_05_12.sqlite
set DECISION_LOG=%ENGINE_DIR%\data\live_ta_trades.jsonl
set LOG_FILE=%ENGINE_DIR%\data\live_ta.combined.log
set WATCHDOG_LOG=%ENGINE_DIR%\data\watchdog_live_ta.log
set VENUE=%1
if "%VENUE%"=="" set VENUE=bitstamp

:loop
echo [%DATE% %TIME%] watchdog: starting attempt (venue=%VENUE%) >> "%WATCHDOG_LOG%"
"%PY%" "%ENGINE_DIR%\scripts\live_ta.py" --db "%DB%" --decision-log "%DECISION_LOG%" --venue %VENUE% --start-at-tail >> "%LOG_FILE%" 2>&1
echo [%DATE% %TIME%] watchdog: inner exited code=%ERRORLEVEL%; sleeping 5s then restart >> "%WATCHDOG_LOG%"
timeout /t 5 /nobreak > nul
goto loop
