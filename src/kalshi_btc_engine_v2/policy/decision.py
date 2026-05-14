"""Decision orchestrator. Combines windows, veto, edge, sizing, exits, and the
risk-guard layer into a single :class:`Decision` per snapshot.

This module is pure orchestration. It does not place orders, does not maintain
fill state, and does not subscribe to feeds. Callers (paper executor,
backtester, live executor) hand in a :class:`DecisionSnapshot` and act on the
:class:`Decision` returned.

Actions:
* ``FLAT``         — do nothing this tick (default).
* ``BUY_YES`` / ``BUY_NO`` — open new position on the named side.
* ``HOLD``         — keep existing position; no exit triggered.
* ``EXIT``         — close existing position (taker on the bid side).
* ``KILL_SWITCH``  — global stop; reconcile only.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal

from kalshi_btc_engine_v2.models.ensemble import (
    EnsembleConfig,
    EnsembleInputs,
    ensemble_probability,
)
from kalshi_btc_engine_v2.models.error_tracker import CalibrationErrorTracker
from kalshi_btc_engine_v2.models.regime import (
    TRADEABLE_REGIMES,
    RegimeConfig,
    RegimeInputs,
    RegimeLabel,
    classify_regime,
    is_tradeable,
)
from kalshi_btc_engine_v2.policy.edge import (
    EdgeInputs,
    EdgeResult,
    best_side,
    compute_edges,
)
from kalshi_btc_engine_v2.policy.exits import (
    ExitConfig,
    ExitDecision,
    ExitInputs,
    Side,
    evaluate_exit,
)
from kalshi_btc_engine_v2.policy.sizing import (
    SizingConfig,
    SizingInputs,
    SizingResult,
    size_position,
)
from kalshi_btc_engine_v2.policy.veto import (
    MarketHealth,
    VetoConfig,
    VetoDecision,
    check_veto,
)
from kalshi_btc_engine_v2.policy.windows import (
    TimeWindow,
    classify_window,
    window_policy,
)
from kalshi_btc_engine_v2.risk.cooldowns import CooldownGuard
from kalshi_btc_engine_v2.risk.guards import (
    EntryIntent,
    PositionSnapshot,
    RiskGuard,
)

Action = Literal[
    "FLAT",
    "BUY_YES",
    "BUY_NO",
    "HOLD",
    "EXIT",
    "KILL_SWITCH",
]


@dataclass(frozen=True, slots=True)
class OpenPosition:
    side: Side
    contracts: int
    entry_price_cents: int
    forecast_edge_at_entry_cents: float
    q_cal_at_entry: float
    spot_at_entry: float | None = None


@dataclass(frozen=True, slots=True)
class DecisionSnapshot:
    market_ticker: str
    seconds_since_open: float
    seconds_to_close: float
    health: MarketHealth
    edge: EdgeInputs
    bankroll_dollars: float
    current_market_exposure_dollars: float = 0.0
    aggregate_btc_exposure_dollars: float = 0.0
    open_position: OpenPosition | None = None
    current_spot: float | None = None
    realized_edge_cents: float = 0.0
    kill_switch_engaged: bool = False
    now_ms: int = 0
    ensemble_inputs: EnsembleInputs | None = None
    regime_inputs: RegimeInputs | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    action: Action
    side: Side | None
    contracts: int
    reason: str
    window: TimeWindow
    edge_cents: float
    veto_code: str | None = None
    sizing_capped_by: str | None = None
    exit_mode: str | None = None
    regime_label: str | None = None
    predicted_q_yes: float | None = None
    market_ticker: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


class DecisionEngine:
    def __init__(
        self,
        *,
        risk_guard: RiskGuard,
        veto_config: VetoConfig | None = None,
        sizing_config: SizingConfig | None = None,
        exit_config: ExitConfig | None = None,
        ensemble_config: EnsembleConfig | None = None,
        regime_config: RegimeConfig | None = None,
        cooldown_guard: CooldownGuard | None = None,
        error_tracker: CalibrationErrorTracker | None = None,
        ungated: bool = False,
        min_edge_cents_override: float | None = None,
        q_cal_min: float = 0.0,
        q_cal_max: float = 1.0,
        tradeable_regimes_override: frozenset[RegimeLabel] | None = None,
    ) -> None:
        self.risk_guard = risk_guard
        self.veto_config = veto_config or VetoConfig()
        self.sizing_config = sizing_config or SizingConfig()
        self.exit_config = exit_config or ExitConfig()
        self.ensemble_config = ensemble_config
        self.regime_config = regime_config
        self.cooldown_guard = cooldown_guard
        self.error_tracker = error_tracker
        self.ungated = ungated
        self.min_edge_cents_override = min_edge_cents_override
        self.q_cal_min = q_cal_min
        self.q_cal_max = q_cal_max
        self.tradeable_regimes: frozenset[RegimeLabel] = (
            tradeable_regimes_override
            if tradeable_regimes_override is not None
            else TRADEABLE_REGIMES
        )

    def decide(self, snapshot: DecisionSnapshot) -> Decision:
        window = classify_window(snapshot.seconds_since_open, snapshot.seconds_to_close)
        diag: dict[str, Any] = {"window": window}

        if snapshot.kill_switch_engaged:
            return Decision(
                "KILL_SWITCH",
                None,
                0,
                "kill_switch",
                window,
                0.0,
                predicted_q_yes=snapshot.edge.q_cal,
                market_ticker=snapshot.market_ticker,
                diagnostics=diag,
            )

        snapshot = self._apply_ensemble(snapshot, diag)
        snapshot = self._apply_model_haircut(snapshot, diag)
        regime_label: str | None = None
        if snapshot.regime_inputs is not None:
            regime = classify_regime(snapshot.regime_inputs, config=self.regime_config)
            regime_label = regime.label
            diag["regime_label"] = regime.label
            diag["regime_confidence"] = regime.confidence
            diag["regime_reason"] = regime.reason
            label_tradeable = (
                is_tradeable(regime.label)
                if self.tradeable_regimes is TRADEABLE_REGIMES
                else regime.label in self.tradeable_regimes
            )
            if not label_tradeable and snapshot.open_position is None and not self.ungated:
                return Decision(
                    "FLAT",
                    None,
                    0,
                    f"regime={regime.label}",
                    window,
                    0.0,
                    veto_code="REGIME_VETO",
                    regime_label=regime.label,
                    diagnostics=diag,
                )

        if snapshot.open_position is not None:
            decision = self._decide_with_position(snapshot, window, diag)
        else:
            decision = self._decide_entry(snapshot, window, diag)
        if regime_label is not None and decision.regime_label is None:
            decision = replace(decision, regime_label=regime_label)
        if decision.predicted_q_yes is None:
            decision = replace(
                decision,
                predicted_q_yes=snapshot.edge.q_cal,
                market_ticker=snapshot.market_ticker,
            )
        return decision

    def _apply_ensemble(
        self,
        snapshot: DecisionSnapshot,
        diag: dict[str, Any],
    ) -> DecisionSnapshot:
        if self.ensemble_config is None or snapshot.ensemble_inputs is None:
            return snapshot
        result = ensemble_probability(snapshot.ensemble_inputs, config=self.ensemble_config)
        diag["ensemble_probability"] = result.probability
        diag["ensemble_base_logit"] = result.base_logit
        return replace(snapshot, edge=replace(snapshot.edge, q_cal=result.probability))

    def _apply_model_haircut(
        self,
        snapshot: DecisionSnapshot,
        diag: dict[str, Any],
    ) -> DecisionSnapshot:
        if self.error_tracker is None:
            return snapshot
        haircut = self.error_tracker.model_haircut_cents()
        if haircut <= snapshot.edge.model_haircut_cents:
            return snapshot
        diag["model_haircut_cents"] = haircut
        return replace(snapshot, edge=replace(snapshot.edge, model_haircut_cents=haircut))

    def _decide_with_position(
        self,
        snapshot: DecisionSnapshot,
        window: TimeWindow,
        diag: dict[str, Any],
    ) -> Decision:
        assert snapshot.open_position is not None
        pos = snapshot.open_position
        side_bid = snapshot.edge.yes_bid_cents if pos.side == "yes" else snapshot.edge.no_bid_cents
        side_ask = snapshot.edge.yes_ask_cents if pos.side == "yes" else snapshot.edge.no_ask_cents
        exit_inputs = ExitInputs(
            side=pos.side,
            entry_price_cents=pos.entry_price_cents,
            current_bid_cents=side_bid,
            current_ask_cents=side_ask,
            q_cal=snapshot.edge.q_cal,
            seconds_to_close=snapshot.seconds_to_close,
            forecast_edge_at_entry_cents=pos.forecast_edge_at_entry_cents,
            realized_edge_cents=snapshot.realized_edge_cents,
            fragility_score=snapshot.health.fragility_score,
            venue_disagreement_bp=snapshot.health.venue_disagreement_bp or 0.0,
            spot_at_entry=pos.spot_at_entry,
            current_spot=snapshot.current_spot,
            feed_healthy=snapshot.health.exchange_active
            and snapshot.health.trading_active
            and not snapshot.health.market_paused,
        )
        exit_decision: ExitDecision = evaluate_exit(exit_inputs, config=self.exit_config)
        diag["current_ev_cents"] = exit_decision.current_ev_cents

        if exit_decision.mode == "hold":
            return Decision(
                "HOLD",
                pos.side,
                pos.contracts,
                "in_position",
                window,
                exit_decision.current_ev_cents,
                exit_mode=exit_decision.mode,
                diagnostics=diag,
            )
        if exit_decision.mode == "hold_to_settlement":
            return Decision(
                "HOLD",
                pos.side,
                pos.contracts,
                exit_decision.reason,
                window,
                exit_decision.current_ev_cents,
                exit_mode=exit_decision.mode,
                diagnostics=diag,
            )
        return Decision(
            "EXIT",
            pos.side,
            pos.contracts,
            exit_decision.reason,
            window,
            exit_decision.current_ev_cents,
            exit_mode=exit_decision.mode,
            diagnostics=diag,
        )

    def _decide_entry(
        self,
        snapshot: DecisionSnapshot,
        window: TimeWindow,
        diag: dict[str, Any],
    ) -> Decision:
        pol = window_policy(window)
        yes_edge, no_edge = compute_edges(snapshot.edge)
        chosen: EdgeResult = best_side(yes_edge, no_edge)
        diag["yes_edge_net_cents"] = yes_edge.edge_net_cents
        diag["no_edge_net_cents"] = no_edge.edge_net_cents
        diag["chosen_side"] = chosen.side
        diag["chosen_edge_net_cents"] = chosen.edge_net_cents

        # Window gate is still enforced even in ungated mode — warmup/freeze
        # are structural safeties, not selectivity filters.
        if not pol.allow_new_entries:
            return Decision(
                "FLAT",
                None,
                0,
                f"window={window}_blocks_entry",
                window,
                chosen.edge_net_cents,
                veto_code="WINDOW_CLOSED",
                diagnostics=diag,
            )

        min_edge = (
            self.min_edge_cents_override
            if self.min_edge_cents_override is not None
            else pol.min_edge_cents
        )
        if chosen.edge_net_cents < min_edge:
            return Decision(
                "FLAT",
                chosen.side,
                0,
                f"edge={chosen.edge_net_cents:.2f}c < min={min_edge:.2f}c",
                window,
                chosen.edge_net_cents,
                diagnostics=diag,
            )

        # Empirical finding (2026-05-12, 4h burn-in): the model's `q_cal` is
        # well calibrated in [0.10, 0.90] but unreliable at the tails. The one
        # losing trade in the 4h slice had q_cal=0.040; every winner had
        # q_cal in [0.30, 0.50]. Default band is [0.0, 1.0] (no filtering);
        # set q_cal_min/q_cal_max to enable extreme-confidence veto.
        if not (self.q_cal_min <= snapshot.edge.q_cal <= self.q_cal_max):
            return Decision(
                "FLAT",
                chosen.side,
                0,
                f"q_cal={snapshot.edge.q_cal:.3f} outside [{self.q_cal_min},{self.q_cal_max}]",
                window,
                chosen.edge_net_cents,
                veto_code="Q_CAL_EXTREME",
                diagnostics=diag,
            )

        # Initial size estimate to feed veto's depth ratio test.
        provisional_size = max(1, self.sizing_config.min_contracts)
        veto: VetoDecision = check_veto(
            snapshot.health,
            window,
            desired_size_contracts=provisional_size,
            config=self.veto_config,
        )
        if not veto.allowed and not self.ungated:
            return Decision(
                "FLAT",
                chosen.side,
                0,
                veto.reason,
                window,
                chosen.edge_net_cents,
                veto_code=veto.code,
                diagnostics=diag,
            )
        if not veto.allowed:
            diag["ungated_veto_override"] = veto.code

        sizing: SizingResult = size_position(
            SizingInputs(
                q_cal=snapshot.edge.q_cal if chosen.side == "yes" else 1.0 - snapshot.edge.q_cal,
                cost_cents=chosen.cost_cents,
                edge_net_cents=chosen.edge_net_cents,
                bankroll_dollars=snapshot.bankroll_dollars,
                top5_depth=snapshot.health.top5_depth,
                window=window,
                current_market_exposure_dollars=snapshot.current_market_exposure_dollars,
                aggregate_btc_exposure_dollars=snapshot.aggregate_btc_exposure_dollars,
            ),
            config=self.sizing_config,
        )
        diag["sizing_capped_by"] = sizing.capped_by
        diag["kelly_fraction"] = sizing.kelly_fraction
        diag["confidence_mult"] = sizing.confidence_mult
        diag["liquidity_mult"] = sizing.liquidity_mult
        if sizing.contracts <= 0:
            return Decision(
                "FLAT",
                chosen.side,
                0,
                f"sizing={sizing.capped_by}",
                window,
                chosen.edge_net_cents,
                sizing_capped_by=sizing.capped_by,
                diagnostics=diag,
            )

        intent = EntryIntent(
            market_ticker=snapshot.market_ticker,
            side=chosen.side,
            action="buy",
            count=sizing.contracts,
            price_cents=chosen.cost_cents,
            tier="POLICY",
        )
        if self.cooldown_guard is not None and not self.ungated:
            cool = self.cooldown_guard.check_entry(
                market_ticker=snapshot.market_ticker,
                side=chosen.side,
                now_ms=snapshot.now_ms,
            )
            if not cool.allowed:
                return Decision(
                    "FLAT",
                    chosen.side,
                    0,
                    cool.reason,
                    window,
                    chosen.edge_net_cents,
                    veto_code=cool.code,
                    sizing_capped_by=sizing.capped_by,
                    diagnostics=diag,
                )
        risk_decision = self.risk_guard.check_entry(
            intent,
            position=PositionSnapshot(market_ticker=snapshot.market_ticker),
        )
        if not risk_decision.allowed:
            return Decision(
                "FLAT",
                chosen.side,
                0,
                risk_decision.reason,
                window,
                chosen.edge_net_cents,
                veto_code=risk_decision.code,
                sizing_capped_by=sizing.capped_by,
                diagnostics=diag,
            )

        action: Action = "BUY_YES" if chosen.side == "yes" else "BUY_NO"
        return Decision(
            action,
            chosen.side,
            sizing.contracts,
            f"edge={chosen.edge_net_cents:.2f}c size={sizing.contracts}",
            window,
            chosen.edge_net_cents,
            sizing_capped_by=sizing.capped_by,
            diagnostics=diag,
        )
