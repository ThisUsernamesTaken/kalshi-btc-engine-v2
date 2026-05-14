# Paper Capture Burn-In

Milestone 2.5 adds a paper-only capture runner:

```powershell
python -m kalshi_btc_engine_v2.cli capture-burnin --db .\data\burnin.sqlite --hours 4
```

Use `--market-ticker KXBTC15M-...` to pin the first Kalshi market; otherwise the runner
discovers the active open `KXBTC15M` market. When lifecycle data shows the current market is no
longer open, the runner discovers the next open market, logs a `kalshi_lifecycle_event`, prints a
rollover line, and continues until the requested duration elapses.

The runner captures Kalshi public order book, public trades, and lifecycle streams. Authenticated
private channels are subscribed only when `ENGINE_V2_KALSHI_KEY_ID` is present; otherwise startup
prints that capture is public-data-only. It also captures Coinbase and Kraken BTC/USD WebSocket
quotes and Bitstamp BTC/USD by 1s polling.

Rows are committed every 500 events or 2 seconds, whichever comes first. `SIGINT`/Ctrl+C sets a
clean stop flag and closes the SQLite connection after a final commit. The runner emits one console
heartbeat per minute with message rates, spot quorum, active market ticker, and elapsed time, and
persists health rows in `capture_health_event`.

The completion report prints runtime, message rates, row counts, Kalshi sequence gaps and
duplicates, reconnect and health counts, max spot staleness, spot quorum coverage, rollover count,
and captured market tickers.
