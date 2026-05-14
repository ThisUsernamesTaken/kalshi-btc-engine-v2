@echo off
REM Indefinite auto-restart watchdog for the Pine Script paper trader.
REM Runs live_paper_ta.py in a loop, restarting on any exit.

setlocal
set ENGINE_DIR=C:\Trading\kalshi-btc-engine-v2
set PY=C:\Users\coleb\AppData\Local\Python\bin\python.exe
set PYTHONPATH=%ENGINE_DIR%\src
set PYTHONIOENCODING=utf-8
set DB=%ENGINE_DIR%\data\burnin_holdpure_2026_05_12.sqlite
set DECISION_LOG=%ENGINE_DIR%\data\paper_ta_2026_05_12.jsonl
set LOG_FILE=%ENGINE_DIR%\data\paper_ta.combined.log
set VENUE=%1
if "%VENUE%"=="" set VENUE=bitstamp

:loop
echo [%DATE% %TIME%] watchdog: starting attempt (venue=%VENUE%) >> "%LOG_FILE%"
"%PY%" "%ENGINE_DIR%\scripts\live_paper_ta.py" --db "%DB%" --decision-log "%DECISION_LOG%" --venue %VENUE% --base-stake 1 --start-at-tail --stale-venue-timeout-s 600 >> "%LOG_FILE%" 2>&1
echo [%DATE% %TIME%] watchdog: inner exited code=%ERRORLEVEL%; sleeping 5s then restart >> "%LOG_FILE%"
timeout /t 5 /nobreak > nul
goto loop
