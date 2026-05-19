"""Fair-value-residual backtest using the gradient engine's per-tick p_model.

The gradient engine ran on 2026-05-18 and emitted ~11,880 decisions across 14
KXBTC15M markets, each with p_model_yes (Brownian-Bridge fair value, BRTI-
averaging-aware) and p_market_yes (from book quotes). The engine's strict
6-gate abstention rejected 100% of entries, but the underlying probability
model + residual is still a clean signal to backtest.

This script:
  1. Loads decisions from gradient engine captures
  2. Determines per-ticker outcome (yes_won) from last-minute book state
  3. For each tick, if |p_model_yes - p_market_yes| >= EDGE_THRESHOLD,
     simulates a single round-trip trade (one entry per market) at displayed
     ask + slip, holds to settle
  4. Applies Kalshi fee formula and compares thresholds

This is structural: the edge is computed from a mathematical model, not from
in-sample bucket WRs, so OOS generalization is the model's calibration, not
small-N noise.
"""
from __future__ import annotations
import json, math
from collections import defaultdict
from pathlib import Path

CAPTURE_DIR = Path(r'C:/Trading/kalshi_btc_gradient_engine/data/captures/2026-05-18')

# Realistic execution costs
SLIP = 2  # cents lifted above ask
def kalshi_fee_taker(price_c: int, contracts: int) -> int:
    """ceil(0.07 * C * P * (1-P)) in cents — Kalshi taker fee."""
    p = price_c / 100
    return math.ceil(0.07 * contracts * p * (1 - p) * 100)

def kalshi_fee_maker(price_c: int, contracts: int) -> int:
    """ceil(0.0175 * C * P * (1-P)) in cents — Kalshi maker fee (75% cheaper)."""
    p = price_c / 100
    return math.ceil(0.0175 * contracts * p * (1 - p) * 100)


def load_decisions() -> dict:
    """Group decisions by ticker; return per-ticker list of (ts, market_state)."""
    by_ticker = defaultdict(list)
    for fn in sorted(CAPTURE_DIR.glob('decisions_*.jsonl')):
        with fn.open(encoding='utf-8', errors='ignore') as f:
            for line in f:
                try: e = json.loads(line)
                except: continue
                ms = e.get('market_state') or {}
                t = ms.get('market_ticker')
                if not t: continue
                by_ticker[t].append((e.get('ts_ms') or 0, ms))
    for t in by_ticker:
        by_ticker[t].sort(key=lambda x: x[0])
    return dict(by_ticker)


# Ground-truth outcomes for the 16 markets in the 2026-05-18 capture.
# Fetched directly from Kalshi REST API; not relying on quote inference.
GROUND_TRUTH = {
    'KXBTC15M-26MAY181730-30': True,
    'KXBTC15M-26MAY181745-45': False,
    'KXBTC15M-26MAY181800-00': True,
    'KXBTC15M-26MAY181815-15': True,
    'KXBTC15M-26MAY181830-30': True,
    'KXBTC15M-26MAY181845-45': False,
    'KXBTC15M-26MAY181900-00': True,
    'KXBTC15M-26MAY181915-15': True,
    'KXBTC15M-26MAY181930-30': False,
    'KXBTC15M-26MAY181945-45': True,
    'KXBTC15M-26MAY182000-00': False,
    'KXBTC15M-26MAY182015-15': True,
    'KXBTC15M-26MAY182030-30': True,
    'KXBTC15M-26MAY182045-45': False,
    'KXBTC15M-26MAY182100-00': False,
    'KXBTC15M-26MAY182115-15': False,
}

def infer_outcome(events, ticker=None):
    if ticker and ticker in GROUND_TRUTH:
        return GROUND_TRUTH[ticker]
    """Determine yes_won from terminal book state. After expiry the ask collapses to 0 or 100c."""
    # Look at the last 5 events
    for ts, ms in reversed(events[-20:]):
        ya = ms.get('yes_ask'); nb = ms.get('no_bid')
        yb = ms.get('yes_bid'); na = ms.get('no_ask')
        # YES winning: yes_ask near 1, no_bid near 0
        if ya is not None and ya >= 0.95:
            return True
        if yb is not None and yb >= 0.95:
            return True
        # NO winning: yes_ask near 0
        if ya is not None and ya <= 0.05:
            return False
        if yb is not None and yb <= 0.05:
            return False
    # Alternate: use the model probability terminal value if quotes are stale
    for ts, ms in reversed(events[-10:]):
        pm = ms.get('p_market_yes')
        if pm is not None:
            return pm >= 0.5
    return None


