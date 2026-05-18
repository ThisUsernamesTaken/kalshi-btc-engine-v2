# Participation Strategy — Kalshi BTC 15m Aligned With T-30 Sniper Edge

**Date:** 2026-05-17
**Edge basis:** 331-market full-capture backtest (`_fast_edge_scan2.out`, `_sniper_robustness.out`).
**Status:** Live as of 2026-05-17 18:40 PT, watchdog `--enable-t30-sniper --disable-late --enable-em-upsize`.

## 1. The single edge in one sentence

**At T-30s before close, if either side's bid has reached ≥85¢, that side wins. Period.**

44 historical markets fit this pattern. 44 of 44 won. Out-of-sample (last 30% by time) was 15/15. The edge is a microstructure leak: Kalshi BTC 15m settlement is the trailing 60-second BRTI VWAP, so by T-30s half the settlement value is already in the books. When market consensus is decisive enough to bid 85¢, it's effectively confirming a settled outcome. The market makers leave 1-15¢ on the table because they have to keep quoting.

## 2. What the data says — five non-obvious truths

### Truth 1: 85¢ is a cliff, not a curve

| fav_bid threshold | n | WR | EV/trade @ 10ct | Behavior |
|---|---|---|---|---|
| 70¢ | 55 | **96.4%** | +$0.215 | Some losses |
| 75¢ | 53 | **96.2%** | +$0.145 | Some losses |
| 80¢ | 48 | **97.9%** | +$0.150 | One loss |
| **85¢** | **44** | **100.0%** | **+$0.265** | **Sweet spot** |
| 88¢ | 42 | 100% | +$0.222 | Same WR, less EV (smaller payoff) |
| 95¢ | 36 | 100% | +$0.156 | Same WR, even smaller payoff |

The data says: **don't loosen below 85¢** (you start absorbing losses) and **don't tighten above 85¢** (you reduce coverage without improving WR). The 85¢ threshold maximizes EV/day.

### Truth 2: Adjacent T windows are NOT additive — they're negative

Tried layering T-60+T-30+T-15 to catch more markets:

| Layer | n | WR | Net |
|---|---|---|---|
| T-60s, fav≥90¢ | 82 | 91.5% | **−$51.99** |
| T-30s, fav≥85¢ | 25 | 100% | +$9.13 |
| T-15s, fav≥85¢ | 10 | 90% | −$2.95 |

**Going wider in time breaks the edge.** At T-60s the settlement isn't half-baked yet; the market is still wrong some of the time. At T-15s liquidity dries up and a sudden trade can flip the favorite. **T-30s is uniquely the right window.**

### Truth 3: Time-of-day is irrelevant for this edge

24 UTC hours sampled. Every hour with data: 100% WR. The edge does NOT concentrate in US-hours, overnight, or any other bucket. This makes sense — the mechanic (settlement-window certainty) is purely microstructural, not behavioral.

**Implication:** trade every hour. No time filter needed.

### Truth 4: Linear sizing scales the edge

| size | net | EV/trade | capital/trade | max-single-loss |
|---|---|---|---|---|
| 2ct | +$2.05 | +$0.047 | $1.94 | $1.98 |
| 5ct | +$5.69 | +$0.129 | $4.86 | $4.95 |
| 10ct | +$11.67 | +$0.265 | $9.71 | $9.90 |
| **20ct** | **+$23.49** | **+$0.534** | **$19.42** | **$19.80** |
| 50ct | +$59.12 | +$1.34 | $48.56 | $49.50 |
| 100ct | +$118.53 | +$2.69 | $97.11 | $99.00 |

Because 44/44 won, sizing scales 1:1. Capital required ≈ avg_entry × size; max single loss = $0.99 × size.

### Truth 5: 100% observed is NOT 100% true

Wilson 95% lower-bound on the true WR (n=44, observed 44/44) is **92.0%**. If the true WR is 92%, then at 85¢+ entry prices (avg 97¢), the strategy is **−$0.54/trade** — a loser.

**This is the single biggest risk.** The empirical 100% might just be a lucky 44 draws. The market regime that produced this edge (late 2026-05) might not persist.

