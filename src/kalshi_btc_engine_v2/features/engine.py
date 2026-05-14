from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal

from kalshi_btc_engine_v2.core.orderbook import KalshiOrderBook
from kalshi_btc_engine_v2.models.fair_prob import (
    SECONDS_PER_BTC_YEAR,
    SettlementProbabilityConfig,
    SettlementProbabilityInput,
    settlement_fair_probability,
)
from kalshi_btc_engine_v2.models.vol_estimator import VolDriftEstimate, estimate_vol_drift

EPS = 1.0e-9
PROB_EPS = 1.0e-6
DEFAULT_TAPE_WINDOWS_SECONDS = (5, 30, 60)
DEFAULT_RETURN_WINDOWS_SECONDS = (5, 30, 60, 300)
DEFAULT_DEPTH_LEVELS = (1, 3, 5, 10)
ROUND_NUMBER_STEP_USD = 1000.0


@dataclass(frozen=True, slots=True)
class FeatureIndex:
    market_ticker: str
    seconds_to_close: float
    event_time_ms: int


@dataclass(frozen=True, slots=True)
class BookDelta:
    side: str
    price: float
    previous_size: float | None = None
    new_size: float | None = None
    delta_size: float | None = None


@dataclass(frozen=True, slots=True)
class TradePrint:
    side: str | None
    price: float
    size: float


@dataclass(frozen=True, slots=True)
class EventFeatureInput:
    event_time_ms: int
    market_ticker: str
    seconds_to_close: float
    book: KalshiOrderBook | None = None
    spot: float | None = None
    strike: float | None = None
    trade: TradePrint | None = None
    book_delta: BookDelta | None = None
    implied_vol_annualized: float | None = None
    observed_settlement_average: float | None = None
    observed_settlement_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class FeatureSnapshot:
    index: FeatureIndex
    best_bid: float | None
    best_ask: float | None
    mid: float | None
    spread: float | None
    l1_queue_imbalance: float | None
    depth_yes_bid: dict[int, float]
    depth_yes_ask: dict[int, float]
    depth_imbalance: dict[int, float | None]
    spread_zscore: float | None
    replenishment_size: float
    cancel_size: float
    cancel_add_size: float
    trade_count: dict[int, int]
    signed_volume: dict[int, float]
    taker_pressure: dict[int, float | None]
    spot: float | None
    log_return_1s: float | None
    rolling_returns: dict[int, float | None]
    realized_vol_annualized: float | None
    drift_annualized: float | None
    vol_estimate: VolDriftEstimate | None
    strike: float | None
    distance_to_strike: float | None
    normalized_cliff_pressure: float | None
    round_number_distance: float | None
    round_number_magnet: float | None
    binary_mid_prob: float | None
    spot_fair_prob: float | None
    logit_divergence: float | None
    bernoulli_entropy_mid: float | None
    bernoulli_entropy_fair: float | None
    entropy_compression_rate: float | None
    reflexivity_residual: float | None
    liquidity_elasticity: float | None


@dataclass(slots=True)
class _TickerState:
    spreads: deque[float] = field(default_factory=lambda: deque(maxlen=120))
    trades: deque[tuple[int, float]] = field(default_factory=deque)
    mid_history: deque[tuple[int, float]] = field(default_factory=deque)
    entropy_history: deque[tuple[int, float]] = field(default_factory=deque)
    previous_mid: float | None = None
    previous_spot: float | None = None


@dataclass(slots=True)
class _SpotState:
    previous_spot: float | None = None
    previous_event_time_ms: int | None = None
    spot_history: deque[tuple[int, float]] = field(default_factory=deque)
    returns_1s: deque[float] = field(default_factory=lambda: deque(maxlen=600))
    last_return_1s: float | None = None


