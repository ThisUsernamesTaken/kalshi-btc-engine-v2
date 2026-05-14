"""Exit rule evaluator.

Modes (priority order, first match wins):
1. ``adverse_revaluation`` — current EV has flipped against entry by ≥0.6c,
   or the feed has degraded. Feed-degraded is operational rare-bail and
   cannot be disabled; the EV-flip branch is disabled by setting
   ``adverse_ev_cents`` very negative (e.g. -1e9).
2. ``spot_circuit_breaker`` — underlying spot moved against entry by the
   configured basis-point threshold. Structural rare-bail.
3. ``profit_capture`` — realized move captured ≥65% of forecast edge at entry.
   Disabled when ``profit_capture_enabled`` is False (used by the
   hold-to-settle-pure preset).
4. ``hold_to_settlement`` — late window, calibrated probability extreme,
   fragility quiet, venue disagreement tight; ride to settlement.
5. ``time_stop`` — within the time-stop buffer (default close-8s) without
   meeting hold criteria; flatten.
6. ``hold`` — none of the above; keep the position.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Side = Literal["yes", "no"]
ExitMode = Literal[
    "hold",
    "adverse_revaluation",
    "spot_circuit_breaker",
    "profit_capture",
    "time_stop",
    "hold_to_settlement",
]


@dataclass(frozen=True, slots=True)
class ExitConfig:
    adverse_ev_cents: float = -0.6
    spot_circuit_breaker_bp: float = 0.0
    profit_capture_fraction: float = 0.65
    profit_capture_enabled: bool = True
    time_stop_buffer_s: float = 8.0
    hold_q_threshold: float = 0.85
    hold_secs_to_close_max: float = 30.0
    hold_fragility_max: float = 0.0
    hold_venue_disagreement_bp_max: float = 5.0


@dataclass(frozen=True, slots=True)
class ExitInputs:
    side: Side
    entry_price_cents: int
    current_bid_cents: int
    current_ask_cents: int
    q_cal: float
    seconds_to_close: float
    forecast_edge_at_entry_cents: float
    realized_edge_cents: float
    fragility_score: float
    venue_disagreement_bp: float
    spot_at_entry: float | None = None
    current_spot: float | None = None
    feed_healthy: bool = True


@dataclass(frozen=True, slots=True)
class ExitDecision:
    mode: ExitMode
    reason: str
    current_ev_cents: float


def _side_q(q_cal: float, side: Side) -> float:
    q = max(0.0, min(1.0, q_cal))
    return q if side == "yes" else 1.0 - q


def _spot_circuit_breaker_reason(
    inputs: ExitInputs,
    threshold_bp: float,
) -> str | None:
    if threshold_bp <= 0.0:
        return None
    if inputs.spot_at_entry is None or inputs.current_spot is None:
        return None
    if inputs.spot_at_entry <= 0.0:
        return None

    spot_move_bp = 10_000.0 * (inputs.current_spot - inputs.spot_at_entry) / inputs.spot_at_entry
    unfavorable_bp = -spot_move_bp if inputs.side == "yes" else spot_move_bp
    if unfavorable_bp >= threshold_bp:
        return (
            f"spot_unfavorable={unfavorable_bp:.1f}bp >= "
            f"{threshold_bp:.1f}bp (move={spot_move_bp:.1f}bp)"
        )
    return None


def evaluate_exit(
    inputs: ExitInputs,
    *,
    config: ExitConfig | None = None,
) -> ExitDecision:
    cfg = config or ExitConfig()
    side_q = _side_q(inputs.q_cal, inputs.side)
    current_ev_cents = 100.0 * side_q - inputs.entry_price_cents

    if not inputs.feed_healthy:
        return ExitDecision("adverse_revaluation", "feed_degraded", current_ev_cents)
    if current_ev_cents < cfg.adverse_ev_cents:
        return ExitDecision(
            "adverse_revaluation",
            f"ev={current_ev_cents:.2f}c < {cfg.adverse_ev_cents}c",
            current_ev_cents,
        )

    spot_reason = _spot_circuit_breaker_reason(inputs, cfg.spot_circuit_breaker_bp)
    if spot_reason is not None:
        return ExitDecision(
            "spot_circuit_breaker",
            spot_reason,
            current_ev_cents,
        )

    if cfg.profit_capture_enabled and inputs.forecast_edge_at_entry_cents > 0.0:
        capture_ratio = inputs.realized_edge_cents / inputs.forecast_edge_at_entry_cents
        if capture_ratio >= cfg.profit_capture_fraction:
            return ExitDecision(
                "profit_capture",
                f"captured {capture_ratio:.1%} of forecast",
                current_ev_cents,
            )

    if inputs.seconds_to_close <= cfg.time_stop_buffer_s:
        if (
            inputs.seconds_to_close <= cfg.hold_secs_to_close_max
            and side_q >= cfg.hold_q_threshold
            and inputs.fragility_score <= cfg.hold_fragility_max
            and inputs.venue_disagreement_bp <= cfg.hold_venue_disagreement_bp_max
        ):
            return ExitDecision(
                "hold_to_settlement",
                f"q={side_q:.2f}, fragility={inputs.fragility_score:.2f}",
                current_ev_cents,
            )
        return ExitDecision(
            "time_stop",
            f"secs_to_close={inputs.seconds_to_close:.1f}s",
            current_ev_cents,
        )

    return ExitDecision("hold", "", current_ev_cents)
