# Favorite-Chase strategy — LIVE as of 2026-05-21 05:23 local

## ⚠️ REAL MONEY ⚠️

`scripts/live/live_favorite_chase.py` is running under
`scripts/live/watchdog_live_favorite_chase.cmd` (auto-restarts on stale-feed).
User explicitly authorized "no caps" on 2026-05-21 after risk briefing —
see `memory/project_favorite_chase_live_2026_05_21.md` for audit trail.

| Setting | Value |
|---|---|
| size | 1 contract per trigger |
| side filter | BOTH (NO-bias not deployed as hard filter) |
| max entry ask | 0.90 |
| max slip (ask − trigger trade px) | 0.10 |
| min strike distance | 4 bps of spot |
| spot source | Coinbase REST per asset family (BTC/ETH/SOL/XRP/DOGE/+ others) |
| assets excluded | any without Coinbase mapping (notably KXBNB) |
| daily loss cap | NONE |
| max concurrent | NONE |
| stop-on-N-losses | NONE |

**Decision log**: `data/live_favorite_chase.jsonl`
**Watchdog stdout**: `data/_live_fc_watchdog.out`

## Kill switch (paste into PowerShell)

```powershell
$wd = (Get-Process cmd | Where-Object {(Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)").CommandLine -like "*watchdog_live_favorite_chase*"} | Select -ExpandProperty Id)
$py = (Get-Process python | Where-Object {(Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)").CommandLine -like "*live_favorite_chase.py*"} | Select -ExpandProperty Id)
if ($wd) { Stop-Process -Id $wd -Force; "killed watchdog $wd" }
if ($py) { Stop-Process -Id $py -Force; "killed python $py" }
```

## Reconcile actual P&L (don't trust the JSONL "exit" log alone)

Per the `feedback_balance_tracking.md` memory: balance is the truth.

```powershell
# Get latest balance via Kalshi API
$py = "C:\Users\coleb\AppData\Local\Python\bin\python.exe"
& $py -c @"
import os, asyncio, sys
sys.path.insert(0, r'D:/Trading/btc-bias-engine')
from pathlib import Path
from kalshi_client import KalshiClient
creds = dict(l.strip().split('=',1) for l in Path(r'D:/Trading/btc-bias-engine/credentials/kalshi.env').read_text().splitlines() if '=' in l and not l.startswith('#'))
async def main():
    c = KalshiClient(key_id=creds['KALSHI_API_KEY'], private_key_pem=Path(creds['KALSHI_PRIVATE_KEY_PATH']).read_text(), demo=False)
    b = await c.get_balance()
    print(b)
asyncio.run(main())
"@
```

## Old paper-forward (stopped)

The paper-forward `paper_favorite_chase.py` and its `watchdog_paper_favorite_chase.cmd` are no longer running. Decisions remain in `data/paper_favorite_chase.jsonl` (read-only, mixed older buggy entries before the saw-below gate was added on 2026-05-20).

## Old status snapshot (paper era, kept for reference)

---

# Favorite-Chase strategy — status snapshot (2026-05-20 ~22:00 local)

## TL;DR

You asked for a simple "buy first contract to hit 75¢ after T+8m, stop at 50¢, hold to settle, 1 contract" rule across all Kalshi crypto. It's built, BTC-backtested over 308 trades, and a paper-forward runner is live across ~956 crypto markets.

**Headline finding from the BTC sweep:** The naive rule is breakeven (−$1.19 over 308 trades). The dominant edge is **side selection**: trading **NO only** with a **max_entry_price cap at 0.90** gives **+$2.80 over 107 trades, +2.62c per trade, 69% WR**. Distance-from-strike filters barely move the needle.

Caveats: 4 days of data, possibly regime-dependent (BTC drifted down during the sample).

## Currently running

