# Favorite-Chase — handoff for the next dispatch instance

If you're a Claude (or other AI) joining this directory to continue work on
the favorite-chase strategy: **read this whole file before touching anything**.
It is real-money live as of 2026-05-21.

## What the strategy is

A simple binary-options momentum chase:

1. For every open Kalshi crypto market, watch the WS trade tape.
2. After T+8 minutes from the market's `open_time`, on the first trade
   printing `yes_price >= 0.75` or `no_price >= 0.75`, enter that side.
3. Buy 1 contract at the current ask via `KalshiClient.place_order(market,
   IOC, action="buy")`.
4. If the held side's mid falls to `<= 0.50`, market-sell at the bid (stop).
5. Otherwise hold to settlement.

Gates applied at entry (skip if any fails):
- `saw_below_trigger` — we must have observed the market <75c on both sides
  before crossing. Protects against firing on markets we subscribed to
  already-elevated.
- `ask <= 0.90` (`--max-entry-price`) — refuse "lose 99 to win 1" fills.
- `(ask - trigger_trade_price) <= 0.10` (`--max-slip`) — refuse fills where
  the book gapped well past the print.
- `|spot - strike| / spot >= 4 bps` (`--min-strike-bps`) — refuse triggers
  where spot is essentially sitting on the strike. Spot comes from Coinbase
  `/products/{id}/ticker`, polled every 2s, cached in-process.

Sizing is **hard-locked to 1 contract** via
`strategies.favorite_chase.MAX_CONTRACTS_PER_TRADE`. Both the entry and stop
legs read from this constant. There is no `--size` flag. Raising the cap
requires editing the constant and explicit re-authorisation.

## Audit trail

The user **explicitly chose** "no caps" on 2026-05-21 after being shown
four paths including one recommending shadow validation. The override is
documented in `memory/project_favorite_chase_live_2026_05_21.md`. The runner
logs `live_authorization: "user explicit 2026-05-21"` in its boot event.

## What's currently running

| Component | PID (at time of writing) | Purpose |
|---|---|---|
| `watchdog_live_favorite_chase.cmd` | 18256 | Auto-restarts python on rc≠0 (i.e., stale-feed exit) |
| `python live_favorite_chase.py` | 6276 | The actual engine, REAL MONEY |

```powershell
# Verify they're alive
Get-Process cmd | Where-Object {(Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)").CommandLine -like "*watchdog_live_favorite*"} | Select Id, StartTime
Get-Process python | Where-Object {(Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)").CommandLine -like "*live_favorite_chase*"} | Select Id, StartTime, WS, CPU
```

PIDs WILL change after any auto-restart. Filter by command-line, not by
remembered PID.

## File map

```
src/kalshi_btc_engine_v2/strategies/
    favorite_chase.py          # Pure rule logic + constants (MAX_CONTRACTS_PER_TRADE,
                               # MIN_STRIKE_DISTANCE_BPS, ENTRY_TRIGGER_PRICE, STOP_PRICE)

scripts/live/
    live_favorite_chase.py     # LIVE order placer (real money)
    paper_favorite_chase.py    # Paper variant (kept for reference; NOT running)
    watchdog_live_favorite_chase.cmd     # Wrapper around the live runner
    watchdog_paper_favorite_chase.cmd    # Wrapper for the paper variant

scripts/research/
    backtest_favorite_chase.py # BTC capture-DB backtest (read-only)
    sweep_favorite_chase.py    # Parameter sweep over backtest JSONL
    analyze_paper_favorite_chase.py  # Per-series rollup of any *_favorite_chase.jsonl

tests/
    test_favorite_chase.py     # 16 unit tests covering rule + bps gate

data/
    live_favorite_chase.jsonl  # LIVE decision log (truth-of-intent)
    paper_favorite_chase.jsonl # Paper log (historical; do not delete)
    _favorite_chase_bt.jsonl   # Backtest output (308 enriched trades)
    _sweep_v2.out              # Sweep results
    _live_fc_watchdog.{out,err}  # Cmd wrapper stdout/stderr

memory/
    project_favorite_chase_2026_05_20.md       # Build + backtest narrative
    project_favorite_chase_live_2026_05_21.md  # Live-deployment authorisation

FAVORITE_CHASE_STATUS.md  # Current-config snapshot + kill switch
FAVORITE_CHASE_HANDOFF.md # This file
```

