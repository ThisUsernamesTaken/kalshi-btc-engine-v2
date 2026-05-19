"""Walk-forward validation: build the edge table only from data BEFORE each
fold, then apply it to the fold. This is the only honest test of whether the
bucket-conditional edge survives out-of-sample.

The full 03_sizing_simulation.py uses in-sample bucket WRs, which inflates
projected P&L. This script splits trades by date into fold_days-wide windows
and replays each fold using a table built from prior folds only.

Run:
    python -m analysis.04_walk_forward
"""
from collections import defaultdict
from .common import (load_all, build_bucket_info, bucket_key, date_bucket,
                     date_to_fold, SLIP)


def simulate_fold(rows_chrono, info, risk_tiers, streak_halt=2, cooldown_n=10,
                  cap_ct=100, min_edge=5):
    """Replay a single fold under the supplied sizing rule + edge table."""
    bucket_streak = defaultdict(int); skip_until = defaultdict(int)
    cum=0; peak=0; max_dd=0; n=0; w=0
    biggest_w=0; biggest_l=0
    for i, r in enumerate(rows_chrono):
        b = info.get(bucket_key(r))
        if not b or b['edge'] < min_edge: continue
        if i < skip_until[bucket_key(r)]: continue
        action = b['action']
        eff = (100 - r['entry'] + SLIP) if action=='INVERT' else (r['entry'] + SLIP)
        risk_c = 0
        for thresh, dollars in risk_tiers:
            if b['edge'] >= thresh: risk_c = dollars*100; break
        if risk_c == 0: continue
        size = max(1, min(int(risk_c/eff), cap_ct))
        won = (not r['won']) if action=='INVERT' else r['won']
        gross = (100-eff)*size if won else -eff*size
        fee = max(1, int(0.07*eff*size/100*100))
        net = gross - fee
        cum += net; n += 1; w += won
        biggest_w = max(biggest_w, net); biggest_l = min(biggest_l, net)
        peak = max(peak, cum); max_dd = min(max_dd, cum-peak)
        if won: bucket_streak[bucket_key(r)] = 0
        else:
            bucket_streak[bucket_key(r)] += 1
            if bucket_streak[bucket_key(r)] >= streak_halt:
                skip_until[bucket_key(r)] = i + cooldown_n
                bucket_streak[bucket_key(r)] = 0
    return dict(trades=n, wins=w, net_c=cum, max_dd_c=max_dd,
                max_win_c=biggest_w, max_loss_c=biggest_l)


def run_walk_forward(rows, risk_tiers, fold_days, min_bucket_n, min_edge,
                     streak_halt=2, cooldown_n=10, label=''):
    """Single walk-forward run; returns (oos_net, oos_dd, in_sample_net, in_sample_dd, oos_trades)."""
    by_fold = defaultdict(list)
    for r in rows:
        fold = date_to_fold(date_bucket(r), fold_days=fold_days)
        by_fold[fold].append(r)
    fold_keys = sorted(k for k in by_fold if k != 'unknown')
    cum_net = 0; running = 0; running_min = 0
    total_n = total_w = 0
    for i, k in enumerate(fold_keys):
        prior_rows = []
        for k2 in fold_keys[:i]:
            prior_rows.extend(by_fold[k2])
        if len(prior_rows) < 30: continue
        info = build_bucket_info(prior_rows, min_n=min_bucket_n)
        fold_rows = by_fold[k]
        res = simulate_fold(fold_rows, info, risk_tiers,
                            streak_halt=streak_halt, cooldown_n=cooldown_n,
                            min_edge=min_edge)
        cum_net += res['net_c']; running += res['net_c']
        if running < running_min: running_min = running
        total_n += res['trades']; total_w += res['wins']
    # in-sample reference
    full_info = build_bucket_info(rows, min_n=min_bucket_n)
    is_ = simulate_fold(rows, full_info, risk_tiers, streak_halt=streak_halt,
                        cooldown_n=cooldown_n, min_edge=min_edge)
    return dict(oos_net_c=cum_net, oos_running_min_c=running_min,
                oos_trades=total_n, oos_wins=total_w,
                is_net_c=is_['net_c'], is_dd_c=is_['max_dd_c'], is_trades=is_['trades'])


