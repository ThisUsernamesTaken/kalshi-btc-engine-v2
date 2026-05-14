"""Pre-trade veto evaluator.

Vetoes are evaluated in priority order: exchange/trading status first, then
market lifecycle, then data freshness, then microstructure (spread, depth),
then fragility/cooldown. A single failing condition denies the entry — there is
no scoring or override path.

Returns a :class:`VetoDecision`. ``allowed=False`` carries a stable ``code`` for
downstream logging and dashboards.
"""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_btc_engine_v2.policy.windows import (
    TimeWindow,
    window_policy,
)


@dataclass(frozen=True, slots=True)
class MarketHealth:
    exchange_active: bool
    trading_active: bool
    market_status: str
    market_paused: bool
    max_staleness_ms: int
    venue_quorum: int
    venue_disagreement_bp: float | None
    spread_cents: int
    top5_depth: float
    fragility_score: float = 0.0
    cooldown_active: bool = False
    cooldown_reason: str = ""


@dataclass(frozen=True, slots=True)
class VetoConfig:
    min_venue_quorum: int = 2
    max_venue_disagreement_bp: float = 15.0
    min_depth_multiplier: float = 5.0
    max_fragility_score: float = 2.0


@dataclass(frozen=True, slots=True)
class VetoDecision:
    allowed: bool
    code: str
    reason: str

    @classmethod
    def allow(cls) -> VetoDecision:
        return cls(True, "ALLOW", "")

    @classmethod
    def block(cls, code: str, reason: str) -> VetoDecision:
        return cls(False, code, reason)


def check_veto(
    health: MarketHealth,
    window: TimeWindow,
    desired_size_contracts: int,
    *,
    config: VetoConfig | None = None,
) -> VetoDecision:
    cfg = config or VetoConfig()
    pol = window_policy(window)

    if not pol.allow_new_entries:
        return VetoDecision.block("WINDOW_CLOSED", f"window={window} blocks new entries")

    if not health.exchange_active:
        return VetoDecision.block("EXCHANGE_INACTIVE", "exchange not active")
    if not health.trading_active:
        return VetoDecision.block("TRADING_INACTIVE", "trading not active")
    if health.market_status != "open":
        return VetoDecision.block("MARKET_NOT_OPEN", f"status={health.market_status}")
    if health.market_paused:
        return VetoDecision.block("MARKET_PAUSED", "paused")

    if health.venue_quorum < cfg.min_venue_quorum:
        return VetoDecision.block(
            "STALE_FEED",
            f"venue_quorum={health.venue_quorum} < {cfg.min_venue_quorum}",
        )
    if health.max_staleness_ms > pol.max_staleness_ms:
        return VetoDecision.block(
            "STALE_FEED",
            f"max_staleness_ms={health.max_staleness_ms} > {pol.max_staleness_ms}",
        )
    if (
        health.venue_disagreement_bp is not None
        and health.venue_disagreement_bp > cfg.max_venue_disagreement_bp
    ):
        return VetoDecision.block(
            "VENUE_DISAGREEMENT",
            f"{health.venue_disagreement_bp:.1f}bp > {cfg.max_venue_disagreement_bp}",
        )

    if health.spread_cents > pol.max_spread_cents:
        return VetoDecision.block(
            "SPREAD_TOO_WIDE",
            f"spread={health.spread_cents}c > {pol.max_spread_cents}c",
        )
    min_depth_needed = cfg.min_depth_multiplier * desired_size_contracts
    if health.top5_depth < min_depth_needed:
        return VetoDecision.block(
            "INSUFFICIENT_DEPTH",
            f"top5_depth={health.top5_depth:g} < {min_depth_needed:g}",
        )

    if health.fragility_score > cfg.max_fragility_score:
        return VetoDecision.block(
            "FRAGILE_BOOK",
            f"fragility={health.fragility_score:.2f} > {cfg.max_fragility_score}",
        )

    if health.cooldown_active:
        return VetoDecision.block(
            "COOLDOWN",
            health.cooldown_reason or "cooldown active",
        )

    return VetoDecision.allow()
