"""Fractional Kelly sizing with edge-confidence, liquidity, and exposure caps.

Per blueprint:

    f* = max(0, (q - c) / (1 - c))                              # Kelly
    f  = 0.20 * f* * conf_mult * liq_mult
    conf_mult = min(1, edge_net_cents / 4.0)
    liq_mult  = min(1, depth_top5 / (20 * desired_size))

Hard caps applied after Kelly target:
* max cost basis per market: 1.5% core, 0.75% precision
* max aggregate BTC exposure: 4% of bankroll
* max top-5 depth participation: 10%
* absolute floor/ceiling on contracts

Returns the contract count and the constraint that bound the size, for audit.
"""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_btc_engine_v2.policy.windows import TimeWindow


@dataclass(frozen=True, slots=True)
class SizingConfig:
    fractional_kelly: float = 0.20
    edge_conf_norm_cents: float = 4.0
    depth_participation_norm: float = 20.0
    max_pos_basis_pct_core: float = 0.015
    max_pos_basis_pct_precision: float = 0.0075
    max_aggregate_btc_pct: float = 0.04
    max_top5_participation: float = 0.10
    min_contracts: int = 1
    max_contracts: int = 100
    # Fee-floor veto: at 1-3 contracts the rounded entry fee is a flat 2c
    # regardless of P. Combined with the 2c exit-side fee under hold-to-settle
    # (settlement fee is zero, so this only bites if there is an early exit),
    # small sizes at off-center prices are dominated by fee drag. Block them
    # unless the edge is materially larger than the fee floor.
    # See HANDOFF.md / docs/EXPERIMENT_REGISTRY_2026_05_12.md.
    fee_floor_max_contracts: int = 3
    fee_floor_off_center_band: float = 0.10
    fee_floor_min_edge_cents: float = 4.0


@dataclass(frozen=True, slots=True)
class SizingInputs:
    q_cal: float
    cost_cents: int
    edge_net_cents: float
    bankroll_dollars: float
    top5_depth: float
    window: TimeWindow
    current_market_exposure_dollars: float = 0.0
    aggregate_btc_exposure_dollars: float = 0.0


@dataclass(frozen=True, slots=True)
class SizingResult:
    contracts: int
    cost_dollars: float
    kelly_fraction: float
    confidence_mult: float
    liquidity_mult: float
    capped_by: str


def _pick_market_cap_pct(window: TimeWindow, cfg: SizingConfig) -> float:
    if window == "core":
        return cfg.max_pos_basis_pct_core
    if window == "precision":
        return cfg.max_pos_basis_pct_precision
    return 0.0


def size_position(
    inputs: SizingInputs,
    *,
    config: SizingConfig | None = None,
) -> SizingResult:
    cfg = config or SizingConfig()

    if inputs.edge_net_cents <= 0.0:
        return SizingResult(0, 0.0, 0.0, 0.0, 0.0, "no_edge")
    if inputs.cost_cents <= 0 or inputs.cost_cents >= 100:
        return SizingResult(0, 0.0, 0.0, 0.0, 0.0, "price_out_of_range")
    if inputs.bankroll_dollars <= 0.0:
        return SizingResult(0, 0.0, 0.0, 0.0, 0.0, "no_bankroll")

    c = inputs.cost_cents / 100.0
    q = max(0.0, min(1.0, inputs.q_cal))
    if q <= c:
        return SizingResult(0, 0.0, 0.0, 0.0, 0.0, "no_kelly_edge")

    kelly = (q - c) / (1.0 - c)
    conf = min(1.0, max(0.0, inputs.edge_net_cents / cfg.edge_conf_norm_cents))
    target_dollars = cfg.fractional_kelly * kelly * conf * inputs.bankroll_dollars
    target_contracts = int(target_dollars / c) if c > 0.0 else 0
    if target_contracts <= 0:
        return SizingResult(0, 0.0, kelly, conf, 0.0, "kelly_target_zero")

    liq = min(
        1.0,
        inputs.top5_depth / max(1.0, cfg.depth_participation_norm * target_contracts),
    )
    target_contracts = max(0, int(target_contracts * liq))
    if target_contracts <= 0:
        return SizingResult(0, 0.0, kelly, conf, liq, "liquidity_squeezed")

    market_cap_pct = _pick_market_cap_pct(inputs.window, cfg)
    market_cap_dollars_total = market_cap_pct * inputs.bankroll_dollars
    market_cap_remaining = market_cap_dollars_total - inputs.current_market_exposure_dollars
    if market_cap_remaining <= 0.0:
        return SizingResult(0, 0.0, kelly, conf, liq, "market_cap_full")

    aggregate_cap_dollars_total = cfg.max_aggregate_btc_pct * inputs.bankroll_dollars
    aggregate_cap_remaining = aggregate_cap_dollars_total - inputs.aggregate_btc_exposure_dollars
    if aggregate_cap_remaining <= 0.0:
        return SizingResult(0, 0.0, kelly, conf, liq, "aggregate_cap_full")

    market_cap_contracts = int(market_cap_remaining / c) if c > 0.0 else 0
    aggregate_cap_contracts = int(aggregate_cap_remaining / c) if c > 0.0 else 0
    depth_cap_contracts = int(inputs.top5_depth * cfg.max_top5_participation)

    candidates: dict[str, int] = {
        "kelly": target_contracts,
        "market_cap": market_cap_contracts,
        "aggregate_cap": aggregate_cap_contracts,
        "depth_participation": depth_cap_contracts,
        "max_contracts": cfg.max_contracts,
    }
    capped_by, final = min(candidates.items(), key=lambda kv: kv[1])
    if final < cfg.min_contracts:
        return SizingResult(0, 0.0, kelly, conf, liq, "min_contracts")

    if (
        final <= cfg.fee_floor_max_contracts
        and abs(c - 0.5) > cfg.fee_floor_off_center_band
        and inputs.edge_net_cents < cfg.fee_floor_min_edge_cents
    ):
        return SizingResult(0, 0.0, kelly, conf, liq, "fee_floor_off_center")

    return SizingResult(
        contracts=final,
        cost_dollars=final * c,
        kelly_fraction=kelly,
        confidence_mult=conf,
        liquidity_mult=liq,
        capped_by=capped_by,
    )
