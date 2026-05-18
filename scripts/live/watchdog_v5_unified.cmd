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

REM Strategy flags (2026-05-17 v3 — DATA-VALIDATED edge replaces speculative ones):
REM
REM   --enable-t30-sniper        : NEW PRIMARY EDGE. At T-30s, if favorite-side
REM                                bid >= 85c, IOC buy 10ct favorite, hold to
REM                                settle. Backtest 331 markets: 44/44 = 100pct WR
REM                                across train+test (29/29 + 15/15). Out-of-sample
REM                                EV +$0.14/trade @5ct, +$0.29/trade @10ct.
REM                                Expected ~14 trades/day, ~$4/day EV.
REM   --disable-late             : Disable structurally-losing T-180 LATE leg.
REM                                Backtest: -EV in test set across all thresholds.
REM                                T-30 sniper replaces it with a profitable rule.
REM   --enable-em-upsize         : KEPT — conservative high-conf upsize (gap>=18
REM                                + entry>=90c + RV<0.025), only adds size when
REM                                evidence is strongest.
REM   --enable-late-vel-align    : MOOT now (LATE disabled), but kept for clarity.
REM   (--enable-rv-regime-gate)  : DROPPED 2026-05-17 — flip lost 0/5 in live.
set SMART_V5_FLAGS=--enable-t30-sniper --disable-late --enable-em-upsize

:loop
echo [%DATE% %TIME%] watchdog: starting attempt >> "%WATCHDOG_LOG%"
"%PY%" "%ENGINE_DIR%\scripts\live\live_v5_unified.py" --decision-log "%DECISION_LOG%" --disable-early --disable-flat-small-flip --no-resting %SMART_V5_FLAGS% >> "%LOG_FILE%" 2>&1
echo [%DATE% %TIME%] watchdog: inner exited code=%ERRORLEVEL%; sleeping 5s then restart >> "%WATCHDOG_LOG%"
timeout /t 5 /nobreak > nul
goto loop
