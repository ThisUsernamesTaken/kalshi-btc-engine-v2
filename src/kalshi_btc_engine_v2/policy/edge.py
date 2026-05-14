"""Net edge calculation in cents, plus Kalshi quadratic fee formulas.

Edge is `100*q - ask` minus fees, slippage, and a model haircut. We compute
edge for both YES and NO sides and let the orchestrator pick the better one
(subject to window-dependent minimum thresholds in :mod:`policy.windows`).

Fee schedule (Kalshi general fees):
* taker:  ceil_cents( 0.07   * count * P * (1 - P) )
* maker:  ceil_cents( 0.0175 * count * P * (1 - P) )

where P is price in dollars and the result is in cents. There is no settlement
fee. Realized fill fees should override this estimate in live runs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

Side = Literal["yes", "no"]


@dataclass(frozen=True, slots=True)
class EdgeInputs:
    q_cal: float
    yes_ask_cents: int
    no_ask_cents: int
    yes_bid_cents: int = 0
    no_bid_cents: int = 0
    fee_cents_yes: float = 0.0
    fee_cents_no: float = 0.0
    slippage_cents_buffer: float = 0.0
    model_haircut_cents: float = 0.0


@dataclass(frozen=True, slots=True)
class EdgeResult:
    side: Side
    cost_cents: int
    edge_gross_cents: float
    edge_net_cents: float


def kalshi_taker_fee_cents(price_cents: int, count: int = 1, k: float = 0.07) -> int:
    if count <= 0:
        return 0
    p = max(0.0, min(1.0, price_cents / 100.0))
    raw_cents = k * count * p * (1.0 - p) * 100.0
    return int(math.ceil(raw_cents - 1e-12))


def kalshi_maker_fee_cents(price_cents: int, count: int = 1, k: float = 0.0175) -> int:
    if count <= 0:
        return 0
    p = max(0.0, min(1.0, price_cents / 100.0))
    raw_cents = k * count * p * (1.0 - p) * 100.0
    return int(math.ceil(raw_cents - 1e-12))


def compute_edges(inputs: EdgeInputs) -> tuple[EdgeResult, EdgeResult]:
    q = max(0.0, min(1.0, inputs.q_cal))
    yes_gross = 100.0 * q - inputs.yes_ask_cents
    no_gross = 100.0 * (1.0 - q) - inputs.no_ask_cents
    yes_net = (
        yes_gross - inputs.fee_cents_yes - inputs.slippage_cents_buffer - inputs.model_haircut_cents
    )
    no_net = (
        no_gross - inputs.fee_cents_no - inputs.slippage_cents_buffer - inputs.model_haircut_cents
    )
    return (
        EdgeResult("yes", inputs.yes_ask_cents, yes_gross, yes_net),
        EdgeResult("no", inputs.no_ask_cents, no_gross, no_net),
    )


def best_side(yes: EdgeResult, no: EdgeResult) -> EdgeResult:
    return yes if yes.edge_net_cents >= no.edge_net_cents else no
