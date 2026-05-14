# HANDOFF: owned by Claude (backtest/). Edit only via HANDOFF.md Open Request.
"""Settlement outcome scanner for captured market data.

Reads ``market_dim`` rows from a captured SQLite, identifies settled markets
(non-null `result`), and emits per-market (ticker, realized_yes_outcome) tuples.

Used by the error tracker to learn calibration from realized outcomes after a
burn-in / paper-trade session completes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from kalshi_btc_engine_v2.storage.sqlite import connect


@dataclass(frozen=True, slots=True)
class SettledMarket:
    market_ticker: str
    yes_won: int
    settlement_value_dollars: float | None
    close_time: str | None


def _coerce_outcome(result_field: str | None, raw_json: str | None) -> int | None:
    """Return 1 if YES won, 0 if NO won, None if not yet settled / unclear."""
    if result_field:
        normalized = result_field.strip().lower()
        if normalized in {"yes", "y", "1", "true"}:
            return 1
        if normalized in {"no", "n", "0", "false"}:
            return 0
    if raw_json:
        try:
            payload = json.loads(raw_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        for key in ("result", "settlement_value", "outcome"):
            val = payload.get(key)
            if val is None:
                continue
            normalized = str(val).strip().lower()
            if normalized in {"yes", "y", "1", "true"}:
                return 1
            if normalized in {"no", "n", "0", "false"}:
                return 0
            try:
                num = float(val)
                if num >= 0.99:
                    return 1
                if num <= 0.01:
                    return 0
            except (TypeError, ValueError):
                continue
    return None


def scan_settled_markets(db_path: str | Path) -> list[SettledMarket]:
    out: list[SettledMarket] = []
    with connect(db_path) as conn:
        rows = conn.execute("SELECT ticker, raw_json, close_time FROM market_dim").fetchall()
    for row in rows:
        ticker = str(row["ticker"])
        raw_json = row["raw_json"]
        # market_dim does not have a dedicated 'result' column; settlement comes
        # via raw_json or a future lifecycle update.
        outcome = _coerce_outcome(None, raw_json)
        if outcome is None:
            continue
        settlement_value: float | None = None
        if raw_json:
            try:
                payload = json.loads(raw_json)
                raw_val = payload.get("settlement_value_dollars") or payload.get("settlement_value")
                if raw_val is not None:
                    settlement_value = float(raw_val)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        out.append(
            SettledMarket(
                market_ticker=ticker,
                yes_won=outcome,
                settlement_value_dollars=settlement_value,
                close_time=row["close_time"],
            )
        )
    return out
