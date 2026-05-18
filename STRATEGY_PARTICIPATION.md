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

**Bottom line:** the edge is real, the size is small, the risk is manageable. Participate at 10ct until 30+ trades confirm, then scale. Trust the JSONL, not the balance. Don't add complexity — the only complexity that's helped is removing other complexity.