class RollingFeatureEngine:
    """Deterministic event-time feature state for synthetic or replayed snapshots."""

    def __init__(
        self,
        *,
        tape_windows_seconds: tuple[int, ...] = DEFAULT_TAPE_WINDOWS_SECONDS,
        return_windows_seconds: tuple[int, ...] = DEFAULT_RETURN_WINDOWS_SECONDS,
        depth_levels: tuple[int, ...] = DEFAULT_DEPTH_LEVELS,
        fair_prob_config: SettlementProbabilityConfig | None = None,
        round_number_step_usd: float = ROUND_NUMBER_STEP_USD,
    ) -> None:
        self.tape_windows_seconds = tape_windows_seconds
        self.return_windows_seconds = return_windows_seconds
        self.depth_levels = depth_levels
        self.fair_prob_config = fair_prob_config or SettlementProbabilityConfig()
        self.round_number_step_usd = round_number_step_usd
        self._tickers: dict[str, _TickerState] = {}
        self._spot = _SpotState()

    def consume(self, event: EventFeatureInput) -> FeatureSnapshot:
        state = self._tickers.setdefault(event.market_ticker, _TickerState())
        now_ms = int(event.event_time_ms)
        spot = self._update_spot(now_ms, event.spot)
        book_values = _book_features(event.book, self.depth_levels)

        if book_values.spread is not None:
            state.spreads.append(book_values.spread)
        if event.trade is not None:
            state.trades.append((now_ms, _signed_trade_size(event.trade)))

        mid = book_values.mid
        if mid is not None:
            state.mid_history.append((now_ms, mid))

        _prune_time_deque(state.trades, now_ms, max(self.tape_windows_seconds))
        _prune_time_deque(self._spot.spot_history, now_ms, max(self.return_windows_seconds))
        _prune_time_deque(state.mid_history, now_ms, max(self.return_windows_seconds))
        _prune_time_deque(state.entropy_history, now_ms, max(self.return_windows_seconds))

        vol_estimate = self._estimate_vol(event.seconds_to_close)
        fair_prob = self._fair_probability(event, spot, vol_estimate)
        mid_prob = _probability_from_mid(mid)
        entropy_mid = _bernoulli_entropy(mid_prob)
        if entropy_mid is not None:
            state.entropy_history.append((now_ms, entropy_mid))

        snapshot = FeatureSnapshot(
            index=FeatureIndex(event.market_ticker, float(event.seconds_to_close), now_ms),
            best_bid=book_values.best_bid,
            best_ask=book_values.best_ask,
            mid=mid,
            spread=book_values.spread,
            l1_queue_imbalance=book_values.l1_queue_imbalance,
            depth_yes_bid=book_values.depth_yes_bid,
            depth_yes_ask=book_values.depth_yes_ask,
            depth_imbalance=book_values.depth_imbalance,
            spread_zscore=_zscore(book_values.spread, state.spreads),
            replenishment_size=_delta_add_size(event.book_delta),
            cancel_size=_delta_cancel_size(event.book_delta),
            cancel_add_size=_delta_cancel_add_size(event.book_delta),
            trade_count=self._trade_counts(state, now_ms),
            signed_volume=self._signed_volumes(state, now_ms),
            taker_pressure=self._taker_pressures(state, now_ms),
            spot=spot,
            log_return_1s=self._spot.last_return_1s,
            rolling_returns=self._rolling_returns(now_ms),
            realized_vol_annualized=None if vol_estimate is None else vol_estimate.sigma_annualized,
            drift_annualized=None if vol_estimate is None else vol_estimate.drift_annualized,
            vol_estimate=vol_estimate,
            strike=event.strike,
            distance_to_strike=_distance_to_strike(spot, event.strike),
            normalized_cliff_pressure=_normalized_cliff_pressure(
                spot, event.strike, event.seconds_to_close, vol_estimate
            ),
            round_number_distance=_round_number_distance(spot, self.round_number_step_usd),
            round_number_magnet=_round_number_magnet(spot, self.round_number_step_usd),
            binary_mid_prob=mid_prob,
            spot_fair_prob=fair_prob,
            logit_divergence=_logit_divergence(mid_prob, fair_prob),
            bernoulli_entropy_mid=entropy_mid,
            bernoulli_entropy_fair=_bernoulli_entropy(fair_prob),
            entropy_compression_rate=self._entropy_compression_rate(state, now_ms, entropy_mid),
            reflexivity_residual=_reflexivity_residual(
                previous_mid=state.previous_mid,
                current_mid=mid,
                previous_spot=state.previous_spot,
                current_spot=spot,
            ),
            liquidity_elasticity=_liquidity_elasticity(
                previous_mid=state.previous_mid,
                current_mid=mid,
                signed_flow=self._signed_volume(state, now_ms, max(self.tape_windows_seconds)),
                depth=book_values.total_depth_l1,
            ),
        )
        if mid is not None:
            state.previous_mid = mid
        if spot is not None:
            state.previous_spot = spot
        return snapshot

    def _update_spot(self, now_ms: int, spot: float | None) -> float | None:
        if spot is None:
            return self._spot.previous_spot
        current = float(spot)
        if current <= 0.0:
            raise ValueError("spot must be positive")
        self._spot.spot_history.append((now_ms, current))
        previous = self._spot.previous_spot
        previous_ms = self._spot.previous_event_time_ms
        self._spot.previous_spot = current
        self._spot.previous_event_time_ms = now_ms
        if previous is None or previous_ms is None or now_ms <= previous_ms:
            return current

        elapsed_seconds = max(1, int((now_ms - previous_ms) // 1000))
        total_return = math.log(current / previous)
        per_second_return = total_return / elapsed_seconds
        for _ in range(elapsed_seconds):
            self._spot.returns_1s.append(per_second_return)
        self._spot.last_return_1s = per_second_return
        return current

    def _estimate_vol(self, seconds_to_close: float) -> VolDriftEstimate | None:
        if not self._spot.returns_1s:
            return None
        return estimate_vol_drift(list(self._spot.returns_1s), seconds_to_close)

    def _fair_probability(
        self,
        event: EventFeatureInput,
        spot: float | None,
        estimate: VolDriftEstimate | None,
    ) -> float | None:
        if spot is None or event.strike is None:
            return None
        result = settlement_fair_probability(
            SettlementProbabilityInput(
                spot=spot,
                strike=float(event.strike),
                seconds_to_close=float(event.seconds_to_close),
                realized_vol_annualized=None if estimate is None else estimate.sigma_annualized,
                implied_vol_annualized=event.implied_vol_annualized,
                drift_annualized=0.0 if estimate is None else estimate.drift_annualized,
                observed_settlement_average=event.observed_settlement_average,
                observed_settlement_seconds=event.observed_settlement_seconds,
            ),
            self.fair_prob_config,
        )
        return result.probability_yes

    def _trade_counts(self, state: _TickerState, now_ms: int) -> dict[int, int]:
        return {
            window: len(_window_values(state.trades, now_ms, window))
            for window in self.tape_windows_seconds
        }

    def _signed_volumes(self, state: _TickerState, now_ms: int) -> dict[int, float]:
        return {
            window: self._signed_volume(state, now_ms, window)
            for window in self.tape_windows_seconds
        }

    def _signed_volume(self, state: _TickerState, now_ms: int, window: int) -> float:
        return sum(value for _, value in _window_values(state.trades, now_ms, window))

    def _taker_pressures(self, state: _TickerState, now_ms: int) -> dict[int, float | None]:
        out: dict[int, float | None] = {}
        for window in self.tape_windows_seconds:
            values = [value for _, value in _window_values(state.trades, now_ms, window)]
            gross = sum(abs(value) for value in values)
            out[window] = None if gross <= 0.0 else sum(values) / gross
        return out

    def _rolling_returns(self, now_ms: int) -> dict[int, float | None]:
        out: dict[int, float | None] = {}
        for window in self.return_windows_seconds:
            values = _window_values(self._spot.spot_history, now_ms, window)
            out[window] = None
            if len(values) >= 2:
                first = values[0][1]
                last = values[-1][1]
                out[window] = math.log(last / first) if first > 0.0 and last > 0.0 else None
        return out

    def _entropy_compression_rate(
        self, state: _TickerState, now_ms: int, entropy_mid: float | None
    ) -> float | None:
        if entropy_mid is None:
            return None
        values = _window_values(state.entropy_history, now_ms, max(self.return_windows_seconds))
        if len(values) < 2:
            return None
        previous_ts, previous_entropy = values[-2]
        elapsed_seconds = (now_ms - previous_ts) / 1000.0
        if elapsed_seconds <= 0.0:
            return None
        return (previous_entropy - entropy_mid) / elapsed_seconds


@dataclass(frozen=True, slots=True)
class _BookFeatureValues:
    best_bid: float | None
    best_ask: float | None
    mid: float | None
    spread: float | None
    l1_queue_imbalance: float | None
    depth_yes_bid: dict[int, float]
    depth_yes_ask: dict[int, float]
    depth_imbalance: dict[int, float | None]
    total_depth_l1: float


def _book_features(book: KalshiOrderBook | None, levels: tuple[int, ...]) -> _BookFeatureValues:
    if book is None:
        return _BookFeatureValues(None, None, None, None, None, {}, {}, {}, 0.0)
    best_bid = _decimal_to_float(book.best_yes_bid)
    best_ask = _decimal_to_float(book.best_yes_ask)
    mid = _decimal_to_float(book.mid_yes)
    spread = _decimal_to_float(book.spread_yes)
    depth_yes_bid: dict[int, float] = {}
    depth_yes_ask: dict[int, float] = {}
    depth_imbalance: dict[int, float | None] = {}
    for level in levels:
        bid_depth = _decimal_to_float(book.depth("yes", level)) or 0.0
        ask_depth = _decimal_to_float(book.depth("no", level)) or 0.0
        depth_yes_bid[level] = bid_depth
        depth_yes_ask[level] = ask_depth
        denom = bid_depth + ask_depth
        depth_imbalance[level] = None if denom <= 0.0 else (bid_depth - ask_depth) / denom
    return _BookFeatureValues(
        best_bid=best_bid,
        best_ask=best_ask,
        mid=mid,
        spread=spread,
        l1_queue_imbalance=_decimal_to_float(book.l1_imbalance()),
        depth_yes_bid=depth_yes_bid,
        depth_yes_ask=depth_yes_ask,
        depth_imbalance=depth_imbalance,
        total_depth_l1=depth_yes_bid.get(1, 0.0) + depth_yes_ask.get(1, 0.0),
    )


def _decimal_to_float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _signed_trade_size(trade: TradePrint) -> float:
    side = (trade.side or "").lower()
    sign = -1.0 if side in {"sell", "no", "bid_no"} else 1.0
    return sign * float(trade.size)


def _window_values(
    items: deque[tuple[int, float]], now_ms: int, window_seconds: int
) -> list[tuple[int, float]]:
    floor_ms = now_ms - (window_seconds * 1000)
    return [(ts_ms, value) for ts_ms, value in items if ts_ms >= floor_ms]


def _prune_time_deque(
    items: deque[tuple[int, float]], now_ms: int, max_window_seconds: int
) -> None:
    floor_ms = now_ms - (max_window_seconds * 1000)
    while items and items[0][0] < floor_ms:
        items.popleft()


def _zscore(value: float | None, values: deque[float]) -> float | None:
    if value is None or len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((item - mean) ** 2 for item in values) / len(values)
    std = math.sqrt(max(variance, 0.0))
    if std <= EPS:
        return 0.0
    return (value - mean) / std


def _delta_size(delta: BookDelta | None) -> float | None:
    if delta is None:
        return None
    if delta.delta_size is not None:
        return float(delta.delta_size)
    if delta.previous_size is None or delta.new_size is None:
        return None
    return float(delta.new_size) - float(delta.previous_size)


def _delta_add_size(delta: BookDelta | None) -> float:
    size = _delta_size(delta)
    return 0.0 if size is None else max(size, 0.0)


def _delta_cancel_size(delta: BookDelta | None) -> float:
    size = _delta_size(delta)
    return 0.0 if size is None else max(-size, 0.0)


def _delta_cancel_add_size(delta: BookDelta | None) -> float:
    size = _delta_size(delta)
    return 0.0 if size is None else size


def _distance_to_strike(spot: float | None, strike: float | None) -> float | None:
    if spot is None or strike is None:
        return None
    return float(spot) - float(strike)


def _normalized_cliff_pressure(
    spot: float | None,
    strike: float | None,
    seconds_to_close: float,
    estimate: VolDriftEstimate | None,
) -> float | None:
    distance = _distance_to_strike(spot, strike)
    if distance is None:
        return None
    sigma_annualized = 0.0 if estimate is None else estimate.sigma_annualized
    sigma_price = (
        float(spot)
        * sigma_annualized
        * math.sqrt(max(float(seconds_to_close), 0.0) / SECONDS_PER_BTC_YEAR)
    )
    return distance / (sigma_price + EPS)


def _round_number_distance(spot: float | None, step: float) -> float | None:
    if spot is None:
        return None
    nearest = round(float(spot) / step) * step
    return float(spot) - nearest


def _round_number_magnet(spot: float | None, step: float) -> float | None:
    distance = _round_number_distance(spot, step)
    if distance is None:
        return None
    return 1.0 - min(abs(distance) / (step / 2.0), 1.0)


def _probability_from_mid(mid: float | None) -> float | None:
    if mid is None:
        return None
    return _clip_probability(mid)


def _clip_probability(probability: float) -> float:
    return min(max(float(probability), PROB_EPS), 1.0 - PROB_EPS)


def _logit(probability: float) -> float:
    clipped = _clip_probability(probability)
    return math.log(clipped / (1.0 - clipped))


def _logit_divergence(mid_prob: float | None, fair_prob: float | None) -> float | None:
    if mid_prob is None or fair_prob is None:
        return None
    return _logit(mid_prob) - _logit(fair_prob)


def _bernoulli_entropy(probability: float | None) -> float | None:
    if probability is None:
        return None
    p = _clip_probability(probability)
    return -(p * math.log(p) + (1.0 - p) * math.log(1.0 - p))


def _reflexivity_residual(
    *,
    previous_mid: float | None,
    current_mid: float | None,
    previous_spot: float | None,
    current_spot: float | None,
) -> float | None:
    if None in {previous_mid, current_mid, previous_spot, current_spot}:
        return None
    if previous_spot == 0.0:
        return None
    binary_move = float(current_mid) - float(previous_mid)
    spot_move = math.log(float(current_spot) / float(previous_spot))
    return binary_move - spot_move


def _liquidity_elasticity(
    *,
    previous_mid: float | None,
    current_mid: float | None,
    signed_flow: float,
    depth: float,
) -> float | None:
    if previous_mid is None or current_mid is None:
        return None
    scaled_flow = abs(float(signed_flow)) / max(float(depth), EPS)
    return (float(current_mid) - float(previous_mid)) / (scaled_flow + EPS)
