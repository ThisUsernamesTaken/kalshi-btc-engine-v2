from __future__ import annotations

from kalshi_btc_engine_v2.risk.cooldowns import CooldownConfig, CooldownGuard


def test_initial_entry_allowed():
    g = CooldownGuard()
    out = g.check_entry(market_ticker="X", side="yes", now_ms=1000)
    assert out.allowed is True


def test_same_side_min_gap_blocks_rapid_reentry():
    g = CooldownGuard(CooldownConfig(same_side_min_gap_ms=20_000))
    g.record_entry(market_ticker="X", side="yes", now_ms=1000)
    blocked = g.check_entry(market_ticker="X", side="yes", now_ms=5000)
    assert blocked.allowed is False
    assert blocked.code == "SAME_SIDE_TOO_SOON"


def test_stop_exit_cooldown_blocks_market():
    g = CooldownGuard(CooldownConfig(stop_exit_cooldown_ms=90_000))
    g.record_exit(market_ticker="X", kind="stop", now_ms=1000)
    blocked = g.check_entry(market_ticker="X", side="yes", now_ms=60_000)
    assert blocked.allowed is False
    assert blocked.code == "EXIT_COOLDOWN"
    allowed = g.check_entry(market_ticker="X", side="yes", now_ms=91_001)
    assert allowed.allowed is True


def test_flip_flop_lock_after_n_side_changes():
    g = CooldownGuard(CooldownConfig(max_side_changes_per_market=2, same_side_min_gap_ms=0))
    g.record_entry(market_ticker="X", side="yes", now_ms=1000)
    g.record_entry(market_ticker="X", side="no", now_ms=2000)
    g.record_entry(market_ticker="X", side="yes", now_ms=3000)
    blocked = g.check_entry(market_ticker="X", side="no", now_ms=4000)
    assert blocked.allowed is False
    assert blocked.code == "FLIP_FLOP_LOCK"


def test_cancel_replace_burst_blocks_entries():
    g = CooldownGuard(
        CooldownConfig(
            cancel_replace_max_in_window=3,
            cancel_replace_window_ms=10_000,
            same_side_min_gap_ms=0,
        )
    )
    for ts in (1000, 2000, 3000):
        g.record_cancel_replace(now_ms=ts)
    blocked = g.check_entry(market_ticker="X", side="yes", now_ms=4000)
    assert blocked.allowed is False
    assert blocked.code == "CANCEL_REPLACE_BURST"


def test_data_degraded_blocks_entries():
    g = CooldownGuard(CooldownConfig(degraded_clear_required_ms=60_000))
    g.mark_data_degraded(now_ms=1000)
    blocked = g.check_entry(market_ticker="X", side="yes", now_ms=30_000)
    assert blocked.allowed is False
    assert blocked.code == "DATA_DEGRADED"
    allowed = g.check_entry(market_ticker="X", side="yes", now_ms=62_000)
    assert allowed.allowed is True


def test_reset_market_clears_side_change_count():
    g = CooldownGuard(CooldownConfig(max_side_changes_per_market=1, same_side_min_gap_ms=0))
    g.record_entry(market_ticker="X", side="yes", now_ms=1000)
    g.record_entry(market_ticker="X", side="no", now_ms=2000)
    blocked = g.check_entry(market_ticker="X", side="yes", now_ms=3000)
    assert blocked.allowed is False
    g.reset_market("X")
    allowed = g.check_entry(market_ticker="X", side="yes", now_ms=4000)
    assert allowed.allowed is True
