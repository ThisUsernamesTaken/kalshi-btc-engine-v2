from __future__ import annotations

import asyncio
import inspect
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from kalshi_btc_engine_v2.capture.burnin import BurnInConfig, BurnInRunner
from kalshi_btc_engine_v2.config import KalshiConfig, Settings, SpotConfig
from kalshi_btc_engine_v2.core.events import SpotQuote
from kalshi_btc_engine_v2.storage.sqlite import connect


class FakeRest:
    def __init__(self) -> None:
        self.create_order_calls = 0
        self.market_calls = 0

    async def get_markets(self, *, series_ticker: str, status: str | None = None) -> dict[str, Any]:
        self.market_calls += 1
        ticker = "KXBTC15M-1" if self.market_calls == 1 else "KXBTC15M-2"
        return {
            "markets": [
                {
                    "ticker": ticker,
                    "series_ticker": series_ticker,
                    "status": status or "open",
                    "close_time": f"2026-05-12T00:{15 * self.market_calls:02d}:00Z",
                }
            ]
        }

    async def get_market(self, ticker: str) -> dict[str, Any]:
        return {"market": {"ticker": ticker, "series_ticker": "KXBTC15M", "status": "open"}}

    async def create_order(self, order: dict[str, Any]) -> dict[str, Any]:
        self.create_order_calls += 1
        raise AssertionError(f"paper burn-in must not place orders: {order}")


class FakeWs:
    def __init__(self) -> None:
        self.subscriptions: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    async def messages(
        self,
        *,
        channels: list[str],
        market_tickers: list[str] | None = None,
        reconnect_delay_s: float = 2.0,
    ):
        del reconnect_delay_s
        if "market_lifecycle_v2" in channels:
            self.subscriptions.append((tuple(channels), tuple(market_tickers or ())))
            await asyncio.sleep(0)
            yield {
                "type": "market_lifecycle_v2",
                "msg": {"market_ticker": "KXBTC15M-1", "event_type": "deactivated"},
            }
            while True:
                await asyncio.sleep(0.01)
        ticker = market_tickers[0]
        self.subscriptions.append((tuple(channels), tuple(market_tickers)))
        if ticker == "KXBTC15M-1":
            yield {
                "type": "orderbook_snapshot",
                "msg": {"market_ticker": ticker, "seq": 1, "yes": [["0.50", "10"]], "no": []},
            }
        else:
            yield {
                "type": "orderbook_delta",
                "msg": {
                    "market_ticker": ticker,
                    "seq": 2,
                    "side": "yes",
                    "price": "0.51",
                    "size": "4",
                },
            }
            yield {
                "type": "trade",
                "msg": {
                    "market_ticker": ticker,
                    "trade_id": "t2",
                    "yes_price_dollars": "0.51",
                    "count_fp": "2.00",
                    "taker_outcome_side": "yes",
                    "taker_book_side": "ask",
                    "ts_ms": 1_800_000_000_100,
                },
            }
            if "fill" in channels:
                yield {
                    "type": "fill",
                    "msg": {
                        "market_ticker": ticker,
                        "order_id": "o1",
                        "trade_id": "f1",
                        "side": "yes",
                        "action": "buy",
                        "yes_price_dollars": "0.5100",
                        "count_fp": "1.00",
                        "fee_dollars": "0.01",
                    },
                }
            if "user_orders" in channels:
                yield {
                    "type": "user_order",
                    "msg": {
                        "ticker": ticker,
                        "order_id": "o1",
                        "client_order_id": "c1",
                        "status": "resting",
                        "side": "yes",
                        "yes_price_dollars": "0.5100",
                        "initial_count_fp": "1.00",
                        "fill_count_fp": "0.00",
                    },
                }
            if "market_positions" in channels:
                yield {
                    "type": "market_position",
                    "msg": {
                        "market_ticker": ticker,
                        "position_fp": "1.00",
                        "realized_pnl_dollars": "0.00",
                    },
                }
        while True:
            await asyncio.sleep(0.01)


class FakeSpotFeed:
    def __init__(self, venue: str, offsets_ms: list[int]) -> None:
        self.venue = venue
        self.offsets_ms = offsets_ms

    async def messages(self, *, interval_s: float | None = None):
        del interval_s
        base = 1_800_000_000_000
        for offset in self.offsets_ms:
            yield SpotQuote(
                received_ts_ms=base + offset,
                exchange_ts_ms=base + offset,
                venue=self.venue,
                symbol="BTC/USD",
                bid=Decimal("100.0"),
                ask=Decimal("102.0"),
                mid=Decimal("101.0"),
                last=Decimal("101.0"),
                raw_json='{"fake":true}',
            )
            await asyncio.sleep(0)
        while True:
            await asyncio.sleep(0.01)


