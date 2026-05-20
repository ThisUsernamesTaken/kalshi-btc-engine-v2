"""One-shot veto status snapshot.

Reads the live trader log and prints a concise summary of recent veto
activity. Useful for quick health checks after launching shadow or live
veto mode.

Usage:
    python scripts/live/veto_status.py [--log PATH] [--last-hours N]

Output:
    - Total veto decisions in the window
    - Breakdown by action (KEEP/SKIP/FLIP)
    - Per-stage skip rate
    - Recent flip-fill outcomes (if any)
    - Realized P&L impact on settles (if any in the window)
    - Most recent veto error (if any) -- early signal of integration issues
"""
from __future__ import annotations
import argparse, json, sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--log', default=r'C:/Trading/kalshi-btc-engine-v2/data/live_v5_unified_trades.jsonl',
                    help='Live trader log (default: live_v5_unified_trades.jsonl)')
    ap.add_argument('--last-hours', type=int, default=24,
                    help='Window for the summary in hours (default 24)')
    args = ap.parse_args()

    log_p = Path(args.log)
    if not log_p.exists():
        print(f'ERROR: log not found: {log_p}', file=sys.stderr); return 1

    since_ms = int((datetime.now(timezone.utc) - timedelta(hours=args.last_hours)).timestamp() * 1000)
    veto_decisions = Counter()
    veto_by_stage = defaultdict(Counter)
    flip_attempts = []
    flip_fills = []
    flip_no_fills = 0
    skip_events_since = 0
    settles_since = []
    veto_errors = []
    n_total_lines = 0
    n_in_window = 0

    for line in log_p.open(encoding='utf-8', errors='ignore'):
        n_total_lines += 1
        try: e = json.loads(line)
        except: continue
        ts = e.get('ts_ms', 0)
        if ts < since_ms: continue
        n_in_window += 1
        k = e.get('kind', '')
        if k == 'model_veto':
            action = e.get('action') or ('SKIP' if e.get('would_skip') else 'KEEP')
            veto_decisions[action] += 1
            stage = e.get('stage', '?')
            veto_by_stage[stage][action] += 1
        elif k == 'model_veto_flip_attempt':
            flip_attempts.append(e)
        elif k == 'model_veto_flip_fill':
            flip_fills.append(e)
        elif k == 'model_veto_flip_no_fill':
            flip_no_fills += 1
        elif k == 'model_veto_error':
            veto_errors.append(e)
        elif k in ('earlier_moderate_skip', 'late_skip', 't30_sniper_skip'):
            if e.get('reason_code') == 'MODEL_VETO':
                skip_events_since += 1
        elif k == 'settle':
            settles_since.append(e)

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=args.last_hours)
    print(f'Window: {window_start.isoformat()}  ->  {now.isoformat()}')
    print(f'Log scanned: {n_total_lines} total lines, {n_in_window} in window')
    print()

    if not veto_decisions:
        print('No model_veto decisions in the window.')
        print('(If the trader is running with --veto-mode=off, this is expected.)')
        return 0

    print('======== VETO ACTIONS ========')
    total_actions = sum(veto_decisions.values())
    for action in ('KEEP', 'SKIP', 'FLIP'):
        n = veto_decisions.get(action, 0)
        pct = n / max(total_actions, 1) * 100
        print(f'  {action:5s}: {n:>4d} ({pct:>5.1f}%)')
    print()

    print('======== PER STAGE ========')
    print(f'  {"stage":22s} {"KEEP":>6s} {"SKIP":>6s} {"FLIP":>6s} {"skip_rate":>11s}')
    for stage, counts in sorted(veto_by_stage.items()):
        k = counts.get('KEEP', 0); s = counts.get('SKIP', 0); f = counts.get('FLIP', 0)
        rate = (s + f) / max(k + s + f, 1) * 100
        print(f'  {stage:22s} {k:>6d} {s:>6d} {f:>6d} {rate:>10.1f}%')
    print()

    if flip_attempts or flip_fills or flip_no_fills:
        print('======== FLIP ACTIVITY ========')
        print(f'  Attempts: {len(flip_attempts)}')
        print(f'  Fills:    {len(flip_fills)}')
        print(f'  No-fills: {flip_no_fills}')
        if flip_fills:
            print('  Recent fills:')
            for f in flip_fills[-5:]:
                print(f'    {f.get("ticker","?")[-15:]:15s} {f.get("side","?"):3s}@{f.get("entry_price_cents","?")}c '
                      f'engine_wanted={f.get("engine_intended_side","?")}@{f.get("engine_intended_price_cents","?")}c '
                      f'p_model={f.get("p_model_yes",0):.3f}')
        print()

    if veto_errors:
        print('======== VETO ERRORS (in window) ========')
        print(f'  Count: {len(veto_errors)}')
        for e in veto_errors[-3:]:
            print(f'  {e.get("ticker","?")[-15:]}  stage={e.get("stage","?")}  error={e.get("error","")[:100]}')
        print()

    # Quick P&L impact: settles that fired skip / flip
    if settles_since:
        skipped_count = 0
        flip_pnl = 0
        flip_n = 0
        for s in settles_since:
            leg = s.get('leg', '')
            if 'FLIP' in leg:
                flip_n += 1
                flip_pnl += s.get('net_cents', 0)
        if flip_n:
            print('======== FLIP-TRADE P&L (in window) ========')
            print(f'  Flip settles: {flip_n}')
            print(f'  Net P&L:      ${flip_pnl/100:+.2f}')
            print(f'  Avg per:      ${flip_pnl/flip_n/100:+.3f}')
            print()

    print(f'======== INTEGRATION HEALTH ========')
    health = 'OK'
    if veto_errors: health = f'WARN ({len(veto_errors)} errors)'
    if any(veto_by_stage[s].get('SKIP',0) + veto_by_stage[s].get('FLIP',0) > 0.7 * sum(veto_by_stage[s].values())
           for s in veto_by_stage):
        health = 'CHECK (skip rate >70% on some leg)'
    print(f'  Status: {health}')

    return 0


if __name__ == '__main__':
    sys.exit(main())