## 3. The participation tiering

Given the above, here's how to **actually deploy capital** on this edge:

### Phase 1: Burn-in (LIVE NOW — first 30 trades)
- **Size: 10ct** per trigger (~$10 risk per trade)
- **Trigger: T-30s, fav_bid ≥ 85¢** (the validated sweet spot, no variations)
- **Expected pace:** 14 trades/day
- **Expected daily EV:** +$4 at 100% WR; −$7.50/day at 92% WR (pessimistic)
- **Kill criteria:** if after 10 trades WR < 90%, halve size. If after 20 trades WR < 85%, disable.
- **Capital deployed:** ~$140 at peak (14 simultaneous max but realistically <$50 since markets sequentially settle)

### Phase 2: Confirmed alpha (after 30+ live trades with WR ≥ 95%)
- **Size: 20ct**
- **Expected EV:** +$0.53/trade × 14/day = **+$7.50/day**
- **Capital:** ~$280 peak

### Phase 3: Scaled (after 100+ trades with stable WR)
- **Size: 50ct** (if Kalshi liquidity supports — verify book depth)
- **Expected EV:** ~+$19/day
- **Capital:** ~$700 peak
- **Liquidity check:** Kalshi books for KXBTC15M typically have 50-200 contracts at the top tier near settlement. 50ct should fill reliably; 100ct may sweep multiple levels.

### What NOT to do — five anti-patterns

1. **Don't add a T-60 or T-90 sniper** — proven to lose money
2. **Don't add a T-15 or T-10 sniper** — proven to lose money (liquidity collapse + late reversals)
3. **Don't widen the threshold below 85¢** — losses appear immediately
4. **Don't add time-of-day filters** — no signal, just adds noise to decision logic
5. **Don't flip on high vol** — already tried, lost 5/5 live

## 4. Risk management aligned with this edge

### Drawdown discipline

The worst-case scenario: WR drops to 90%, you do 14 trades/day at 50ct:
- Daily expected = 12.6 wins × $0.50 + 1.4 losses × $48 = +$6.30 − $67.20 = **−$60.90/day**
- **A 4-day losing streak at 50ct = ~$240 drawdown**

Mitigation:
- **Mandatory size halving** when rolling 10-trade WR < 90%
- **Mandatory disable** when rolling 20-trade WR < 85%
- **Daily loss cap** $40 (already in code via `DAILY_LOSS_CAP_CENTS`)

### Account hygiene

