"""Audit step 2: bucket-conditional edge table.

For each (engine_family, leg_or_bar, entry_bucket, sv60_bucket) bucket,
compute realized WR and decide:
  - KEEP if engine WR > breakeven-WR (true edge in engine's direction)
  - INVERT if engine WR < breakeven-WR by enough that the opposite side
    becomes positive-EV (the inverse trade pays better than the original)
  - SKIP otherwise

This is the core discovery of the analysis: the engine has a sign problem
(systematic bias in the mid-conviction stripe), not just a sizing problem.

Run:
    python -m analysis.02_bucket_edge_table
"""
from .common import load_all, build_bucket_info, SLIP


def main():
    rows = load_all()
    info = build_bucket_info(rows, min_n=3)
    print(f'Loaded {len(rows)} settles; {len(info)} buckets met n>=3 threshold\n')

    print('======== BUCKET-CONDITIONAL EDGE TABLE (slip=+2c) ========')
    print(f'{"bucket":58s} {"n":>3s} {"WR":>6s} {"avg_e":>6s} {"edge_c":>7s} {"action":>7s} {"kelly":>7s}')
    rows_sorted = sorted(info.items(), key=lambda kv: -kv[1]['edge'] * kv[1]['n'])
    for k, b in rows_sorted:
        name = '/'.join(map(str, k))
        print(f'  {name:56s} {b["n"]:3d} {b["WR_engine"]*100:5.1f}% '
              f'{b["avg_entry"]:6.1f}c {b["edge"]:+6.1f}c {b["action"]:>7s} '
              f'{b["kelly"]*100:6.1f}%')

    print()
    print('Top KEEP buckets (engine has true edge):')
    keeps = [(k, b) for k, b in info.items() if b['action'] == 'KEEP']
    for k, b in sorted(keeps, key=lambda x: -x[1]['edge']):
        print(f'  {"/".join(map(str,k)):56s} n={b["n"]:3d} WR={b["WR_engine"]*100:5.1f}% '
              f'edge=+{b["edge"]:.1f}c/ct')

    print()
    print('Top INVERT buckets (engine is systematically wrong here):')
    invs = [(k, b) for k, b in info.items() if b['action'] == 'INVERT']
    for k, b in sorted(invs, key=lambda x: -x[1]['edge']):
        print(f'  {"/".join(map(str,k)):56s} n={b["n"]:3d} engine_WR={b["WR_engine"]*100:5.1f}% '
              f'inv_edge=+{b["edge"]:.1f}c/ct')

    # Summary stat: total expected edge with discrete sizing
    print()
    print('Total realised edge if every trade had been sized 10ct:')
    total = 0
    for r in rows:
        from .common import bucket_key
        b = info.get(bucket_key(r))
        if not b: continue
        if b['action'] == 'INVERT':
            eff_entry = 100 - r['entry'] + SLIP
            won = not r['won']
        else:
            eff_entry = r['entry'] + SLIP
            won = r['won']
        gross = (100 - eff_entry) * 10 if won else -eff_entry * 10
        fee = max(1, int(0.07 * eff_entry * 10 / 100 * 100))
        total += gross - fee
    print(f'  Fixed-10ct, edge-routed (KEEP/INVERT): ${total/100:+.2f}')


if __name__ == '__main__':
    main()
