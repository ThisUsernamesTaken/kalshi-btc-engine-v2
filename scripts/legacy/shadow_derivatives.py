"""Shadow engine: derivatives microstructure paper trader.

Polls OKX REST every 15 minutes (aligned to :00/:15/:30/:45 UTC). At each
boundary:
  1) Settle the previous paper trade (if any) using the new mark-price direction.
  2) Compute current derivatives features and the signal.
  3) Open a new paper trade if the signal fires.
  4) Append a JSONL record for every event.

Primary signal (from backtest): B.taker-contract-follow(1.2/0.8)
  - taker_buy_vol / taker_sell_vol on BTC-USDT-SWAP, period=5m, latest bar
  - signal = 'YES' if ratio > 1.20, 'NO' if ratio < 0.80, else None

All other features are logged (account L/S, top trader L/S, OI delta,
spot taker ratio, funding rate, perp premium) so a richer model can be
trained later from the recorded JSONL.

PAPER ONLY. Does not import KalshiClient. Does not place real orders.

Usage:
  python scripts/shadow_derivatives.py [--log data/shadow_derivatives.jsonl]
  python scripts/shadow_derivatives.py --once   # do one boundary cycle and exit (smoke test)
"""

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
DEFAULT_LOG = Path(__file__).resolve().parent.parent / "data" / "shadow_derivatives.jsonl"

# Signal thresholds (from backtest)
TAKER_HI = 1.20
TAKER_LO = 0.80


def http_get_json(url, retries=3, timeout=10):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
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


def fetch_features():
    """Snapshot all derivatives features at the current moment.
    Returns a dict; missing values are None.
    """
    out = {
        "fetched_at_ms": int(time.time() * 1000),
        "taker_contract_buy": None, "taker_contract_sell": None, "taker_contract_ratio": None,
        "taker_spot_buy": None, "taker_spot_sell": None, "taker_spot_ratio": None,
        "lsratio_global": None,
        "lsratio_contract": None,
        "lsratio_toptrader": None,
        "oi_usd_now": None, "oi_usd_15m_ago": None, "oi_delta_15m": None,
        "oi_usd_30m_ago": None, "oi_delta_30m": None,
        "funding_rate": None, "funding_ts_ms": None,
        "perp_mark": None, "spot_index": None, "perp_premium_bps": None,
        "mark_close_15m_ago": None, "perp_price_change_15m": None,
    }

    # Taker volume contract (latest 5m bar)
    try:
        d = http_get_json("https://www.okx.com/api/v5/rubik/stat/taker-volume-contract?instId=BTC-USDT-SWAP&period=5m&limit=1")
        rows = d.get("data", [])
        if rows:
            buy, sell = float(rows[0][1]), float(rows[0][2])
            out["taker_contract_buy"] = buy
            out["taker_contract_sell"] = sell
            out["taker_contract_ratio"] = (buy / sell) if sell > 0 else None
    except Exception as e:
        out["err_taker_contract"] = str(e)

    # Taker volume spot
    try:
        d = http_get_json("https://www.okx.com/api/v5/rubik/stat/taker-volume?ccy=BTC&instType=SPOT&period=5m&limit=1")
        rows = d.get("data", [])
        if rows:
            buy, sell = float(rows[0][1]), float(rows[0][2])
            out["taker_spot_buy"] = buy
            out["taker_spot_sell"] = sell
            out["taker_spot_ratio"] = (buy / sell) if sell > 0 else None
    except Exception as e:
        out["err_taker_spot"] = str(e)

    # L/S ratios
    try:
        d = http_get_json("https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy=BTC&period=5m&limit=1")
        rows = d.get("data", [])
        if rows:
            out["lsratio_global"] = float(rows[0][1])
    except Exception as e:
        out["err_lsratio_global"] = str(e)

    try:
        d = http_get_json("https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio-contract?instId=BTC-USDT-SWAP&period=5m&limit=1")
        rows = d.get("data", [])
        if rows:
            out["lsratio_contract"] = float(rows[0][1])
    except Exception as e:
        out["err_lsratio_contract"] = str(e)

    try:
        d = http_get_json("https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio-contract-top-trader?instId=BTC-USDT-SWAP&period=5m&limit=1")
        rows = d.get("data", [])
        if rows:
            out["lsratio_toptrader"] = float(rows[0][1])
    except Exception as e:
        out["err_lsratio_toptrader"] = str(e)

    # OI delta — pull 7 bars (35 min span) and compute now vs 15m / 30m ago
    try:
        d = http_get_json("https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume?ccy=BTC&period=5m&limit=7")
        rows = d.get("data", [])
        if rows:
            # rows[0] = most recent
            oi_now = float(rows[0][1])
            out["oi_usd_now"] = oi_now
            if len(rows) > 3:
                oi_15 = float(rows[3][1])
                out["oi_usd_15m_ago"] = oi_15
                out["oi_delta_15m"] = (oi_now - oi_15) / oi_15 if oi_15 else None
            if len(rows) > 6:
                oi_30 = float(rows[6][1])
                out["oi_usd_30m_ago"] = oi_30
                out["oi_delta_30m"] = (oi_now - oi_30) / oi_30 if oi_30 else None
    except Exception as e:
        out["err_oi"] = str(e)

    # Funding rate (most recent)
    try:
        d = http_get_json("https://www.okx.com/api/v5/public/funding-rate-history?instId=BTC-USDT-SWAP&limit=1")
        rows = d.get("data", [])
        if rows:
            out["funding_rate"] = float(rows[0]["fundingRate"])
            out["funding_ts_ms"] = int(rows[0]["fundingTime"])
    except Exception as e:
        out["err_funding"] = str(e)

    # Perp mark vs spot index (15m candles, last 5 to also get 15-min-ago mark)
    try:
        mark = http_get_json("https://www.okx.com/api/v5/market/mark-price-candles?instId=BTC-USDT-SWAP&bar=15m&limit=5")
        idx = http_get_json("https://www.okx.com/api/v5/market/index-candles?instId=BTC-USDT&bar=15m&limit=5")
        mrows = mark.get("data", [])
        irows = idx.get("data", [])
        if mrows and irows:
            mark_close = float(mrows[0][4])
            idx_close = float(irows[0][4])
            out["perp_mark"] = mark_close
            out["spot_index"] = idx_close
            out["perp_premium_bps"] = (mark_close - idx_close) / idx_close * 1e4 if idx_close else None
            if len(mrows) >= 2:
                mark_prev = float(mrows[1][4])
                out["mark_close_15m_ago"] = mark_prev
                out["perp_price_change_15m"] = (mark_close - mark_prev) / mark_prev if mark_prev else None
    except Exception as e:
        out["err_perp"] = str(e)

    return out


