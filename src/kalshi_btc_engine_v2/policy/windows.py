"""Event-time window classifier for 15-minute KXBTC contracts.

Windows gate everything downstream. The blueprint defines five distinct phases:

* warmup        — open to open+30s: observe only
* core          — open+30s to close-75s: maker-first allowed, selective aggressive
* precision     — close-75s to close-15s: high-threshold entries only
* freeze        — close-15s to close: no new entries, manage/exit only
* settlement_hold — close to finalization: reconcile only

Each window carries its own spread/edge thresholds. Veto and edge gating read
the active ``WindowPolicy`` to decide whether an entry is even theoretically
allowed before consulting the rest of the stack.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TimeWindow = Literal[
    "warmup",
    "core",
    "precision",
    "freeze",
    "settlement_hold",
]

WARMUP_DURATION_S = 30.0
PRECISION_BOUNDARY_S = 75.0
FREEZE_BOUNDARY_S = 15.0
SETTLEMENT_FORWARD_S = 60.0


@dataclass(frozen=True, slots=True)
class WindowPolicy:
    allow_new_entries: bool
    aggressive_ok: bool
    max_spread_cents: int
    min_edge_cents: float
    max_staleness_ms: int


WINDOW_POLICIES: dict[TimeWindow, WindowPolicy] = {
    "warmup": WindowPolicy(False, False, 999, 999.0, 1000),
    "core": WindowPolicy(True, True, 4, 1.2, 1000),
    "precision": WindowPolicy(True, True, 3, 1.8, 500),
    "freeze": WindowPolicy(False, False, 999, 999.0, 500),
    "settlement_hold": WindowPolicy(False, False, 999, 999.0, 500),
}


def classify_window(seconds_since_open: float, seconds_to_close: float) -> TimeWindow:
    """Pure function over the contract clock — no I/O, no state."""
    if seconds_to_close <= -SETTLEMENT_FORWARD_S:
        return "settlement_hold"
    if seconds_to_close <= 0.0:
        return "settlement_hold"
    if seconds_since_open < WARMUP_DURATION_S:
        return "warmup"
    if seconds_to_close < FREEZE_BOUNDARY_S:
        return "freeze"
    if seconds_to_close < PRECISION_BOUNDARY_S:
        return "precision"
    return "core"


def window_policy(window: TimeWindow) -> WindowPolicy:
    return WINDOW_POLICIES[window]
