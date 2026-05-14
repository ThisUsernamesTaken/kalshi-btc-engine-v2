from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from kalshi_btc_engine_v2.core.time import utc_now_ms
from kalshi_btc_engine_v2.storage.schema import DDL, SCHEMA_VERSION


def connect(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path: str | Path) -> None:
    with connect(path) as conn:
        for statement in DDL:
            conn.execute(statement)
        conn.execute(
            "INSERT OR IGNORE INTO meta_schema_version(version, applied_ts_ms) VALUES (?, ?)",
            (SCHEMA_VERSION, utc_now_ms()),
        )
        conn.commit()


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if not rows:
        raise ValueError(f"unknown table: {table}")
    return {str(row["name"]) for row in rows}


def insert_record(conn: sqlite3.Connection, table: str, record: dict[str, Any]) -> int:
    columns = table_columns(conn, table)
    filtered = {key: value for key, value in record.items() if key in columns}
    if not filtered:
        raise ValueError(f"no insertable columns for {table}")
    column_names = list(filtered)
    placeholders = ", ".join("?" for _ in column_names)
    sql = f"INSERT INTO {table} ({', '.join(column_names)}) VALUES ({placeholders})"
    cursor = conn.execute(sql, tuple(filtered[name] for name in column_names))
    return int(cursor.lastrowid)


def upsert_market(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
    columns = table_columns(conn, "market_dim")
    filtered = {key: value for key, value in record.items() if key in columns}
    update_columns = [key for key in filtered if key != "ticker"]
    assignments = ", ".join(f"{key}=excluded.{key}" for key in update_columns)
    placeholders = ", ".join("?" for _ in filtered)
    sql = (
        f"INSERT INTO market_dim ({', '.join(filtered)}) VALUES ({placeholders}) "
        f"ON CONFLICT(ticker) DO UPDATE SET {assignments}"
    )
    conn.execute(sql, tuple(filtered.values()))


def bulk_insert(conn: sqlite3.Connection, table: str, records: Iterable[dict[str, Any]]) -> int:
    count = 0
    for record in records:
        insert_record(conn, table, record)
        count += 1
    return count


def fetch_all(
    conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()
) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]
