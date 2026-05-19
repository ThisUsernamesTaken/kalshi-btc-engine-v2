"""Post-hoc veto auditor: compare model_veto decisions to actual settle outcomes.

After shadow mode (or live mode) runs for a while, this script reads the
trade log, finds every `model_veto` event, looks up the corresponding
`settle` for that ticker, and reports:

  - Veto accuracy on losers (would_skip=True ∩ engine lost = TP)
  - Veto false positives (would_skip=True ∩ engine won = FP)
  - Veto misses (would_skip=False ∩ engine lost = FN)
  - Veto agreement on winners (would_skip=False ∩ engine won = TN)

Plus net-P&L impact: "if the veto had been live (skip mode), what would
the realized P&L have been?"

Usage:
    python scripts/live/veto_audit.py \\
        --log data/live_v5_unified_trades.jsonl \\
        [--since 2026-05-17T00:00:00Z]
"""
from __future__ import annotations
import argparse, json, sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def parse_iso(s: str) -> int:
    """Parse an ISO 8601 timestamp to ms epoch. Naive strings assumed UTC."""
    if s.endswith('Z'):
        s = s.replace('Z', '+00:00')
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--log', type=str, required=True,
                    help='Trade log JSONL (live_v5_unified_trades.jsonl)')
    ap.add_argument('--settles-log', type=str, default=None,
                    help='Optional separate log for settles (if --log is from '
                         'veto_shadow_monitor.py, supply the original trade log here)')
    ap.add_argument('--since', type=str, default=None,
                    help='Only audit events at or after this ISO timestamp (UTC)')
    args = ap.parse_args()

    since_ms = parse_iso(args.since) if args.since else 0
    veto_events = []          # (ticker, ts_ms, would_skip, p_model, engine_side, stage, reason)
    settle_events = {}        # ticker -> latest settle event
    skip_events = {}          # ticker -> 'MODEL_VETO' skip record
    log_p = Path(args.log)
    if not log_p.exists():
        print(f'ERROR: log not found: {log_p}', file=sys.stderr)
        return 1
    n_lines = 0
    def _ingest(line, target_for_settles=True):
        nonlocal n_lines
        n_lines += 1
        try: e = json.loads(line)
        except: return
        ts = e.get('ts_ms', 0)
        if ts < since_ms: return
        k = e.get('kind')
        t = e.get('ticker')
        if not t: return
        # Live-trader native event
        if k == 'model_veto':
            # New schema has 'action' (KEEP/SKIP/FLIP); old has 'would_skip' bool
            action = e.get('action')
            if action is None:
                action = 'SKIP' if e.get('would_skip', False) else 'KEEP'
            veto_events.append(dict(
                ticker=t, ts_ms=ts,
                action=action,
                would_skip=(action in ('SKIP', 'FLIP')),
                p_model=e.get('p_model_yes'),
                engine_side=e.get('engine_side'),
                engine_price=e.get('engine_price_cents'),
                stage=e.get('stage'),
                mode=e.get('mode'),
                flip_details=e.get('flip_details'),
                reason=e.get('reason', '')[:140],
            ))
        # veto_shadow_monitor output (historical audit)
        elif k == 'veto_shadow_decision':
            tk = e.get('trigger_kind', '').replace('_trigger', '')
            ws = e.get('would_skip', False)
            veto_events.append(dict(
                ticker=t, ts_ms=ts,
                action=('SKIP' if ws else 'KEEP'),  # shadow monitor has no FLIP yet
                would_skip=ws,
                p_model=e.get('p_model_yes'),
                engine_side=e.get('engine_side'),
                engine_price=e.get('engine_price_cents'),
                stage=tk,
                mode='shadow',
                flip_details=None,
                reason=e.get('reason', '')[:140],
            ))
        elif k in ('earlier_moderate_skip', 'late_skip', 't30_sniper_skip'):
            if e.get('reason_code') == 'MODEL_VETO':
                skip_events[t] = e
        elif k == 'settle' and target_for_settles:
            settle_events[t] = e

    for line in log_p.open(encoding='utf-8', errors='ignore'):
        _ingest(line, target_for_settles=True)
    if args.settles_log:
        settles_p = Path(args.settles_log)
        if settles_p.exists():
            for line in settles_p.open(encoding='utf-8', errors='ignore'):
                _ingest(line, target_for_settles=True)

    print(f'Scanned {n_lines} log lines{"" if not args.since else f" (since {args.since})"}')
    print(f'Found {len(veto_events)} model_veto events on {len(set(v["ticker"] for v in veto_events))} unique tickers')
    print(f'Found {len(settle_events)} settles, {len(skip_events)} MODEL_VETO skips\n')

    if not veto_events:
        print('No model_veto events to audit.')
        return 0

    # Confusion matrix: per ticker, match the veto event's STAGE to the
    # leg of the actual settled trade. If the engine filled on EARLIER_MODERATE,
    # only the EM veto event is the relevant one. Otherwise pick the latest
    # veto event for that ticker.
    stage_map = {
        'EARLIER_MODERATE': 'earlier_moderate',
        'LATE': 'late',
        'T30_SNIPER': 't30_sniper',
        'EARLY': 'early',
    }
    by_ticker = {}
    for v in veto_events:
        t = v['ticker']
        settle = settle_events.get(t)
        if settle:
            wanted_stage = stage_map.get(str(settle.get('leg', '')).upper())
            if wanted_stage and v['stage'] == wanted_stage:
                by_ticker[t] = v
                continue
        # Otherwise keep latest as fallback
        if t not in by_ticker:
            by_ticker[t] = v

    tp = fp = fn = tn = 0
    saved_loss = sacrificed_win = 0
    no_settle = 0
    rows = []
    for t, v in by_ticker.items():
        settle = settle_events.get(t)
        if not settle:
            no_settle += 1
            continue
        won = (settle.get('result') == settle.get('side') or
               settle.get('outcome') == settle.get('side'))
        net_c = settle.get('net_cents', 0)
        if v['would_skip']:
            if not won:
                tp += 1; saved_loss += net_c
            else:
                fp += 1; sacrificed_win += net_c
        else:
            if not won:
                fn += 1
            else:
                tn += 1
        rows.append((t, v['stage'], v['would_skip'], won, net_c, v['p_model'],
                     v['engine_side'], v['engine_price']))

    print('======== CONFUSION MATRIX (engine\'s POV) ========')
    print('  TP = veto skipped a loser (good)')
    print('  FP = veto skipped a winner (bad — sacrificed gain)')
    print('  FN = veto missed a loser  (regression)')
    print('  TN = veto did not skip a winner (good)')
    print()
    print(f'           |  Won  |  Lost  |')
    print(f'  Skip     | {fp:>4d} (FP) | {tp:>4d} (TP) |')
    print(f'  Not skip | {tn:>4d} (TN) | {fn:>4d} (FN) |')
    print()
    total = tp + fp + fn + tn
    if total:
        precision = tp / max(tp + fp, 1)
        recall = tp / max(tp + fn, 1)
        print(f'  Precision (when veto says skip, how often is it a loser?): {precision*100:.1f}%')
        print(f'  Recall (of all losers, how many did veto catch?):           {recall*100:.1f}%')
        print(f'  Total settled+vetoed: {total}, missing settles: {no_settle}')
    print()
    print(f'======== P&L IMPACT (if all `would_skip` had been live) ========')
    print(f'  Saved loss (skipped losers):   ${-saved_loss/100:+.2f}')
    print(f'  Sacrificed win (skipped wins): ${-sacrificed_win/100:.2f}')
    print(f'  Net swing from veto: ${(-saved_loss - sacrificed_win)/100:+.2f}')
    print()

    # Per-stage breakdown
    by_stage = defaultdict(lambda: dict(tp=0, fp=0, fn=0, tn=0))
    for t, stage, skip, won, _, _, _, _ in rows:
        bucket = by_stage[stage]
        if skip and not won: bucket['tp'] += 1
        elif skip and won: bucket['fp'] += 1
        elif not skip and not won: bucket['fn'] += 1
        else: bucket['tn'] += 1
    print('======== PER STAGE ========')
    print(f'{"stage":22s} {"TP":>4s} {"FP":>4s} {"FN":>4s} {"TN":>4s} {"precision":>10s} {"recall":>8s}')
    for stage, s in sorted(by_stage.items()):
        prec = s['tp'] / max(s['tp']+s['fp'], 1) * 100
        rec = s['tp'] / max(s['tp']+s['fn'], 1) * 100
        print(f'  {stage:22s} {s["tp"]:>4d} {s["fp"]:>4d} {s["fn"]:>4d} {s["tn"]:>4d} '
              f'{prec:>9.1f}% {rec:>7.1f}%')
    print()

    # Specific FP cases (where veto would have sacrificed a winner)
    fps = [r for r in rows if r[2] and r[3]]
    if fps:
        print('======== FALSE POSITIVES (veto would have skipped winners) ========')
        print(f'{"ticker":20s} {"stage":22s} {"side":4s} {"entry":>6s} {"net":>6s} {"p_model":>8s}')
        for t, stage, _, _, net, p, side, entry in fps[:20]:
            print(f'  {t[-15:]:20s} {stage:22s} {side:4s} {entry:>5}c {net:>5}c {p*100:>6.1f}%')
    return 0


if __name__ == '__main__':
    sys.exit(main())
