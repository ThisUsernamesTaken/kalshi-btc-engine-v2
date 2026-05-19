"""Edge persistence filter: require the model's edge signal to hold for N
consecutive ticks before trading. Hypothesis: single-tick signals are
stale-quote artifacts; persistent signals are real mispricings.

Implementation: replay each gradient-engine decision stream; track when
abs(edge_yes) crosses each threshold; require it to stay above the
threshold for `min_persistence_s` before counting as a fillable tick.

If the edge dies in <N seconds, it was probably noise. If it holds, the
market is genuinely mispricing the contract and a maker rest at our bid
should fill once a passive seller arrives.
"""
from __future__ import annotations
import json, math, bisect, sys
from collections import defaultdict
from pathlib import Path

CAPTURE_DIR = Path(r'C:/Trading/kalshi_btc_gradient_engine/data/captures/2026-05-18')
STRIKES = json.loads((Path(__file__).resolve().parent / 'strikes_cache.json').read_text())
SLIP = 2

GROUND_TRUTH = {
    'KXBTC15M-26MAY181730-30': True,  'KXBTC15M-26MAY181745-45': False,
    'KXBTC15M-26MAY181800-00': True,  'KXBTC15M-26MAY181815-15': True,
    'KXBTC15M-26MAY181830-30': True,  'KXBTC15M-26MAY181845-45': False,
    'KXBTC15M-26MAY181900-00': True,  'KXBTC15M-26MAY181915-15': True,
    'KXBTC15M-26MAY181930-30': False, 'KXBTC15M-26MAY181945-45': True,
    'KXBTC15M-26MAY182000-00': False, 'KXBTC15M-26MAY182015-15': True,
    'KXBTC15M-26MAY182030-30': True,  'KXBTC15M-26MAY182045-45': False,
    'KXBTC15M-26MAY182100-00': False, 'KXBTC15M-26MAY182115-15': False,
}


def kalshi_fee_maker(price_c, contracts):
    p = price_c/100; return math.ceil(0.0175 * contracts * p * (1-p) * 100)


def load_decisions():
    by_t = defaultdict(list)
    for fn in sorted(CAPTURE_DIR.glob('decisions_*.jsonl')):
        with fn.open(encoding='utf-8', errors='ignore') as f:
            for line in f:
                try: e = json.loads(line)
                except: continue
                ms = e.get('market_state') or {}
                t = ms.get('market_ticker')
                if t: by_t[t].append((e.get('ts_ms') or 0, ms))
    for t in by_t:
        by_t[t].sort(key=lambda x: x[0])
    return dict(by_t)


def trade_with_persistence(decisions, ticker, won, threshold_c=10, min_persist_s=5):
    """Walk the decision stream; trade once when edge has persisted for min_persist_s."""
    persist_start = None
    persist_side = None
    for ts, ms in decisions.get(ticker, []):
        edge = ms.get('edge_yes')
        stc = ms.get('seconds_to_close')
        ya = ms.get('yes_ask'); na = ms.get('no_ask')
        if edge is None or ya is None or na is None or stc is None: continue
        if stc < 30 or stc > 600: continue
        if abs(edge) * 100 < threshold_c:
            persist_start = None; persist_side = None
            continue
        cur_side = 'yes' if edge > 0 else 'no'
        if persist_side != cur_side:
            persist_start = ts; persist_side = cur_side
            continue
        # Same side, check duration
        if (ts - persist_start) / 1000 < min_persist_s:
            continue
        # Triggered — simulate maker rest at (best_bid+1)
        if cur_side == 'yes':
            # rest at yes_bid+1; effective entry = yes_ask - 2 conservatively
            yes_bid = ms.get('yes_bid')
            if yes_bid is None: continue
            limit_c = max(1, min(int(round(yes_bid*100)) + 1,
                                 int(round(ya*100)) - 1))
            won_trade = won
        else:
            no_bid = ms.get('no_bid')
            if no_bid is None: continue
            limit_c = max(1, min(int(round(no_bid*100)) + 1,
                                 int(round(na*100)) - 1))
            won_trade = not won
        if limit_c < 1 or limit_c > 99: continue
        contracts = 10
        payoff = (100 - limit_c) * contracts if won_trade else -limit_c * contracts
        fee = kalshi_fee_maker(limit_c, contracts)
        return dict(net=payoff - fee, won=won_trade, limit=limit_c,
                    side=cur_side, ts=ts, stc=stc, p_model=ms.get('p_model_yes'),
                    edge=edge)
    return None


def main():
    decisions = load_decisions()
    print(f'Loaded {sum(len(v) for v in decisions.values())} decisions on {len(decisions)} markets')

    print('\n======== EDGE PERSISTENCE STUDY (gradient engine 2026-05-18) ========')
    print(f'{"persist_s":>10s} {"thresh":>7s} {"trades":>7s} {"WR":>6s} {"net_$":>9s}')
    for persist_s in [0, 1, 2, 5, 10, 30, 60]:
        for thresh in [5, 8, 10, 12]:
            trades = []
            for ticker, won in GROUND_TRUTH.items():
                t = trade_with_persistence(decisions, ticker, won,
                                            threshold_c=thresh, min_persist_s=persist_s)
                if t: trades.append(t)
            if not trades: continue
            n = len(trades); w = sum(1 for t in trades if t['won'])
            net = sum(t['net'] for t in trades)
            print(f'  {persist_s:>10d}s {thresh:>5d}c {n:>7d} {w/n*100:>5.1f}% '
                  f'${net/100:>+8.2f}')

    # Detail on the best
    print('\n======== TRADE DETAIL: persist=5s, threshold=10c ========')
    trades = []
    for ticker, won in GROUND_TRUTH.items():
        t = trade_with_persistence(decisions, ticker, won, threshold_c=10, min_persist_s=5)
        if t:
            t['ticker'] = ticker
            trades.append(t)
    if trades:
        n = len(trades); w = sum(1 for t in trades if t['won'])
        net = sum(t['net'] for t in trades)
        print(f'  n={n}  WR={w/n*100:.1f}%  net=${net/100:+.2f}')
        for t in trades:
            print(f'    {t["ticker"][-15:]} stc={t["stc"]:6.1f}s {t["side"]:3s}@{t["limit"]:3d}c '
                  f'edge={t["edge"]*100:+5.1f}c p_mod={t["p_model"]*100:5.1f}% '
                  f'{"WIN" if t["won"] else "LOS"} net={t["net"]:+5d}c')


if __name__ == '__main__':
    main()
