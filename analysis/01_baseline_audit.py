"""Audit step 1: realized P&L per engine, reported vs realistic execution.

Run from repo root:
    python -m analysis.01_baseline_audit

Output: per-engine and combined P&L; per-leg fill-rate breakdown for v5_unified
(the fill-rate skew that motivated the slippage adjustment).
"""
from collections import defaultdict
from .common import load_all, load_log, DATA_DIR, LIVE_TRADE_LOGS, SLIP


def main():
    rows = load_all()
    print(f'Loaded {len(rows)} total settles across {len(LIVE_TRADE_LOGS)} engines\n')

    # Realistic-P&L recompute (uses won/entry + slip)
    def realistic_net(r):
        eff = r['entry'] + SLIP
        ctr = r['ctr']
        gross = (100 - eff) * ctr if r['won'] else -eff * ctr
        fee = max(1, int(0.07 * eff * ctr / 100 * 100))
        return gross - fee

    print('Per-source: reported P&L vs realistic (+2c slip):')
    print(f'  {"src":12s} {"n":>3s} {"reported":>11s} {"realistic":>11s} {"delta":>8s}')
    grand_rep = grand_real = 0
    for label, _ in LIVE_TRADE_LOGS:
        sub = [r for r in rows if r['src'] == label]
        if not sub: continue
        rep = sum(r['net_c'] for r in sub) / 100
        real = sum(realistic_net(r) for r in sub) / 100
        grand_rep += rep; grand_real += real
        print(f'  {label:12s} {len(sub):3d} {rep:+11.2f} {real:+11.2f} {real-rep:+8.2f}')
    print(f'  {"COMBINED":12s} {len(rows):3d} {grand_rep:+11.2f} {grand_real:+11.2f} {grand_real-grand_rep:+8.2f}')
    print()

    # v5_unified fill-rate by leg — the execution-adverse-selection finding
    print('v5_unified fill rates by leg (the execution skew):')
    import json
    fills = defaultdict(lambda: dict(trigger=0, fill=0, no_fill=0))
    path = DATA_DIR / 'live_v5_unified_trades.jsonl'
    if path.exists():
        with path.open(encoding='utf-8', errors='ignore') as f:
            for line in f:
                try: e = json.loads(line)
                except: continue
                k = e.get('kind', '')
                # order matters: '_no_fill' suffix would also match '_fill'
                if k.endswith('_no_fill'):
                    fills[k[:-len('_no_fill')]]['no_fill'] += 1
                elif k.endswith('_fill'):
                    fills[k[:-len('_fill')]]['fill'] += 1
                elif k.endswith('_trigger'):
                    fills[k[:-len('_trigger')]]['trigger'] += 1
    print(f'  {"leg":25s} {"trigger":>8s} {"fill":>5s} {"no_fill":>8s} {"fill_rate":>10s}')
    for leg, s in sorted(fills.items(), key=lambda x: -x[1]['trigger']):
        tot = s['trigger']
        if not tot: continue
        fr = s['fill'] / tot * 100
        print(f'  {leg:25s} {tot:8d} {s["fill"]:5d} {s["no_fill"]:8d} {fr:9.1f}%')
    print()
    print('Note: T-30 sniper has the validated 44/44 backtest edge but the lowest')
    print('fill rate, while EARLIER_MODERATE (the loss-driving leg) fills most often.')
    print('This is execution adverse selection — the market lets us in only when our')
    print('information is stale, which is why every backtest projection overstates real P&L.')


if __name__ == '__main__':
    main()
