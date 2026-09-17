@echo off
REM Auto-restart watchdog for engine_v3 PAPER/SHADOW trader.
REM Runs alongside the production live_v5_unified.py (PID changes on restart).
REM
REM Engine_v3 features (per literature review 2026-05-17):
REM   - T-30 sniper at fav_bid>=85c (baseline edge: 44/44 = 100% WR backtest)
REM   - VWAP lock-in check (Sun et al. 2025; CME BRTI settlement mechanic)
REM   - Intraday-jump filter (Bozovic 2025; jumps cluster at close)
REM   - Bayesian WR tracking (Beta posterior; Beta(45,1) backtest prior)
REM   - Quarter-Kelly sizing (Thorp/MacLean-Thorp-Ziemba)
REM   - CUSUM kill switch (Page 1954; 2 losses in 10 trades -> halt)
REM
REM PAPER MODE: tracks all decisions, simulates fills, NO REAL ORDERS.
REM Log path: data/engine_v3_paper_decisions.jsonl

setlocal
set ENGINE_DIR=C:\Trading\kalshi-btc-engine-v2
set PY=C:\Users\coleb\AppData\Local\Python\bin\python.exe
set PYTHONPATH=%ENGINE_DIR%\src
set PYTHONIOENCODING=utf-8
set DECISION_LOG=%ENGINE_DIR%\data_local\engine_v3_paper_decisions.jsonl
set LOG_FILE=%ENGINE_DIR%\data_local\engine_v3_paper.combined.log
set WATCHDOG_LOG=%ENGINE_DIR%\data_local\watchdog_engine_v3_paper.log

REM Mode: shadow (compares against production trader's log)
REM Sizing: backtest prior + Bayesian sizing (quarter-Kelly)
REM Filters: all on (vwap, jump, cusum)
set V3_FLAGS=--mode shadow --posterior-prior backtest --enable-vwap-lock --enable-jump-filter --enable-bayesian-sizing --enable-cusum --bankroll-dollars 30

:loop
echo [%DATE% %TIME%] watchdog: starting engine_v3_paper attempt >> "%WATCHDOG_LOG%"
"%PY%" "%ENGINE_DIR%\scripts\research\engine_v3_paper.py" --decision-log "%DECISION_LOG%" %V3_FLAGS% >> "%LOG_FILE%" 2>&1
echo [%DATE% %TIME%] watchdog: inner exited code=%ERRORLEVEL%; sleeping 10s then restart >> "%WATCHDOG_LOG%"
timeout /t 10 /nobreak > nul
goto loop