def backtest_thresh(decisions: dict, edge_thresh: float, max_seconds_to_close: float = 600.0,
                    min_seconds_to_close: float = 30.0, contracts: int = 10,
                    one_trade_per_ticker: bool = True, use_maker: bool = False):
    """Trade when |edge_yes| >= edge_thresh; size = `contracts`; take_first_signal."""
    trades = []
    fee_fn = kalshi_fee_maker if use_maker else kalshi_fee_taker
    for ticker, evs in decisions.items():
        won = infer_outcome(evs, ticker)
        if won is None: continue
        taken = False
        for ts, ms in evs:
            if one_trade_per_ticker and taken: break
            edge = ms.get('edge_yes')
            ya = ms.get('yes_ask'); na = ms.get('no_ask')
            stc = ms.get('seconds_to_close')
            if edge is None or ya is None or na is None or stc is None: continue
            if not (min_seconds_to_close <= stc <= max_seconds_to_close): continue
            if abs(edge) < edge_thresh: continue
            # Determine side: positive edge -> buy YES; negative -> buy NO
            if edge > 0:
                ask_c = int(round(ya * 100))
                eff_entry = ask_c + (0 if use_maker else SLIP)
                won_trade = won
            else:
                ask_c = int(round(na * 100))
                eff_entry = ask_c + (0 if use_maker else SLIP)
                won_trade = not won
            if eff_entry <= 0 or eff_entry >= 100: continue
            payoff = (100 - eff_entry) * contracts if won_trade else -eff_entry * contracts
            fee = fee_fn(eff_entry, contracts)
            net = payoff - fee
            trades.append(dict(
                ticker=ticker, ts_ms=ts, side='yes' if edge>0 else 'no',
                edge=edge, ask=ask_c, eff_entry=eff_entry, stc=stc,
                p_model_yes=ms.get('p_model_yes'), p_market_yes=ms.get('p_market_yes'),
                regime=ms.get('regime'), won=won_trade, net=net,
            ))
            taken = True
    if not trades: return 0, 0, 0, []
    total_net = sum(t['net'] for t in trades)
    n = len(trades); w = sum(1 for t in trades if t['won'])
    return total_net, n, w, trades


def build_calibration_table(decisions, outcomes, train_tickers, bins=10):
    """Fit a piecewise calibration: empirical p(yes) per p_model bucket on training tickers."""
    cal = [{'n':0,'w':0} for _ in range(bins)]
    for t in train_tickers:
        won = outcomes.get(t)
        if won is None: continue
        for ts, ms in decisions[t]:
            pm = ms.get('p_model_yes'); stc = ms.get('seconds_to_close')
            if pm is None or stc is None or stc < 60 or stc > 600: continue
            b = min(bins-1, int(pm * bins))
            cal[b]['n'] += 1
            if won: cal[b]['w'] += 1
    # Smoothing: pull toward bucket midpoint
    smoothed = []
    for i, c in enumerate(cal):
        midpoint = (i + 0.5) / bins
        if c['n'] >= 30:
            smoothed.append(c['w']/c['n'])
        elif c['n'] >= 5:
            # weighted average with midpoint prior, prior strength = 10
            smoothed.append((c['w'] + midpoint*10) / (c['n'] + 10))
        else:
            smoothed.append(midpoint)
    # Enforce monotonic (isotonic): each bucket's calibrated p >= prior
    for i in range(1, len(smoothed)):
        if smoothed[i] < smoothed[i-1]:
            smoothed[i] = smoothed[i-1]
    return smoothed


def calibrated_p(pm, cal_table):
    if pm is None or cal_table is None: return pm
    bins = len(cal_table)
    return cal_table[min(bins-1, int(pm * bins))]