## How to read the decision log

Each line is a JSON object with `kind` and `log_ts_ms`. The meaningful kinds:

| Kind | When | Important fields |
|---|---|---|
| `boot` | Process start | `shadow`, `max_entry_price`, `max_slip`, `min_strike_bps` |
| `discover` | First time a market is registered | `ticker`, `series`, `strike`, `open_ms`, `close_ms` |
| `discovery` | After each REST sweep | `tracked`, `seen`, `new` |
| `ws_subscribe` | After (re-)subscribing | `n_tickers` |
| `ws_error` | WS connection dropped | `error` |
| `spot_poll_error` | Coinbase REST hiccup | `product`, `error` |
| `skip_max_price` / `skip_max_slip` / `skip_no_spot` / `skip_min_strike_bps` | Entry gates rejected | `ticker`, plus the relevant param |
| `trigger_no_book` | Trigger fired but no ask available | `ticker`, `side` |
| `entry` | All gates passed; order was placed | `ticker`, `side`, `fill_price` (=ask we paid), `trigger_yes`, `trigger_no`, `slip`, `strike` |
| `live_entry_placed` | KalshiClient.place_order returned without raising | `order_id`, `qty`, `ask_cents` |
| `live_entry_error` | KalshiClient.place_order raised | `error` |
| `exit` | Position closed | `reason` (`stop` / `settle_win` / `settle_loss`), `fill_price`, `exit_price` |
| `live_stop_placed` / `live_stop_error` | Stop sell order outcome | `order_id` / `error` |
| `stale_feed_exit` | Watchdog tripped | `stale_s` (seconds idle) |

**Important**: `entry` is logged when the order is *sent to Kalshi*, not when
it *fills*. The order may be partially filled, rejected, or filled at a
different price. **Do not derive realised P&L from the JSONL.** See "P&L
reconciliation" below.

Quick stats:
```powershell
$log = "C:\Trading\kalshi-btc-engine-v2\data\live_favorite_chase.jsonl"
$py = "C:\Users\coleb\AppData\Local\Python\bin\python.exe"
$env:PYTHONPATH = "src"
& $py C:\Trading\kalshi-btc-engine-v2\scripts\research\analyze_paper_favorite_chase.py --log $log
```

## P&L reconciliation (do not skip)

Per `feedback_balance_tracking.md` in memory: the `feedback_balance_tracking`
incident lost $164 from trusting take-profit fills without checking balance.
Same principle here: balance is the truth.

```powershell
$py = "C:\Users\coleb\AppData\Local\Python\bin\python.exe"
& $py -c @"
import asyncio, sys
from pathlib import Path
sys.path.insert(0, r'D:/Trading/btc-bias-engine')
from kalshi_client import KalshiClient
creds = dict(l.strip().split('=',1) for l in Path(r'D:/Trading/btc-bias-engine/credentials/kalshi.env').read_text().splitlines() if '=' in l and not l.startswith('#'))
async def main():
    c = KalshiClient(key_id=creds['KALSHI_API_KEY'], private_key_pem=Path(creds['KALSHI_PRIVATE_KEY_PATH']).read_text(), demo=False)
    print(await c.get_balance())
    print(await c.get_positions())
asyncio.run(main())
"@
```

## Kill switch

```powershell
$wd = (Get-Process cmd | Where-Object {(Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)").CommandLine -like "*watchdog_live_favorite_chase*"} | Select -ExpandProperty Id)
$py = (Get-Process python | Where-Object {(Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)").CommandLine -like "*live_favorite_chase.py*"} | Select -ExpandProperty Id)
if ($wd) { Stop-Process -Id $wd -Force; "killed watchdog $wd" }
if ($py) { Stop-Process -Id $py -Force; "killed python $py" }
```

Kill the watchdog first or it will respawn python.

## Relaunch (if you've killed it)

