from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from typing import Any

from kalshi_btc_engine_v2.core.time import utc_now_ms
from kalshi_btc_engine_v2.storage.sqlite import insert_record


@dataclass(frozen=True, slots=True)
class ContinuityStats:
    source: str
    market_ticker: str | None
    total_messages: int
    sequence_gaps: int
    duplicate_sequences: int
    runtime_seconds: float
    window_start_ms: int | None
    window_end_ms: int | None
    details: dict[str, Any]

    def to_record(self) -> dict[str, Any]:
        return {
            "created_ts_ms": utc_now_ms(),
            "window_start_ms": self.window_start_ms,
            "window_end_ms": self.window_end_ms,
            "source": self.source,
            "market_ticker": self.market_ticker,
            "total_messages": self.total_messages,
            "sequence_gaps": self.sequence_gaps,
            "duplicate_sequences": self.duplicate_sequences,
            "runtime_seconds": self.runtime_seconds,
            "details_json": json.dumps(self.details, separators=(",", ":")),
        }


def analyze_kalshi_l2_rows(rows: list[dict[str, Any]]) -> list[ContinuityStats]:
    by_market: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_market.setdefault(str(row["market_ticker"]), []).append(row)

    stats: list[ContinuityStats] = []
    for market_ticker, market_rows in sorted(by_market.items()):
        ordered = sorted(
            market_rows,
            key=lambda item: (
                int(item.get("received_ts_ms") or 0),
                int(item.get("event_id") or 0),
            ),
        )
        previous_seq: int | None = None
        gaps = 0
        duplicates = 0
        seen: set[int] = set()
        for row in ordered:
            seq = row.get("seq")
            if seq is None:
                continue
            current = int(seq)
            if current in seen:
                duplicates += 1
            if previous_seq is not None and current > previous_seq + 1:
                gaps += current - previous_seq - 1
            if previous_seq is None or current > previous_seq:
                previous_seq = current
            seen.add(current)

        start = int(ordered[0]["received_ts_ms"]) if ordered else None
        end = int(ordered[-1]["received_ts_ms"]) if ordered else None
        runtime = ((end - start) / 1000) if start is not None and end is not None else 0.0
        stats.append(
            ContinuityStats(
                source="kalshi_l2_event",
                market_ticker=market_ticker,
                total_messages=len(ordered),
                sequence_gaps=gaps,
                duplicate_sequences=duplicates,
                runtime_seconds=runtime,
                window_start_ms=start,
                window_end_ms=end,
                details={
                    "first_seq": ordered[0].get("seq") if ordered else None,
                    "last_seq": previous_seq,
                    "messages_with_seq": len(seen),
                },
            )
        )
    return stats


def sqlite_continuity_report(
    conn: sqlite3.Connection, *, persist: bool = False
) -> list[ContinuityStats]:
    rows = [dict(row) for row in conn.execute("""
            SELECT event_id, received_ts_ms, market_ticker, seq
            FROM kalshi_l2_event
            ORDER BY market_ticker, received_ts_ms, event_id
            """).fetchall()]
    stats = analyze_kalshi_l2_rows(rows)
    if persist:
        for item in stats:
            insert_record(conn, "continuity_window", item.to_record())
        conn.commit()
    return stats


def continuity_json(stats: list[ContinuityStats]) -> str:
    return json.dumps([asdict(item) for item in stats], indent=2)