def backtest_thresh_v2(decisions, outcomes, edge_thresh, cal_table=None,
                        max_stc=600.0, min_stc=30.0, contracts=10,
                        one_trade_per_ticker=True, use_maker=False,
                        edge_recompute_from_cal=False):
    trades = []
    fee_fn = kalshi_fee_maker if use_maker else kalshi_fee_taker
    for t, evs in decisions.items():
        won = outcomes.get(t)
        if won is None: continue
        taken = False
        for ts, ms in evs:
            if one_trade_per_ticker and taken: break
            ya = ms.get('yes_ask'); na = ms.get('no_ask'); stc = ms.get('seconds_to_close')
            pm_model = ms.get('p_model_yes'); pm_mkt = ms.get('p_market_yes')
            if any(x is None for x in (ya, na, stc, pm_model, pm_mkt)): continue
            if not (min_stc <= stc <= max_stc): continue
            # Use calibrated edge if requested
            if edge_recompute_from_cal and cal_table is not None:
                pm_use = calibrated_p(pm_model, cal_table)
                edge = pm_use - pm_mkt
            else:
                edge = ms.get('edge_yes', pm_model - pm_mkt)
            if abs(edge) < edge_thresh: continue
            if edge > 0:
                ask_c = int(round(ya * 100)); won_trade = won
            else:
                ask_c = int(round(na * 100)); won_trade = not won
            eff = ask_c + (0 if use_maker else SLIP)
            if eff <= 0 or eff >= 100: continue
            payoff = (100 - eff) * contracts if won_trade else -eff * contracts
            fee = fee_fn(eff, contracts)
            trades.append(dict(ticker=t, ts_ms=ts, edge=edge, won=won_trade,
                               net=payoff - fee, stc=stc, eff_entry=eff,
                               regime=ms.get('regime')))
            taken = True
    if not trades: return 0, 0, 0, []
    return sum(t['net'] for t in trades), len(trades), sum(1 for t in trades if t['won']), trades


