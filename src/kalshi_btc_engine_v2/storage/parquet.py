from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


def export_query_to_parquet(
    conn: sqlite3.Connection,
    sql: str,
    output_path: str | Path,
    params: tuple[Any, ...] = (),
) -> int:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("Install pyarrow to export Parquet slices") from exc

    rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows)
    pq.write_table(table, output)
    return len(rows)
