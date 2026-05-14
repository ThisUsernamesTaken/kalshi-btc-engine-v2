# HANDOFF: owned by Claude (monitoring/health/). Edit only via HANDOFF.md Open Request.
"""Health monitor + kill-switch state machine."""

from kalshi_btc_engine_v2.monitoring.health.monitor import (
    Alert,
    AlertSeverity,
    HealthConfig,
    HealthMonitor,
    HealthSignal,
    KillSwitchReason,
)

__all__ = [
    "Alert",
    "AlertSeverity",
    "HealthConfig",
    "HealthMonitor",
    "HealthSignal",
    "KillSwitchReason",
]
