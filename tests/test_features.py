from __future__ import annotations

import math
from decimal import Decimal

import pytest

from kalshi_btc_engine_v2.core.orderbook import KalshiOrderBook
from kalshi_btc_engine_v2.features import (
    BookDelta,
    EventFeatureInput,
    RollingFeatureEngine,
    TradePrint,
)


def _book(
    *,
    ticker: str = "KXBTC15M-TEST",
    yes: list[list[str]] | None = None,
    no: list[list[str]] | None = None,
) -> KalshiOrderBook:
    book = KalshiOrderBook(ticker)
    book.apply_snapshot(
        yes or [["0.48", "10"], ["0.47", "5"], ["0.46", "4"]],
        no or [["0.50", "6"], ["0.49", "7"], ["0.48", "8"]],
        seq=1,
    )
    return book


def test_book_features_depth_imbalance_and_delta_placeholders() -> None:
    engine = RollingFeatureEngine()
    snapshot = engine.consume(
        EventFeatureInput(
            event_time_ms=1_800_000_000_000,
            market_ticker="KXBTC15M-TEST",
            seconds_to_close=600.0,
            book=_book(),
            book_delta=BookDelta(side="yes", price=0.48, previous_size=10.0, new_size=14.0),
        )
    )

    assert snapshot.best_bid == pytest.approx(0.48)
    assert snapshot.best_ask == pytest.approx(0.50)
    assert snapshot.mid == pytest.approx(0.49)
    assert snapshot.spread == pytest.approx(0.02)
    assert snapshot.l1_queue_imbalance == pytest.approx((10 - 6) / (10 + 6))
    assert snapshot.depth_yes_bid[3] == pytest.approx(19.0)
    assert snapshot.depth_yes_ask[3] == pytest.approx(21.0)
    assert snapshot.depth_imbalance[3] == pytest.approx((19 - 21) / (19 + 21))
    assert snapshot.replenishment_size == pytest.approx(4.0)
    assert snapshot.cancel_size == pytest.approx(0.0)
    assert snapshot.cancel_add_size == pytest.approx(4.0)


def test_tape_features_use_rolling_time_windows() -> None:
    engine = RollingFeatureEngine(tape_windows_seconds=(5, 30))
    first = EventFeatureInput(
        event_time_ms=1_800_000_000_000,
        market_ticker="KXBTC15M-TEST",
        seconds_to_close=600.0,
        trade=TradePrint(side="yes", price=0.5, size=10.0),
    )
    second = EventFeatureInput(
        event_time_ms=1_800_000_003_000,
        market_ticker="KXBTC15M-TEST",
        seconds_to_close=597.0,
        trade=TradePrint(side="sell", price=0.49, size=4.0),
    )
    engine.consume(first)
    snapshot = engine.consume(second)

    assert snapshot.trade_count[5] == 2
    assert snapshot.signed_volume[5] == pytest.approx(6.0)
    assert snapshot.taker_pressure[5] == pytest.approx(6.0 / 14.0)

    later = engine.consume(
        EventFeatureInput(
            event_time_ms=1_800_000_010_000,
            market_ticker="KXBTC15M-TEST",
            seconds_to_close=590.0,
        )
    )
    assert later.trade_count[5] == 0
    assert later.trade_count[30] == 2


def test_btc_motion_bridges_to_vol_estimator_and_fair_probability() -> None:
    engine = RollingFeatureEngine(return_windows_seconds=(5, 30))
    base_ms = 1_800_000_000_000
    prices = [100_000.0, 100_100.0, 100_250.0]
    snapshot = None
    for idx, spot in enumerate(prices):
        snapshot = engine.consume(
            EventFeatureInput(
                event_time_ms=base_ms + (idx * 1000),
                market_ticker="KXBTC15M-TEST",
                seconds_to_close=600.0 - idx,
                spot=spot,
                strike=100_000.0,
            )
        )

    assert snapshot is not None
    assert snapshot.log_return_1s == pytest.approx(math.log(100_250.0 / 100_100.0))
    assert snapshot.rolling_returns[5] == pytest.approx(math.log(100_250.0 / 100_000.0))
    assert snapshot.vol_estimate is not None
    assert snapshot.realized_vol_annualized is not None
    assert snapshot.drift_annualized is not None
    assert snapshot.spot_fair_prob is not None
    assert snapshot.spot_fair_prob > 0.5
    assert snapshot.distance_to_strike == pytest.approx(250.0)
    assert snapshot.normalized_cliff_pressure is not None


