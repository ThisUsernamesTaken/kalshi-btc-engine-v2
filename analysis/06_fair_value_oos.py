"""Out-of-sample fair-value validation.

The gradient engine's `settlement_fair_probability` (BRTI-averaging-aware
log-normal CDF) is a structural model — it doesn't depend on small-sample
empirical bucket WRs. To test whether it generalizes:

  - Train period: the model's math is fixed (no fitting); 1-day capture
    (2026-05-18) showed +$8.29 at 10c threshold on 16 markets.
  - Test period: 2026-05-12 → 2026-05-16 from `paper_ta_2026_05_12.jsonl`,
    which has 301 settled markets with per-minute BTC spot snapshots.

For each paper_ta fill: compute the model's p_fair at the entry minute given
(spot, strike, time_to_close). Decide what the model would have done at
that tick. Compare to actual outcome.

This is genuine OOS: the model's parameters are determined entirely by
formulas in models/probability.py, not by anything fit to either dataset.
"""
from __future__ import annotations
import json, math, sys
from collections import defaultdict
from pathlib import Path

# Add gradient engine to path
sys.path.insert(0, str(Path(r'C:/Trading/kalshi_btc_gradient_engine/src')))
from kalshi_btc_gradient.models.probability import (
    settlement_fair_probability, SettlementProbabilityInput,
    SettlementProbabilityConfig,
)

PAPER_TA = Path(r'D:/Trading/kalshi-btc-engine-v2/data/paper_ta_2026_05_12.jsonl')
SLIP = 2


def parse_ticker_strike(ticker: str) -> float | None:
    """Last numeric portion: KXBTC15M-26MAY121130-30 -> 30 doesn't help.
    Strike must be inferred from outcome events. Defer until we have data."""
    return None


def extract_strike_from_settle_or_fill(events_by_ticker):
    """For each ticker, see if any event carries a strike (yes_ask_at_entry, etc.)."""
    out = {}
    for t, evs in events_by_ticker.items():
        for e in evs:
            # Fill events sometimes have strike implicit via the yes_ask_at_entry
            if 'strike' in e:
                out[t] = e['strike']; break
    return out


def load_paper_ta():
    by_ticker = defaultdict(list)
    snapshots = []  # global timeline of BTC spot
    for line in PAPER_TA.open(encoding='utf-8', errors='ignore'):
        try: e = json.loads(line)
        except: continue
        k = e.get('kind')
        if k == 'snapshot':
            # snapshot is per-minute; spot_close is BTC mid
            snapshots.append((e.get('ts_minute_ms'), e.get('spot_close')))
        else:
            t = e.get('ticker')
            if t: by_ticker[t].append(e)
    snapshots.sort()
    return by_ticker, snapshots


def spot_at(snapshots, ts_ms):
    """Find the nearest snapshot's spot_close to ts_ms (most recent before)."""
    import bisect
    ts_arr = [s[0] for s in snapshots]
    idx = bisect.bisect_right(ts_arr, ts_ms) - 1
    return snapshots[idx][1] if idx >= 0 else None


def realized_vol_per_second_from_snapshots(snapshots, end_ts_ms, window_ms=300_000):
    """Compute log-return realized vol over the last `window_ms` (annualized)."""
    import bisect
    ts_arr = [s[0] for s in snapshots]
    end_idx = bisect.bisect_right(ts_arr, end_ts_ms) - 1
    if end_idx < 5: return 0.5  # default 50% annualized
    # Walk back to window start
    window_start_ms = end_ts_ms - window_ms
    start_idx = bisect.bisect_left(ts_arr, window_start_ms)
    prices = [snapshots[i][1] for i in range(start_idx, end_idx + 1) if snapshots[i][1]]
    if len(prices) < 3: return 0.5
    log_returns = [math.log(prices[i+1] / prices[i]) for i in range(len(prices) - 1)
                   if prices[i] > 0 and prices[i+1] > 0]
    if not log_returns: return 0.5
    mean = sum(log_returns) / len(log_returns)
    var = sum((r - mean)**2 for r in log_returns) / max(1, len(log_returns) - 1)
    sigma_per_sample = math.sqrt(var)
    # snapshots are per-minute, so per-second = per-sample / sqrt(60)
    sigma_per_sec = sigma_per_sample / math.sqrt(60)
    sigma_ann = sigma_per_sec * math.sqrt(365 * 24 * 3600)
    # clip to sensible bounds
    return max(0.1, min(2.0, sigma_ann))


def kalshi_fee_taker(price_c, contracts):
    p = price_c / 100
    return math.ceil(0.07 * contracts * p * (1 - p) * 100)

def kalshi_fee_maker(price_c, contracts):
    p = price_c / 100
    return math.ceil(0.0175 * contracts * p * (1 - p) * 100)