| What | PID | Cmdline |
|---|---|---|
| Paper-forward favorite-chase | 17872 | `paper_favorite_chase.py --side-filter no --max-entry-price 0.90 --max-slip 0.10` |
| Decision log | — | `data/paper_favorite_chase.jsonl` (~5 MB growing) |

NSSM services (untouched):
- KalshiCapture: RUNNING (don't stop — load-bearing)
- KalshiPaperTA: RUNNING
- KalshiLiveTA, KalshiPaperEngine, KalshiLadderShadow: STOPPED (as before)

## What was built

| File | What it does |
|---|---|
| `src/kalshi_btc_engine_v2/strategies/favorite_chase.py` | Pure rule logic |
| `scripts/research/backtest_favorite_chase.py` | BTC backtest over capture DBs |
| `scripts/research/sweep_favorite_chase.py` | Parameter sweep over backtest output |
| `scripts/research/analyze_paper_favorite_chase.py` | Per-series rollup of paper log |
| `scripts/live/paper_favorite_chase.py` | Live paper-forward runner |
| `tests/test_favorite_chase.py` | 11 new tests; full suite 214 pass |

## How to check on it

```powershell
# Paper-forward status
Get-Process -Id 17872 -ErrorAction SilentlyContinue | Select Id, WS, CPU, StartTime

# Latest paper log
Get-Content C:\Trading\kalshi-btc-engine-v2\data\paper_favorite_chase.jsonl -Tail 20

# Per-series rollup since the restart with correct defaults
$py = "C:\Users\coleb\AppData\Local\Python\bin\python.exe"
$env:PYTHONPATH = "src"
& $py C:\Trading\kalshi-btc-engine-v2\scripts\research\analyze_paper_favorite_chase.py `
  --since-ms 1779338820000
```

## How to stop it

```powershell
Stop-Process -Id 17872 -Force
```

(The PID will likely change if you've restarted; check first with `Get-Process python`
filtered by command line.)

## Outstanding questions

1. **Is the NO-bias persistent or sample-specific?** May 13-17 may have been a BTC down-leg. Need an up-leg sample to validate.
2. **Does the NO-bias hold across ETH/SOL/etc.?** Backtest data only exists for KXBTC15M; everything else is paper-forward-only. Once the paper log has ~50+ closed trades per series, re-run the analyzer and decide per-asset.
3. **Slippage**: the paper-forward uses next-available-ask as the fill. Real live execution will be worse due to latency between ws receipt and order placement. Maker-rest entries (75% cheaper fees) would improve the edge but require a different execution path (not built; out of scope for this iteration).
4. **Daily markets**: KXBTC-26MAY... (the longer hourly+ markets) typically have less time-pressure asymmetry than KXBTC15M; the rule may behave differently. The `saw_below_trigger` gate protects against firing on already-elevated markets, but the per-asset behavior is unstudied.

## Files left behind for debugging (safe to delete)

- `data/_favorite_chase_bt.jsonl` — 308 enriched backtest trades (the v2 sweep input)
- `data/_sweep_v2.out` — captured sweep output
- `data/_bt_full.out` / `data/_bt_full.err` — full backtest summary + stderr
- `data/_paper_fc.out` / `data/_paper_fc.err` — paper-forward stdout/stderr (mostly empty; the JSONL is the real log)

## What I did NOT do (would need explicit approval)

- Install as an NSSM service (would auto-start on boot; chose manual run)
- Modify `scripts/live/live_v5_unified.py` / `live_ta.py` / `live_paper.py` (untouched)
- Touch `C:\Trading\btc-bias-engine\` (used as Kalshi client source only)
- Set `live_enabled: true` or `ENGINE_V2_LIVE=true` (still false)
- Place any real orders

## Pickup instructions

If you want to revisit: read `memory/project_favorite_chase_2026_05_20.md` for the full backstory. The strategy lives in `src/kalshi_btc_engine_v2/strategies/favorite_chase.py` — it's ~80 lines and self-explanatory.
