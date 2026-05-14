from __future__ import annotations

from pathlib import Path

from kalshi_btc_engine_v2.cli import _insert_smoke_data
from kalshi_btc_engine_v2.monitoring.continuity import sqlite_continuity_report
from kalshi_btc_engine_v2.replay.engine import DeterministicReplayer
from kalshi_btc_engine_v2.storage.sqlite import connect


def test_smoke_replay_and_continuity(tmp_path: Path) -> None:
    db_path = tmp_path / "engine.sqlite"
    start_ms, end_ms = _insert_smoke_data(db_path)

    with connect(db_path) as conn:
        ticks = list(DeterministicReplayer(conn).run(start_ms=start_ms, end_ms=end_ms))
        stats = sqlite_continuity_report(conn)

    assert len(ticks) == 7
    assert ticks[0].event.table == "kalshi_l2_event"
    assert "mid=" in ticks[0].summary()
    assert stats[0].total_messages == 2
    assert stats[0].sequence_gaps == 0
