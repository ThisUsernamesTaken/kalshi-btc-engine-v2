from __future__ import annotations

SCHEMA_VERSION = 1


DDL = [
    """
    CREATE TABLE IF NOT EXISTS meta_schema_version (
        version INTEGER PRIMARY KEY,
        applied_ts_ms INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS market_dim (
        ticker TEXT PRIMARY KEY,
        series_ticker TEXT NOT NULL,
        event_ticker TEXT,
        market_type TEXT,
        title TEXT,
        open_time TEXT,
        close_time TEXT,
        expiration_time TEXT,
        settlement_source TEXT,
        status TEXT,
        fee_type TEXT,
        fee_multiplier TEXT,
        price_level_structure_json TEXT,
        raw_json TEXT NOT NULL,
        created_at_ms INTEGER NOT NULL,
        updated_at_ms INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kalshi_l2_event (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        received_ts_ms INTEGER NOT NULL,
        exchange_ts_ms INTEGER,
        seq INTEGER,
        market_ticker TEXT NOT NULL,
        event_type TEXT NOT NULL CHECK (event_type IN ('snapshot', 'delta')),
        side TEXT CHECK (side IS NULL OR side IN ('yes', 'no')),
        price TEXT,
        size TEXT,
        delta TEXT,
        yes_levels_json TEXT,
        no_levels_json TEXT,
        best_yes_bid TEXT,
        best_yes_ask TEXT,
        spread TEXT,
        source_channel TEXT,
        raw_json TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_kalshi_l2_time
    ON kalshi_l2_event(market_ticker, COALESCE(exchange_ts_ms, received_ts_ms), event_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_kalshi_l2_seq
    ON kalshi_l2_event(market_ticker, seq)
    """,
    """
    CREATE TABLE IF NOT EXISTS kalshi_trade_event (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        received_ts_ms INTEGER NOT NULL,
        exchange_ts_ms INTEGER,
        market_ticker TEXT NOT NULL,
        trade_id TEXT,
        side TEXT,
        taker_side TEXT,
        yes_price TEXT,
        no_price TEXT,
        price TEXT,
        count TEXT,
        raw_json TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_kalshi_trade_time
    ON kalshi_trade_event(market_ticker, COALESCE(exchange_ts_ms, received_ts_ms), event_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS kalshi_lifecycle_event (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        received_ts_ms INTEGER NOT NULL,
        exchange_ts_ms INTEGER,
        market_ticker TEXT,
        event_ticker TEXT,
        series_ticker TEXT,
        status TEXT,
        open_time TEXT,
        close_time TEXT,
        expiration_time TEXT,
        raw_json TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kalshi_user_order_event (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        received_ts_ms INTEGER NOT NULL,
        exchange_ts_ms INTEGER,
        market_ticker TEXT NOT NULL,
        order_id TEXT,
        client_order_id TEXT,
        status TEXT,
        side TEXT,
        action TEXT,
        price TEXT,
        count TEXT,
        filled_count TEXT,
        queue_position TEXT,
        raw_json TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kalshi_fill_event (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        received_ts_ms INTEGER NOT NULL,
        exchange_ts_ms INTEGER,
        market_ticker TEXT NOT NULL,
        order_id TEXT,
        client_order_id TEXT,
        trade_id TEXT,
        side TEXT,
        action TEXT,
        price TEXT,
        count TEXT,
        fee TEXT,
        raw_json TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kalshi_position_event (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        received_ts_ms INTEGER NOT NULL,
        exchange_ts_ms INTEGER,
        market_ticker TEXT NOT NULL,
        yes_count TEXT,
        no_count TEXT,
        realized_pnl TEXT,
        raw_json TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS spot_quote_event (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        received_ts_ms INTEGER NOT NULL,
        exchange_ts_ms INTEGER,
        venue TEXT NOT NULL,
        symbol TEXT NOT NULL,
        bid TEXT,
        ask TEXT,
        mid TEXT NOT NULL,
        last TEXT,
        label_confidence TEXT,
        raw_json TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_spot_quote_time
    ON spot_quote_event(symbol, venue, COALESCE(exchange_ts_ms, received_ts_ms), event_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS spot_trade_event (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        received_ts_ms INTEGER NOT NULL,
        exchange_ts_ms INTEGER,
        venue TEXT NOT NULL,
        symbol TEXT NOT NULL,
        trade_id TEXT,
        side TEXT,
        price TEXT NOT NULL,
        size TEXT,
        raw_json TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS decision_snapshot (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_ts_ms INTEGER NOT NULL,
        market_ticker TEXT NOT NULL,
        seconds_to_close REAL,
        regime TEXT,
        p_binary TEXT,
        p_spot TEXT,
        p_options TEXT,
        p_fair TEXT,
        edge_buy_yes TEXT,
        edge_sell_yes TEXT,
        action TEXT NOT NULL,
        veto_reason TEXT,
        feature_json TEXT,
        raw_json TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS replay_checkpoint (
        name TEXT PRIMARY KEY,
        table_name TEXT NOT NULL,
        event_id INTEGER NOT NULL,
        event_time_ms INTEGER NOT NULL,
        updated_ts_ms INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS continuity_window (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_ts_ms INTEGER NOT NULL,
        window_start_ms INTEGER,
        window_end_ms INTEGER,
        source TEXT NOT NULL,
        market_ticker TEXT,
        total_messages INTEGER NOT NULL,
        sequence_gaps INTEGER NOT NULL,
        duplicate_sequences INTEGER NOT NULL,
        runtime_seconds REAL NOT NULL,
        details_json TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS capture_health_event (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_ms INTEGER NOT NULL,
        source TEXT NOT NULL,
        event_kind TEXT NOT NULL CHECK (
            event_kind IN (
                'reconnect',
                'staleness_breach',
                'quorum_loss',
                'quorum_regained',
                'heartbeat'
            )
        ),
        detail_json TEXT
    )
    """,
]


def ddl_script() -> str:
    return ";\n".join(statement.strip() for statement in DDL) + ";"
