# HANDOFF: owned by Claude (ecology/). Edit only via HANDOFF.md Open Request.
"""VPIN-style toxicity estimator.

Builds volume-time buckets from signed trade flow. Each bucket of
``bucket_size_contracts`` worth of executed volume yields one imbalance number
in [0, 1]. The recent N buckets' mean imbalance is the toxicity score.

High toxicity means recent fills have been one-sided — passive quotes on the
losing side are getting picked off. Use to either widen passive quotes or skip
maker-mode entries entirely.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ToxicityConfig:
    bucket_size_contracts: float = 50.0
    recent_buckets: int = 50


@dataclass(slots=True)
class ToxicityState:
    pending_buy: float = 0.0
    pending_sell: float = 0.0
    bucket_imbalances: deque[float] = field(default_factory=deque)
    capacity: int = 50

    def vpin(self) -> float | None:
        if not self.bucket_imbalances:
            return None
        return sum(self.bucket_imbalances) / len(self.bucket_imbalances)


def _new_state(config: ToxicityConfig) -> ToxicityState:
    return ToxicityState(
        bucket_imbalances=deque(maxlen=config.recent_buckets),
        capacity=config.recent_buckets,
    )


def update_toxicity(
    state: ToxicityState | None,
    *,
    buy_contracts: float,
    sell_contracts: float,
    config: ToxicityConfig | None = None,
) -> tuple[ToxicityState, float | None]:
    """Push new trade volume into the state. Returns updated state and current VPIN."""
    cfg = config or ToxicityConfig()
    if state is None:
        state = _new_state(cfg)
    elif state.capacity != cfg.recent_buckets:
        new_deque: deque[float] = deque(state.bucket_imbalances, maxlen=cfg.recent_buckets)
        state = ToxicityState(
            pending_buy=state.pending_buy,
            pending_sell=state.pending_sell,
            bucket_imbalances=new_deque,
            capacity=cfg.recent_buckets,
        )

    state.pending_buy += max(0.0, buy_contracts)
    state.pending_sell += max(0.0, sell_contracts)
    total_pending = state.pending_buy + state.pending_sell
    bucket_size = max(cfg.bucket_size_contracts, 1e-9)

    while total_pending >= bucket_size:
        if state.pending_buy + state.pending_sell <= 0:
            break
        ratio = state.pending_buy / (state.pending_buy + state.pending_sell)
        take_buy = bucket_size * ratio
        take_sell = bucket_size - take_buy
        imbalance = abs(take_buy - take_sell) / bucket_size
        state.bucket_imbalances.append(imbalance)
        state.pending_buy -= take_buy
        state.pending_sell -= take_sell
        total_pending = state.pending_buy + state.pending_sell

    return state, state.vpin()


def vpin_from_history(
    flow: Iterable[tuple[float, float]],
    *,
    config: ToxicityConfig | None = None,
) -> float | None:
    """Convenience: replay (buy, sell) pairs through ``update_toxicity``."""
    cfg = config or ToxicityConfig()
    state: ToxicityState | None = None
    last: float | None = None
    for buy, sell in flow:
        state, vpin = update_toxicity(state, buy_contracts=buy, sell_contracts=sell, config=cfg)
        last = vpin
    return last
