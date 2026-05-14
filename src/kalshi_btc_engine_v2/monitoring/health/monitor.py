# HANDOFF: owned by Claude (monitoring/health/). Edit only via HANDOFF.md Open Request.
"""HealthMonitor — single-state observer that ingests signals and emits alerts.

Signals the monitor consumes (callers push these in):
* venue freshness (per venue, last_ts_ms)
* WS connection state for Kalshi
* unmatched fill ages
* daily realized P&L
* rate-limit utilization
* model calibration slope (rolling)

Outputs:
* ``kill_switch_engaged`` boolean + ``KillSwitchReason``
* list of active :class:`Alert` records since last clear

The monitor is intentionally *not* a long-running service — it's a pure state
object the decision engine and execution layer can interrogate per tick.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Literal

AlertSeverity = Literal["info", "warn", "error", "critical"]


@dataclass(frozen=True, slots=True)
class HealthConfig:
    venue_freshness_ms_core: int = 1000
    venue_freshness_ms_precision: int = 500
    min_venue_quorum: int = 2
    max_unmatched_fill_ms: int = 5_000
    daily_loss_stop_pct: float = 0.03
    daily_loss_stop_dollars: float | None = None
    rate_limit_util_warn: float = 0.80
    calibration_slope_min: float = 0.85
    calibration_slope_max: float = 1.15
    repeated_veto_window_ms: int = 600_000  # 10 min
    repeated_veto_threshold: int = 10
    venue_disagreement_bp_max: float = 15.0


KillSwitchReason = Literal[
    "",
    "VENUE_QUORUM_LOSS",
    "KALSHI_WS_DOWN",
    "UNMATCHED_FILL_TIMEOUT",
    "DAILY_LOSS_STOP",
    "VENUE_DISAGREEMENT",
    "MANUAL",
    "PORTFOLIO_MISMATCH",
]


@dataclass(frozen=True, slots=True)
class Alert:
    ts_ms: int
    severity: AlertSeverity
    code: str
    message: str


@dataclass(slots=True)
class HealthSignal:
    venue_last_ts_ms: dict[str, int] = field(default_factory=dict)
    venue_disagreement_bp: float | None = None
    kalshi_ws_connected: bool = True
    unmatched_fill_ages_ms: list[int] = field(default_factory=list)
    rate_limit_util: float = 0.0
    realized_pnl_dollars: float = 0.0
    bankroll_dollars: float = 0.0
    calibration_slope: float | None = None
    portfolio_reconciled: bool = True
    veto_count_in_window: int = 0


@dataclass(slots=True)
class HealthMonitor:
    config: HealthConfig = field(default_factory=HealthConfig)
    kill_switch_engaged: bool = False
    kill_switch_reason: KillSwitchReason = ""
    alerts: deque[Alert] = field(default_factory=lambda: deque(maxlen=512))
    manual_halt: bool = False

    def engage(self, reason: KillSwitchReason, ts_ms: int, *, message: str = "") -> None:
        self.kill_switch_engaged = True
        self.kill_switch_reason = reason or "MANUAL"
        self._push(Alert(ts_ms, "critical", reason or "MANUAL", message or reason))

    def release(self) -> None:
        # Manual halt sticks until explicitly cleared via ``clear_manual_halt``.
        if self.manual_halt:
            return
        self.kill_switch_engaged = False
        self.kill_switch_reason = ""

    def set_manual_halt(self, ts_ms: int, message: str = "") -> None:
        self.manual_halt = True
        self.engage("MANUAL", ts_ms, message=message or "manual halt engaged")

    def clear_manual_halt(self) -> None:
        self.manual_halt = False
        self.release()

    def ingest(self, signal: HealthSignal, *, now_ms: int) -> None:
        if self.manual_halt:
            self.kill_switch_engaged = True
            self.kill_switch_reason = "MANUAL"
            return
        prior_engaged = self.kill_switch_engaged
        active_reason: KillSwitchReason = ""

        fresh_venues = sum(
            1
            for ts in signal.venue_last_ts_ms.values()
            if now_ms - ts <= self.config.venue_freshness_ms_core
        )
        if fresh_venues < self.config.min_venue_quorum:
            active_reason = "VENUE_QUORUM_LOSS"
            self._push(
                Alert(
                    now_ms,
                    "critical",
                    "VENUE_QUORUM_LOSS",
                    f"fresh venues={fresh_venues} < {self.config.min_venue_quorum}",
                )
            )

        if not signal.kalshi_ws_connected:
            active_reason = "KALSHI_WS_DOWN"
            self._push(Alert(now_ms, "critical", "KALSHI_WS_DOWN", "Kalshi WS disconnected"))

        if (
            signal.venue_disagreement_bp is not None
            and signal.venue_disagreement_bp > self.config.venue_disagreement_bp_max
        ):
            active_reason = "VENUE_DISAGREEMENT"
            disagreement_msg = (
                f"{signal.venue_disagreement_bp:.1f}bp "
                f"> {self.config.venue_disagreement_bp_max}"
            )
            self._push(Alert(now_ms, "critical", "VENUE_DISAGREEMENT", disagreement_msg))

        if signal.unmatched_fill_ages_ms:
            worst = max(signal.unmatched_fill_ages_ms)
            if worst > self.config.max_unmatched_fill_ms:
                active_reason = "UNMATCHED_FILL_TIMEOUT"
                self._push(
                    Alert(
                        now_ms,
                        "critical",
                        "UNMATCHED_FILL_TIMEOUT",
                        f"oldest unmatched={worst}ms > {self.config.max_unmatched_fill_ms}",
                    )
                )

        if not signal.portfolio_reconciled:
            active_reason = "PORTFOLIO_MISMATCH"
            self._push(Alert(now_ms, "critical", "PORTFOLIO_MISMATCH", "portfolio mismatch"))

        if self._daily_loss_breached(signal):
            active_reason = "DAILY_LOSS_STOP"
            self._push(
                Alert(
                    now_ms,
                    "critical",
                    "DAILY_LOSS_STOP",
                    f"realized pnl=${signal.realized_pnl_dollars:.2f}",
                )
            )

        # Warnings (do not engage kill switch, but logged)
        if signal.rate_limit_util > self.config.rate_limit_util_warn:
            self._push(
                Alert(
                    now_ms,
                    "warn",
                    "RATE_LIMIT_HIGH",
                    f"util={signal.rate_limit_util:.2f}",
                )
            )
        if signal.calibration_slope is not None and (
            signal.calibration_slope < self.config.calibration_slope_min
            or signal.calibration_slope > self.config.calibration_slope_max
        ):
            self._push(
                Alert(
                    now_ms,
                    "warn",
                    "CALIBRATION_DRIFT",
                    f"slope={signal.calibration_slope:.2f}",
                )
            )
        if signal.veto_count_in_window >= self.config.repeated_veto_threshold:
            self._push(
                Alert(
                    now_ms,
                    "warn",
                    "REPEATED_VETO",
                    f"{signal.veto_count_in_window} vetoes in window",
                )
            )

        if active_reason:
            self.engage(active_reason, now_ms)
        elif prior_engaged and not self.manual_halt:
            self.release()

    def recent_alerts(self, severity: AlertSeverity | None = None) -> list[Alert]:
        if severity is None:
            return list(self.alerts)
        return [a for a in self.alerts if a.severity == severity]

    def _push(self, alert: Alert) -> None:
        self.alerts.append(alert)

    def _daily_loss_breached(self, signal: HealthSignal) -> bool:
        loss = -min(0.0, signal.realized_pnl_dollars)
        if (
            self.config.daily_loss_stop_dollars is not None
            and loss >= self.config.daily_loss_stop_dollars
        ):
            return True
        if signal.bankroll_dollars > 0.0:
            pct = loss / signal.bankroll_dollars
            if pct >= self.config.daily_loss_stop_pct:
                return True
        return False
