# Warehouse Schema

The schema is append-first and replay-oriented. Decimal values are stored as strings to preserve
fixed-point venue data exactly.

Core tables:

- `market_dim`: Kalshi market metadata, lifecycle clocks, settlement source, fee metadata, and raw
  market JSON.
- `kalshi_l2_event`: order-book snapshots and deltas with reconstructed BBO fields.
- `kalshi_trade_event`: public Kalshi prints.
- `kalshi_lifecycle_event`: market status and lifecycle messages.
- `kalshi_user_order_event`: authenticated user order state changes.
- `kalshi_fill_event`: authenticated fills.
- `kalshi_position_event`: authenticated position snapshots/updates.
- `spot_quote_event`: Coinbase, Kraken, Bitstamp, and fused BTC quotes.
- `spot_trade_event`: BTC spot public trades when available.
- `decision_snapshot`: future model/policy audit log.
- `replay_checkpoint`: named replay positions.
- `continuity_window`: persisted continuity summary rows.
- `capture_health_event`: burn-in runner reconnect, staleness, quorum, and heartbeat events.

Run `python -m kalshi_btc_engine_v2.cli print-ddl` to inspect the exact SQLite DDL.