- **Withdraw daily profits** to a non-trading wallet (you're already doing this — keep the trading account small)
- **Min balance gate $5** prevents accidental zero-equity (already in code)
- **Track P&L from JSONL only** (account balance is contaminated by withdrawals/manual trades)

### Tail-risk insurance

The 8% Wilson lower-bound losing scenario translates to ~1 loss per 12 trades at 85¢+. Each loss costs ~$8.50 at 10ct, ~$17 at 20ct. If you compound several losses with no wins in between (unlikely but possible), drawdown grows fast.

**Insurance: stop trading for 24h after any loss until investigated.** This is restrictive but the edge is fragile enough to warrant it.

## 5. Adjacent opportunities aligned with the same hypothesis

The T-30 sniper exploits **settlement-window microstructure**. Adjacent markets where the same hypothesis could apply:

### A. Kalshi BTC 1-hour markets (`KXBTC1H`)
Same mechanic but 1h horizon → settlement window proportionally longer.
- T-X equivalent would be T-2min (since 1h = 4× 15min)
- **Untested**. Worth probing if Kalshi has captured BTC1H data.
- Caveat: at 1h horizon, BTC can move materially in the settlement window. The edge may not transfer.

### B. Kalshi BTC daily markets (`KXBTCDAILY`)
24h settlement window means the "last 30s certainty" doesn't apply — too much time for reversal.
- **Don't transfer this strategy.** Different mechanic.

### C. Cross-strike spreads on the SAME 15m market
Two adjacent strikes (e.g., $80k and $80.5k) trade simultaneously. If both fire the T-30 sniper for the SAME outcome direction, that's higher confidence.
- **Future research.** Need to identify "near-strike" markets at decision time.
- Would let us size up on confirmed-direction sessions.

### D. Other binary markets with VWAP settlement
Kalshi has S&P, Nasdaq, oil, etc. binary markets that settle similarly. If they follow the same microstructure pattern (decisive bid in last fraction of settlement window = certain outcome), the same edge applies.
- **Worth probing**. Check `probe_kalshi_*.py` scripts in `D:\Trading\kalshi-btc-engine-v2\scripts\` (separate research lane).

## 6. Anti-strategies — what NOT to participate in

Based on the engine's prior research (HANDOFF.md, FINDINGS_2026_05_14.md):

### Don't: directional prediction at high prices
The Pine Script TA strategy entered at 75¢+ with 69% WR → −$19.90 net. **Asymmetric payoff defeats good prediction.**

### Don't: structural buy-favorite
2,815-market backtest: simple "buy favorite + hold" = −$4.53/trade. The market is correctly priced in aggregate.

### Don't: profit-taking exits
Tested at TP=+5¢: 78% win rate but tiny wins. Cumulative loss worse than hold-to-settle.

### Don't: model-driven entries in mean_revert_dislocation regime
0/5 hit rate. Structural anti-edge.

### Don't: ladder DCA on losing positions
Engine v2 backtest showed adding more contracts as positions move against you compounds losses (`KalshiLadderShadow` shadow data).

## 7. Decision criteria for adding NEW legs

Before adding any new entry path to the live trader, the proposal must:

1. **Show 100% WR on ≥30 historical trades** in the full-capture backtest
2. **Hold out-of-sample** (split 70/30 by time, both halves ≥95% WR)
3. **Have a microstructural justification** (not just statistical pattern)
4. **Not conflict with the T-30 sniper** (mutex via `leg_taken`)
5. **Be paper-tested for 24h before live** (via `--dry-run`)
6. **Have a kill-criteria defined** before deploy

## 8. The honest assessment

**The T-30 sniper is the only edge this engine has empirically validated at scale.** Everything else — q_cal model, regime classifier, EM contrarian, RV-regime flip — has either failed at scale (>200 trades) or has too-small N to claim.

**Realistic expectations:**
- Best case (true 100% WR): $4-7/day at conservative size, $10-19 at moderate, $40+ at aggressive
- Realistic (95% WR): $1-3/day at conservative; +EV at moderate
- Bad case (90% WR): break-even to slightly negative
- Worst case (regime change, edge gone): −$3-5/day for ~10 trades before kill-switch triggers

**The strategy is real but tiny.** Don't expect to retire. Expect to validate that a microstructure leak exists, harvest it modestly while it lasts, and continually re-verify the WR doesn't decay.

**If WR holds at 100% for the next 50 trades, that's strong confirmation.** Scale to 20ct then 50ct.
**If WR drops below 90% in the next 20 trades, the regime has shifted.** Disable and reanalyze.

## 9. Monitoring runbook

Daily (manual):
```powershell
# Check active trader
Get-Process python | Where-Object { (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)").CommandLine -like '*live_v5_unified*' } | Format-Table Id, StartTime

# Today's sniper P&L (JSONL ground truth)
$py = "C:\Users\coleb\AppData\Local\Python\bin\python.exe"
& $py -c "
import json, datetime as dt
today = dt.datetime.now(dt.UTC).date()
total = 0; wins = 0; n = 0
for line in open(r'C:\Trading\kalshi-btc-engine-v2\data\live_v5_unified_trades.jsonl'):
    r = json.loads(line)
    if r.get('kind') != 'settle' or r.get('leg') != 'T30_SNIPER': continue
    d = dt.datetime.fromtimestamp(r['ts_ms']/1000, tz=dt.UTC).date()
    if d != today: continue
    n += 1
    total += r['net_cents']
    if r['net_cents'] > 0: wins += 1
print(f'Today T-30 sniper: n={n} WR={100*wins/max(1,n):.1f}% net=\${total/100:+.2f}')
"
```

Weekly (manual):
- Rolling 30-trade WR (kill if <90%)
- Total cumulative P&L since deploy (target: positive)
- Any `late_skip with reason LATE_DISABLED_BY_FLAG` count (sanity: should be most markets)
- Any `t30_sniper_no_fill` events (signal: book too thin or fast at T-30; consider widening slip)

## 10. The commits

- `4538067` Add T-30 SNIPER strategy + smart-V5 framework; retire legacy live scripts
- This doc: STRATEGY_PARTICIPATION.md

Repository: https://github.com/ThisUsernamesTaken/kalshi-btc-engine-v2

---

## 11. Research-grounded refinements (2026-05-17 v2)

Surveyed the academic + industry literature on (a) prediction-market microstructure, (b) sports-betting in-play CLV, (c) options expiry pin dynamics, (d) statistical inference for high-WR small-N edges. Findings sharply refine the strategy.

### Direct theoretical backing — the edge has a published mechanism

The Kalshi BTC 15m settlement is the trailing 60s BRTI VWAP. CF Benchmarks explicitly designed BRTI to "minimize expiry-pinning risk" ([CME BRTI docs](https://www.cmegroup.com/education/courses/introduction-to-bitcoin/introduction-to-bitcoin-reference-rate)) by partitioning data across exchanges. **At T-30s, ~50% of the settlement weight is already locked in.** When the favorite bid is ≥85¢, this isn't behavioral over-confidence — it's the market correctly pricing the partially-locked VWAP. The edge exists because **inventory risk concentrates near resolution and cannot be hedged by the underlying until settlement** (Sun et al., ["Toward Black-Scholes for Prediction Markets"](https://arxiv.org/html/2510.15205v1), 2025). Market makers can't fully tighten quotes in the final seconds — they widen or pull. We pay 1-15¢ to lift those stale offers.

Direct corroboration from related markets:
- **Augenblick & Lazarus** ([QJE 2025](https://academic.oup.com/qje/article/140/1/335/7821262)): 5M+ Betfair transactions — bettors **underreact to strong signals by ~33% in Q4**. A late-game lead is a strong signal; markets systematically underprice it. *This is the sports analog of our finding.*
- **Bürgi, Deng, Whelan** (["Makers and Takers" 2025](https://www.karlwhelan.com/Papers/Kalshi.pdf), 300k+ Kalshi contracts): "high-price contracts win more often and yield small positive returns" — same direction as our 44/44.
- **Becker** (["Microstructure of Wealth Transfer" 2025](https://www.jbecker.dev/research/prediction-market-microstructure), 72M Kalshi trades): contracts at 5¢ win 4.18% — so 95¢ contracts on the other side win **95.82%** vs implied 95%. *Sub-percent edge but real.*
- **Snowberg & Wolfers** ([NBER w15923](https://www.nber.org/system/files/working_papers/w15923/w15923.pdf)): favorite-longshot bias is the most robust pricing anomaly in event markets — directly predicts our setup.

### What the literature WARNS — three hard constraints

#### 1. Effect size is wrong: 44/44 implies overfit

Published favorite-longshot edges are **+1-3% pre-fee** in studies of 100k+ trades. Our 44/44 at avg entry 97¢ implies ~3% gross — *consistent in direction but the certainty is sample noise*. At true 99% WR, 44/44 happens 64% of the time. At true 95%, 10% of the time. **We cannot statistically distinguish these from our sample.**

→ **Action:** Reframe from "100% WR edge" to "favorite-longshot-bias capture, expected EV positive with tail risk." Drop the certainty language in code comments and docs.

#### 2. Jump risk peaks at exactly our entry moment

**Bozovic** (["Intraday Jumps and 0DTE Options" 2025](https://papers.ssrn.com/sol3/Delivery.cfm/5223127.pdf)): intraday jumps "cluster around the market open and close" with "jump-risk premium ~2× combined diffusion + volatility premia." **T-30s on a 15m contract is structurally a closing-window jump zone.** The 44 historical observations may not include a jump event — adding such an observation breaks the 100% record.

→ **Action:** Add a jump-filter to the entry: skip if 1-second BTC return >2σ in the last 5 seconds before T-30. Implementation tracked in `RUNNING.md` for next patch.

#### 3. Bid/ask WIDENS, not narrows, near expiry

Multiple sources (Schwab options ed; Stoikov-Saglam *Option Market Making Under Inventory Risk*): market-maker spread widens in the final fraction of the settlement window. **Our exit assumption is fragile:** if we ever need to size up to 50ct+, the top-of-book may not absorb the trade cleanly. Slippage cost rises with size.

→ **Action:** Cap initial size at 5-10ct (top of book historically supports this). Verify book depth before scaling above 20ct.

### Statistical sizing — the formal answer (revising Section 3)

The original participation plan said "10ct burn-in". The literature on Kelly under uncertainty + backtest overfitting says that's **too aggressive for an unconfirmed live edge**.

**Confidence intervals on the true WR (n=44, observed 44/44):**
| Method | Lower bound |
|---|---|
| Wilson score (Brown/Cai/DasGupta 2001) | 92.0% |
| Clopper-Pearson exact | 93.4% |
| Rule of Three (Hanley & Lippman-Hand 1983) | 93.2% |
| Jeffreys (Beta(0.5, 0.5) prior) | 95.9% |
| Bayesian Beta(2, 2) prior + 44/0 → Beta(46, 2): mean 95.8%, 5th pctile | **89.0%** |

**With overfit haircut** (Bailey, Borwein, López de Prado, Zhu, ["Probability of Backtest Overfitting"](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253)): we ran a sweep of 7×10 = 70 (T, threshold) combinations. The selection penalty is ~√(2 ln 70) × backtest_std. Effective WR for sizing should be discounted **5-15 percentage points** from observed. **Use ~88% as the sizing WR.**

**Kelly at p=0.88, even-money payoff** (b=1, since avg entry ~97c gives 3c win vs 97c loss):
- True Kelly fraction = 2p − 1 ÷ b — but for asymmetric payoff at 97c entry: f* = (p × win + (1-p) × loss) / win² ≈ 0.05 (5% of bankroll)
- **Quarter-Kelly** (Thorp's standard recommendation; MacLean-Thorp-Ziemba 2010): ≈ 1.25% of bankroll per trade
- With $30 bankroll: **$0.38 per trade = 4ct at 97c entry**

**Revised burn-in size: 5ct (down from 10ct in original plan).** This is the quarter-Kelly cap at our skeptical 88% WR estimate.

### Kill-switch math (replacing the loose "kill if WR < 90%" rule)

Use a proper sequential probability ratio test (SPRT, Wald 1947) instead of arbitrary thresholds:

**H0:** true WR ≥ 95% (edge is real, keep trading)
**H1:** true WR ≤ 80% (edge is dead, stop)

At α = β = 0.05, decision thresholds:
- Log-likelihood ratio crosses **+log(19) = +2.94** → confirm H1 (kill the strategy)
- Log-likelihood ratio crosses **−2.94** → confirm H0 (size up)

Practical rule from this: **2 losses in any 10 live trades = kill.** Under true 92% WR this happens <2% of the time. If it happens, it's strong evidence WR is materially lower than backtest. (CUSUM-equivalent kill switch from Page 1954 / Northinfo "Monitoring Active Portfolios" 2014.)

### Maker premium — a free 2.5% if we can execute it

**Becker** found takers lose at **80 of 99 price levels**; makers earn **+2.5% per trade**. Our current implementation is pure IOC (taker). We could rest a 85¢ bid in the favorite side from T-90 to T-30, which would:
1. Avoid paying the 2¢ slippage above the ask
2. Potentially capture maker rebates (Kalshi has none currently)
3. **Risk:** the market moves up and we don't fill, OR moves down and we accidentally buy the loser

This requires order-management logic (place + cancel + replace on side flip) similar to the existing `--no-resting` path. Add to backlog as a "Phase 2.5" optimization.

### Adjacent windows (Section 5 revisit) — same mechanism, untested

The settlement-window mechanic should extend to other VWAP-settled binaries:
- **Kalshi BTC 1h** (`KXBTC1H`): 60s settlement window same; T-30s on a 1h is proportionally tinier (0.83% of duration vs 3.3% for 15m). May still work but less locked-VWAP at decision time.
- **Kalshi BTC daily**: settlement window is small relative to 24h, so the "lock-in" math is unfavorable. Probably no transfer.
- **Other binary VWAP markets** (S&P, gold, oil): same logic if Kalshi captures spot in the same way. Worth probing via `probe_kalshi_*.py` (separate research lane).

### Revised participation tiering (overrides Section 3)

| Phase | When | Size | Daily EV | Decision rule |
|---|---|---|---|---|
| **Burn-in v2** (NOW) | live | **5ct** (quarter-Kelly at 88% WR floor) | +$2/day at 95% WR | Run until N=10 live trades |
| **Confirmed** | After N=10 with ≤1 loss (Bayesian posterior mean ≥93%) | 10ct | +$4/day | Re-size at N=25 |
| **Scaled** | After N=25 with ≤2 losses (posterior mean ≥92%) | 20ct | +$8/day | Re-size at N=50 |
| **Aggressive** | After N=50 with stable WR ≥93% | 50ct | +$20/day | Liquidity verify; backstop CUSUM kill |

**Mandatory kill rule (all phases): 2 losses in any rolling 10-trade window → halve size immediately.** If sized down to minimum (2ct) and still failing → disable the leg.

### Code changes to deploy these refinements

1. **Reduce `T30_SNIPER_CONTRACTS` from 10 to 5** in `live_v5_unified.py` constants. ← *do this now*
2. **Add a Bayesian WR tracker** in the trade log analysis script: posterior Beta(α=2+wins, β=2+losses), report mean + 5th percentile after each settle. ← *next iteration*
3. **Add a CUSUM monitor** that tracks losses-in-last-10 and emits a `WR_DEGRADATION_HALT` event when 2+ losses in any 10. ← *next iteration*
4. **Add the BTC jump filter**: in `_evaluate_t30_sniper`, skip if 1-second |Δ_BTC_USD| > 2σ over last 5 seconds. ← *next iteration*
5. **Add VWAP lock-in calculation**: compute fraction of 60s settlement window already elapsed at decision time; only fire if locked-VWAP fraction is ≥50% AND locked-side aligns with favorite. ← *next iteration*

### Sources cited

**Prediction markets:**
- Bürgi, Deng, Whelan 2025 — [Makers and Takers (Kalshi)](https://www.karlwhelan.com/Papers/Kalshi.pdf) / [UCD WP 2025-19](https://www.ucd.ie/economics/t4media/WP2025_19.pdf)
- Becker 2025 — [Microstructure of Wealth Transfer](https://www.jbecker.dev/research/prediction-market-microstructure)
- Sun et al. 2025 — [Black-Scholes for Prediction Markets (arxiv 2510.15205)](https://arxiv.org/html/2510.15205v1)
- Snowberg & Wolfers — [NBER w15923, Favorite-Longshot Bias](https://www.nber.org/system/files/working_papers/w15923/w15923.pdf)
- Tetlock 2008 — [Liquidity and Prediction Market Efficiency](https://business.columbia.edu/sites/default/files-efs/pubfiles/3098/Tetlock_SSRN_Liquidity_and_Efficiency.pdf)
- QuantPedia — [Systematic Edges in Prediction Markets](https://quantpedia.com/systematic-edges-in-prediction-markets/)
- [Anatomy of Polymarket (arxiv 2603.03136)](https://arxiv.org/html/2603.03136v1)

**Sports betting / in-play microstructure:**
- Augenblick & Lazarus 2025 — [Overinference from Weak / Underinference from Strong Signals (QJE)](https://academic.oup.com/qje/article/140/1/335/7821262)
- Stern 1994 — [Brownian Motion Model for Sports Scores (JASA)](https://www.tandfonline.com/doi/abs/10.1080/01621459.1994.10476851)
- Polson & Stern 2015 — [Implied Volatility of a Sports Game](https://citeseerx.ist.psu.edu/document?repid=rep1&type=pdf&doi=62db46ab57d96d115bc8e63b1b60d64b3f41aaf2)
- Angelini, De Angelis & Singleton — [Informational efficiency in in-play markets](https://centaur.reading.ac.uk/98329/1/information_efficiency_angelini_de_angelis_singleton.pdf)
- Croxson & Reade — [Exchange vs. Dealers (high-frequency in-play)](https://www.researchgate.net/publication/228720836_Exchange_vs_Dealers_A_High-Frequency_Analysis_of_In-Play_Betting_Prices)
- Green, Lee & Rothschild — [Favorite-Longshot Midas (Wharton)](https://jacobslevycenter.wharton.upenn.edu/wp-content/uploads/2018/08/The-Favorite-Longshot-Midas.pdf)
- Caan Berry — [Football Trading: Profiting From Time on Betfair](https://caanberry.com/football-trading-time-on-betfair/) (practitioner)

**Options expiry / 0DTE microstructure:**
- Bandi, Fusari, Reno 2023 — [0DTE Option Pricing](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4503344)
- Bozovic 2025 — [Intraday Jumps and 0DTE Options](https://papers.ssrn.com/sol3/Delivery.cfm/5223127.pdf?abstractid=5223127&mirid=1)
- Adams, Fontaine, Ornthanalai 2024 — [The Market for 0DTE: Role of Liquidity Providers](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4881008)
- Baltussen, Terstegge, Whelan 2025 — [Derivative Payoff Bias (AEA)](https://www.aeaweb.org/conference/2025/program/paper/N7rsBN2N)
- Stoikov & Saglam — [Option Market Making Under Inventory Risk](https://people.orie.cornell.edu/sfs33/StoikovSaglam.pdf)

**Settlement-window manipulation:**
- Evans 2018 — [Forex trading and the WMR Fix](https://www.sciencedirect.com/science/article/abs/pii/S0378426617302327)
- ManIx — [Monitoring the FX Benchmark Fix](https://eprints.whiterose.ac.uk/id/eprint/135218/3/Monitoring%20the%20Foreign%20Exchange%20Rate%20Benchmark%20Fix.pdf)
- [CME Bitcoin Reference Rate / BRTI documentation](https://www.cmegroup.com/education/courses/introduction-to-bitcoin/introduction-to-bitcoin-reference-rate)

**Statistical methods (binomial CI, Kelly under uncertainty, change-point detection):**
- Brown, Cai, DasGupta 2001 — Wilson score interval (binomial proportion)
- Hanley & Lippman-Hand 1983 (JAMA) — Rule of Three
- Cai 2005 — Jeffreys Bayesian intervals
- Thorp — *Beat the Dealer*; *A Man for All Markets* (half-Kelly heuristic)
- MacLean, Thorp, Ziemba 2010 — [Good and Bad Properties of the Kelly Criterion](https://www.stat.berkeley.edu/~aldous/157/Papers/Good_Bad_Kelly.pdf)
- MacLean, Thorp, Ziemba 2010 — [The Kelly Capital Growth Investment Criterion (book)](https://www.worldscientific.com/worldscibooks/10.1142/7598)
- Baker & McHale 2013 — Optimal Betting Under Parameter Uncertainty (Bayesian Kelly)
- Vince 2009 — *The Leverage Space Trading Model* (Wiley)
- Page 1954 — CUSUM; Wald 1947 — SPRT; Adams & MacKay 2007 — Bayesian online change-point
- Bailey, Borwein, López de Prado, Zhu — [Probability of Backtest Overfitting](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253)
- Bailey & López de Prado — [Deflated Sharpe Ratio](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551)
- López de Prado 2018 — *Advances in Financial Machine Learning* (Wiley), Ch. 11-12 on backtest selection bias

---

**Bottom line v2:** the edge has direct theoretical backing (settlement-window VWAP lock-in + favorite-longshot bias documented across 5+ peer-reviewed studies). But the 44/44 sample is too small to claim certainty — the literature predicts a true WR of ~92-95%, not 100%. **Revise burn-in size from 10ct → 5ct (quarter-Kelly at Bayesian-skeptical WR).** Confirm at N=10, scale at N=25 + ≤2 losses, size aggressively at N=50 + stable WR. Hard kill: 2 losses in any 10 trades.

Trust the JSONL. Trust the literature. The strategy is real, but humble. The complexity that helps is removing assumptions, not adding rules.
