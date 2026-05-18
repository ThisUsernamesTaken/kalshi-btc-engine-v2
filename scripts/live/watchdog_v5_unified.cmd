@echo off
REM Indefinite auto-restart watchdog for the LIVE V5 UNIFIED trader.
REM Strategy: V5 EM + LATE + SMART-V5 enhancements (RV regime flip,
REM EM upsize, LATE vel-align). Hold to settlement. Daily cap $40.
REM Backtest-validated swing: -$50 -> +$104 net on 309 entries (see
REM _v5_verify_and_explore.out and _LIVE_INSIGHTS_2026_05_16.md).
REM Source: scripts/live/live_v5_unified.py.

setlocal
set ENGINE_DIR=C:\Trading\kalshi-btc-engine-v2
set PY=C:\Users\coleb\AppData\Local\Python\bin\python.exe
set PYTHONPATH=%ENGINE_DIR%\src
set PYTHONIOENCODING=utf-8
set DECISION_LOG=%ENGINE_DIR%\data\live_v5_unified_trades.jsonl
set LOG_FILE=%ENGINE_DIR%\data\live_v5_unified.combined.log
set WATCHDOG_LOG=%ENGINE_DIR%\data\watchdog_v5_unified.log

REM Strategy flags (2026-05-18 EMERGENCY FIX — disabled EM after $22.57 bleed):
REM
REM   --disable-earlier-moderate : DISABLED 2026-05-18 after EM produced -$22.57
REM                                in 9 trades since 2026-05-17. The two killer
REM                                losses: -$18.13 @ 90c NO settled YES, -$16.80
REM                                @ 83c YES settled NO. This is the structural
REM                                asymmetric-payoff bleeder the engine_v2 docs
REM                                warned about a year ago ("refuse entries
REM                                above 60c"). Cannot be tuned away.
REM   --enable-t30-sniper        : PRIMARY EDGE — fired ZERO times since deploy
REM                                because EM was mutex-locking every market.
REM                                With EM disabled, sniper can finally fire.
REM                                Backtest 331 markets: 44/44 = 100pct WR.
REM                                Engine_v3 paper confirms: 9/9 wins.
REM                                Combined evidence: 53/53 = perfect on
REM                                independent samples.
REM   --disable-late             : LATE leg structurally -EV (backtest test set).
REM   --enable-em-upsize         : MOOT now (EM disabled), kept for code clarity.
REM   --enable-late-vel-align    : MOOT now (LATE disabled), kept for code clarity.
REM   (--enable-rv-regime-gate)  : DROPPED 2026-05-17 — flip lost 0/5 in live.
set SMART_V5_FLAGS=--disable-earlier-moderate --enable-t30-sniper --disable-late --enable-em-upsize

:loop
echo [%DATE% %TIME%] watchdog: starting attempt >> "%WATCHDOG_LOG%"
"%PY%" "%ENGINE_DIR%\scripts\live\live_v5_unified.py" --decision-log "%DECISION_LOG%" --disable-early --disable-flat-small-flip --no-resting %SMART_V5_FLAGS% >> "%LOG_FILE%" 2>&1
echo [%DATE% %TIME%] watchdog: inner exited code=%ERRORLEVEL%; sleeping 5s then restart >> "%WATCHDOG_LOG%"
timeout /t 5 /nobreak > nul
goto loop