def main():
    rows = load_all()
    rows = sorted(rows, key=lambda r: (r['entry_ts'] or 0, r['ticker']))
    print(f'Total settles: {len(rows)}')

    # Group by fold; each fold = 2 calendar days
    FOLD_DAYS = 2
    by_fold = defaultdict(list)
    for r in rows:
        fold = date_to_fold(date_bucket(r), fold_days=FOLD_DAYS)
        by_fold[fold].append(r)
    fold_keys = sorted(by_fold.keys())
    fold_keys = [k for k in fold_keys if k != 'unknown']
    print(f'Folds ({FOLD_DAYS} days each): {len(fold_keys)}')
    print(f'  {"fold":12s}  n  range')
    for k in fold_keys:
        sub = by_fold[k]
        d0 = date_bucket(sub[0]); d1 = date_bucket(sub[-1])
        print(f'  {k:12s} {len(sub):3d}  {d0} to {d1}')
    print()

    # Strategy variants for walk-forward
    RISK_TIERS = [(30, 100), (20, 60), (10, 30), (5, 12)]
    print(f'Sizing: $risk 12/30/60/100, streak halt @2/10  (the recommended scheme)\n')
    print(f'{"fold":12s} {"train_n":>7s} {"fold_n":>6s} {"buckets":>7s} {"in-fold_n":>9s} {"WR":>6s} {"net_$":>9s} {"DD_$":>9s} {"cum_net_$":>10s}')
    cum_net = 0; cum_dd = 0; total_n = 0; total_w = 0; running = 0; running_min = 0
    fold_totals = []
    for i, k in enumerate(fold_keys):
        prior_rows = []
        for k2 in fold_keys[:i]:
            prior_rows.extend(by_fold[k2])
        if len(prior_rows) < 30:
            print(f'  {k:12s} (insufficient training data, n_prior={len(prior_rows)})')
            continue
        info = build_bucket_info(prior_rows, min_n=3)
        fold_rows = by_fold[k]
        res = simulate_fold(fold_rows, info, RISK_TIERS, streak_halt=2, cooldown_n=10)
        cum_net += res['net_c']
        running += res['net_c']
        if running < running_min: running_min = running
        print(f'  {k:12s} {len(prior_rows):>7d} {len(fold_rows):>6d} {len(info):>7d} '
              f'{res["trades"]:>9d} {res["wins"]/max(res["trades"],1)*100:5.1f}% '
              f'{res["net_c"]/100:>+8.2f} {res["max_dd_c"]/100:>+8.2f} {running/100:>+9.2f}')
        fold_totals.append(res)
        total_n += res['trades']; total_w += res['wins']

    print()
    print(f'WALK-FORWARD TOTAL: trades={total_n}  WR={total_w/max(total_n,1)*100:5.1f}%  '
          f'net=${cum_net/100:+.2f}  running_min=${running_min/100:.2f}')

    # Compare to fully-in-sample reference using same rules
    full_info = build_bucket_info(rows, min_n=3)
    in_sample = simulate_fold(rows, full_info, RISK_TIERS, streak_halt=2, cooldown_n=10)
    print(f'IN-SAMPLE REFERENCE:  trades={in_sample["trades"]}  '
          f'WR={in_sample["wins"]/max(in_sample["trades"],1)*100:5.1f}%  '
          f'net=${in_sample["net_c"]/100:+.2f}  max_DD=${in_sample["max_dd_c"]/100:.2f}')
    print()
    print(f'Overfitting tax: ${(in_sample["net_c"] - cum_net)/100:+.2f} '
          f'({(1 - cum_net/in_sample["net_c"])*100:+.1f}% of in-sample)' if in_sample['net_c'] else '')

    # === LIVE-ONLY walk-forward: do live engines alone produce stable edges? ===
    print()
    print('======== LIVE-ONLY WALK-FORWARD (paper sources excluded) ========')
    live_rows = [r for r in rows if r['src'] in ('v5_unified','v5_old','live_ta','live_ta_v2')]
    print(f'Live trades: {len(live_rows)}')
    if len(live_rows) > 30:
        configs_live = [
            ('live 5/12/25/40, 1d folds, n>=5, e>=5', [(30,40),(20,25),(10,12),(5,5)], 1, 5, 5),
            ('live 5/12/25/40, 1d folds, n>=8, e>=8', [(30,40),(20,25),(10,12),(5,5)], 1, 8, 8),
        ]
        for label, tiers, fd, mn, me in configs_live:
            res = run_walk_forward(live_rows, tiers, fold_days=fd,
                                   min_bucket_n=mn, min_edge=me)
            print(f'  {label:50s} OOS_n={res["oos_trades"]:>3d}  '
                  f'WR={res["oos_wins"]/max(res["oos_trades"],1)*100:5.1f}%  '
                  f'OOS_net=${res["oos_net_c"]/100:+8.2f}  '
                  f'run_min=${res["oos_running_min_c"]/100:+8.2f}  '
                  f'IS_net=${res["is_net_c"]/100:+8.2f}')

    # === SKIP-ONLY walk-forward: no inverts; just skip negative-edge buckets ===
    print()
    print('======== SKIP-ONLY WALK-FORWARD (no inverts; safer subset) ========')
    # Build a skip-only fold simulator by post-filtering: only KEEP actions are taken
    by_fold = defaultdict(list)
    for r in rows:
        fold = date_to_fold(date_bucket(r), fold_days=1)
        by_fold[fold].append(r)
    fold_keys_s = sorted(k for k in by_fold if k != 'unknown')

    def skip_only_sim(rows_in_fold, info, risk_tiers):
        cum=0; peak=0; mdd=0; n=0; w=0
        for r in rows_in_fold:
            b = info.get(bucket_key(r))
            if not b or b['action'] != 'KEEP': continue
            if b['edge'] < 5: continue
            eff = r['entry'] + SLIP
            risk_c = 0
            for thresh, dollars in risk_tiers:
                if b['edge'] >= thresh: risk_c = dollars*100; break
            if risk_c == 0: continue
            size = max(1, min(int(risk_c/eff), 100))
            won = r['won']
            gross = (100-eff)*size if won else -eff*size
            fee = max(1, int(0.07*eff*size/100*100))
            cum += gross - fee
            n += 1; w += won
            peak = max(peak, cum); mdd = min(mdd, cum-peak)
        return cum, mdd, n, w

    for tiers, label in [
        ([(30,40),(20,25),(10,12),(5,5)], 'skip-only 5/12/25/40, n>=5'),
        ([(30,25),(20,15),(10,8),(5,3)], 'skip-only 3/8/15/25, n>=10'),
    ]:
        min_n = int(label.split('n>=')[-1])
        cum_net = 0; running_min = 0; running = 0; total_n = 0; total_w = 0
        for i, k in enumerate(fold_keys_s):
            prior = []
            for k2 in fold_keys_s[:i]: prior.extend(by_fold[k2])
            if len(prior) < 30: continue
            info = build_bucket_info(prior, min_n=min_n)
            c, dd, n, w = skip_only_sim(by_fold[k], info, tiers)
            cum_net += c; running += c
            if running < running_min: running_min = running
            total_n += n; total_w += w
        print(f'  {label:50s} OOS_n={total_n:>3d}  '
              f'WR={total_w/max(total_n,1)*100:5.1f}%  '
              f'OOS_net=${cum_net/100:+8.2f}  run_min=${running_min/100:+8.2f}')

    # === Parameter sweep: which configuration is OOS-robust? ===
    print()
    print('======== PARAMETER SWEEP (OOS via walk-forward) ========')
    print(f'{"config":50s} {"OOS_n":>6s} {"OOS_WR":>7s} {"OOS_net":>9s} {"OOS_run_min":>11s} {"IS_net":>9s} {"tax":>9s}')
    configs = [
        # (label, risk_tiers, fold_days, min_n, min_edge)
        ('default 12/30/60/100, 2d folds, n>=3, e>=5', [(30,100),(20,60),(10,30),(5,12)], 2, 3, 5),
        ('default 12/30/60/100, 1d folds, n>=3, e>=5', [(30,100),(20,60),(10,30),(5,12)], 1, 3, 5),
        ('conservative 5/12/25/40, 2d folds, n>=10, e>=8', [(30,40),(20,25),(10,12),(5,5)], 2, 10, 8),
        ('conservative 5/12/25/40, 1d folds, n>=10, e>=8', [(30,40),(20,25),(10,12),(5,5)], 1, 10, 8),
        ('strict 3/8/15/25 ct, 2d folds, n>=15, e>=10', [(30,25),(20,15),(10,8),(5,3)], 2, 15, 10),
        ('strict 3/8/15/25 ct, 1d folds, n>=15, e>=10', [(30,25),(20,15),(10,8),(5,3)], 1, 15, 10),
        ('ultra-strict 2/5/10/15 ct, 1d folds, n>=20, e>=12', [(30,15),(20,10),(10,5),(5,2)], 1, 20, 12),
    ]
    for label, tiers, fd, mn, me in configs:
        res = run_walk_forward(rows, tiers, fold_days=fd, min_bucket_n=mn, min_edge=me)
        tax = res['is_net_c'] - res['oos_net_c']
        print(f'  {label:50s} {res["oos_trades"]:>6d} '
              f'{res["oos_wins"]/max(res["oos_trades"],1)*100:>6.1f}% '
              f'{res["oos_net_c"]/100:>+8.2f} {res["oos_running_min_c"]/100:>+10.2f} '
              f'{res["is_net_c"]/100:>+8.2f} {tax/100:>+8.2f}')

    # Bucket-stability across folds: which buckets persist?
    print()
    print('======== BUCKET STABILITY ACROSS FOLDS ========')
    bucket_appearance = defaultdict(lambda: dict(folds=0, total_n=0, total_w=0,
                                                  edge_vals=[], actions=[]))
    for i, k in enumerate(fold_keys):
        prior_rows = []
        for k2 in fold_keys[:i]:
            prior_rows.extend(by_fold[k2])
        if len(prior_rows) < 30: continue
        info = build_bucket_info(prior_rows, min_n=3)
        for bk, b in info.items():
            bucket_appearance[bk]['folds'] += 1
            bucket_appearance[bk]['total_n'] += b['n']
            bucket_appearance[bk]['total_w'] += int(b['n'] * b['WR_engine'])
            bucket_appearance[bk]['edge_vals'].append(b['edge'])
            bucket_appearance[bk]['actions'].append(b['action'])

    n_folds_eval = len([k for i,k in enumerate(fold_keys) if sum(len(by_fold[k2]) for k2 in fold_keys[:i]) >= 30])
    print(f'Across {n_folds_eval} evaluated folds:')
    print(f'{"bucket":58s} {"folds":>6s} {"action_flips":>13s} {"edge_min..max":>16s}')
    # Buckets that appeared in many folds
    for bk, info in sorted(bucket_appearance.items(),
                            key=lambda x: -x[1]['folds'])[:20]:
        actions = info['actions']
        flips = sum(1 for j in range(1, len(actions)) if actions[j] != actions[j-1])
        e_min = min(info['edge_vals']); e_max = max(info['edge_vals'])
        bn = '/'.join(map(str, bk))
        print(f'  {bn:56s} {info["folds"]:>6d} {flips:>13d} {e_min:+6.1f}..{e_max:+6.1f}')


if __name__ == '__main__':
    main()
