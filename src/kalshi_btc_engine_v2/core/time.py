from __future__ import annotations

from datetime import UTC, datetime


def utc_now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def parse_rfc3339_ms(value: str | None) -> int | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    return int(datetime.fromisoformat(normalized).timestamp() * 1000)
