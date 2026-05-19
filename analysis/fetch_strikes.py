"""Batch-fetch strike + settlement data for every ticker that appears in
paper_ta / shadow_velocity / live_ta / etc., cache to JSON.

Public Kalshi REST endpoint (no auth needed): /trade-api/v2/markets/{ticker}

Cache lives at analysis/strikes_cache.json. Re-running is idempotent — only
fetches tickers not already in the cache. Polite to Kalshi: 0.25s between
requests, simple retry on 429 / connection error.

Run:
    python -m analysis.fetch_strikes                  # fetch all missing
    python -m analysis.fetch_strikes --refresh        # re-fetch every ticker
    python -m analysis.fetch_strikes --source paper_ta # only one source
"""
from __future__ import annotations
import argparse, json, time, urllib.request, urllib.error
from collections import defaultdict
from pathlib import Path

CACHE_PATH = Path(__file__).resolve().parent / 'strikes_cache.json'
DATA_DIR = Path(__file__).resolve().parent.parent / 'data'
KALSHI_BASE = 'https://api.elections.kalshi.com/trade-api/v2/markets/'
REQUEST_DELAY_S = 0.25
MAX_RETRIES = 3


def collect_tickers(filter_source: str | None = None) -> dict[str, str]:
    """Return {ticker: source_log_basename} for every ticker that has a settle."""
    sources = [
        ('paper_ta',         'paper_ta_2026_05_12.jsonl'),
        ('shadow_velocity',  'shadow_velocity_2026_05_14.jsonl'),
        ('live_ta',          'live_ta_trades.jsonl'),
        ('live_ta_v2',       'live_ta_v2_trades.jsonl'),
        ('live_v5_unified',  'live_v5_unified_trades.jsonl'),
        ('live_v5',          'live_v5_trades.jsonl'),
        ('ladder_shadow',    'ladder_shadow.jsonl'),
    ]
    tickers = {}
    for label, fn in sources:
        if filter_source and label != filter_source: continue
        p = DATA_DIR / fn
        if not p.exists(): continue
        for line in p.open(encoding='utf-8', errors='ignore'):
            try: e = json.loads(line)
            except: continue
            k = e.get('kind')
            t = e.get('ticker')
            if not t: continue
            if k in ('settle', 'settle_with_ladder', 'fill'):
                tickers.setdefault(t, label)
    return tickers


def load_cache() -> dict:
    if not CACHE_PATH.exists(): return {}
    try: return json.loads(CACHE_PATH.read_text())
    except: return {}


def save_cache(cache: dict):
    CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True))


def fetch_one(ticker: str) -> dict | None:
    """Fetch one ticker from Kalshi REST. Return the trimmed market dict or None on error."""
    url = KALSHI_BASE + ticker
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'kalshi-research/1.0'})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
            m = data.get('market') or {}
            return dict(
                ticker=m.get('ticker'),
                status=m.get('status'),
                result=m.get('result'),
                strike=float(m.get('floor_strike') or m.get('strike_price') or 0.0) or None,
                strike_type=m.get('strike_type'),
                close_time=m.get('close_time'),
                expiration_value=m.get('expiration_value'),
                settlement_value_dollars=m.get('settlement_value_dollars'),
                event_ticker=m.get('event_ticker'),
            )
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2 ** attempt); continue
            if e.code == 404:
                return {'ticker': ticker, 'status': 'unknown', 'error': '404'}
            return {'ticker': ticker, 'error': f'HTTP {e.code}'}
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(1.0); continue
            return {'ticker': ticker, 'error': str(e)[:120]}
    return {'ticker': ticker, 'error': 'max_retries_exhausted'}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--refresh', action='store_true',
                    help='re-fetch every ticker (default: only missing)')
    ap.add_argument('--source', type=str, default=None,
                    help='only fetch tickers from this source log')
    ap.add_argument('--limit', type=int, default=None,
                    help='cap on number of new fetches (for testing)')
    ap.add_argument('--from-sqlite', type=str, default=None,
                    help='also include all KXBTC15M tickers from this SQLite database')
    args = ap.parse_args()

    cache = {} if args.refresh else load_cache()
    print(f'Cache loaded with {len(cache)} existing tickers')

    tickers = collect_tickers(args.source)
    if args.from_sqlite:
        import sqlite3
        con = sqlite3.connect(f'file:{args.from_sqlite}?mode=ro', uri=True)
        cur = con.cursor()
        cur.execute("select ticker from market_dim where ticker like 'KXBTC15M-%'")
        for (t,) in cur.fetchall():
            tickers.setdefault(t, 'sqlite_market_dim')
        con.close()
    print(f'Unique tickers across logs: {len(tickers)}')

    to_fetch = [t for t in tickers if t not in cache]
    if args.limit:
        to_fetch = to_fetch[:args.limit]
    print(f'Fetching {len(to_fetch)} new tickers (~{len(to_fetch) * REQUEST_DELAY_S:.0f}s)...')

    errors = 0
    for i, ticker in enumerate(to_fetch):
        rec = fetch_one(ticker)
        cache[ticker] = rec or {'ticker': ticker, 'error': 'unknown'}
        if rec and 'error' in rec:
            errors += 1
        if (i + 1) % 25 == 0:
            print(f'  [{i+1}/{len(to_fetch)}] ok={i+1-errors} err={errors}')
            save_cache(cache)
        time.sleep(REQUEST_DELAY_S)
    save_cache(cache)

    # Summary
    n_ok = sum(1 for v in cache.values() if v.get('strike'))
    n_err = sum(1 for v in cache.values() if 'error' in v)
    print(f'\nSaved {CACHE_PATH}')
    print(f'  total cached: {len(cache)}')
    print(f'  have strike : {n_ok}')
    print(f'  errors      : {n_err}')


if __name__ == '__main__':
    main()