def main():
    decisions = load_decisions()
    print(f'Loaded {sum(len(v) for v in decisions.values())} decisions across {len(decisions)} tickers')
    # Outcomes
    outcomes = {}
    for t, evs in decisions.items():
        outcomes[t] = infer_outcome(evs, t)
    determinable = sum(1 for v in outcomes.values() if v is not None)
    print(f'Outcomes determinable: {determinable}/{len(decisions)}')
    for t, o in outcomes.items():
        print(f'  {t}: yes_won={o}')
    print()

    # Calibration: model probability vs realized outcome
    print('======== MODEL CALIBRATION (p_model_yes vs realized) ========')
    buckets = [(0,10),(10,20),(20,30),(30,40),(40,50),(50,60),(60,70),(70,80),(80,90),(90,100)]
    cal = {b: [0,0] for b in buckets}
    for t, evs in decisions.items():
        won = outcomes[t]
        if won is None: continue
        for ts, ms in evs:
            pm = ms.get('p_model_yes')
            stc = ms.get('seconds_to_close')
            if pm is None or stc is None or stc < 60 or stc > 600: continue
            pc = int(pm*100)
            for b in buckets:
                if b[0] <= pc < b[1]:
                    cal[b][0] += 1
                    if won: cal[b][1] += 1
                    break
    print(f'{"p_model bucket":18s} {"n":>6s} {"empirical p(yes)":>17s} {"abs err":>8s}')
    for b in buckets:
        n, w = cal[b]
        if n < 30: continue
        emp = w/n
        mid = (b[0]+b[1])/200
        err = abs(emp - mid)
        print(f'  {b[0]:2d}-{b[1]:2d}%       n={n:5d} {emp*100:>16.1f}%  {err*100:+7.1f}pp')
    print()

    print('======== BACKTEST: edge-threshold sweep ========')
    print(f'(All trades 10ct, taker fees, +{SLIP}c slip, take 1 trade per ticker)')
    print(f'{"thresh":>7s} {"trades":>7s} {"WR":>6s} {"net_$":>9s} {"avg_$/tr":>10s}')
    for th in [0.02, 0.03, 0.05, 0.08, 0.10, 0.15, 0.20]:
        net, n, w, trades = backtest_thresh(decisions, th)
        if n == 0: continue
        print(f'  {th:>6.2f}  {n:>7d} {w/n*100:>5.1f}% {net/100:>+8.2f}  {net/n/100:>+8.3f}')
    print()
    print('======== BACKTEST: maker-rest assumption (fee 75% lower) ========')
    print(f'{"thresh":>7s} {"trades":>7s} {"WR":>6s} {"net_$":>9s} {"avg_$/tr":>10s}')
    for th in [0.02, 0.03, 0.05, 0.08, 0.10, 0.15]:
        net, n, w, trades = backtest_thresh(decisions, th, use_maker=True)
        if n == 0: continue
        print(f'  {th:>6.2f}  {n:>7d} {w/n*100:>5.1f}% {net/100:>+8.2f}  {net/n/100:>+8.3f}')
    print()

    # By regime/time-to-close
    print('======== BEST THRESHOLD: trade-by-trade ========')
    net, n, w, trades = backtest_thresh(decisions, 0.05)
    if trades:
        print(f'Threshold 5c (taker), 10ct: n={n} WR={w/n*100:.1f}% net=${net/100:+.2f}')

    # === Calibrated backtest ===
    print()
    print('======== CALIBRATED MODEL (isotonic, leave-one-ticker-out) ========')
    print(f'(Use empirical p(yes) per bucket trained on N-1 tickers, applied to held-out ticker)')
    print(f'{"thresh":>7s} {"trades":>7s} {"WR":>6s} {"net_$":>9s} {"avg_$/tr":>10s}')
    all_tickers = [t for t in decisions if outcomes[t] is not None]
    for th in [0.05, 0.08, 0.10, 0.12, 0.15]:
        all_trades = []
        for held_out in all_tickers:
            train = [t for t in all_tickers if t != held_out]
            cal = build_calibration_table(decisions, outcomes, train, bins=10)
            sub_decisions = {held_out: decisions[held_out]}
            sub_outcomes = {held_out: outcomes[held_out]}
            net, n, w, trades = backtest_thresh_v2(
                sub_decisions, sub_outcomes, th,
                cal_table=cal, edge_recompute_from_cal=True,
            )
            all_trades.extend(trades)
        if not all_trades: continue
        net = sum(t['net'] for t in all_trades); n = len(all_trades)
        w = sum(1 for t in all_trades if t['won'])
        print(f'  {th:>6.2f}  {n:>7d} {w/n*100:>5.1f}% {net/100:>+8.2f}  {net/n/100:>+8.3f}')

    # Also try with maker fees
    print()
    print('======== CALIBRATED + MAKER fees ========')
    print(f'{"thresh":>7s} {"trades":>7s} {"WR":>6s} {"net_$":>9s} {"avg_$/tr":>10s}')
    for th in [0.05, 0.08, 0.10, 0.12, 0.15]:
        all_trades = []
        for held_out in all_tickers:
            train = [t for t in all_tickers if t != held_out]
            cal = build_calibration_table(decisions, outcomes, train, bins=10)
            sub_decisions = {held_out: decisions[held_out]}
            sub_outcomes = {held_out: outcomes[held_out]}
            net, n, w, trades = backtest_thresh_v2(
                sub_decisions, sub_outcomes, th,
                cal_table=cal, edge_recompute_from_cal=True, use_maker=True,
            )
            all_trades.extend(trades)
        if not all_trades: continue
        net = sum(t['net'] for t in all_trades); n = len(all_trades)
        w = sum(1 for t in all_trades if t['won'])
        print(f'  {th:>6.2f}  {n:>7d} {w/n*100:>5.1f}% {net/100:>+8.2f}  {net/n/100:>+8.3f}')

    # Window/regime breakdown
    print()
    print('======== BY ENTRY WINDOW (taker, threshold=0.10) ========')
    for stc_lo, stc_hi in [(30,60),(60,120),(120,300),(300,600)]:
        all_trades = []
        for held_out in all_tickers:
            train = [t for t in all_tickers if t != held_out]
            cal = build_calibration_table(decisions, outcomes, train)
            net, n, w, trades = backtest_thresh_v2(
                {held_out: decisions[held_out]}, {held_out: outcomes[held_out]},
                0.10, cal_table=cal, edge_recompute_from_cal=True,
                min_stc=stc_lo, max_stc=stc_hi,
            )
            all_trades.extend(trades)
        if not all_trades: continue
        net = sum(t['net'] for t in all_trades); n = len(all_trades)
        w = sum(1 for t in all_trades if t['won'])
        print(f'  stc {stc_lo:>3d}-{stc_hi:>3d}s   n={n:>3d} WR={w/n*100:>5.1f}%  net=${net/100:+7.2f}')


if __name__ == '__main__':
    main()
