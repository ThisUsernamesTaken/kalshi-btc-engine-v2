# Milestone 4 Fair Probability

This milestone adds a standalone settlement-aware probability module. It does
not place orders, alter policy, or depend on the live BTC Bias engine.

Implemented:

- Case one (`seconds_to_close > 60`): log-space diffusion to the final 60s
  settlement-window average.
- Case two (`seconds_to_close <= 60`): required remaining average
  `K_req = (K * w - observed_sum) / h`.
- Drift shrinkage and annualized volatility floor.
- Optional realized/implied volatility selection.
- Dependency-free isotonic calibration by `seconds_to_close` bucket.
- Prediction-market power-logit recalibration helper.

The model treats volatility and drift as annualized log-return quantities. It
uses a normal approximation to the log of the relevant settlement-window
average, which is suitable for fast gating and backtest iteration but should be
calibrated against captured KXBTC15M outcomes before live use.