def compute_signal(features):
    """Primary signal: B.taker-contract-follow(1.2/0.8). Returns (side, confidence_components)."""
    components = {
        "primary": None,
        "taker_contract_ratio": features.get("taker_contract_ratio"),
        "lsratio_toptrader": features.get("lsratio_toptrader"),
        "lsratio_global": features.get("lsratio_global"),
        "oi_delta_15m": features.get("oi_delta_15m"),
        "perp_premium_bps": features.get("perp_premium_bps"),
        "funding_rate": features.get("funding_rate"),
    }
    ratio = features.get("taker_contract_ratio")
    side = None
    if ratio is not None:
        if ratio > TAKER_HI:
            side = "YES"
        elif ratio < TAKER_LO:
            side = "NO"
    components["primary"] = side

    # Auxiliary "agreement count" for confidence: how many of the other signals
    # would have voted the same way (informational only, not used to gate).
    votes_yes = votes_no = 0
    if features.get("lsratio_toptrader") is not None:
        v = features["lsratio_toptrader"]
        # 0.55/0.45 = 1.222; fade signal
        if v > 1.222: votes_no += 1
        elif v < 0.818: votes_yes += 1
    if features.get("perp_premium_bps") is not None:
        p = features["perp_premium_bps"]
        if p > 1.0: votes_yes += 1  # follow
        elif p < -1.0: votes_no += 1
    if features.get("funding_rate") is not None:
        f = features["funding_rate"]
        # follow at 5bp/8h
        if f > 0.00005: votes_yes += 1
        elif f < -0.00005: votes_no += 1
    if features.get("oi_delta_15m") is not None and features.get("perp_price_change_15m") is not None:
        oi = features["oi_delta_15m"]; px = features["perp_price_change_15m"]
        if abs(oi) > 0.001:
            if oi > 0:
                votes_yes += 1 if px > 0 else 0
                votes_no += 1 if px < 0 else 0
            else:
                votes_no += 1 if px > 0 else 0
                votes_yes += 1 if px < 0 else 0

    components["aux_votes_yes"] = votes_yes
    components["aux_votes_no"] = votes_no
    components["aux_agree_with_primary"] = (
        (side == "YES" and votes_yes > votes_no) or
        (side == "NO" and votes_no > votes_yes)
    )
    return side, components


def fee_per_contract(p: float) -> float:
    return math.ceil(0.07 * p * (1.0 - p) * 100.0) / 100.0


def pnl_neutral(side: str, outcome_yes: bool, entry=0.50):
    win = outcome_yes if side == "YES" else (not outcome_yes)
    fee = fee_per_contract(entry)
    return (1.0 if win else 0.0) - entry - fee


