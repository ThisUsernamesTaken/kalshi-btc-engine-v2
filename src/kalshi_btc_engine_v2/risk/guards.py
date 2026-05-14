from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Side = Literal["yes", "no"]
Action = Literal["buy", "sell"]
FillSource = Literal["engine", "manual", "reconcile"]
BalanceCheckResult = Literal["ok", "needs_second_fetch", "confirmed_drop"]


@dataclass(frozen=True, slots=True)
class RiskConfig:
    max_risk_per_window_dollars: float = 15.0
    max_entries_per_window: int = 99
    per_ticker_entry_lock_enabled: bool = True
    oversell_hardening_enabled: bool = True
    adopt_manual_fills: bool = False
    catastrophic_balance_drop_dollars: float = 20.0
    catastrophic_balance_drop_fraction: float = 0.50


@dataclass(slots=True)
class WindowRiskState:
    window_id: str | None = None
    committed_cents: int = 0
    entry_count: int = 0
    entered_tickers: set[str] = field(default_factory=set)

    def reset(self, window_id: str | None = None) -> None:
        self.window_id = window_id
        self.committed_cents = 0
        self.entry_count = 0
        self.entered_tickers.clear()


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    market_ticker: str
    yes_count: float = 0.0
    no_count: float = 0.0

    def side_count(self, side: Side) -> float:
        return self.yes_count if side == "yes" else self.no_count


@dataclass(frozen=True, slots=True)
class EntryIntent:
    market_ticker: str
    side: Side
    action: Action
    count: int
    price_cents: int
    tier: str = "unknown"
    reduce_only: bool = False
    visible_offsetting_buy: bool = False

    @property
    def gross_cost_cents(self) -> int:
        if self.action == "sell":
            return 0
        return max(0, int(self.count)) * max(0, int(self.price_cents))


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    code: str
    reason: str

    @classmethod
    def allow(cls) -> RiskDecision:
        return cls(True, "ALLOW", "")

    @classmethod
    def block(cls, code: str, reason: str) -> RiskDecision:
        return cls(False, code, reason)


class RiskGuard:
    def __init__(
        self,
        config: RiskConfig | None = None,
        state: WindowRiskState | None = None,
    ) -> None:
        self.config = config or RiskConfig()
        self.state = state or WindowRiskState()

    def check_entry(
        self,
        intent: EntryIntent,
        *,
        position: PositionSnapshot | None = None,
    ) -> RiskDecision:
        if intent.count <= 0:
            return RiskDecision.block("BAD_SIZE", "count must be positive")
        if not 0 <= intent.price_cents <= 100:
            return RiskDecision.block("BAD_PRICE", "price_cents must be in [0, 100]")

        window_decision = self._check_window_caps(intent)
        if not window_decision.allowed:
            return window_decision

        if (
            self.config.per_ticker_entry_lock_enabled
            and intent.market_ticker in self.state.entered_tickers
            and not intent.reduce_only
        ):
            return RiskDecision.block(
                "WINDOW_TICKER_LOCK",
                f"{intent.market_ticker} already entered this window",
            )

        if self.config.oversell_hardening_enabled:
            oversell_decision = self._check_oversell(intent, position)
            if not oversell_decision.allowed:
                return oversell_decision

        return RiskDecision.allow()

    def record_fill(
        self,
        *,
        market_ticker: str,
        count: int,
        price_cents: int,
        source: FillSource = "engine",
    ) -> None:
        if source == "manual" and not self.config.adopt_manual_fills:
            return
        n = max(0, int(count))
        px = max(0, int(price_cents))
        if n <= 0:
            return
        self.state.committed_cents += n * px
        self.state.entry_count += 1
        self.state.entered_tickers.add(market_ticker)

    def check_balance_drop(
        self,
        *,
        previous_balance_dollars: float,
        first_fetch_balance_dollars: float,
        second_fetch_balance_dollars: float | None = None,
    ) -> BalanceCheckResult:
        previous = max(0.0, float(previous_balance_dollars))
        first = max(0.0, float(first_fetch_balance_dollars))
        catastrophic = self._is_catastrophic_balance_drop(previous=previous, current=first)
        if not catastrophic:
            return "ok"
        if second_fetch_balance_dollars is None:
            return "needs_second_fetch"
        second = max(0.0, float(second_fetch_balance_dollars))
        still_catastrophic = self._is_catastrophic_balance_drop(
            previous=previous,
            current=second,
        )
        return "confirmed_drop" if still_catastrophic else "ok"

    def _is_catastrophic_balance_drop(self, *, previous: float, current: float) -> bool:
        drop = previous - current
        return drop >= self.config.catastrophic_balance_drop_dollars or (
            previous > 0.0 and drop / previous >= self.config.catastrophic_balance_drop_fraction
        )

    def _check_window_caps(self, intent: EntryIntent) -> RiskDecision:
        cap_cents = int(max(0.0, self.config.max_risk_per_window_dollars) * 100)
        if cap_cents > 0:
            next_committed = self.state.committed_cents + intent.gross_cost_cents
            if next_committed > cap_cents:
                return RiskDecision.block(
                    "WINDOW_RISK_CAP",
                    (
                        f"{intent.tier} cost=${intent.gross_cost_cents / 100:.2f} + "
                        f"committed=${self.state.committed_cents / 100:.2f} "
                        f"> cap=${cap_cents / 100:.2f}"
                    ),
                )

        max_entries = int(self.config.max_entries_per_window)
        if max_entries > 0 and self.state.entry_count >= max_entries:
            return RiskDecision.block(
                "WINDOW_ENTRY_CAP",
                f"{intent.tier} entries {self.state.entry_count}/{max_entries}",
            )
        return RiskDecision.allow()

    def _check_oversell(
        self,
        intent: EntryIntent,
        position: PositionSnapshot | None,
    ) -> RiskDecision:
        if intent.action != "sell":
            return RiskDecision.allow()
        if intent.reduce_only:
            return RiskDecision.allow()
        if intent.visible_offsetting_buy:
            return RiskDecision.allow()
        held = position.side_count(intent.side) if position is not None else 0.0
        if held + 1.0e-9 >= intent.count:
            return RiskDecision.allow()
        return RiskDecision.block(
            "SAFETY_OVERSELL",
            (
                f"sell {intent.count} {intent.side.upper()} without visible offset; "
                f"held={held:g}"
            ),
        )
