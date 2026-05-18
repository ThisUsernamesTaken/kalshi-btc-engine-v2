"""Quick P&L summary for the Pine Script paper trader.

Usage: python scripts/_audit_ta_pnl.py [decision_log_path]

Default decision log is data/paper_ta_2026_05_12.jsonl. Read-only — safe
to run while the trader is live.
"""
import json
import sys

LOG = sys.argv[1] if len(sys.argv) > 1 else r"C:\Trading\kalshi-btc-engine-v2\data\paper_ta_2026_05_12.jsonl"

settled = [json.loads(l) for l in open(LOG) if json.loads(l).get("kind") == "settle"]
wins = sum(1 for t in settled if t["net_cents"] > 0)
total = sum(t["net_cents"] for t in settled)
dollars = total / 100
print(f"pine_ta: settled={len(settled)} wins={wins} net={total:+d}c (${dollars:+.2f})")
