"""Quick P&L summary for the engine v2 paper trader (cross-references lifecycle).

The PaperExecutor has a known bug: it doesn't reconcile settlement, so its
in-process P&L counter is stale. This script joins decision-log entries
against lifecycle determinations to compute the real outcome.

Usage: python scripts/_audit_engine_pnl.py [decision_log] [capture_db]
"""
import json
import math
import sqlite3
import sys

LOG = sys.argv[1] if len(sys.argv) > 1 else r"C:\Trading\kalshi-btc-engine-v2\data\paper_holdpure_2026_05_12.jsonl"
DB = sys.argv[2] if len(sys.argv) > 2 else r"C:\Trading\kalshi-btc-engine-v2\data\burnin_holdpure_2026_05_12.sqlite"

entries = [json.loads(l) for l in open(LOG) if json.loads(l).get("action") in ("BUY_YES", "BUY_NO")]

c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
outcomes: dict[str, str] = {}
for ticker, raw in c.execute(
    "SELECT market_ticker, raw_json FROM kalshi_lifecycle_event "
    "WHERE status='determined' AND market_ticker LIKE 'KXBTC15M-%'"
):
    try:
        msg = json.loads(raw).get("msg", {})
        outcomes[ticker] = msg.get("result")
    except Exception:
        pass
c.close()

wins = losses = unsettled = net = 0
for e in entries:
    side = e.get("side")
    n = e.get("contracts") or 0
    cost = e.get("yes_ask_cents") if side == "yes" else e.get("no_ask_cents")
    if cost is None:
        continue
    fee = math.ceil(0.07 * n * (cost / 100) * (1 - cost / 100) * 100 - 1e-12)
    out = outcomes.get(e.get("market_ticker"))
    if out is None:
        unsettled += 1
        continue
    gross = n * (100 - cost) if side == out else -n * cost
    net += gross - fee
    if side == out:
        wins += 1
    else:
        losses += 1

dollars = net / 100
print(
    f"engine_v2: entries={len(entries)} wins={wins} losses={losses} "
    f"unsettled={unsettled} net={net:+d}c (${dollars:+.2f})"
)