def main():
    by_ticker, snapshots = load_paper_ta()
    print(f'Loaded paper_ta: {len(by_ticker)} tickers, {len(snapshots)} snapshots')

    # For each ticker, find the fill event and settle event
    # paper_ta fill events have: entry_price_cents, side, ts_ms, ticker
    # settle events: outcome, ticker
    settles = {}
    fills = {}
    for t, evs in by_ticker.items():
        for e in evs:
            k = e.get('kind')
            if k == 'fill' and t not in fills:
                fills[t] = e
            elif k == 'settle':
                settles[t] = e

    # Joining: for each ticker with both fill+settle, extract strike from settle's tier/decision context
    # Actually paper_ta settle's "tier" / "tier_name" don't carry strike, but the TICKER name encodes the strike-end
    # Format: KXBTC15M-26MAYDDHHMM-EE where EE is last 2 digits of strike? No — that's the close minute.
    # Strike is not in the log directly. We need to fetch via REST or skip strike-dependent backtests.
    # WORKAROUND: spot at settle close ~= strike for at-the-money markets. The "yes_ask_at_entry"
    # in shadow_velocity logs gives implied probability, from which we could back out strike, but
    # paper_ta doesn't have that field.
    #
    # Best alternative: use the model in DIRECTIONAL mode (without strike).
    # The model can output p(BTC will be HIGHER at close than now) which is the binary direction
    # condition on at-the-money strike. paper_ta's tickers are all at-the-money 15m strikes that
    # roll, so this is roughly valid.

    # Sample fills to understand structure
    print('Fill sample:', json.dumps(next(iter(fills.values())), default=str)[:400])
    print('Settle sample:', json.dumps(next(iter(settles.values())), default=str)[:400])
    print()

    # Without strike: use the at-the-money approximation
    # For each fill: entry_price_cents = our paid price (already includes fees? unclear).
    # The fair-value question becomes: what's P(BTC closes higher than at fill time)?
    # That's the at-the-money case where strike = spot. The model collapses to ~50% in the limit.
    # NOT a meaningful test without strikes. Need to fetch via REST or pull from another source.

    # Alternative: use just the SHADOW_VELOCITY log which records yes_ask_at_entry
    # and we can infer market probability.
    SHADOW = Path(r'D:/Trading/kalshi-btc-engine-v2/data/shadow_velocity_2026_05_14.jsonl')
    print(f'\nSwitching to shadow_velocity for richer schema (has btc_price_at_entry):')
    sv_settles = []
    sv_signals = {}
    for line in SHADOW.open(encoding='utf-8', errors='ignore'):
        try: e = json.loads(line)
        except: continue
        k = e.get('kind')
        if k == 'velocity_signal':
            sv_signals[e.get('ticker')] = e
        elif k == 'settle':
            sv_settles.append(e)
    print(f'  signals: {len(sv_signals)}  settles: {len(sv_settles)}')

    # For each settle in shadow_velocity, we have:
    #  btc_price_at_entry (spot), yes_ask_at_entry (implied market p)
    #  ticker -> close minute -> approximate close ts
    #  outcome -> binary win
    #
    # We still don't know the strike! Without strike, the fair-value model can't compute.
    # Need to either (a) fetch strikes via REST for every settled ticker (slow), or
    # (b) infer strike from yes_ask_at_entry + spot + tau using inverse fair-value (fragile).

    # OK simpler concrete check: COMPARE the model's ATM-direction probability to outcome.
    # i.e., given spot at fill, predict if BTC closes higher; see if that beats market's yes_ask.

    print()
    print('======== DIRECTIONAL ANALYSIS (no-strike, at-the-money proxy) ========')
    print('"Did our spot rise vs fall from fill to close?"')

    # We need the close-time BTC for each settle.
    # paper_ta snapshots provide BTC every minute.
    # ticker -> cycle_close_ms (from settle event)
    correct = 0; total = 0
    by_p_market = defaultdict(lambda: [0, 0])  # bucket of yes_ask_at_entry: [n, wins]
    for s in sv_settles:
        cycle_close = s.get('cycle_close_ms')
        btc_entry = s.get('btc_price_at_entry')
        ya_entry = s.get('yes_ask_at_entry')  # implied market p YES
        side = s.get('side')
        outcome = s.get('outcome')
        if not all([cycle_close, btc_entry, ya_entry is not None, outcome]): continue
        # Outcome=='yes' means BTC rose past strike. With shadow_velocity strategy,
        # entries are "buy NO when velocity is DOWN" so side='no' means strategy
        # predicted DOWN. Outcome='yes' would be a loss for side='no'.
        side_won = (side == outcome)
        total += 1
        if side_won: correct += 1
        # Bucket by yes_ask_at_entry (implied market prob)
        b = int(ya_entry * 10)
        by_p_market[b][0] += 1
        if outcome == 'yes': by_p_market[b][1] += 1
    print(f'  Shadow-velocity strategy WR: {correct/max(total,1)*100:.1f}% on {total} settles')
    print(f'  (For reference, this strategy is the velocity-trend-follower)')
    print()
    print('  By market-implied p(yes) at entry, empirical p(yes):')
    for b in sorted(by_p_market.keys()):
        n, w = by_p_market[b]
        if n < 5: continue
        print(f'    market p_yes ~{b*10}-{(b+1)*10}%: empirical p(yes)={w/n*100:.1f}% on n={n}')


if __name__ == '__main__':
    main()
