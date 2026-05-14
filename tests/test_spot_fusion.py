from __future__ import annotations

from decimal import Decimal

from kalshi_btc_engine_v2.adapters.spot import SpotQuote, fuse_spot_quotes


def test_two_of_three_fusion_uses_median_and_confidence() -> None:
    now = 1_000_000
    quotes = [
        SpotQuote(now - 100, "coinbase", "BTC-USD", None, None, Decimal("100.0")),
        SpotQuote(now - 200, "kraken", "BTC/USD", None, None, Decimal("101.0")),
        SpotQuote(now - 5_000, "bitstamp", "btcusd", None, None, Decimal("120.0")),
    ]

    fused = fuse_spot_quotes(quotes, now_ms=now, max_age_ms=1500, min_venues=2)

    assert fused is not None
    assert fused.quote.mid == Decimal("100.5")
    assert fused.quote.label_confidence == Decimal("0.6666666666666666666666666667")
    assert fused.source_venues == ("coinbase", "kraken")
