# HANDOFF: owned by Claude (backtest/). Edit only via HANDOFF.md Open Request.
"""Event-driven backtester.

Loops a stream of ``ReplayEvent`` rows (or pulls them from a captured SQLite),
maintains :class:`SimulationState`, and on every Kalshi L2 update (subject to
``decision_interval_ms``) builds a :class:`DecisionSnapshot`, runs the
:class:`DecisionEngine`, and routes any non-FLAT action to a paper executor.

This module is the integration test for the entire v2 stack: it exercises
adapters → core → models → features (lite) → policy → execution → risk in one
deterministic loop.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kalshi_btc_engine_v2.backtest.state import SimulationState
from kalshi_btc_engine_v2.core.events import ReplayEvent
from kalshi_btc_engine_v2.core.time import parse_rfc3339_ms
from kalshi_btc_engine_v2.execution.paper import PaperExecutor
from kalshi_btc_engine_v2.execution.types import ExecutionFill
from kalshi_btc_engine_v2.features.ta_score import (
    OHLCBar,
    ScoreSnapshot,
    TAScoreConfig,
    TAScoreState,
)
from kalshi_btc_engine_v2.models.ensemble import EnsembleConfig
from kalshi_btc_engine_v2.models.fair_prob import (
    SettlementProbabilityConfig,
    SettlementProbabilityInput,
    settlement_fair_probability,
)
from kalshi_btc_engine_v2.models.regime import RegimeConfig
from kalshi_btc_engine_v2.models.vol_estimator import estimate_vol_drift
from kalshi_btc_engine_v2.policy.decision import (
    Decision,
    DecisionEngine,
    DecisionSnapshot,
    OpenPosition,
)
from kalshi_btc_engine_v2.policy.edge import EdgeInputs
from kalshi_btc_engine_v2.policy.exits import ExitConfig
from kalshi_btc_engine_v2.policy.sizing import SizingConfig
from kalshi_btc_engine_v2.policy.veto import MarketHealth, VetoConfig
from kalshi_btc_engine_v2.risk.guards import RiskConfig, RiskGuard, WindowRiskState

StrikeProvider = Callable[[str, dict[str, Any]], float | None]
DEFAULT_DECISION_INTERVAL_MS = 250

# Match "$103,000" / "$103000" / "103,000" in titles.
_STRIKE_RE = re.compile(r"\$?\s*(\d{1,3}(?:,\d{3})+|\d{4,7})")


def default_strike_provider(market_ticker: str, dim: dict[str, Any]) -> float | None:
    # 1. Try dedicated columns
    for key in ("floor_strike", "functional_strike", "cap_strike"):
        raw = dim.get(key)
        if raw is None:
            continue
        try:
            value = float(raw)
            if value > 0.0:
                return value
        except (TypeError, ValueError):
            continue
    # 2. Try parsing the raw_json blob (Kalshi often stores strike there)
    raw_json = dim.get("raw_json")
    if isinstance(raw_json, str) and raw_json:
        try:
            import json as _json

            payload = _json.loads(raw_json)
            for key in ("floor_strike", "functional_strike", "cap_strike"):
                raw = payload.get(key)
                if raw is None:
                    continue
                try:
                    value = float(raw)
                    if value > 0.0:
                        return value
                except (TypeError, ValueError):
                    continue
        except (ValueError, TypeError):
            pass
    # 3. Title / subtitle regex fallback (e.g. "Will BTC be above $103,000?")
    for key in ("title", "subtitle"):
        text = dim.get(key)
        if not text:
            continue
        match = _STRIKE_RE.search(str(text))
        if not match:
            continue
        try:
            return float(match.group(1).replace(",", ""))
        except ValueError:
            continue
    return None


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    bankroll_dollars: float = 200.0
    decision_interval_ms: int = DEFAULT_DECISION_INTERVAL_MS
    settlement_window_s: int = 60
    min_returns_for_decision: int = 30
    risk_config: RiskConfig | None = None
    sizing_config: SizingConfig | None = None
    veto_config: VetoConfig | None = None
    exit_config: ExitConfig | None = None
    fair_prob_config: SettlementProbabilityConfig | None = None
    ensemble_config: EnsembleConfig | None = None
    regime_config: RegimeConfig | None = None
    enable_cooldowns: bool = True
    enable_error_tracker: bool = True
    ungated: bool = False
    min_edge_cents_override: float | None = None
    q_cal_min: float = 0.0
    q_cal_max: float = 1.0
    tradeable_regimes_override: tuple[str, ...] | None = None


@dataclass(slots=True)
class BacktestSummary:
    runtime_ms: int
    events_processed: int
    decisions_made: int
    decisions_flat: int
    decisions_buy: int
    decisions_exit: int
    fills: int
    total_pnl_cents: float
    total_fees_cents: float
    net_pnl_cents: float
    markets_traded: tuple[str, ...]
    per_market_pnl_cents: dict[str, float] = field(default_factory=dict)
    regime_histogram: dict[str, int] = field(default_factory=dict)
    veto_histogram: dict[str, int] = field(default_factory=dict)
    sizing_capped_histogram: dict[str, int] = field(default_factory=dict)
    exit_mode_histogram: dict[str, int] = field(default_factory=dict)
    decisions_log_sample: tuple[Decision, ...] = ()
    calibration_samples: int = 0
    calibration_mean_abs_error: float | None = None
    calibration_brier_score: float | None = None
    calibration_haircut_cents: float | None = None
    settled_markets: int = 0
    settled_yes_wins: int = 0


class Backtester:
    def __init__(
        self,
        *,
        config: BacktestConfig | None = None,
        strike_provider: StrikeProvider | None = None,
        decision_log_path: str | Path | None = None,
    ) -> None:
        from kalshi_btc_engine_v2.models.ensemble import EnsembleConfig
        from kalshi_btc_engine_v2.models.error_tracker import CalibrationErrorTracker
        from kalshi_btc_engine_v2.risk.cooldowns import CooldownGuard

        self.config = config or BacktestConfig()
        self.decision_log_path = Path(decision_log_path) if decision_log_path else None
        self._decision_log_fp = None
        if self.decision_log_path is not None:
            self.decision_log_path.parent.mkdir(parents=True, exist_ok=True)
            self._decision_log_fp = self.decision_log_path.open("w", encoding="utf-8")
        self.strike_provider = strike_provider or default_strike_provider
        self.state = SimulationState()
        self.risk_guard = RiskGuard(
            self.config.risk_config or RiskConfig(),
            WindowRiskState(window_id="BACKTEST"),
        )
        self.executor = PaperExecutor(self.risk_guard)
        self.cooldown_guard = CooldownGuard() if self.config.enable_cooldowns else None
        self.error_tracker = CalibrationErrorTracker() if self.config.enable_error_tracker else None
        self.engine = DecisionEngine(
            risk_guard=self.risk_guard,
            veto_config=self.config.veto_config,
            sizing_config=self.config.sizing_config,
            exit_config=self.config.exit_config,
            ensemble_config=self.config.ensemble_config or EnsembleConfig(),
            regime_config=self.config.regime_config,
            cooldown_guard=self.cooldown_guard,
            error_tracker=self.error_tracker,
            ungated=self.config.ungated,
            min_edge_cents_override=self.config.min_edge_cents_override,
            q_cal_min=self.config.q_cal_min,
            q_cal_max=self.config.q_cal_max,
            tradeable_regimes_override=(
                frozenset(self.config.tradeable_regimes_override)  # type: ignore[arg-type]
                if self.config.tradeable_regimes_override is not None
                else None
            ),
        )
        self._last_decision_ms: dict[str, int] = {}
        self._decisions: list[Decision] = []
        self._open_positions: dict[str, OpenPosition] = {}
        self._market_first_seen_ms: dict[str, int] = {}
        self._events_processed = 0
        self._first_ts_ms: int | None = None
        self._last_ts_ms: int = 0
        self._db_path: Path | None = None

        # TA-score sidecar: aggregates coinbase mid into 1-min OHLC bars,
        # tracks the Pine Script directional score in parallel with the
        # existing q_cal cascade. Score is appended to the decision log
        # but does NOT influence engine decisions.
        self._ta_sidecar_venue: str = "coinbase"
        self._ta_state = TAScoreState(config=TAScoreConfig())
        self._ta_current_minute_ms: int | None = None
        self._ta_minute_open: float | None = None
        self._ta_minute_high: float = float("-inf")
        self._ta_minute_low: float = float("inf")
        self._ta_minute_close: float = 0.0
        self._ta_current_cycle_floor_ms: int | None = None
        self._ta_cycle_open_price: float | None = None
        self._ta_latest_snapshot: ScoreSnapshot | None = None

    # ---------- Public API ----------

    def upsert_market_dim(self, market_ticker: str, dim: dict[str, Any]) -> None:
        self.state.upsert_market_dim(market_ticker, dim)

    def run_events(self, events: Iterable[ReplayEvent]) -> BacktestSummary:
        for event in events:
            self._ingest(event)
        return self.summary()

    def run_db(
        self, db_path: str | Path, *, start_ms: int = 0, end_ms: int | None = None
    ) -> BacktestSummary:
        self._db_path = Path(db_path)
        return self.run_events(self._iter_db_events(db_path, start_ms=start_ms, end_ms=end_ms))

    def close(self) -> None:
        if self._decision_log_fp is not None:
            self._decision_log_fp.close()
            self._decision_log_fp = None

    def summary(self) -> BacktestSummary:
        per_market: dict[str, float] = {}
        markets: set[str] = set()
        total_fees = 0.0
        total_pnl = 0.0
        for fill in self.executor.fills:
            markets.add(fill.market_ticker)
            total_fees += fill.fee_cents
            per_market.setdefault(fill.market_ticker, 0.0)
        for ticker, pos in self.executor.positions.items():
            per_market[ticker] = pos.realized_pnl_cents
            total_pnl += pos.realized_pnl_cents
        buy = sum(1 for d in self._decisions if d.action in {"BUY_YES", "BUY_NO"})
        exit_d = sum(1 for d in self._decisions if d.action == "EXIT")
        flat = sum(1 for d in self._decisions if d.action == "FLAT")
        regime_hist: dict[str, int] = {}
        veto_hist: dict[str, int] = {}
        sizing_hist: dict[str, int] = {}
        exit_hist: dict[str, int] = {}
        for d in self._decisions:
            if d.regime_label:
                regime_hist[d.regime_label] = regime_hist.get(d.regime_label, 0) + 1
            if d.veto_code:
                veto_hist[d.veto_code] = veto_hist.get(d.veto_code, 0) + 1
            if d.sizing_capped_by:
                sizing_hist[d.sizing_capped_by] = sizing_hist.get(d.sizing_capped_by, 0) + 1
            if d.exit_mode:
                exit_hist[d.exit_mode] = exit_hist.get(d.exit_mode, 0) + 1

        calibration_samples = 0
        calibration_mae: float | None = None
        calibration_brier: float | None = None
        calibration_haircut: float | None = None
        settled_count = 0
        settled_yes_wins = 0
        if self.error_tracker is not None and self._db_path is not None:
            from kalshi_btc_engine_v2.backtest.settlement import scan_settled_markets

            try:
                settled = scan_settled_markets(self._db_path)
            except Exception:  # noqa: BLE001
                settled = []
            outcomes = {s.market_ticker: s.yes_won for s in settled}
            settled_count = len(settled)
            settled_yes_wins = sum(1 for s in settled if s.yes_won == 1)
            for d in self._decisions:
                if d.market_ticker not in outcomes or d.predicted_q_yes is None:
                    continue
                self.error_tracker.record(d.predicted_q_yes, outcomes[d.market_ticker])
            calibration_samples = self.error_tracker.sample_count()
            if calibration_samples > 0:
                calibration_mae = self.error_tracker.mean_abs_error()
                calibration_brier = self.error_tracker.brier_score()
                calibration_haircut = self.error_tracker.model_haircut_cents()

        return BacktestSummary(
            runtime_ms=self._last_ts_ms - (self._first_ts_ms or self._last_ts_ms),
            events_processed=self._events_processed,
            decisions_made=len(self._decisions),
            decisions_flat=flat,
            decisions_buy=buy,
            decisions_exit=exit_d,
            fills=len(self.executor.fills),
            total_pnl_cents=total_pnl,
            total_fees_cents=total_fees,
            net_pnl_cents=total_pnl - total_fees,
            markets_traded=tuple(sorted(markets)),
            per_market_pnl_cents=per_market,
            regime_histogram=regime_hist,
            veto_histogram=veto_hist,
            sizing_capped_histogram=sizing_hist,
            exit_mode_histogram=exit_hist,
            decisions_log_sample=tuple(self._decisions[:20]),
            calibration_samples=calibration_samples,
            calibration_mean_abs_error=calibration_mae,
            calibration_brier_score=calibration_brier,
            calibration_haircut_cents=calibration_haircut,
            settled_markets=settled_count,
            settled_yes_wins=settled_yes_wins,
        )

    # ---------- Internals ----------

    def _ingest(self, event: ReplayEvent) -> None:
        self._events_processed += 1
        if self._first_ts_ms is None:
            self._first_ts_ms = event.event_time_ms
        self._last_ts_ms = event.event_time_ms
        self.state.apply_event(event)
        if event.table == "spot_quote_event":
            self._update_ta_sidecar(event)
            return
        if event.table != "kalshi_l2_event":
            return
        ticker = str(event.payload.get("market_ticker") or "")
        if not ticker:
            return
        self._market_first_seen_ms.setdefault(ticker, event.event_time_ms)
        self._maybe_decide(ticker, event.event_time_ms)

    def _update_ta_sidecar(self, event: ReplayEvent) -> None:
        """Aggregate coinbase mids into 1-min bars and refresh the TA score."""
        venue = event.payload.get("source_channel") or event.payload.get("venue")
        if venue != self._ta_sidecar_venue:
            return
        # mid may be Decimal-stringified
        mid_raw = event.payload.get("price") or event.payload.get("mid")
        if mid_raw is None:
            return
        try:
            mid = float(mid_raw)
        except (TypeError, ValueError):
            return
        ts_ms = int(event.event_time_ms)
        minute_ms = (ts_ms // 60_000) * 60_000

        if self._ta_current_minute_ms is None:
            self._ta_current_minute_ms = minute_ms
            self._ta_minute_open = mid
            self._ta_minute_high = mid
            self._ta_minute_low = mid
            self._ta_minute_close = mid
            return
        if minute_ms == self._ta_current_minute_ms:
            self._ta_minute_high = max(self._ta_minute_high, mid)
            self._ta_minute_low = min(self._ta_minute_low, mid)
            self._ta_minute_close = mid
            return
        # New minute: close out current bar and update score
        bar_ts = self._ta_current_minute_ms
        cycle_floor = (bar_ts // (15 * 60_000)) * (15 * 60_000)
        if (
            self._ta_current_cycle_floor_ms is None
            or cycle_floor != self._ta_current_cycle_floor_ms
        ):
            self._ta_current_cycle_floor_ms = cycle_floor
            self._ta_cycle_open_price = self._ta_minute_open
            self._ta_state = TAScoreState(config=self._ta_state.config)
        bars_in_cycle = ((bar_ts - cycle_floor) // 60_000) + 1
        bar = OHLCBar(
            ts_minute_ms=bar_ts,
            open=self._ta_minute_open or mid,
            high=self._ta_minute_high,
            low=self._ta_minute_low,
            close=self._ta_minute_close,
            volume=None,
            cycle_open_price=self._ta_cycle_open_price or self._ta_minute_open or mid,
            bars_in_cycle=int(bars_in_cycle),
        )
        self._ta_latest_snapshot = self._ta_state.update(bar)
        # Reset for next minute
        self._ta_current_minute_ms = minute_ms
        self._ta_minute_open = mid
        self._ta_minute_high = mid
        self._ta_minute_low = mid
        self._ta_minute_close = mid

    def _maybe_decide(self, ticker: str, event_time_ms: int) -> None:
        last = self._last_decision_ms.get(ticker, 0)
        if event_time_ms - last < self.config.decision_interval_ms:
            return
        self._last_decision_ms[ticker] = event_time_ms
        snapshot = self._build_snapshot(ticker, event_time_ms)
        if snapshot is None:
            return
        decision = self.engine.decide(snapshot)
        self._decisions.append(decision)
        self._write_decision_log(decision, snapshot, event_time_ms)
        self._act(decision, ticker, event_time_ms)

    def _write_decision_log(
        self,
        decision: Decision,
        snapshot: DecisionSnapshot,
        event_time_ms: int,
    ) -> None:
        if self._decision_log_fp is None:
            return
        import json as _json

        record = {
            "ts_ms": event_time_ms,
            "market_ticker": snapshot.market_ticker,
            "action": decision.action,
            "side": decision.side,
            "contracts": decision.contracts,
            "reason": decision.reason,
            "window": decision.window,
            "edge_cents": decision.edge_cents,
            "regime_label": decision.regime_label,
            "veto_code": decision.veto_code,
            "sizing_capped_by": decision.sizing_capped_by,
            "exit_mode": decision.exit_mode,
            "seconds_since_open": snapshot.seconds_since_open,
            "seconds_to_close": snapshot.seconds_to_close,
            "q_cal": snapshot.edge.q_cal,
            "yes_ask_cents": snapshot.edge.yes_ask_cents,
            "no_ask_cents": snapshot.edge.no_ask_cents,
            "yes_bid_cents": snapshot.edge.yes_bid_cents,
            "no_bid_cents": snapshot.edge.no_bid_cents,
            "spread_cents": snapshot.health.spread_cents,
            "top5_depth": snapshot.health.top5_depth,
            "diag": decision.diagnostics,
        }
        ta = self._ta_latest_snapshot
        if ta is not None:
            record["ta_score"] = ta.score
            record["ta_bull_conf"] = ta.bull_conf
            record["ta_bear_conf"] = ta.bear_conf
            record["ta_bull_tier"] = ta.bull_tier
            record["ta_bear_tier"] = ta.bear_tier
            record["ta_score_velocity"] = ta.score_velocity
            record["ta_bar_in_cycle"] = ta.bars_in_cycle
        self._decision_log_fp.write(_json.dumps(record, default=str) + "\n")
        self._decision_log_fp.flush()

    def _build_snapshot(self, ticker: str, event_time_ms: int) -> DecisionSnapshot | None:
        book = self.state.books.get(ticker)
        if book is None:
            return None
        market_dim = self.state.market_dims.get(ticker)
        if market_dim is None:
            return None
        spot = self.state.fused_spot
        if spot is None:
            return None
        if len(self.state.spot_returns_1s) < self.config.min_returns_for_decision:
            return None
        strike = self.strike_provider(ticker, market_dim)
        if strike is None or strike <= 0.0:
            return None

        open_ms = parse_rfc3339_ms(market_dim.get("open_time")) or self._market_first_seen_ms.get(
            ticker, event_time_ms
        )
        close_ms = parse_rfc3339_ms(market_dim.get("close_time"))
        if close_ms is None:
            return None
        seconds_since_open = max(0.0, (event_time_ms - open_ms) / 1000.0)
        seconds_to_close = (close_ms - event_time_ms) / 1000.0

        vol = estimate_vol_drift(
            list(self.state.spot_returns_1s),
            seconds_to_close=seconds_to_close,
        )
        sigma_ann = max(vol.sigma_annualized, 1e-4)
        fair = settlement_fair_probability(
            SettlementProbabilityInput(
                spot=spot,
                strike=strike,
                seconds_to_close=seconds_to_close,
                realized_vol_annualized=sigma_ann,
                drift_annualized=vol.drift_annualized,
            ),
            config=self.config.fair_prob_config
            or SettlementProbabilityConfig(
                drift_shrinkage=1.0,
                settlement_window_seconds=self.config.settlement_window_s,
            ),
        )

        yes_bid = _book_yes_bid_cents(book)
        yes_ask = _book_yes_ask_cents(book)
        no_bid = _book_no_bid_cents(book)
        no_ask = _book_no_ask_cents(book)
        if yes_ask is None or no_ask is None:
            return None

        spread_cents = max(0, yes_ask - (yes_bid or 0))
        top5_depth = float(book.depth("yes", levels=5) + book.depth("no", levels=5))
        health = MarketHealth(
            exchange_active=True,
            trading_active=True,
            market_status="open",
            market_paused=False,
            max_staleness_ms=0,
            venue_quorum=3,
            venue_disagreement_bp=0.0,
            spread_cents=spread_cents,
            top5_depth=top5_depth,
            fragility_score=0.0,
            cooldown_active=False,
        )
        edge = EdgeInputs(
            q_cal=fair.probability_yes,
            yes_ask_cents=yes_ask,
            no_ask_cents=no_ask,
            yes_bid_cents=yes_bid or 0,
            no_bid_cents=no_bid or 0,
        )

        # Compute exposure for this market and aggregate BTC.
        market_pos = self.executor.positions.get(ticker)
        market_exposure = (
            (market_pos.avg_entry_price_cents * market_pos.contracts) / 100.0
            if market_pos and not market_pos.is_flat
            else 0.0
        )
        aggregate = sum(
            (pos.avg_entry_price_cents * pos.contracts) / 100.0
            for pos in self.executor.positions.values()
            if not pos.is_flat
        )
        open_pos = self._open_positions.get(ticker)

        import math as _math

        from kalshi_btc_engine_v2.models.ensemble import EnsembleInputs
        from kalshi_btc_engine_v2.models.regime import RegimeInputs

        p_spot = fair.probability_yes
        if yes_bid is not None and yes_ask is not None:
            p_binary_mid = (yes_bid + yes_ask) / 200.0
        else:
            p_binary_mid = None
        divergence = None
        if p_binary_mid is not None and 0 < p_binary_mid < 1 and 0 < p_spot < 1:
            divergence = _math.log(p_binary_mid / (1 - p_binary_mid)) - _math.log(
                p_spot / (1 - p_spot)
            )
        ensemble_inputs = EnsembleInputs(
            p_spot=p_spot,
            p_binary_mid=p_binary_mid,
            divergence_logit=divergence,
        )
        regime_inputs = RegimeInputs(
            seconds_to_close=seconds_to_close,
            fresh_venues=health.venue_quorum,
            venue_disagreement_bp=health.venue_disagreement_bp or 0.0,
            market_status_open=health.market_status == "open",
            market_paused=health.market_paused,
            spread_cents=health.spread_cents,
            top5_depth=health.top5_depth,
            fragility_score=health.fragility_score,
            divergence_logit=divergence,
        )

        return DecisionSnapshot(
            market_ticker=ticker,
            seconds_since_open=seconds_since_open,
            seconds_to_close=seconds_to_close,
            health=health,
            edge=edge,
            bankroll_dollars=self.config.bankroll_dollars,
            current_market_exposure_dollars=market_exposure,
            aggregate_btc_exposure_dollars=aggregate,
            open_position=open_pos,
            current_spot=spot,
            realized_edge_cents=_realized_edge(open_pos, edge) if open_pos else 0.0,
            now_ms=event_time_ms,
            ensemble_inputs=ensemble_inputs,
            regime_inputs=regime_inputs,
        )

    def _act(self, decision: Decision, ticker: str, event_time_ms: int) -> None:
        book = self.state.books.get(ticker)
        if book is None:
            return
        if decision.action in {"BUY_YES", "BUY_NO"} and decision.side is not None:
            result = self.executor.submit_buy(
                market_ticker=ticker,
                side=decision.side,
                contracts=decision.contracts,
                book=book,
                now_ms=event_time_ms,
            )
            if result.accepted and result.fills:
                avg_price = sum(f.price_cents * f.contracts for f in result.fills) / sum(
                    f.contracts for f in result.fills
                )
                self._open_positions[ticker] = OpenPosition(
                    side=decision.side,
                    contracts=sum(f.contracts for f in result.fills),
                    entry_price_cents=int(round(avg_price)),
                    forecast_edge_at_entry_cents=decision.edge_cents,
                    q_cal_at_entry=0.5,
                    spot_at_entry=self.state.fused_spot,
                )
                if self.cooldown_guard is not None:
                    self.cooldown_guard.record_entry(
                        market_ticker=ticker,
                        side=decision.side,
                        now_ms=event_time_ms,
                    )
        elif decision.action == "EXIT" and decision.side is not None:
            result = self.executor.submit_sell(
                market_ticker=ticker,
                side=decision.side,
                contracts=decision.contracts,
                book=book,
                now_ms=event_time_ms,
            )
            if result.accepted:
                self._open_positions.pop(ticker, None)
                if self.cooldown_guard is not None:
                    kind = "stop" if decision.exit_mode == "adverse_revaluation" else "scratch"
                    self.cooldown_guard.record_exit(
                        market_ticker=ticker,
                        kind=kind,
                        now_ms=event_time_ms,
                    )

    def _iter_db_events(
        self,
        db_path: str | Path,
        *,
        start_ms: int = 0,
        end_ms: int | None = None,
    ) -> Iterator[ReplayEvent]:
        from kalshi_btc_engine_v2.replay.engine import load_events
        from kalshi_btc_engine_v2.storage.sqlite import connect, fetch_all

        end_ms = end_ms if end_ms is not None else 9_999_999_999_999
        with connect(db_path) as conn:
            for row in fetch_all(conn, "SELECT * FROM market_dim"):
                self.upsert_market_dim(str(row["ticker"]), dict(row))
            yield from load_events(conn, start_ms=start_ms, end_ms=end_ms)


def _book_yes_bid_cents(book) -> int | None:
    bid = book.best_yes_bid
    return _dollars_to_cents(bid) if bid is not None else None


def _book_yes_ask_cents(book) -> int | None:
    ask = book.best_yes_ask
    return _dollars_to_cents(ask) if ask is not None else None


def _book_no_bid_cents(book) -> int | None:
    bid = book.best_no_bid
    return _dollars_to_cents(bid) if bid is not None else None


def _book_no_ask_cents(book) -> int | None:
    if book.best_yes_bid is None:
        return None
    from decimal import Decimal as _D

    no_ask = _D("1") - book.best_yes_bid
    return _dollars_to_cents(no_ask)


def _dollars_to_cents(value) -> int:
    from decimal import Decimal as _D

    return int((_D(str(value)) * _D("100")).to_integral_value())


def _realized_edge(open_pos: OpenPosition | None, edge: EdgeInputs) -> float:
    if open_pos is None:
        return 0.0
    if open_pos.side == "yes":
        return float(edge.yes_bid_cents - open_pos.entry_price_cents)
    return float(edge.no_bid_cents - open_pos.entry_price_cents)


def aggregate_summary_to_dict(summary: BacktestSummary) -> dict[str, Any]:
    """Plain-dict form for CLI/JSON dumps."""
    return {
        "runtime_ms": summary.runtime_ms,
        "events_processed": summary.events_processed,
        "decisions_made": summary.decisions_made,
        "decisions_flat": summary.decisions_flat,
        "decisions_buy": summary.decisions_buy,
        "decisions_exit": summary.decisions_exit,
        "fills": summary.fills,
        "total_pnl_cents": summary.total_pnl_cents,
        "total_fees_cents": summary.total_fees_cents,
        "net_pnl_cents": summary.net_pnl_cents,
        "markets_traded": list(summary.markets_traded),
        "per_market_pnl_cents": summary.per_market_pnl_cents,
        "regime_histogram": summary.regime_histogram,
        "veto_histogram": summary.veto_histogram,
        "sizing_capped_histogram": summary.sizing_capped_histogram,
        "exit_mode_histogram": summary.exit_mode_histogram,
        "calibration_samples": summary.calibration_samples,
        "calibration_mean_abs_error": summary.calibration_mean_abs_error,
        "calibration_brier_score": summary.calibration_brier_score,
        "calibration_haircut_cents": summary.calibration_haircut_cents,
        "settled_markets": summary.settled_markets,
        "settled_yes_wins": summary.settled_yes_wins,
    }


class _ExecutionFillList(list[ExecutionFill]):
    """Type alias for clarity in result accessors (not exported)."""
