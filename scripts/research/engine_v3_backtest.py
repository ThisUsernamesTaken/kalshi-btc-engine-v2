"""Backtest engine_v3 against the historical 331-market capture.

For each settled market:
  - Replay BTC spot prices up to T-30s into the BTCPriceTracker
  - Build a MarketSnapshot at T-30s using the L2 book
  - Run evaluate_entry()
  - If ENTER, compute settlement P&L
  - Update Bayesian posterior + CUSUM tracker as we go
  - Stream output as we process markets so we see progress

Walk-forward by market close time. Compare to the simple T-30 sniper baseline
("fav_bid>=85c, 5ct, no filters") to measure the value of each refinement.
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine_v3 import (
    EngineV3Config, BayesianPosterior, CUSUMTracker, BTCPriceTracker,
    MarketSnapshot, evaluate_entry, settle_pnl_cents, kalshi_fee_cents,
)

DB = Path(r'C:\Trading\kalshi-btc-engine-v2\data\burnin_holdpure_2026_05_12.sqlite')

def load_markets():
    con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=60.0)
    cur = con.cursor()
    cur.execute("""SELECT lc.market_ticker, lc.raw_json, md.close_time, md.open_time, md.raw_json AS m_raw
                   FROM kalshi_lifecycle_event lc
                   JOIN market_dim md ON md.ticker = lc.market_ticker
                   WHERE lc.status='determined' AND md.series_ticker='KXBTC15M'""")
    markets = []
    for tk, raw, ct_str, ot_str, m_raw in cur.fetchall():
        try:
            result = json.loads(raw)['msg'].get('result')
            if result not in ('yes', 'no'): continue
            ct_ms = int(datetime.fromisoformat(ct_str.replace('Z','+00:00')).timestamp()*1000)
            ot_ms = int(datetime.fromisoformat(ot_str.replace('Z','+00:00')).timestamp()*1000)
            strike = json.loads(m_raw).get('floor_strike')
            if not strike: continue
            markets.append({'tk': tk, 'ot_ms': ot_ms, 'ct_ms': ct_ms,
                            'result': result, 'strike': float(strike)})
        except Exception:
            continue
    markets.sort(key=lambda m: m['ct_ms'])
    return markets, con, cur

def fetch_btc_prices(cur, start_ms, end_ms):
    """Get BTC bitstamp prices in [start_ms, end_ms]."""
    cur.execute("""SELECT COALESCE(exchange_ts_ms, received_ts_ms), mid
                   FROM spot_quote_event
                   WHERE symbol='btcusd' AND venue='bitstamp'
                   AND COALESCE(exchange_ts_ms, received_ts_ms) BETWEEN ? AND ?
                   ORDER BY 1 ASC""",
                (start_ms, end_ms))
    return [(int(ts), float(p)) for ts, p in cur.fetchall() if p]

def book_at(cur, ticker, ts):
    cur.execute("""SELECT best_yes_bid, best_yes_ask FROM kalshi_l2_event
                   WHERE market_ticker=? AND COALESCE(exchange_ts_ms, received_ts_ms) BETWEEN ? AND ?
                   AND best_yes_bid IS NOT NULL AND best_yes_ask IS NOT NULL
                   ORDER BY COALESCE(exchange_ts_ms, received_ts_ms) DESC LIMIT 1""",
                (ticker, ts-10_000, ts))
    r = cur.fetchone()
    if not r: return None
    return int(round(float(r[0])*100)), int(round(float(r[1])*100))

def run_backtest(decision_time_s: int = 30,
                 enable_vwap_lock: bool = True,
                 enable_jump_filter: bool = True,
                 enable_bayesian_sizing: bool = True,
                 enable_cusum: bool = True,
                 use_backtest_prior: bool = False,
                 fixed_contracts: int = 5,
                 label: str = 'v3_full'):
    """Run the engine on all settled markets at the given decision time.

    Toggleable refinements let us ablate each feature. When Bayesian sizing
    is OFF, uses `fixed_contracts` (default 5). When ON, uses quarter-Kelly.
    """
    print(f"\n{'='*88}", flush=True)
    print(f" BACKTEST: {label}", flush=True)
    print(f"   decision_time_s={decision_time_s} vwap_lock={enable_vwap_lock} "
          f"jump_filter={enable_jump_filter} bayes_size={enable_bayesian_sizing} "
          f"cusum={enable_cusum} prior={'BT' if use_backtest_prior else 'Beta(2,2)'} "
          f"fixed_ct={fixed_contracts if not enable_bayesian_sizing else 'KELLY'}", flush=True)
    print(f"{'='*88}", flush=True)

    markets, con, cur = load_markets()
    print(f" Loaded {len(markets)} settled markets", flush=True)

    config = EngineV3Config()
    if not enable_jump_filter:
        config.jump_sigma_threshold = 999.0
    if not enable_vwap_lock:
        config.vwap_settlement_window_s = 0
    if not enable_cusum:
        config.cusum_kill_losses = 99
    if not enable_bayesian_sizing:
        config.posterior_mean_min = 0.0   # disable gate
        config.fixed_contracts = fixed_contracts  # use deterministic size
    # else: leave fixed_contracts=None so Kelly path is used

    # Posterior prior choice
    if use_backtest_prior:
        # Use backtest evidence as prior: Beta(45, 1) = 44 wins + uniform
        prior_a, prior_b = 45.0, 1.0
    else:
        prior_a, prior_b = config.prior_wins, config.prior_losses

    posterior = BayesianPosterior(alpha=prior_a, beta=prior_b)
    cusum = CUSUMTracker(config.cusum_window, config.cusum_kill_losses, config.cusum_alert_losses)

    decisions = []
    for i, m in enumerate(markets):
        if i % 50 == 0:
            print(f"   processing market {i}/{len(markets)}...", flush=True)
        decision_ts = m['ct_ms'] - decision_time_s * 1000
        if decision_ts < m['ot_ms']:
            continue

        # Fresh BTC tracker per market (avoids stale-buffer contamination)
        btc = BTCPriceTracker(window_s=320)
        prices = fetch_btc_prices(cur, decision_ts - 320*1000, decision_ts + 1000)
        for ts, p in prices:
            btc.add(ts, p)

        book = book_at(cur, m['tk'], decision_ts)
        if not book: continue
        yb, ya = book
        snap = MarketSnapshot(
            ticker=m['tk'], secs_to_close=float(decision_time_s),
            close_ts_ms=m['ct_ms'], strike=m['strike'],
            yes_bid=yb, yes_ask=ya,
            no_bid=100-ya, no_ask=100-yb,
            btc_now=prices[-1][1] if prices else None,
        )

        dec = evaluate_entry(snap, btc, posterior, cusum, config)
        rec = {
            'ticker': m['tk'],
            'close_ts_ms': m['ct_ms'],
            'action': dec.action, 'reason': dec.reason,
            'side': dec.side, 'contracts': dec.contracts,
            'limit_cents': dec.limit_cents,
            'fav_bid': dec.fav_bid_cents, 'fav_ask': dec.fav_ask_cents,
            'posterior_mean': dec.posterior_mean,
            'posterior_skeptical_p': dec.posterior_skeptical_p,
            'cusum_loss_count': dec.cusum_loss_count,
            'vwap_locked_avg': dec.vwap_locked_avg,
            'vwap_locked_dir': dec.vwap_locked_direction,
            'jump_detected': dec.jump_detected,
            'kelly_quarter': dec.kelly_quarter,
            'result': m['result'],
        }
        if dec.action == "ENTER":
            # Use entry_price = fav_ask (taker), not limit_cents (which adds slip)
            pnl = settle_pnl_cents(dec.fav_ask_cents, dec.contracts,
                                   dec.side, m['result'])
            won = (dec.side == m['result'])
            rec['pnl_cents'] = pnl
            rec['won'] = won
            posterior.update(won)
            cusum.update(won)
        decisions.append(rec)

    # Aggregate
    entries = [d for d in decisions if d['action'] == 'ENTER']
    n = len(entries)
    wins = sum(1 for d in entries if d.get('won'))
    net_c = sum(d.get('pnl_cents', 0) for d in entries)
    skips = [d for d in decisions if d['action'] == 'SKIP']
    skip_reasons: dict[str, int] = {}
    for d in skips:
        k = d['reason'].split()[0]
        skip_reasons[k] = skip_reasons.get(k, 0) + 1

    print(f"\n RESULTS ({label}):", flush=True)
    print(f"   Total decisions: {len(decisions)}")
    print(f"   ENTER: {n}  ({100*n/max(1,len(decisions)):.1f}% of markets)")
    print(f"   SKIP:  {len(skips)}")
    print(f"   Wins:  {wins} / {n} = {100*wins/max(1,n):.1f}% WR")
    print(f"   Net P&L: ${net_c/100:+.2f}")
    print(f"   EV per ENTER: ${net_c/max(1,n)/100:+.4f}")
    print(f"   Final posterior: Beta({posterior.alpha:.1f}, {posterior.beta:.1f}) mean={posterior.mean():.3f}")
    print(f"   Top skip reasons:")
    for k, c in sorted(skip_reasons.items(), key=lambda x: -x[1])[:10]:
        print(f"     {k}: {c}")

    # Also show losing trades if any
    losers = [d for d in entries if d.get('pnl_cents', 0) < 0]
    if losers:
        print(f"\n   LOSING TRADES ({len(losers)}):")
        for d in losers[:10]:
            print(f"     {d['ticker'][-25:]} side={d['side']} entry={d['fav_ask']}c "
                  f"qty={d['contracts']} result={d['result']} pnl={d['pnl_cents']}c "
                  f"vwap={d.get('vwap_locked_avg')} jump={d.get('jump_detected')}")

    return {
        'label': label, 'n': n, 'wins': wins, 'net_c': net_c,
        'skips': len(skips), 'posterior_mean': posterior.mean(),
        'decisions': decisions,
    }


if __name__ == "__main__":
    print("[engine_v3 backtest]", flush=True)
    t0 = time.time()

    results = []
    # 1. Baseline: just T-30 sniper at fav>=85c, 5ct fixed, no other features
    results.append(run_backtest(
        decision_time_s=30,
        enable_vwap_lock=False, enable_jump_filter=False,
        enable_bayesian_sizing=False, enable_cusum=False,
        fixed_contracts=5,
        label='BASELINE (T-30 fav>=85c, 5ct fixed, no filters)',
    ))

    # 2. + VWAP lock-in
    results.append(run_backtest(
        decision_time_s=30,
        enable_vwap_lock=True, enable_jump_filter=False,
        enable_bayesian_sizing=False, enable_cusum=False,
        fixed_contracts=5,
        label='+ VWAP_LOCK (5ct)',
    ))

    # 3. + jump filter
    results.append(run_backtest(
        decision_time_s=30,
        enable_vwap_lock=True, enable_jump_filter=True,
        enable_bayesian_sizing=False, enable_cusum=False,
        fixed_contracts=5,
        label='+ VWAP_LOCK + JUMP_FILTER (5ct)',
    ))

    # 4. + CUSUM (kill switch — only matters if we ever lose)
    results.append(run_backtest(
        decision_time_s=30,
        enable_vwap_lock=True, enable_jump_filter=True,
        enable_bayesian_sizing=False, enable_cusum=True,
        fixed_contracts=5,
        label='+ ALL FILTERS + CUSUM (5ct fixed)',
    ))

    # 5. Bayesian sizing with cold-start prior (Beta(2,2))
    # This shows whether the engine SELF-VALIDATES — does it eventually start trading?
    results.append(run_backtest(
        decision_time_s=30,
        enable_vwap_lock=True, enable_jump_filter=True,
        enable_bayesian_sizing=True, enable_cusum=True,
        use_backtest_prior=False,
        label='FULL ENGINE_V3 (Bayes cold-start, Beta(2,2))',
    ))

    # 6. Bayesian sizing with backtest prior (Beta(45,1) = "I trust the backtest")
    results.append(run_backtest(
        decision_time_s=30,
        enable_vwap_lock=True, enable_jump_filter=True,
        enable_bayesian_sizing=True, enable_cusum=True,
        use_backtest_prior=True,
        label='FULL ENGINE_V3 (Bayes with backtest prior Beta(45,1))',
    ))

    print(f"\n{'='*88}", flush=True)
    print(" SUMMARY", flush=True)
    print(f"{'='*88}", flush=True)
    print(f"{'config':<60s} {'n':>4s} {'WR':>6s} {'net':>9s} {'EV':>9s}")
    for r in results:
        wr = 100*r['wins']/max(1,r['n'])
        ev = r['net_c']/max(1,r['n'])/100
        print(f"  {r['label'][:58]:<60s} {r['n']:>4d} {wr:>5.1f}% ${r['net_c']/100:>+7.2f} ${ev:>+.4f}")

    print(f"\n[done] {time.time()-t0:.1f}s total", flush=True)
