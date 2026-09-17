@echo off
REM Auto-restart wrapper for live_favorite_chase.py (REAL MONEY).
REM On any non-zero exit (stale feed, WS error, crash) it sleeps 5s and relaunches.
REM
REM Authorized 2026-05-21 by user with explicit "no caps" override of
REM safety-rails recommendation. See FAVORITE_CHASE_STATUS.md.
REM
REM TO STOP: kill this cmd's PID with Stop-Process -Force, then kill the
REM python child it spawned.

setlocal
set PY=C:\Users\coleb\AppData\Local\Python\bin\python.exe
set PYTHONPATH=src
set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8

cd /d C:\Trading\kalshi-btc-engine-v2

:loop
echo [%DATE% %TIME%] launching LIVE favorite-chase (real orders)
"%PY%" scripts/live/live_favorite_chase.py ^
    --decision-log C:\Trading\kalshi-btc-engine-v2\data\live_favorite_chase.jsonl ^
    --max-entry-price 0.90 ^
    --max-slip 0.10 ^
    --min-strike-bps 4.0
set RC=%ERRORLEVEL%
echo [%DATE% %TIME%] live_favorite_chase exited rc=%RC%
if "%RC%"=="0" goto done
timeout /t 5 /nobreak >nul
goto loop

:done
echo [%DATE% %TIME%] clean exit, watchdog stopping
endlocal
