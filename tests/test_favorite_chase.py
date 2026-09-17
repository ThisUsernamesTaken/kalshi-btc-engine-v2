from __future__ import annotations

from kalshi_btc_engine_v2.strategies.favorite_chase import (
    ENTRY_AFTER_MS,
    ENTRY_TRIGGER_PRICE,
    STOP_PRICE,
    BookTick,
    Side,
    TradeTick,
    ask_for_side,
    bid_for_side,
    detect_entry,
    detect_stop,
    mid_for_side,
)

SESSION_OPEN_MS = 1_700_000_000_000  # arbitrary


def _trade(after_s: float, yes: float, no: float | None = None) -> TradeTick:
    return TradeTick(
        ts_ms=SESSION_OPEN_MS + int(after_s * 1000),
        yes_price=yes,
        no_price=(1.0 - yes) if no is None else no,
    )


def test_no_entry_before_t8():
    # Hits 80c YES at T+7m → must NOT trigger.
    t = _trade(7 * 60, 0.80)
    assert detect_entry(t, SESSION_OPEN_MS) is None


def test_entry_at_t8_threshold_yes():
    t = _trade(8 * 60, 0.75)
    assert detect_entry(t, SESSION_OPEN_MS) is Side.YES


def test_entry_no_side():
    # NO=0.80 means YES=0.20; should trigger NO.
    t = _trade(9 * 60, 0.20)
    assert detect_entry(t, SESSION_OPEN_MS) is Side.NO


def test_no_entry_below_threshold():
    t = _trade(10 * 60, 0.74)  # NO=0.26
    assert detect_entry(t, SESSION_OPEN_MS) is None


def test_yes_wins_tie():
    # At exactly 0.75/0.25 both sides clear; YES side wins by tiebreaker.
    t = TradeTick(ts_ms=SESSION_OPEN_MS + ENTRY_AFTER_MS, yes_price=0.75, no_price=0.25)
    assert detect_entry(t, SESSION_OPEN_MS) is Side.YES


def test_side_quote_helpers():
    b = BookTick(ts_ms=0, yes_bid=0.70, yes_ask=0.78)
    assert ask_for_side(b, Side.YES) == 0.78
    assert bid_for_side(b, Side.YES) == 0.70
    assert abs(mid_for_side(b, Side.YES) - 0.74) < 1e-9  # type: ignore[arg-type]
    # NO derives: bid = 1 - yes_ask = 0.22; ask = 1 - yes_bid = 0.30; mid = 0.26
    assert abs(ask_for_side(b, Side.NO) - 0.30) < 1e-9  # type: ignore[arg-type]
    assert abs(bid_for_side(b, Side.NO) - 0.22) < 1e-9  # type: ignore[arg-type]
    assert abs(mid_for_side(b, Side.NO) - 0.26) < 1e-9  # type: ignore[arg-type]


def test_detect_stop_yes():
    # Held YES; mid = 0.50 → stop fires (<=).
    b = BookTick(ts_ms=0, yes_bid=0.48, yes_ask=0.52)
    assert detect_stop(b, Side.YES) is True


def test_no_stop_when_holding():
    b = BookTick(ts_ms=0, yes_bid=0.60, yes_ask=0.66)  # YES mid = 0.63 > 0.50
    assert detect_stop(b, Side.YES) is False


def test_detect_stop_no_side():
    # Held NO; NO mid <= 0.50 ⇒ YES mid >= 0.50.
    b = BookTick(ts_ms=0, yes_bid=0.55, yes_ask=0.59)  # NO mid = 0.43
    assert detect_stop(b, Side.NO) is True


def test_detect_stop_missing_book():
    b = BookTick(ts_ms=0, yes_bid=None, yes_ask=None)
    assert detect_stop(b, Side.YES) is False


def test_constants_sanity():
    assert ENTRY_AFTER_MS == 8 * 60 * 1000
    assert ENTRY_TRIGGER_PRICE == 0.75
    assert STOP_PRICE == 0.50


from kalshi_btc_engine_v2.strategies.favorite_chase import (  # noqa: E402
    MIN_STRIKE_DISTANCE_BPS,
    passes_strike_distance,
)


def test_passes_strike_distance_exact_at_bps():
    # 4 bps of $78,000 = $31.2; spot vs strike differing by exactly that passes.
    spot = 78_000.0
    strike = 78_000.0 - 32.0  # safely above 31.2
    assert passes_strike_distance(spot, strike) is True


def test_passes_strike_distance_below_bps():
    spot = 78_000.0
    strike = 78_000.0 - 30.0  # 3.85 bps
    assert passes_strike_distance(spot, strike) is False


def test_passes_strike_distance_far_above():
    assert passes_strike_distance(2_700.0, 2_650.0) is True  # ~185 bps


def test_passes_strike_distance_bad_spot():
    assert passes_strike_distance(0.0, 78_000.0) is False
    assert passes_strike_distance(-1.0, 78_000.0) is False


def test_min_bps_constant():
    assert MIN_STRIKE_DISTANCE_BPS == 4.0
