@echo off
REM Indefinite auto-restart watchdog for the LIVE HYBRID trader
REM (velocity entry + HWM trail + signal-flip + TA confirmation gate).
REM Runs live_hybrid.py in a loop, restarting on any exit. Writes to its
REM own decision log so per-cycle dedupe state is independent of the
REM other live/paper variants.

setlocal
set ENGINE_DIR=C:\Trading\kalshi-btc-engine-v2
set PY=C:\Users\coleb\AppData\Local\Python\bin\python.exe
set PYTHONPATH=%ENGINE_DIR%\src
set PYTHONIOENCODING=utf-8
set DB=%ENGINE_DIR%\data\burnin_holdpure_2026_05_12.sqlite
set DECISION_LOG=%ENGINE_DIR%\data\live_hybrid_trades.jsonl
set LOG_FILE=%ENGINE_DIR%\data\live_hybrid.combined.log
set WATCHDOG_LOG=%ENGINE_DIR%\data\watchdog_live_hybrid.log
set VENUE=%1
if "%VENUE%"=="" set VENUE=bitstamp

:loop
echo [%DATE% %TIME%] watchdog: starting attempt (venue=%VENUE%) >> "%WATCHDOG_LOG%"
"%PY%" "%ENGINE_DIR%\scripts\live_hybrid.py" --db "%DB%" --decision-log "%DECISION_LOG%" --venue %VENUE% --start-at-tail --stale-venue-timeout-s 600 >> "%LOG_FILE%" 2>&1
echo [%DATE% %TIME%] watchdog: inner exited code=%ERRORLEVEL%; sleeping 5s then restart >> "%WATCHDOG_LOG%"
timeout /t 5 /nobreak > nul
goto loop
