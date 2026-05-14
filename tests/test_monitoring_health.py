from __future__ import annotations

from kalshi_btc_engine_v2.monitoring.health import (
    HealthConfig,
    HealthMonitor,
    HealthSignal,
)


def _fresh_venues(now_ms: int, count: int = 3) -> dict[str, int]:
    return {name: now_ms - 100 for name in ["coinbase", "kraken", "bitstamp"][:count]}


def test_healthy_signal_does_not_engage_kill_switch():
    mon = HealthMonitor()
    now_ms = 1_000_000_000_000
    mon.ingest(
        HealthSignal(
            venue_last_ts_ms=_fresh_venues(now_ms, 3),
            kalshi_ws_connected=True,
            bankroll_dollars=200.0,
        ),
        now_ms=now_ms,
    )
    assert mon.kill_switch_engaged is False
    assert mon.kill_switch_reason == ""


def test_venue_quorum_loss_engages_kill_switch():
    mon = HealthMonitor()
    now_ms = 1_000_000_000_000
    mon.ingest(
        HealthSignal(
            venue_last_ts_ms={"coinbase": now_ms - 100},  # only 1 venue
            kalshi_ws_connected=True,
        ),
        now_ms=now_ms,
    )
    assert mon.kill_switch_engaged is True
    assert mon.kill_switch_reason == "VENUE_QUORUM_LOSS"


def test_kalshi_ws_down_engages_kill_switch():
    mon = HealthMonitor()
    now_ms = 1_000_000_000_000
    mon.ingest(
        HealthSignal(
            venue_last_ts_ms=_fresh_venues(now_ms),
            kalshi_ws_connected=False,
        ),
        now_ms=now_ms,
    )
    assert mon.kill_switch_engaged is True
    assert mon.kill_switch_reason == "KALSHI_WS_DOWN"


def test_venue_disagreement_engages_kill_switch():
    mon = HealthMonitor()
    now_ms = 1_000_000_000_000
    mon.ingest(
        HealthSignal(
            venue_last_ts_ms=_fresh_venues(now_ms),
            venue_disagreement_bp=18.0,
            kalshi_ws_connected=True,
        ),
        now_ms=now_ms,
    )
    assert mon.kill_switch_engaged is True
    assert mon.kill_switch_reason == "VENUE_DISAGREEMENT"


def test_unmatched_fill_timeout_engages():
    mon = HealthMonitor()
    now_ms = 1_000_000_000_000
    mon.ingest(
        HealthSignal(
            venue_last_ts_ms=_fresh_venues(now_ms),
            kalshi_ws_connected=True,
            unmatched_fill_ages_ms=[10_000],
        ),
        now_ms=now_ms,
    )
    assert mon.kill_switch_engaged is True
    assert mon.kill_switch_reason == "UNMATCHED_FILL_TIMEOUT"


def test_portfolio_mismatch_engages():
    mon = HealthMonitor()
    now_ms = 1_000_000_000_000
    mon.ingest(
        HealthSignal(
            venue_last_ts_ms=_fresh_venues(now_ms),
            kalshi_ws_connected=True,
            portfolio_reconciled=False,
        ),
        now_ms=now_ms,
    )
    assert mon.kill_switch_engaged is True
    assert mon.kill_switch_reason == "PORTFOLIO_MISMATCH"


def test_daily_loss_stop_engages_on_pct():
    mon = HealthMonitor(HealthConfig(daily_loss_stop_pct=0.03))
    now_ms = 1_000_000_000_000
    mon.ingest(
        HealthSignal(
            venue_last_ts_ms=_fresh_venues(now_ms),
            kalshi_ws_connected=True,
            realized_pnl_dollars=-7.0,  # 3.5% of 200
            bankroll_dollars=200.0,
        ),
        now_ms=now_ms,
    )
    assert mon.kill_switch_engaged is True
    assert mon.kill_switch_reason == "DAILY_LOSS_STOP"


def test_daily_loss_stop_engages_on_absolute_dollars():
    mon = HealthMonitor(HealthConfig(daily_loss_stop_pct=999.0, daily_loss_stop_dollars=5.0))
    now_ms = 1_000_000_000_000
    mon.ingest(
        HealthSignal(
            venue_last_ts_ms=_fresh_venues(now_ms),
            kalshi_ws_connected=True,
            realized_pnl_dollars=-6.0,
            bankroll_dollars=1000.0,
        ),
        now_ms=now_ms,
    )
    assert mon.kill_switch_engaged is True
    assert mon.kill_switch_reason == "DAILY_LOSS_STOP"


def test_warnings_do_not_engage_kill_switch():
    mon = HealthMonitor()
    now_ms = 1_000_000_000_000
    mon.ingest(
        HealthSignal(
            venue_last_ts_ms=_fresh_venues(now_ms),
            kalshi_ws_connected=True,
            rate_limit_util=0.95,
            calibration_slope=1.40,
            veto_count_in_window=15,
        ),
        now_ms=now_ms,
    )
    assert mon.kill_switch_engaged is False
    warn_codes = {a.code for a in mon.recent_alerts("warn")}
    assert "RATE_LIMIT_HIGH" in warn_codes
    assert "CALIBRATION_DRIFT" in warn_codes
    assert "REPEATED_VETO" in warn_codes


def test_manual_halt_sticks_through_healthy_signal():
    mon = HealthMonitor()
    now_ms = 1_000_000_000_000
    mon.set_manual_halt(now_ms, "operator stop")
    assert mon.kill_switch_engaged is True
    mon.ingest(
        HealthSignal(
            venue_last_ts_ms=_fresh_venues(now_ms + 1000),
            kalshi_ws_connected=True,
        ),
        now_ms=now_ms + 1000,
    )
    assert mon.kill_switch_engaged is True
    assert mon.kill_switch_reason == "MANUAL"
    mon.clear_manual_halt()
    mon.ingest(
        HealthSignal(
            venue_last_ts_ms=_fresh_venues(now_ms + 2000),
            kalshi_ws_connected=True,
        ),
        now_ms=now_ms + 2000,
    )
    assert mon.kill_switch_engaged is False


def test_kill_switch_releases_when_root_cause_clears():
    mon = HealthMonitor()
    now_ms = 1_000_000_000_000
    mon.ingest(
        HealthSignal(
            venue_last_ts_ms={"coinbase": now_ms - 100},
            kalshi_ws_connected=True,
        ),
        now_ms=now_ms,
    )
    assert mon.kill_switch_engaged is True
    mon.ingest(
        HealthSignal(
            venue_last_ts_ms=_fresh_venues(now_ms + 1000, 3),
            kalshi_ws_connected=True,
        ),
        now_ms=now_ms + 1000,
    )
    assert mon.kill_switch_engaged is False
