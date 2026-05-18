@echo off
REM Indefinite auto-restart watchdog for the LIVE V5 LATE-CERTAINTY trader.
REM Strategy: discover KXBTC15M markets via REST, wait until minute 12 of
REM each 15-min window, fire IOC buy at the first side to hit >= 80c, hold
REM to settlement. Source: scripts/live_v5_certainty.py.

setlocal
set ENGINE_DIR=C:\Trading\kalshi-btc-engine-v2
set PY=C:\Users\coleb\AppData\Local\Python\bin\python.exe
set PYTHONPATH=%ENGINE_DIR%\src
set PYTHONIOENCODING=utf-8
set DECISION_LOG=%ENGINE_DIR%\data\live_v5_trades.jsonl
set LOG_FILE=%ENGINE_DIR%\data\live_v5.combined.log
set WATCHDOG_LOG=%ENGINE_DIR%\data\watchdog_v5.log

:loop
echo [%DATE% %TIME%] watchdog: starting attempt >> "%WATCHDOG_LOG%"
"%PY%" "%ENGINE_DIR%\scripts\live_v5_certainty.py" --decision-log "%DECISION_LOG%" >> "%LOG_FILE%" 2>&1
echo [%DATE% %TIME%] watchdog: inner exited code=%ERRORLEVEL%; sleeping 5s then restart >> "%WATCHDOG_LOG%"
timeout /t 5 /nobreak > nul
goto loop
