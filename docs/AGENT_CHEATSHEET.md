# Agent cheat sheet — common operations

Copy-pasteable commands for diagnosis, inspection, and safe interaction
with the running paper-trading stack. **Read [`../CLAUDE.md`](../CLAUDE.md)
first** for context on what's running and why.

## Constants

```powershell
$PY     = "C:\Users\coleb\AppData\Local\Python\bin\python.exe"
$ENGINE = "C:\Trading\kalshi-btc-engine-v2"
$DB     = "$ENGINE\data\burnin_holdpure_2026_05_12.sqlite"
$env:PYTHONPATH = "$ENGINE\src"
$env:PYTHONIOENCODING = "utf-8"
```

## Liveness checks

```powershell
# All running python + cmd processes
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='cmd.exe'" |
  Select-Object ProcessId, ParentProcessId, Name, CreationDate |
  Sort-Object CreationDate | Format-Table -AutoSize

# Recent status from each paper trader
Get-Content "$ENGINE\data\paper_holdpure.combined.log" -Tail 3
Get-Content "$ENGINE\data\paper_ta.combined.log" -Tail 5

# Capture process heartbeat (mps rates per channel)
Get-Content "$ENGINE\data\burnin_holdpure.combined.log" -Tail 2

# File mtimes — quick "is it still writing?"
Get-ChildItem "$ENGINE\data\paper*","$ENGINE\data\burnin_holdpure*" |
  Sort-Object LastWriteTime -Descending |
  Select-Object Name, LastWriteTime, @{N='KB';E={[math]::Round($_.Length/1KB,1)}} -First 8
```

## P&L and trade counts

Save these two scripts as separate `.py` files and run with `& $PY <script>`.
This keeps PowerShell from eating `$` inside Python f-strings.

`scripts/_audit_ta_pnl.py`:

```python
import json, sys
LOG = sys.argv[1] if len(sys.argv) > 1 else r"C:\Trading\kalshi-btc-engine-v2\data\paper_ta_2026_05_12.jsonl"
settled = [json.loads(l) for l in open(LOG) if json.loads(l).get("kind") == "settle"]
wins = sum(1 for t in settled if t["net_cents"] > 0)
total = sum(t["net_cents"] for t in settled)
dollars = total / 100
print(f"pine_ta: settled={len(settled)} wins={wins} net={total:+d}c (${dollars:+.2f})")
```

`scripts/_audit_engine_pnl.py`:

```python
import json, sqlite3, math, sys
LOG = sys.argv[1] if len(sys.argv) > 1 else r"C:\Trading\kalshi-btc-engine-v2\data\paper_holdpure_2026_05_12.jsonl"
DB  = sys.argv[2] if len(sys.argv) > 2 else r"C:\Trading\kalshi-btc-engine-v2\data\burnin_holdpure_2026_05_12.sqlite"
entries = [json.loads(l) for l in open(LOG) if json.loads(l).get("action") in ("BUY_YES","BUY_NO")]
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
outcomes = {}
for t, raw in c.execute("SELECT market_ticker, raw_json FROM kalshi_lifecycle_event WHERE status='determined' AND market_ticker LIKE 'KXBTC15M-%'"):
    try:
        msg = json.loads(raw).get("msg", {})
        outcomes[t] = msg.get("result")
    except Exception:
        pass
c.close()
wins = losses = unsettled = net = 0
for e in entries:
    side = e.get("side"); n = e.get("contracts") or 0
    cost = e.get("yes_ask_cents") if side == "yes" else e.get("no_ask_cents")
    if cost is None: continue
    fee = math.ceil(0.07 * n * (cost/100) * (1 - cost/100) * 100 - 1e-12)
    out = outcomes.get(e.get("market_ticker"))
    if out is None: unsettled += 1; continue
    gross = n * (100 - cost) if side == out else -n * cost
    net += gross - fee
    if side == out: wins += 1
    else: losses += 1
dollars = net / 100
print(f"engine_v2: entries={len(entries)} wins={wins} losses={losses} unsettled={unsettled} net={net:+d}c (${dollars:+.2f})")
```

Run with `& $PY $ENGINE\scripts\_audit_ta_pnl.py` and
`& $PY $ENGINE\scripts\_audit_engine_pnl.py`. Both safe to re-run (read-only).

## Capture-DB inspection (read-only)

```powershell
# Row counts and basic health
& $PY -m kalshi_btc_engine_v2.cli db-stats --db $DB

# Settled markets via lifecycle (NOT settled-markets CLI — that's broken)
& $PY -c @"
import sqlite3, json
c = sqlite3.connect('file:$DB?mode=ro', uri=True)
rows = c.execute(\"SELECT market_ticker, raw_json FROM kalshi_lifecycle_event WHERE status='determined' AND market_ticker LIKE 'KXBTC15M-%'\").fetchall()
yes = sum(1 for t,r in rows if r and '\"result\":\"yes\"' in r)
no = sum(1 for t,r in rows if r and '\"result\":\"no\"' in r)
print(f'btc15m settled: {len(rows)}  yes={yes}  no={no}')
"@

# Spot quote freshness per venue (detects WS dropouts)
& $PY -c @"
import sqlite3
c = sqlite3.connect('file:$DB?mode=ro', uri=True)
for r in c.execute('SELECT venue, COUNT(*), MAX(received_ts_ms) FROM spot_quote_event GROUP BY venue'):
    print(f'{r[0]:<20} count={r[1]:>6}  max_ts={r[2]}')
"@
```

## Latency diagnostic

```powershell
# Effective staleness floor for the captured stream
& $PY "$ENGINE\scripts\latency_budget.py" --db $DB --decision-interval-ms 250 --json-only |
  Select-String -Pattern "effective_staleness|verdict|p50|p99|median_market" |
  Select-Object -First 8
```

## Testing

```powershell
# Full suite — should report 179 passed
& $PY -m pytest "$ENGINE\tests" -q --no-header 2>&1 | Select-String -Pattern "passed|failed|error"
```

## Stopping things safely

```powershell
# Stop only the Pine Script watchdog (auto-restart goes away)
Stop-Process -Id <watchdog_cmd_pid> -Force

# If access denied, use WMI terminate
Invoke-CimMethod -InputObject (Get-CimInstance Win32_Process -Filter "ProcessId=<PID>") -MethodName Terminate

# Stop EVERYTHING (capture too — only do this with user approval)
# Identify PIDs first, then issue Stop-Process on each.
```

## Restarting

See `RUNNING.md` "How to restart" section. Always re-verify after restart
with the liveness checks above. The capture's WAL is replayed automatically
on next open so abrupt stops are safe.

## Adding a new analysis

Put one-off scripts in the engine root as `_<name>.py` (excluded by `.gitignore`
patterns informally — clean them up after). Persistent scripts go in
`scripts/`. Persistent CLI subcommands go in `cli.py` with a matching test.
