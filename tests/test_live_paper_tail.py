from __future__ import annotations

from pathlib import Path

from kalshi_btc_engine_v2.storage.sqlite import connect, init_db, insert_record
from scripts.live_paper import TAIL_TABLES, _fetch_tail_rows, _tail_watermarks


def _seed_tail_db(db_path: Path) -> None:
    init_db(db_path)
    with connect(db_path) as conn:
        insert_record(
            conn,
            "kalshi_l2_event",
            {
                "received_ts_ms": 1_000,
                "exchange_ts_ms": 900,
                "market_ticker": "KXBTC15M-TEST",
                "event_type": "snapshot",
                "yes_levels_json": "[]",
                "no_levels_json": "[]",
            },
        )
        insert_record(
            conn,
            "kalshi_trade_event",
            {
                "received_ts_ms": 1_100,
                "exchange_ts_ms": 950,
                "market_ticker": "KXBTC15M-TEST",
                "trade_id": "t1",
                "side": "yes",
                "price": "0.5000",
                "count": "1",
            },
        )
        insert_record(
            conn,
            "spot_quote_event",
            {
                "received_ts_ms": 1_050,
                "exchange_ts_ms": 925,
                "venue": "fusion:median2of3",
                "symbol": "BTC-USD",
                "mid": "100000.0",
            },
        )
        conn.commit()


def test_fetch_tail_rows_uses_per_table_event_id_watermarks(tmp_path: Path) -> None:
    db_path = tmp_path / "tail.sqlite"
    _seed_tail_db(db_path)

    with connect(db_path) as conn:
        watermarks = dict.fromkeys(TAIL_TABLES, 0)
        rows = _fetch_tail_rows(conn, watermarks, limit_per_table=10)
        assert [row["table_name"] for row in rows] == [
            "kalshi_l2_event",
            "spot_quote_event",
            "kalshi_trade_event",
        ]

        watermarks["kalshi_l2_event"] = 1
        rows = _fetch_tail_rows(conn, watermarks, limit_per_table=10)
        assert [row["table_name"] for row in rows] == [
            "spot_quote_event",
            "kalshi_trade_event",
        ]


def test_tail_watermarks_can_start_near_live_tail(tmp_path: Path) -> None:
    db_path = tmp_path / "tail.sqlite"
    _seed_tail_db(db_path)

    with connect(db_path) as conn:
        watermarks = _tail_watermarks(conn, start_at_tail=True, warmup_lookback_s=0.02)

    assert watermarks["kalshi_l2_event"] == 1
    assert watermarks["kalshi_trade_event"] == 0
    assert watermarks["spot_quote_event"] == 1
