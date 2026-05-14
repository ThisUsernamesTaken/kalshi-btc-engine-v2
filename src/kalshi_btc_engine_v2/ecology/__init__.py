# HANDOFF: owned by Claude (ecology/). Edit only via HANDOFF.md Open Request.
"""Market-ecology features: toxicity, reflexivity meters, pressure aggregation."""

from kalshi_btc_engine_v2.ecology.toxicity import (
    ToxicityConfig,
    ToxicityState,
    update_toxicity,
    vpin_from_history,
)

__all__ = [
    "ToxicityConfig",
    "ToxicityState",
    "update_toxicity",
    "vpin_from_history",
]