```powershell
Start-Process -FilePath "cmd.exe" `
  -ArgumentList @("/c", "C:\Trading\kalshi-btc-engine-v2\scripts\live\watchdog_live_favorite_chase.cmd") `
  -WorkingDirectory "C:\Trading\kalshi-btc-engine-v2" `
  -RedirectStandardOutput "C:\Trading\kalshi-btc-engine-v2\data\_live_fc_watchdog.out" `
  -RedirectStandardError "C:\Trading\kalshi-btc-engine-v2\data\_live_fc_watchdog.err" `
  -NoNewWindow -PassThru
```

## Backtest evidence (for context, not validation)

- 336 KXBTC15M markets, May 13-17 2026, BRTI-settled.
- 308 enriched trades (after dropping missing-strike/spot rows).
- Baseline naive rule: **-$1.19 / -0.39c per trade.**
- NO-only + max_p ≤ 0.90 (the closest single-config match to current live
  settings, but **WITH side filter — which the live runner does NOT
  apply**): +$2.80 / 107 trades / +2.62c/trade / 69% WR.

Sample is 4 days, one regime. Walk-forward validation was not done.
The user accepted this risk explicitly.

## Things you should be careful about

1. **Don't auto-tune parameters without explicit re-authorisation.** Even if
   accumulated paper-forward data suggests a different combo is better, do
   not silently change the running config. The user wants to be in the loop
   on parameter changes.

2. **Don't add a `--size` CLI flag or argument.** Sizing must remain at
   `MAX_CONTRACTS_PER_TRADE = 1`. If the user wants to raise it, they should
   edit the constant in code — a deliberate code change, not a runtime arg.

3. **Don't modify `C:\Trading\btc-bias-engine\` or any of its files.** The
   live runner imports `KalshiClient` from there; that engine is also
   load-bearing (different live process, `live_v5_unified.py`). Treat it
   read-only.

4. **Don't stop other NSSM services.** `KalshiCapture` (capture DB) and
   `KalshiPaperTA` (Pine Script paper) are running. They're independent of
   favorite-chase. Per `RUNNING.md` they must keep running.

5. **WS subscription is global across all 791 markets in one connection.**
   If you want to add/remove markets, change `CRYPTO_SERIES_PREFIXES` or the
   `SERIES_TO_COINBASE` map at the top of `live_favorite_chase.py`. Then
   restart — the engine doesn't hot-reload the prefix list.

6. **The watchdog cmd respawns on any rc≠0.** If you want to *stop* live
   trading without leaving the watchdog to respawn, kill the cmd FIRST,
   then the python. There is no graceful "drain mode" — open positions
   ride to stop or settle on their own.

7. **`live_authorization` field in the boot event is your audit anchor.**
   Don't remove it. If you change the authorisation status (e.g., a future
   user re-authorises with different caps), update the field — that's how
   future-you knows the current state was deliberate.

## Known open questions

1. **Side selection.** Backtest shows NO is +EV and YES bleeds. The live
   runner trades BOTH sides because (a) 4 days is too small to be confident,
   (b) the bias may be regime-dependent. As the live log accumulates, run
   `analyze_paper_favorite_chase.py` against `live_favorite_chase.jsonl` to
   see if the NO-bias holds out-of-sample. If it does and you want to add a
   `--side-filter` gate, **ask the user first**.

2. **Slippage in practice.** Paper-forward yesterday showed individual fills
   moving 7-10c past the trigger trade price. The `--max-slip 0.10` cap
   absorbs most of this but the live data will reveal whether it's enough.

3. **Out-of-sample drawdown.** The backtest's worst stretch was ~2.5h of
   -$2.46 → +$0.36 swing. A live equivalent could conceivably be larger.
   There is no auto-halt — the user is the halt.

4. **Lifecycle settlement.** The engine logs `exit` with `reason=settle_*`
   on `market_lifecycle_v2 status=determined`. But the live position may
   already have stopped out earlier with a separate `exit reason=stop`. The
   per-market state machine has `closed: bool` to prevent double-counting,
   but verify behaviour from the JSONL when reviewing.

## When in doubt

- Read `CLAUDE.md` (this directory) for the broader project rules.
- Read `RUNNING.md` for the other services and operational state.
- Read the two `project_favorite_chase_*.md` memory entries for the full
  build narrative + authorisation history.
- Ask the user before changing live parameters, raising the sizing cap, or
  stopping/restarting services other than the favorite-chase one.
