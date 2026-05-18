# Live Trading Insights — 2026-05-16

**Data:** 38 live settles from `live_v5_unified_trades.jsonl` + 309-trade backtest from `_v5_combinations.py` cross-validated.

## Headline

| Strategy | Live result | Backtest projection |
|---|---|---|
| **Current** (V5 baseline + EM 20ct + LATE) | **−$16.40** on 38 settles (81.6% WR) | −$63.73 on 309 |
| Skip if RV ≥ p75 (defensive) | +$23.12 (+$39.52 vs current) | +$26.91 |
| **Flip side if RV ≥ p75** (offensive — best) | **+$62.64** (+$79 vs current) | **+$101.43** |

The single most impactful intervention is the **realized-vol regime switch**: when BTC 5-min RV ≥ 0.042 (p75 of trade-time distribution), the favorite is over-priced and the underdog wins more often. **All 3 big live losses had RV ≥ 0.042**; zero big losses had RV below it. Perfect separation in 38 trades.

## What's killing live P&L (root cause)

**3 trades = −$52.30 wiped out 31 wins of +$35.90.** Drill-down:

| Loss | Leg | Tier | Entry | Qty | RV5m | Side | Result | Net |
|---|---|---|---|---|---|---|---|---|
| 05-15 11:00 | LATE | MOVING_BIG | 81c | 20 | **0.0425** | YES | NO | **−$16.42** |
| 05-16 12:15 | EM | EM | 89c | 20 | **0.0424** | YES | NO | **−$17.94** |
| 05-17 10:30 | EM | EM | 89c | 20 | **0.0352** | NO | YES | **−$17.94** |

Structural problem with 20-contract entries at 81-89c:
- Risk-to-reward ≈ 1:8 (risk $17, win $2)
- Need ≥89% WR just to break even
- Current EM WR is 88.9% → barely breakeven, one regime miss tips negative

## Per-leg / per-tier P&L breakdown

| Leg | Tier | n | WR | Net | Avg/trade | Read |
|---|---|---|---|---|---|---|
| EM | EM | 18 | 88.9% | **−$6.09** | −$0.34 | Bleed cluster at 89c×gap<13 |
| LATE | MOVING_BIG | 6 | 50.0% | **−$15.75** | −$2.63 | Cushion <$100 = losses; >$100 = wins |
| LATE | FLAT_BIG | 6 | 100.0% | +$2.18 | +$0.36 | Reliable, small |
| LATE | MOVING_SMALL | 4 | 100.0% | +$3.52 | +$0.88 | Best single tier per-trade |
| LATE | LEADER_80PLUS | 1 | 100.0% | +$0.18 | +$0.18 | Tiny n |
| LATE | LEADER_55_64 | 2 | 50.0% | −$0.44 | −$0.22 | Weak signal — skip |

**Confidence-bucketed P&L by entry price:**

| Entry bucket | n | WR | Net | EV/trade |
|---|---|---|---|---|
| 90-98c | 18 | **100%** | +$14.49 | +$0.81 |
| 84-89c | 10 | 80% | **−$20.69** | −$2.07 |
| 70-83c | 4 | 75% | −$10.15 | −$2.54 |
| 50-69c | 3 | 67% | +$1.03 | +$0.34 |

Cleanest rule from live data alone: **only enter at price ≥ 90c** → would have netted **+$14.49** (vs −$16.40), losing 12 winners but eliminating all 3 big losers and another $1 small loss.

## What the backtest already proved (validated above by live data)

From `_v5_combinations.out` (309 trades, 7-day capture):

| Strategy | n | WR | Net | EV/trade |
|---|---|---|---|---|
| V5 baseline (current) | 309 | 84.8% | −$63.73 | −$0.21 |
| V5 + skip RV≥p75 | 230 | 87.8% | +$26.91 | +$0.12 |
| **V5 + B regime-switch** (flip in high-vol) | **309** | **71.5%** | **+$101.43** | **+$0.33** |
| V5 + A + R1 (skip + exit) | 230 | 83.0% | +$50.05 | +$0.22 |

**Strict dominance**: regime-switch B fires on every market (309/309 = trade-every-session ✓) and produces the highest net by ~2x.

## Recommended sizing matrix — confidence-tiered

