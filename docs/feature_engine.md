# M3 Event-Time Feature Engine

The feature engine lives in `kalshi_btc_engine_v2.features` and is deterministic:
callers pass `EventFeatureInput` snapshots in event-time order and receive an immutable
`FeatureSnapshot` indexed by `(market_ticker, seconds_to_close, event_time_ms)`.
It does not require a live database and only reads existing order book and model APIs.

## Inputs

- `KalshiOrderBook` from `core.orderbook`, using YES bids and NO bids where YES ask is
  `1 - best_no_bid`.
- Optional `TradePrint` for Kalshi tape updates.
- Optional BTC `spot`, `strike`, and implied or settlement fields for fair-probability features.
- Optional `BookDelta` for cancel/add placeholders. If only previous and new size are known,
  the engine derives the delta as `new_size - previous_size`.

## Formulas

- Best bid, ask, mid, and spread use the book properties already defined in `KalshiOrderBook`.
- L1 queue imbalance is `(yes_bid_qty - no_bid_qty) / (yes_bid_qty + no_bid_qty)`.
- Multi-level depth is summed over top `L1`, `L3`, `L5`, and `L10` price levels. Depth imbalance
  uses `(bid_depth - ask_depth) / (bid_depth + ask_depth)`.
- Spread z-score is the current spread against the per-market rolling spread distribution.
- Tape signed volume is positive for YES/buy prints and negative for NO/sell prints. Taker
  pressure is `signed_volume / gross_volume` for each rolling time window.
- BTC 1-second returns are log returns. If event spacing is larger than one second, the quote
  move is evenly allocated across elapsed whole seconds before calling `models.vol_estimator`.
- Realized volatility and drift come directly from `estimate_vol_drift`.
- Distance to strike is `spot - strike`.
- Normalized cliff pressure is `(spot - strike) / (spot * sigma_ann * sqrt(tau/year) + eps)`.
- Round-number distance is measured to the nearest configurable BTC round-number step; magnet is
  `1 - min(abs(distance) / (step / 2), 1)`.
- Spot fair probability is `settlement_fair_probability` fed by the current spot, strike, rolling
  realized volatility, and drift estimate.
- Logit divergence is `logit(binary_mid_prob) - logit(spot_fair_prob)` with probabilities clipped
  to `[1e-6, 1 - 1e-6]`.
- Bernoulli entropy is `-[p log(p) + (1-p) log(1-p)]`.
- Entropy compression rate is previous rolling-window entropy minus current entropy.
- Liquidity elasticity is `mid_move / (abs(signed_flow) / depth + eps)`.

## Placeholders

The replenishment, cancel, and cancel-add fields are deterministic placeholders because exchange
L2 deltas do not fully identify queue position, hidden liquidity, or true order intent. They are
production-usable as signed visible depth changes, not as order-level attribution.

The reflexivity residual is also a placeholder: `binary_mid_move - BTC_log_move`. It is intended as
a stable diagnostic for whether binary repricing is outpacing the latest spot move, not as a causal
microstructure model.