def test_divergence_entropy_and_seconds_to_close_indexing() -> None:
    engine = RollingFeatureEngine()
    snapshot = engine.consume(
        EventFeatureInput(
            event_time_ms=1_800_000_000_000,
            market_ticker="KXBTC15M-TEST",
            seconds_to_close=123.0,
            book=_book(yes=[["0.60", "5"]], no=[["0.35", "5"]]),
            spot=100_000.0,
            strike=100_000.0,
        )
    )

    assert snapshot.index.market_ticker == "KXBTC15M-TEST"
    assert snapshot.index.seconds_to_close == pytest.approx(123.0)
    assert snapshot.binary_mid_prob == pytest.approx(0.625)
    assert snapshot.spot_fair_prob == pytest.approx(0.5, abs=1e-6)
    assert snapshot.logit_divergence == pytest.approx(math.log(0.625 / 0.375))
    assert snapshot.bernoulli_entropy_mid == pytest.approx(
        -(0.625 * math.log(0.625) + 0.375 * math.log(0.375))
    )
    assert snapshot.bernoulli_entropy_fair == pytest.approx(math.log(2), abs=1e-6)


def test_entropy_compression_is_rate_per_second() -> None:
    engine = RollingFeatureEngine()
    base_ms = 1_800_000_000_000
    first = engine.consume(
        EventFeatureInput(
            event_time_ms=base_ms,
            market_ticker="KXBTC15M-TEST",
            seconds_to_close=120.0,
            book=_book(yes=[["0.49", "5"]], no=[["0.50", "5"]]),
        )
    )
    second = engine.consume(
        EventFeatureInput(
            event_time_ms=base_ms + 2_000,
            market_ticker="KXBTC15M-TEST",
            seconds_to_close=118.0,
            book=_book(yes=[["0.80", "5"]], no=[["0.19", "5"]]),
        )
    )

    assert first.entropy_compression_rate is None
    assert second.bernoulli_entropy_mid is not None
    assert first.bernoulli_entropy_mid is not None
    expected = (first.bernoulli_entropy_mid - second.bernoulli_entropy_mid) / 2.0
    assert second.entropy_compression_rate == pytest.approx(expected)


def test_reflexivity_residual_and_liquidity_elasticity_are_deterministic() -> None:
    engine = RollingFeatureEngine(tape_windows_seconds=(30,))
    base_ms = 1_800_000_000_000
    engine.consume(
        EventFeatureInput(
            event_time_ms=base_ms,
            market_ticker="KXBTC15M-TEST",
            seconds_to_close=600.0,
            book=_book(yes=[["0.48", "10"]], no=[["0.50", "10"]]),
            spot=100_000.0,
        )
    )
    snapshot = engine.consume(
        EventFeatureInput(
            event_time_ms=base_ms + 1000,
            market_ticker="KXBTC15M-TEST",
            seconds_to_close=599.0,
            book=_book(yes=[["0.50", "20"]], no=[["0.46", "20"]]),
            spot=100_100.0,
            trade=TradePrint(side="yes", price=0.52, size=8.0),
        )
    )

    previous_mid = (0.48 + 0.50) / 2
    current_mid = (0.50 + 0.54) / 2
    expected_spot_move = math.log(100_100.0 / 100_000.0)
    assert snapshot.reflexivity_residual == pytest.approx(
        (current_mid - previous_mid) - expected_spot_move
    )
    expected_scaled_flow = Decimal("8") / Decimal("40")
    assert snapshot.liquidity_elasticity == pytest.approx(
        (current_mid - previous_mid) / float(expected_scaled_flow)
    )


def test_round_number_magnet_peaks_at_round_number() -> None:
    engine = RollingFeatureEngine(round_number_step_usd=1000.0)
    at_round = engine.consume(
        EventFeatureInput(
            event_time_ms=1,
            market_ticker="KXBTC15M-TEST",
            seconds_to_close=10.0,
            spot=100_000.0,
        )
    )
    off_round = engine.consume(
        EventFeatureInput(
            event_time_ms=2,
            market_ticker="KXBTC15M-TEST",
            seconds_to_close=9.0,
            spot=100_250.0,
        )
    )

    assert at_round.round_number_distance == pytest.approx(0.0)
    assert at_round.round_number_magnet == pytest.approx(1.0)
    assert off_round.round_number_distance == pytest.approx(250.0)
    assert off_round.round_number_magnet == pytest.approx(0.5)
