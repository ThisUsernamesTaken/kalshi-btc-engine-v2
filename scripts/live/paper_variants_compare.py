"""Compare paper-trade variants side by side.

Reads each variant's decision log and reports head-to-head:
  - Trigger counts (engine-side decisions before veto)
  - Veto action distribution
  - Settle outcomes
  - Realized P&L
  - First markets where the variants diverge

Usage:
    python scripts/live/paper_variants_compare.py
    python scripts/live/paper_variants_compare.py --since 2026-05-19T18:00:00Z
"""
from __future__ import annotations
import argparse, json, sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


VARIANTS = [
    ('baseline',    'paper_baseline_trades.jsonl'),
    ('veto_skip',   'paper_veto_skip_trades.jsonl'),
    ('veto_flip',   'paper_veto_flip_trades.jsonl'),
    ('veto_flip_EM','paper_veto_flip_EM_trades.jsonl'),
]


def parse_iso(s: str) -> int:
    if s.endswith('Z'): s = s.replace('Z', '+00:00')
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def load_variant(path: Path, since_ms: int):
    stats = dict(
        triggers=0, fills=0, skips=0, flips_attempts=0, flips_fills=0,
        veto_keep=0, veto_skip=0, veto_flip=0, veto_errors=0,
        settles=[], n_lines=0,
    )
    if not path.exists():
        return stats
    for line in path.open(encoding='utf-8', errors='ignore'):
        stats['n_lines'] += 1
        try: e = json.loads(line)
        except: continue
        ts = e.get('ts_ms', 0)
        if ts < since_ms: continue
        k = e.get('kind', '')
        if k.endswith('_trigger'):
            stats['triggers'] += 1
        elif k in ('early_fill', 'earlier_moderate_fill', 'late_fill', 't30_sniper_fill'):
            stats['fills'] += 1
        elif k in ('early_skip', 'earlier_moderate_skip', 'late_skip', 't30_sniper_skip'):
            stats['skips'] += 1
        elif k == 'model_veto_flip_attempt':
            stats['flips_attempts'] += 1
        elif k == 'model_veto_flip_fill':
            stats['flips_fills'] += 1
        elif k == 'model_veto':
            action = e.get('action') or ('SKIP' if e.get('would_skip') else 'KEEP')
            stats[f'veto_{action.lower()}'] += 1
        elif k == 'model_veto_error':
            stats['veto_errors'] += 1
        elif k == 'settle':
            stats['settles'].append(e)
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data-dir', default=r'C:/Trading/kalshi-btc-engine-v2/data')
    ap.add_argument('--since', default=None, help='ISO 8601 UTC timestamp')
    args = ap.parse_args()

    since_ms = parse_iso(args.since) if args.since else 0
    data_dir = Path(args.data_dir)

    all_stats = {}
    for name, fn in VARIANTS:
        all_stats[name] = load_variant(data_dir / fn, since_ms)

    print(f'Window: {"start of logs" if since_ms == 0 else args.since}')
    print()

    # Activity summary
    print(f'{"variant":15s} {"lines":>7s} {"triggers":>9s} {"fills":>6s} {"skips":>6s} '
          f'{"v_KEEP":>7s} {"v_SKIP":>7s} {"v_FLIP":>7s} {"flip_fills":>10s} {"errors":>7s} {"settles":>8s}')
    for name, _ in VARIANTS:
        s = all_stats[name]
        print(f'{name:15s} {s["n_lines"]:>7d} {s["triggers"]:>9d} {s["fills"]:>6d} {s["skips"]:>6d} '
              f'{s["veto_keep"]:>7d} {s["veto_skip"]:>7d} {s["veto_flip"]:>7d} {s["flips_fills"]:>10d} '
              f'{s["veto_errors"]:>7d} {len(s["settles"]):>8d}')

    # Realized P&L per variant
    print()
    print('======== REALIZED P&L (--dry-run; figured from settle net_cents) ========')
    print(f'{"variant":15s} {"settles":>8s} {"wins":>5s} {"losses":>7s} {"WR":>6s} {"net_$":>9s}')
    for name, _ in VARIANTS:
        s = all_stats[name]
        settles = s['settles']
        if not settles:
            print(f'{name:15s} {0:>8d}    --     --     --      --')
            continue
        n = len(settles)
        nets = [int(x.get('net_cents', 0)) for x in settles]
        wins = sum(1 for x in settles if (x.get('result') or x.get('outcome')) == x.get('side'))
        net = sum(nets)
        print(f'{name:15s} {n:>8d} {wins:>5d} {n-wins:>7d} {wins/n*100:>5.1f}% ${net/100:>+8.2f}')

    # Per-ticker divergence: where do variants disagree?
    print()
    print('======== DIVERGENCE SUMMARY ========')
    by_t_by_v: dict[str, dict[str, str]] = defaultdict(dict)
    for name, _ in VARIANTS:
        for s in all_stats[name]['settles']:
            t = s['ticker']
            side = s.get('side')
            net = int(s.get('net_cents', 0))
            leg = s.get('leg')
            by_t_by_v[t][name] = f'{leg}:{side}@{s.get("entry_price_cents")}c=${net/100:+.2f}'

    diverging = [t for t, v in by_t_by_v.items() if len(set(v.values())) > 1]
    print(f'  Tickers settled in any variant: {len(by_t_by_v)}')
    print(f'  Tickers where outcomes diverge: {len(diverging)}')

    if diverging:
        print('\n  Divergent tickers (first 10):')
        for t in diverging[:10]:
            print(f'    {t}')
            for name, _ in VARIANTS:
                v = by_t_by_v[t].get(name, '(no trade)')
                print(f'      {name:15s} {v}')


if __name__ == '__main__':
    main()