def next_quarter_hour_ms(now_ms: int = None) -> int:
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    quarter = 15 * 60 * 1000
    return ((now_ms // quarter) + 1) * quarter


def log_event(log_path: Path, record: dict):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


def boundary_iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def run_once(log_path: Path, pending_trade: dict | None):
    """One boundary cycle. Returns the new pending_trade (or None if no signal fired)."""
    now_ms = int(time.time() * 1000)
    features = fetch_features()
    side, components = compute_signal(features)

    settled = None
    if pending_trade is not None:
        # Settle previous trade. Outcome = mark price now > mark price at entry.
        entry_mark = pending_trade.get("entry_mark")
        cur_mark = features.get("perp_mark")
        if entry_mark is not None and cur_mark is not None:
            outcome_yes = cur_mark > entry_mark
            pnl = pnl_neutral(pending_trade["side"], outcome_yes)
            win = pnl > 0
            settled = {
                "kind": "settle",
                "ts_ms": now_ms,
                "ts_iso": boundary_iso(now_ms),
                "entry_ts_ms": pending_trade["ts_ms"],
                "side": pending_trade["side"],
                "entry_price": 0.50,
                "entry_mark": entry_mark,
                "settle_mark": cur_mark,
                "outcome_yes": outcome_yes,
                "win": win,
                "pnl_neutral": round(pnl, 4),
                "fee": fee_per_contract(0.50),
            }
            log_event(log_path, settled)
        else:
            log_event(log_path, {
                "kind": "settle_error",
                "ts_ms": now_ms,
                "ts_iso": boundary_iso(now_ms),
                "reason": "missing mark price",
                "pending": pending_trade,
            })

    # Always log the feature snapshot
    feature_record = {
        "kind": "features",
        "ts_ms": now_ms,
        "ts_iso": boundary_iso(now_ms),
        **features,
    }
    log_event(log_path, feature_record)

    new_pending = None
    if side in ("YES", "NO"):
        entry_mark = features.get("perp_mark")
        new_pending = {
            "kind": "entry",
            "ts_ms": now_ms,
            "ts_iso": boundary_iso(now_ms),
            "side": side,
            "entry_price": 0.50,
            "entry_mark": entry_mark,
            "components": components,
        }
        log_event(log_path, new_pending)
    else:
        log_event(log_path, {
            "kind": "no_signal",
            "ts_ms": now_ms,
            "ts_iso": boundary_iso(now_ms),
            "components": components,
        })
    return new_pending, settled


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", default=str(DEFAULT_LOG), help="JSONL output path")
    parser.add_argument("--once", action="store_true", help="One cycle then exit (smoke test)")
    parser.add_argument("--align", action="store_true", default=True, help="Sleep until next :00/:15/:30/:45")
    args = parser.parse_args()

    log_path = Path(args.log)
    pending = None

    # Recover pending trade from log (last entry record with no matching settle after it)
    if log_path.exists():
        last_entry = None
        last_settle_ms = 0
        for line in log_path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("kind") == "entry":
                last_entry = rec
            elif rec.get("kind") == "settle":
                last_settle_ms = rec.get("ts_ms", 0)
        if last_entry and last_entry.get("ts_ms", 0) > last_settle_ms:
            pending = last_entry
            print(f"[recover] pending trade from {last_entry['ts_iso']} side={last_entry['side']}")

    if args.once:
        new_pending, settled = run_once(log_path, pending)
        print(json.dumps({"settled": settled, "new_pending": new_pending}, default=str, indent=2))
        return

    while True:
        next_ms = next_quarter_hour_ms()
        sleep_s = (next_ms - int(time.time() * 1000)) / 1000.0
        if sleep_s > 0:
            print(f"[wait] sleeping {sleep_s:.0f}s until {boundary_iso(next_ms)}")
            try:
                time.sleep(sleep_s)
            except KeyboardInterrupt:
                print("[stop] interrupt")
                return
        # Run boundary cycle
        try:
            new_pending, settled = run_once(log_path, pending)
            if settled:
                print(f"[settle] side={settled['side']} pnl={settled['pnl_neutral']:+.2f} win={settled['win']}")
            if new_pending:
                print(f"[entry] side={new_pending['side']} ratio={new_pending['components']['taker_contract_ratio']:.3f}")
            else:
                print(f"[idle] no signal")
            pending = new_pending
        except Exception as e:
            print(f"[err] {e!r}", file=sys.stderr)
            log_event(log_path, {
                "kind": "error",
                "ts_ms": int(time.time() * 1000),
                "ts_iso": boundary_iso(int(time.time() * 1000)),
                "error": repr(e),
            })


if __name__ == "__main__":
    main()