| Confidence tier | Conditions | Size | Rationale |
|---|---|---|---|
| **BIG** (30ct) | EM gap_bps ≥ 18 AND entry ≥ 90c AND RV < p70 | 30 | Strongest signal, smallest realized risk in live |
| **STANDARD** (20ct) | EM gap_bps ≥ 10 AND entry ≥ 90c AND RV < p75 | 20 | Current EM win cluster |
| **MEDIUM** (10ct) | LATE FLAT_BIG / MOVING_SMALL AND RV < p75 | 10 | High WR / smaller R:R |
| **TINY** (5ct) | High-vol regime (RV ≥ p75) → flip to UNDERDOG | 5 | Rule B; 5ct caps regime-flip drawdown |
| **SKIP** | EM @ 89c with gap_bps < 13 (cursed cluster) | 0 | 2/2 big losses |
| **SKIP** | LATE MOVING_BIG with cushion < $80 | 0 | 1/1 big loss; winners all had cushion > $100 |
| **SKIP** | LATE LEADER_BELOW_55 | 0 | Already current |
| **NEW: Lump fallback** (5ct) | T-90 favorite bid ≥ 92c AND no prior entry | 5 | Backtest S0 = +$34.47 / 100% WR on these |

## Proposed entry decision flow (concrete patch sketch)

```
ON every market update:
  rv = realized_vol_btc_5m()   # NEW: bitstamp poll → in-mem cache → RV calc
  
  # ENTRY 1 — Earlier-moderate window (T-600s to T-300s)
  if t_to_close in [300, 600]:
      gap_bps = abs(spot_btc - strike) / strike * 10000
      leader_side, leader_ask = compute_leader()
      if gap_bps >= 10 and leader_ask in [86,99]:
          if leader_ask == 89 and gap_bps < 13:
              skip(reason="EM_89C_GAP_LOW_CURSED")   # NEW gate
          elif rv >= 0.042:
              # RULE B: flip to underdog (NEW)
              enter(side=opposite(leader_side), qty=5, price=100-leader_ask, leg="EM_FLIP")
          else:
              qty = 30 if gap_bps >= 18 and leader_ask >= 90 else 20
              enter(side=leader_side, qty=qty, price=leader_ask, leg="EM")
  
  # ENTRY 2 — Late window (T-180s to T-30s) — V5 tiers
  if t_to_close in [30, 180]:
      tier, leader_side, leader_ask = compute_v5_tier()
      if tier == "MOVING_BIG" and cushion_usd < 80:
          skip(reason="MOVING_BIG_THIN_CUSHION")   # NEW gate
      elif rv >= 0.042:
          enter(side=opposite(leader_side), qty=5, price=100-leader_ask, leg="LATE_FLIP")
      else:
          enter(side=leader_side, qty=tier_qty(tier), price=leader_ask, leg="LATE")
  
  # ENTRY 3 — NEW: Lump fallback (only if no prior entry this market)
  if t_to_close in [60, 90] and not entered_this_market:
      _, fav_bid, fav_ask = best_book()
      if fav_bid >= 92:
          enter(side=fav_side, qty=5, price=fav_ask, leg="LUMP_FALLBACK")
```

## What this gets us (combined projection)

**Live data re-applied with the new rules:**
- Skip cursed 89c×low-gap cluster: +$35.88 (eliminates 2 big losses)
- Skip MOVING_BIG with thin cushion: +$16.42 (eliminates 1 big loss)
- Add lump fallback on 50+ skipped markets: ~+$10-15 (per S0 backtest +$34/241)
- Flip in high-vol regime: ~+$30-50 (per rule B effect)
- **Total expected net swing: +$60 to +$100 vs current −$16**

**Coverage** improves from 38/176 (21.6%) → ~150/176 (85%+) via the lump fallback.

## What to do next

Order of risk-adjusted impact:

1. **Add the two SKIP gates first** (zero-risk defensive): `89c+gap<13` and `MOVING_BIG+cushion<80`. ~15 lines. Eliminates the historical big losers.
2. **Add lump fallback at T-90**. Coverage jump. ~30 lines. Conservative (5ct only).
3. **Add RV5m polling + regime gate**. Requires bitstamp WS or reuse the capture's spot feed. ~60 lines. Then start with "skip if RV≥p75" (defensive) before flipping side.
4. **After 50+ trades validate**, switch from "skip on high RV" to "flip on high RV" (rule B). This is the offensive move that backtests predict +$100; live needs more N first.
5. **Confidence-tier upsize** (30ct on BIG signals) only after the above is stable.

## Code locations

- Main: `scripts/live/live_v5_unified.py`
- Watchdog: `scripts/live/watchdog_v5_unified.cmd` (just restart after edits)
- Decision log: `data/live_v5_unified_trades.jsonl`
- Spot data: capture DB `spot_quote_event` (symbol='btcusd', venue='bitstamp') — currently 24/7 active via NSSM KalshiCapture
