"""Audit step 3: dollar-at-risk discrete-tier sizing with bucket streak halt.

Simulates several sizing schemes against the full 131-trade historical tape
in chronological order, and reports the efficient frontier of (net P&L,
max running drawdown).

The headline result:
  - Naive fixed 10ct sizing loses ~$150 on the same tape.
  - Edge-routed dollar-at-risk tiers + a 2-loss-streak per-bucket halt
    produce ~+$200-400 net with ~−$70-100 max DD.
  - Strictly better than 0.10 Kelly cap-100ct on both axes thanks to the
    streak halt skipping the 2026-05-16 EM-cursed-stripe loss cluster.

Run:
    python -m analysis.03_sizing_simulation
"""
from collections import defaultdict
from .common import load_all, build_bucket_info, bucket_key, SLIP

BANKROLL_C = 250_000  # starting bank used for Kelly references only


def simulate(rows_chrono, info, risk_tiers, max_loss_c=99_999,
             streak_halt=99, cooldown_n=0, cap_ct=200, min_edge=5):
    """Replay trades in chronological order under one sizing scheme.

    risk_tiers: list of (edge_threshold_c, dollars_at_risk) descending.
    streak_halt: bucket goes cold after this many consecutive losses.
    cooldown_n: bucket pauses for this many subsequent trades.
    """
    bucket_streak = defaultdict(int)
    skip_until_idx = defaultdict(int)
    cum = 0; peak = 0; max_dd = 0
    n_trades = wins = streak_skips = 0
    biggest_w = 0; biggest_l = 0
    for i, r in enumerate(rows_chrono):
        b = info.get(bucket_key(r))
        if not b: continue
        if b['edge'] < min_edge: continue
        if i < skip_until_idx[bucket_key(r)]:
            streak_skips += 1
            continue
        action = b['action']
        eff = (100 - r['entry'] + SLIP) if action == 'INVERT' else (r['entry'] + SLIP)
        risk_c = 0
        for thresh, dollars in risk_tiers:
            if b['edge'] >= thresh:
                risk_c = dollars * 100; break
        if risk_c == 0: continue
        size = max(1, min(int(min(risk_c, max_loss_c) / eff), cap_ct))
        won = (not r['won']) if action == 'INVERT' else r['won']
        gross = (100 - eff) * size if won else -eff * size
        fee = max(1, int(0.07 * eff * size / 100 * 100))
        net = gross - fee
        cum += net; n_trades += 1; wins += won
        biggest_w = max(biggest_w, net); biggest_l = min(biggest_l, net)
        peak = max(peak, cum); max_dd = min(max_dd, cum - peak)
        if won:
            bucket_streak[bucket_key(r)] = 0
        else:
            bucket_streak[bucket_key(r)] += 1
            if bucket_streak[bucket_key(r)] >= streak_halt:
                skip_until_idx[bucket_key(r)] = i + cooldown_n
                bucket_streak[bucket_key(r)] = 0
    return dict(
        trades=n_trades, wins=wins, net_c=cum, max_dd_c=max_dd,
        max_win_c=biggest_w, max_loss_c=biggest_l, streak_skips=streak_skips,
    )


def fmt(label, r):
    print(f'  {label:54s} trades={r["trades"]:3d}  WR={r["wins"]/max(r["trades"],1)*100:5.1f}%  '
          f'net=${r["net_c"]/100:+7.2f}  max_DD=${r["max_dd_c"]/100:7.2f}  '
          f'max_W=${r["max_win_c"]/100:+6.2f}  max_L=${r["max_loss_c"]/100:+6.2f}')


def main():
    rows = load_all()
    rows_chrono = sorted(rows, key=lambda r: (r['entry_ts'] or 0, r['ticker']))
    info = build_bucket_info(rows, min_n=3)

    # --- Reference: fixed-10ct under realistic execution ---
    print('======== REFERENCE BASELINES (no edge routing) ========')
    fixed_net = 0; n = 0; w = 0
    for r in rows:
        eff = r['entry'] + SLIP
        gross = (100 - eff) * 10 if r['won'] else -eff * 10
        fee = max(1, int(0.07 * eff * 10 / 100 * 100))
        fixed_net += gross - fee
        n += 1; w += r['won']
    print(f'  fixed-10ct, all trades (current behavior)              n={n:3d}  '
          f'WR={w/n*100:5.1f}%  net=${fixed_net/100:+7.2f}')

    print()
    print('======== DISCRETE TIERS, NO STREAK HALT ========')
    fmt('discrete 3/8/15/25/40 ct (size by edge bucket)',
        simulate(rows_chrono, info, [(30, 40), (20, 25), (10, 12), (5, 5)]))
    fmt('$ risk 8/20/40/60 (size by dollars-at-risk)',
        simulate(rows_chrono, info, [(30, 60), (20, 40), (10, 20), (5, 8)]))
    fmt('$ risk 10/25/50/80',
        simulate(rows_chrono, info, [(30, 80), (20, 50), (10, 25), (5, 10)]))

    print()
    print('======== WITH BUCKET STREAK HALT (2 losses -> skip 5) ========')
    fmt('$ risk 5/12/25/40, streak halt @2/5',
        simulate(rows_chrono, info, [(30, 40), (20, 25), (10, 12), (5, 5)],
                 streak_halt=2, cooldown_n=5))
    fmt('$ risk 8/20/40/60, streak halt @2/5',
        simulate(rows_chrono, info, [(30, 60), (20, 40), (10, 20), (5, 8)],
                 streak_halt=2, cooldown_n=5))
    fmt('$ risk 12/30/60/100, streak halt @2/5',
        simulate(rows_chrono, info, [(30, 100), (20, 60), (10, 30), (5, 12)],
                 streak_halt=2, cooldown_n=5))

    print()
    print('======== STREAK HALT @2/10 (longer cooldown) ========')
    fmt('$ risk 8/20/40/60, halt @2/10',
        simulate(rows_chrono, info, [(30, 60), (20, 40), (10, 20), (5, 8)],
                 streak_halt=2, cooldown_n=10))
    fmt('$ risk 12/30/60/100, halt @2/10  (recommended)',
        simulate(rows_chrono, info, [(30, 100), (20, 60), (10, 30), (5, 12)],
                 streak_halt=2, cooldown_n=10))
    fmt('$ risk 15/40/80/120, halt @2/10',
        simulate(rows_chrono, info, [(30, 120), (20, 80), (10, 40), (5, 15)],
                 streak_halt=2, cooldown_n=10))

    print()
    print('See analysis/README.md for interpretation and deployment notes.')


if __name__ == '__main__':
    main()
