# HANDOFF: owned by Claude (backtest/). Edit only via HANDOFF.md Open Request.
"""Backfill ``market_dim`` from captured Kalshi lifecycle events.

When the burn-in runner rolls over to a new market, earlier versions only
inserted a `kalshi_lifecycle_event` row — they did not upsert the new market's
metadata into `market_dim`. This leaves downstream analytics (backtester,
strike provider, settled-markets scanner) blind to all markets after the first.

This module scans captured lifecycle events, extracts each market's
``floor_strike``, ``determined`` outcome, and ``settled`` event, derives open
and close times from the ticker, and upserts a complete ``market_dim`` row.

Ticker grammar (observed): ``KXBTC15M-{YY}{MMM}{DD}{HHMM}-{MM}`` where the
``HHMM`` portion is the close time in US/Eastern (EDT/EST). For now we
hardcode EDT (UTC-4) — correct for May 2026 data; revisit for non-DST data.
"""

from __future__ import annotations

import contextlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kalshi_btc_engine_v2.core.time import utc_now_ms
from kalshi_btc_engine_v2.storage.sqlite import connect, upsert_market

_TICKER_RE = re.compile(r"^(KXBTC15M)-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})-(\d{2})$")
_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}


def parse_ticker_close_time(ticker: str, *, et_offset_hours: int = 4) -> datetime | None:
    """Return close time as a UTC datetime, parsing ``KXBTC15M-26MAY120815-15``."""
    match = _TICKER_RE.match(ticker)
    if not match:
        return None
    _, yy, mmm, dd, hh, mm, _suffix = match.groups()
    month = _MONTHS.get(mmm)
    if month is None:
        return None
    year = 2000 + int(yy)
    try:
        close_et_naive = datetime(year, month, int(dd), int(hh), int(mm))
    except ValueError:
        return None
    return (close_et_naive + timedelta(hours=et_offset_hours)).replace(tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def backfill_from_lifecycle(db_path: str | Path) -> dict[str, int]:
    """Read captured lifecycle events, upsert one ``market_dim`` row per ticker.

    Returns counts of markets discovered / upserted / settled.
    """
    db_path = Path(db_path)
    per_ticker: dict[str, dict[str, Any]] = {}
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT received_ts_ms, raw_json FROM kalshi_lifecycle_event "
            "WHERE raw_json LIKE '%KXBTC15M%' "
            "ORDER BY received_ts_ms"
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["raw_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            msg = payload.get("msg") if isinstance(payload.get("msg"), dict) else payload
            ticker = msg.get("market_ticker") or payload.get("next")
            if not ticker or not ticker.startswith("KXBTC15M"):
                continue
            slot = per_ticker.setdefault(
                ticker,
                {
                    "ticker": ticker,
                    "first_seen_ms": int(row["received_ts_ms"]),
                    "floor_strike": None,
                    "result": None,
                    "settlement_value": None,
                    "determination_ts": None,
                    "settled_ts": None,
                },
            )
            event_type = msg.get("event_type") or payload.get("event")
            if event_type == "metadata_updated" and msg.get("floor_strike") is not None:
                with contextlib.suppress(TypeError, ValueError):
                    slot["floor_strike"] = float(msg["floor_strike"])
            elif event_type == "determined":
                slot["result"] = msg.get("result")
                slot["settlement_value"] = msg.get("settlement_value")
                slot["determination_ts"] = msg.get("determination_ts")
            elif event_type == "settled":
                slot["settled_ts"] = msg.get("settled_ts")

        # Also seed any markets we have ONLY via the L2 stream (no lifecycle row).
        l2_tickers = {
            str(r[0])
            for r in conn.execute("SELECT DISTINCT market_ticker FROM kalshi_l2_event").fetchall()
        }
        for ticker in l2_tickers:
            per_ticker.setdefault(
                ticker,
                {
                    "ticker": ticker,
                    "first_seen_ms": utc_now_ms(),
                    "floor_strike": None,
                    "result": None,
                    "settlement_value": None,
                    "determination_ts": None,
                    "settled_ts": None,
                },
            )

        existing = {str(r[0]) for r in conn.execute("SELECT ticker FROM market_dim").fetchall()}
        upserted = 0
        settled = 0
        for ticker, slot in per_ticker.items():
            close_dt = parse_ticker_close_time(ticker)
            open_dt = close_dt - timedelta(minutes=15) if close_dt else None
            raw_payload = {
                "ticker": ticker,
                "floor_strike": slot["floor_strike"],
                "result": slot["result"],
                "settlement_value": slot["settlement_value"],
                "determination_ts": slot["determination_ts"],
                "settled_ts": slot["settled_ts"],
                "source": "backfill_from_lifecycle",
            }
            now_ms = slot["first_seen_ms"]
            record = {
                "ticker": ticker,
                "series_ticker": "KXBTC15M",
                "event_ticker": ticker.rsplit("-", 1)[0],
                "market_type": "binary",
                "title": "BTC price up in next 15 mins?",
                "open_time": _iso(open_dt) if open_dt else None,
                "close_time": _iso(close_dt) if close_dt else None,
                "expiration_time": _iso(close_dt) if close_dt else None,
                "settlement_source": "brti",
                "status": "settled" if slot["result"] else "active",
                "fee_type": "quadratic",
                "fee_multiplier": "0.07",
                "price_level_structure_json": "{}",
                "raw_json": json.dumps(raw_payload, separators=(",", ":")),
                "created_at_ms": now_ms,
                "updated_at_ms": utc_now_ms(),
            }
            if ticker in existing:
                # Don't blow away the original capture's richer raw_json.
                continue
            upsert_market(conn, record)
            upserted += 1
            if slot["result"]:
                settled += 1
        conn.commit()
        return {
            "lifecycle_tickers_seen": len(per_ticker),
            "already_in_market_dim": len(existing),
            "upserted": upserted,
            "settled": settled,
        }
