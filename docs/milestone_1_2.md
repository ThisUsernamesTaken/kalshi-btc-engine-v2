# Milestone 1 and 2 Stop Point

Implemented scope:

1. Market adapters
   - Kalshi REST signing and public/authenticated request wrapper.
   - Kalshi WebSocket subscription wrapper with optional auth headers.
   - Kalshi order-book normalization for bid-only YES/NO books and implied YES asks.
   - Coinbase and Kraken WebSocket ticker/trade parsers.
   - Bitstamp public ticker poll adapter.
   - 2-of-3 median spot fusion with `label_confidence`.

2. Warehouse and replay
   - SQLite DDL for normalized raw-event capture and downstream decision snapshots.
   - Optional Parquet export helper for archival slices.
   - Deterministic event-time replay ordered by exchange timestamp, receive timestamp, and event id.
   - Continuity reporting for Kalshi sequence gaps, duplicates, message counts, and runtime.

Not implemented yet:

- Settlement-aware fair probability.
- Isotonic calibration.
- Regime classifier.
- Decision policy.
- Order execution.
- Live order placement.

Open operational gate:

- A real 24-hour burn-in must be run before continuing. The scaffold can compute the stats, but this
  setup pass did not sit on live feeds for 24 hours.
