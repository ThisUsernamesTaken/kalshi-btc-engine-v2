"""Live executor — gated by ``ENGINE_V2_LIVE`` and explicit ``live_enabled``.

Submission is **disabled by default**. Even when ``live_enabled`` is true, every
call goes through :class:`RiskGuard.check_entry` first and only proceeds if the
guard returns ``ALLOW``. The actual REST call is :meth:`KalshiRestClient.create_order`,
which itself blocks unless its own ``live_enabled`` flag is set.

This module deliberately mirrors :class:`PaperExecutor` so callers can swap
modes by changing the executor reference, not the call site.
"""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_btc_engine_v2.adapters.kalshi import KalshiRestClient
from kalshi_btc_engine_v2.core.time import utc_now_ms
from kalshi_btc_engine_v2.execution.types import (
    BookSide,
    ExecutionFill,
    ExecutionResult,
    Position,
)
from kalshi_btc_engine_v2.policy.edge import kalshi_taker_fee_cents
from kalshi_btc_engine_v2.risk.guards import (
    EntryIntent,
    PositionSnapshot,
    RiskGuard,
)


@dataclass(frozen=True, slots=True)
class LiveExecutorConfig:
    enabled: bool = False
    default_time_in_force: str = "immediate_or_cancel"
    cancel_on_pause: bool = True


class LiveExecutor:
    def __init__(
        self,
        *,
        risk_guard: RiskGuard,
        rest_client: KalshiRestClient,
        config: LiveExecutorConfig | None = None,
    ) -> None:
        self.risk_guard = risk_guard
        self.rest_client = rest_client
        self.config = config or LiveExecutorConfig()
        self.positions: dict[str, Position] = {}
        self.fills: list[ExecutionFill] = []
        self._order_counter = 0

    def position(self, market_ticker: str) -> Position:
        return self.positions.setdefault(market_ticker, Position(market_ticker=market_ticker))

    async def submit_buy(
        self,
        *,
        market_ticker: str,
        side: BookSide,
        contracts: int,
        ask_price_cents: int,
        slip_cents: int = 0,
        now_ms: int | None = None,
    ) -> ExecutionResult:
        if not self.config.enabled:
            return ExecutionResult(False, (), "live_executor_disabled")
        if not self.rest_client.live_enabled:
            return ExecutionResult(False, (), "rest_client_live_disabled")

        intent = EntryIntent(
            market_ticker=market_ticker,
            side=side,
            action="buy",
            count=contracts,
            price_cents=ask_price_cents,
            tier="LIVE",
        )
        risk_decision = self.risk_guard.check_entry(
            intent,
            position=PositionSnapshot(market_ticker=market_ticker),
        )
        if not risk_decision.allowed:
            return ExecutionResult(False, (), risk_decision.reason)

        now_ms = now_ms or utc_now_ms()
        self._order_counter += 1
        client_id = f"LIVE-{market_ticker}-{now_ms}-{self._order_counter}"
        target_price = min(99, ask_price_cents + slip_cents)
        order_payload = {
            "ticker": market_ticker,
            "side": side,
            "action": "buy",
            "count": contracts,
            f"{side}_price": target_price,
            "type": "limit",
            "time_in_force": self.config.default_time_in_force,
            "client_order_id": client_id,
        }
        if self.config.cancel_on_pause:
            order_payload["cancel_order_on_pause"] = True

        response = await self.rest_client.create_order(order_payload)
        # In live mode we trust REST to return order state; for v1 we treat
        # the submit as accepted but defer real fill reconciliation to a
        # separate fill-watcher (out of scope here).
        position = self.position(market_ticker)
        fee_cents = kalshi_taker_fee_cents(target_price, contracts)
        fill = ExecutionFill(
            market_ticker=market_ticker,
            side=side,
            action="buy",
            contracts=contracts,
            price_cents=target_price,
            fee_cents=fee_cents,
            client_order_id=client_id,
            mode="live",
            timestamp_ms=now_ms,
            is_taker=True,
        )
        self.fills.append(fill)
        self.risk_guard.record_fill(
            market_ticker=market_ticker,
            count=contracts,
            price_cents=target_price,
            source="engine",
        )
        return ExecutionResult(
            accepted=True,
            fills=(fill,),
            rejection_reason=str(response.get("error", "")) if response else "",
            position_after=position,
        )
