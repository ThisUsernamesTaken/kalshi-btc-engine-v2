from __future__ import annotations

from decimal import Decimal

from kalshi_btc_engine_v2.adapters.kalshi import apply_l2_payload
from kalshi_btc_engine_v2.core.orderbook import KalshiOrderBook


def test_kalshi_bid_only_book_implies_yes_ask() -> None:
    book = KalshiOrderBook("KXBTC15M-TEST")
    status = book.apply_snapshot(
        yes_levels=[["0.4800", "10"], ["0.4900", "20"]],
        no_levels=[["0.5000", "15"]],
        seq=10,
    )

    assert status.gap is False
    assert book.best_yes_bid == Decimal("0.4900")
    assert book.best_yes_ask == Decimal("0.5000")
    assert book.mid_yes == Decimal("0.4950")
    assert book.spread_yes == Decimal("0.0100")


def test_sequence_gap_and_duplicate_tracking() -> None:
    book = KalshiOrderBook("KXBTC15M-TEST")
    book.apply_snapshot([], [], seq=10)
    duplicate = book.apply_delta("yes", "0.50", "1", seq=10)
    gap = book.apply_delta("yes", "0.51", "1", seq=13)

    assert duplicate.duplicate is True
    assert gap.gap is True
    assert book.duplicates == 1
    assert book.gaps == 2


def test_current_kalshi_fp_snapshot_and_delta_payload_names() -> None:
    book = KalshiOrderBook("KXBTC15M-TEST")
    snapshot = apply_l2_payload(
        book,
        {
            "type": "orderbook_snapshot",
            "seq": 1,
            "msg": {
                "market_ticker": "KXBTC15M-TEST",
                "yes_dollars_fp": [["0.4800", "10.00"]],
                "no_dollars_fp": [["0.5100", "12.00"]],
            },
        },
    )
    delta = apply_l2_payload(
        book,
        {
            "type": "orderbook_delta",
            "seq": 2,
            "msg": {
                "market_ticker": "KXBTC15M-TEST",
                "side": "yes",
                "price_dollars": "0.4800",
                "delta_fp": "-4.00",
                "ts_ms": 1_800_000_000_000,
            },
        },
    )

    assert snapshot is not None
    assert delta is not None
    assert delta.exchange_ts_ms == 1_800_000_000_000
    assert book.yes_bids[Decimal("0.4800")] == Decimal("6.00")
    assert book.best_yes_ask == Decimal("0.4900")
