"""Fetch BTC derivatives microstructure data from OKX (Binance is US-IP blocked).

Writes to a single SQLite DB: kalshi-btc-engine-v2/data/derivatives.sqlite

Coverage:
  - funding_rate (8h, BTC-USDT-SWAP): full history (paginable, ~years)
  - mark_15m / index_15m (BTC-USDT-SWAP, BTC-USDT): paginable back to Mar 2026
  - rubik features (account L/S, top trader L/S, OI/volume, taker volume):
    ONLY the most recent ~48h (OKX caps this).
"""

import json
import sqlite3
import time
import urllib.request
import urllib.error
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "derivatives.sqlite"
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def http_get(url, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                time.sleep(2 ** i)
                continue
            raise
        except Exception as e:
            last = e
            time.sleep(1 + i)
    raise last


def init_db():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS funding_rate (
            ts INTEGER PRIMARY KEY,
            inst_id TEXT,
            funding_rate REAL,
            realized_rate REAL
        );
        CREATE TABLE IF NOT EXISTS perp_candles_15m (
            ts INTEGER PRIMARY KEY,
            mark_open REAL, mark_high REAL, mark_low REAL, mark_close REAL,
            idx_open REAL,  idx_high REAL,  idx_low REAL,  idx_close REAL
        );
        CREATE TABLE IF NOT EXISTS rubik_lsratio_global (
            ts INTEGER PRIMARY KEY,
            ratio REAL
        );
        CREATE TABLE IF NOT EXISTS rubik_lsratio_contract (
            ts INTEGER PRIMARY KEY,
            ratio REAL
        );
        CREATE TABLE IF NOT EXISTS rubik_lsratio_toptrader (
            ts INTEGER PRIMARY KEY,
            ratio REAL
        );
        CREATE TABLE IF NOT EXISTS rubik_oi_volume (
            ts INTEGER PRIMARY KEY,
            oi_usd REAL,
            volume_usd REAL
        );
        CREATE TABLE IF NOT EXISTS rubik_taker_spot (
            ts INTEGER PRIMARY KEY,
            buy_vol REAL,
            sell_vol REAL
        );
        CREATE TABLE IF NOT EXISTS rubik_taker_contract (
            ts INTEGER PRIMARY KEY,
            buy_vol REAL,
            sell_vol REAL
        );
        """
    )
    conn.commit()
    return conn


def fetch_funding(conn, start_ms, end_ms):
    """Paginate OKX funding-rate-history backwards from end_ms to start_ms."""
    cur = conn.cursor()
    after = end_ms
    total = 0
    while True:
        url = f"https://www.okx.com/api/v5/public/funding-rate-history?instId=BTC-USDT-SWAP&after={after}&limit=100"
        d = http_get(url)
        rows = d.get("data", [])
        if not rows:
            break
        oldest = None
        for r in rows:
            t = int(r["fundingTime"])
            if t < start_ms:
                continue
            cur.execute(
                "INSERT OR REPLACE INTO funding_rate VALUES (?,?,?,?)",
                (t, r["instId"], float(r["fundingRate"]), float(r.get("realizedRate", r["fundingRate"]))),
            )
            total += 1
            oldest = t
        if oldest is None or oldest <= start_ms:
            break
        after = oldest
        time.sleep(0.15)
    conn.commit()
    return total


def fetch_15m_candles(conn, start_ms, end_ms):
    """Paginate mark + index candles, merge by ts."""
    cur = conn.cursor()
    # mark
    after = end_ms
    mark_rows = {}
    while True:
        url = f"https://www.okx.com/api/v5/market/history-mark-price-candles?instId=BTC-USDT-SWAP&bar=15m&after={after}&limit=300"
        d = http_get(url)
        rows = d.get("data", [])
        if not rows:
            break
        oldest = None
        for r in rows:
            t = int(r[0])
            if t < start_ms:
                continue
            mark_rows[t] = (float(r[1]), float(r[2]), float(r[3]), float(r[4]))
            oldest = t if oldest is None else min(oldest, t)
        if oldest is None or oldest <= start_ms:
            break
        after = oldest
        time.sleep(0.15)
    # index
    after = end_ms
    idx_rows = {}
    while True:
        url = f"https://www.okx.com/api/v5/market/history-index-candles?instId=BTC-USDT&bar=15m&after={after}&limit=300"
        d = http_get(url)
        rows = d.get("data", [])
        if not rows:
            break
        oldest = None
        for r in rows:
            t = int(r[0])
            if t < start_ms:
                continue
            idx_rows[t] = (float(r[1]), float(r[2]), float(r[3]), float(r[4]))
            oldest = t if oldest is None else min(oldest, t)
        if oldest is None or oldest <= start_ms:
            break
        after = oldest
        time.sleep(0.15)
    keys = set(mark_rows) | set(idx_rows)
    for t in keys:
        m = mark_rows.get(t, (None, None, None, None))
        i = idx_rows.get(t, (None, None, None, None))
        cur.execute(
            "INSERT OR REPLACE INTO perp_candles_15m VALUES (?,?,?,?,?,?,?,?,?)",
            (t, m[0], m[1], m[2], m[3], i[0], i[1], i[2], i[3]),
        )
    conn.commit()
    return len(keys)


def fetch_rubik_simple(conn, url, table, value_cols, paginate=False):
    """Fetch a rubik endpoint. If paginate=True, walk backwards via `end=` until ~48h of data is collected."""
    cur = conn.cursor()
    base_url = url
    n_total = 0
    end_param = None
    seen_oldest = None
    max_pages = 8  # 8 * 100 = 800 5-min entries = ~67h, enough for 48h
    for _ in range(max_pages if paginate else 1):
        u = base_url + (f"&end={end_param}" if end_param else "")
        d = http_get(u)
        rows = d.get("data", [])
        if not rows:
            break
        for r in rows:
            t = int(r[0])
            vals = tuple(float(x) for x in r[1 : 1 + len(value_cols)])
            placeholders = ",".join("?" * (1 + len(value_cols)))
            cur.execute(f"INSERT OR REPLACE INTO {table} VALUES ({placeholders})", (t, *vals))
            n_total += 1
        oldest = min(int(r[0]) for r in rows)
        if seen_oldest is not None and oldest >= seen_oldest:
            break
        seen_oldest = oldest
        end_param = oldest
        if not paginate:
            break
        time.sleep(0.25)
    conn.commit()
    return n_total


def main():
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = init_db()

    # Kalshi window: btc_1m covers 2026-03-25 18:05 to 2026-04-24 16:10 UTC
    start_ms = 1774461900 * 1000
    end_ms = 1777047000 * 1000
    # Extend the end a bit so rubik may overlap recent (it won't for this range, but harmless)

    print("Fetching funding rates (Mar 25 - Apr 24)...")
    n = fetch_funding(conn, start_ms, end_ms)
    print(f"  funding_rate rows: {n}")

    print("Fetching 15m mark + index candles (Mar 25 - Apr 24)...")
    n = fetch_15m_candles(conn, start_ms, end_ms)
    print(f"  perp_candles_15m rows (historical): {n}")

    # Also fetch recent 48h (for rubik-overlap backtest)
    recent_end = int(time.time() * 1000)
    recent_start = recent_end - 50 * 3600 * 1000  # 50h to be safe
    print("Fetching 15m mark + index candles (recent 50h, for rubik overlap)...")
    n = fetch_15m_candles(conn, recent_start, recent_end)
    print(f"  perp_candles_15m rows (recent): {n}")

    print("Fetching rubik features (last ~48h, recent only)...")
    rubik = [
        ("https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy=BTC&period=5m&limit=500",
         "rubik_lsratio_global", ["ratio"], False),
        ("https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio-contract?instId=BTC-USDT-SWAP&period=5m&limit=100",
         "rubik_lsratio_contract", ["ratio"], True),
        ("https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio-contract-top-trader?instId=BTC-USDT-SWAP&period=5m&limit=100",
         "rubik_lsratio_toptrader", ["ratio"], True),
        ("https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume?ccy=BTC&period=5m&limit=500",
         "rubik_oi_volume", ["oi_usd", "volume_usd"], False),
        ("https://www.okx.com/api/v5/rubik/stat/taker-volume?ccy=BTC&instType=SPOT&period=5m&limit=500",
         "rubik_taker_spot", ["buy_vol", "sell_vol"], False),
        ("https://www.okx.com/api/v5/rubik/stat/taker-volume-contract?instId=BTC-USDT-SWAP&period=5m&limit=100",
         "rubik_taker_contract", ["buy_vol", "sell_vol"], True),
    ]
    for url, table, cols, pag in rubik:
        try:
            n = fetch_rubik_simple(conn, url, table, cols, paginate=pag)
            print(f"  {table}: {n}")
        except Exception as e:
            print(f"  {table} ERR {e}")
        time.sleep(0.3)

    conn.close()
    print(f"Done. DB: {DB}")


if __name__ == "__main__":
    main()
