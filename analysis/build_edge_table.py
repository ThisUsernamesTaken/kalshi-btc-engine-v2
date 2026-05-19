"""Extract a versioned edge_table.json from live trade logs.

The edge table is the source of truth used by deploy-time sizing rules:
for each (engine_family, leg/bar_segment, entry_bucket, sv60_bucket) bucket,
it records the realized WR, n, edge, action (KEEP/INVERT/SKIP), and
recommended Kelly fraction.

The table is recomputed from the data; do not hand-edit edge_table.json.

Run:
    python -m analysis.build_edge_table             # write to analysis/edge_table.json
    python -m analysis.build_edge_table --dry-run   # print to stdout, do not write

Convention: increment SCHEMA_VERSION when adding/removing fields. Bump
TABLE_VERSION whenever the bucketing logic in common.py changes.
"""
from __future__ import annotations
import argparse, datetime, hashlib, json, os
from pathlib import Path
from .common import (load_all, build_bucket_info, LIVE_TRADE_LOGS,
                     DATA_DIR, SLIP, sv60_bucket, entry_bucket, pine_bar_segment)

SCHEMA_VERSION = 1
TABLE_VERSION = 1


def fingerprint_inputs() -> dict:
    """Record sizes/mtimes of every input log so a stale table is detectable."""
    out = {}
    for label, fname in LIVE_TRADE_LOGS:
        p = DATA_DIR / fname
        if not p.exists():
            out[label] = None; continue
        st = p.stat()
        # Hash first/last 64KB and full size — cheap drift detector, no full read
        with p.open('rb') as f:
            head = f.read(65536)
            f.seek(max(0, st.st_size - 65536))
            tail = f.read(65536)
        out[label] = dict(
            path=str(p),
            size_bytes=st.st_size,
            mtime_iso=datetime.datetime.fromtimestamp(st.st_mtime, datetime.timezone.utc).isoformat(),
            head_tail_sha256=hashlib.sha256(head + tail).hexdigest()[:16],
        )
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true',
                    help='print to stdout instead of writing edge_table.json')
    ap.add_argument('--min-n', type=int, default=3,
                    help='minimum trades per bucket to include (default 3)')
    ap.add_argument('--out', type=str,
                    default=str(Path(__file__).resolve().parent / 'edge_table.json'))
    args = ap.parse_args()

    rows = load_all()
    info = build_bucket_info(rows, min_n=args.min_n)

    # Convert bucket-key tuples to a JSON-friendly form
    buckets = []
    for k, b in sorted(info.items(), key=lambda kv: -kv[1]['edge'] * kv[1]['n']):
        engine_family, leg_or_bar, e_b, sv_b = k
        buckets.append(dict(
            key=dict(
                engine_family=engine_family,
                leg_or_bar=leg_or_bar,
                entry_bucket=e_b,
                sv60_bucket=sv_b,
            ),
            n=b['n'],
            WR_engine=round(b['WR_engine'], 4),
            WR_effective=round(b['WR_effective'], 4),  # = (1 - WR_engine) for INVERT
            avg_entry_c=round(b['avg_entry'], 2),
            edge_per_ct_c=round(b['edge'], 3),
            kelly_fraction=round(b['kelly'], 4),
            action=b['action'],
        ))

    # Aggregate stats — useful for downstream sanity checks
    n_total = len(rows)
    n_keep = sum(1 for x in buckets if x['action'] == 'KEEP')
    n_invert = sum(1 for x in buckets if x['action'] == 'INVERT')
    trades_covered = sum(x['n'] for x in buckets)

    # Recommended discrete sizing tiers from the simulation analysis.
    # Tested as best-Pareto in analysis/03_sizing_simulation.py.
    sizing_recommendation = dict(
        scheme='dollar-at-risk discrete tiers + bucket streak halt',
        slip_cents=SLIP,
        risk_tiers_c=[
            dict(min_edge_per_ct_c=30, dollars_at_risk=12.0),
            dict(min_edge_per_ct_c=20, dollars_at_risk=6.0),
            dict(min_edge_per_ct_c=10, dollars_at_risk=3.0),
            dict(min_edge_per_ct_c=5,  dollars_at_risk=1.2),
        ],
        max_size_contracts=100,
        min_edge_per_ct_c=5,
        streak_halt_losses=2,
        streak_cooldown_trades=10,
        notes=('Risk-tier dollars scaled to a small starting bankroll. '
               'Scale linearly with bankroll. The streak halt is the load-bearing '
               'piece of drawdown control — without it, big tiers blow up during '
               'the 2026-05-16 EM-cursed-stripe loss cluster.'),
    )

    out = dict(
        schema_version=SCHEMA_VERSION,
        table_version=TABLE_VERSION,
        generated_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        slip_cents=SLIP,
        min_bucket_n=args.min_n,
        input_fingerprint=fingerprint_inputs(),
        totals=dict(
            n_settles=n_total,
            n_buckets=len(buckets),
            n_keep_buckets=n_keep,
            n_invert_buckets=n_invert,
            trades_covered=trades_covered,
        ),
        bucketing_spec=dict(
            sv60_thresholds=dict(rev_max=-30, flat_max=30, mom_lo_max=80),
            entry_thresholds=dict(lottery_max=15, underdog_max=70, mid_max=84, cursed_max=91),
            pine_bar_thresholds=dict(early_max=4, sweet_max=6),
        ),
        sizing_recommendation=sizing_recommendation,
        buckets=buckets,
    )

    text = json.dumps(out, indent=2)
    if args.dry_run:
        print(text)
        return
    Path(args.out).write_text(text, encoding='utf-8')
    print(f'wrote {args.out}  ({len(buckets)} buckets, {trades_covered}/{n_total} trades covered)')


if __name__ == '__main__':
    main()
