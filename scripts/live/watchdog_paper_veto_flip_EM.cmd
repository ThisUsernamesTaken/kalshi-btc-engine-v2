@echo off
REM Paper-mode "highest alpha" variant: EM re-enabled + veto+flip.
REM
REM This is what the OOS analysis projected at +$173 swing OOS. Current
REM production has EM disabled per the 2026-05-18 emergency fix, but
REM the veto specifically catches the exhaustion entries that bled in EM
REM (87% loser-flag-rate on EM in OOS).
REM
REM Running in --dry-run alongside the other three paper variants to
REM see whether the veto would actually have prevented the bleed under
REM live conditions.

setlocal
set ENGINE_DIR=C:\Trading\kalshi-btc-engine-v2
set PY=C:\Users\coleb\AppData\Local\Python\bin\python.exe
set PYTHONPATH=%ENGINE_DIR%\src
set PYTHONIOENCODING=utf-8
set DECISION_LOG=%ENGINE_DIR%\data\paper_veto_flip_EM_trades.jsonl
set LOG_FILE=%ENGINE_DIR%\data\paper_veto_flip_EM.combined.log
set WATCHDOG_LOG=%ENGINE_DIR%\data\watchdog_paper_veto_flip_EM.log
REM EM ENABLED (no --disable-earlier-moderate) + T-30 sniper + EM upsize
set SMART_V5_FLAGS=--enable-t30-sniper --disable-late --enable-em-upsize

:loop
echo [%date% %time%] starting paper-veto-flip-EM >> "%WATCHDOG_LOG%"
"%PY%" "%ENGINE_DIR%\scripts\live\live_v5_unified.py" ^
    --decision-log "%DECISION_LOG%" ^
    --poll-interval-s 1.5 ^
    --status-every-s 60 ^
    --no-resting ^
    --dry-run ^
    %SMART_V5_FLAGS% ^
    --veto-mode flip --veto-threshold 5 --veto-flip-threshold 30 ^
    >> "%LOG_FILE%" 2>&1
echo [%date% %time%] paper-veto-flip-EM exited with %ERRORLEVEL%; restarting in 5s >> "%WATCHDOG_LOG%"
timeout /t 5 /nobreak > nul
goto loop