def fake_settings(*, key_id: str | None = None, max_age_ms: int = 100) -> Settings:
    return Settings(
        environment="demo",
        live_enabled=False,
        data_dir=Path("data"),
        kalshi=KalshiConfig(
            series_ticker="KXBTC15M",
            rest_base_url="https://example.invalid",
            ws_url="wss://example.invalid",
            key_id=key_id,
            private_key_path=None,
        ),
        spot=SpotConfig(
            max_quote_age_ms=max_age_ms, min_venues=2, venues=("coinbase", "kraken", "bitstamp")
        ),
    )


def run_fake(tmp_path: Path, *, key_id: str | None = None, on_commit=None):
    rest = FakeRest()
    ws = FakeWs()
    runner = BurnInRunner(
        BurnInConfig(
            db_path=tmp_path / "burnin.sqlite",
            hours=0.0002,
            commit_events=2,
            commit_interval_s=60,
            heartbeat_interval_s=0.01,
            staleness_check_interval_s=60,
        ),
        settings=fake_settings(key_id=key_id),
        rest_client=rest,
        ws_client=ws,
        coinbase_feed=FakeSpotFeed("coinbase", [0]),
        kraken_feed=FakeSpotFeed("kraken", [10, 400]),
        bitstamp_feed=FakeSpotFeed("bitstamp", [420]),
        print_line=lambda _: None,
        on_commit=on_commit,
    )
    report = asyncio.run(runner.run())
    return report, rest, ws, runner.config.db_path


def test_rollover_across_two_markets(tmp_path: Path) -> None:
    report, _, ws, db_path = run_fake(tmp_path)

    assert report.rollover_count == 1
    assert report.market_tickers_captured == ["KXBTC15M-1", "KXBTC15M-2"]
    assert ("KXBTC15M-1",) in [sub[1] for sub in ws.subscriptions]
    assert ("KXBTC15M-2",) in [sub[1] for sub in ws.subscriptions]
    assert () in [sub[1] for sub in ws.subscriptions]
    with connect(db_path) as conn:
        lifecycle_count = conn.execute("SELECT COUNT(*) FROM kalshi_lifecycle_event").fetchone()[0]
    assert lifecycle_count >= 2


def test_spot_quorum_loss_and_regain(tmp_path: Path) -> None:
    report, _, _, db_path = run_fake(tmp_path)

    assert report.quorum_loss_count >= 1
    assert report.quorum_regained_count >= 1
    assert report.spot_fusion_quorum_coverage > 0
    with connect(db_path) as conn:
        kinds = [
            row[0]
            for row in conn.execute(
                "SELECT event_kind FROM capture_health_event ORDER BY event_id"
            ).fetchall()
        ]
    assert "quorum_loss" in kinds
    assert "quorum_regained" in kinds


def test_commit_batching(tmp_path: Path) -> None:
    commits = 0

    def on_commit() -> None:
        nonlocal commits
        commits += 1

    report, _, _, _ = run_fake(tmp_path, on_commit=on_commit)

    assert commits >= 2
    assert sum(report.row_counts_by_source.values()) >= 5


def test_cancellation_cleanup_closes_db(tmp_path: Path) -> None:
    runner = BurnInRunner(
        BurnInConfig(db_path=tmp_path / "cancel.sqlite", hours=1, heartbeat_interval_s=60),
        settings=fake_settings(),
        rest_client=FakeRest(),
        ws_client=FakeWs(),
        coinbase_feed=FakeSpotFeed("coinbase", [0]),
        kraken_feed=FakeSpotFeed("kraken", [10]),
        bitstamp_feed=FakeSpotFeed("bitstamp", [20]),
        print_line=lambda _: None,
    )

    async def cancel_run() -> None:
        task = asyncio.create_task(runner.run())
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_run())
    with connect(runner.config.db_path) as conn:
        conn.execute("SELECT COUNT(*) FROM capture_health_event").fetchone()


def test_no_order_placement_path_exists_or_is_called(tmp_path: Path) -> None:
    report, rest, _, _ = run_fake(tmp_path, key_id="fake-key")

    assert report.runtime_seconds > 0
    assert rest.create_order_calls == 0
    source = inspect.getsource(BurnInRunner)
    assert "create_order" not in source


def test_private_streams_are_persisted_when_credentials_present(tmp_path: Path) -> None:
    _, _, _, db_path = run_fake(tmp_path, key_id="fake-key")

    with connect(db_path) as conn:
        fills = conn.execute("SELECT COUNT(*) FROM kalshi_fill_event").fetchone()[0]
        orders = conn.execute("SELECT COUNT(*) FROM kalshi_user_order_event").fetchone()[0]
        positions = conn.execute("SELECT COUNT(*) FROM kalshi_position_event").fetchone()[0]

    assert fills == 1
    assert orders == 1
    assert positions == 1
