# Strategy discovery analysis (2026-05-18)

This folder captures the analysis that turned the current live engines — all of
which are net-negative under realistic execution — into a sizing rule that
projects positive on the same historical tape.

## TL;DR findings

1. **No live engine is profitable** under realistic execution. Adding a flat
   +2c slippage assumption to every entry converts the combined 131-trade tape
   from a reported −$64 to a realistic −$152. The 2c assumption is calibrated
   to the v5_unified T-30 sniper's 2.9% fill rate — the only "validated edge"
   in the docs is essentially undeployable as currently coded.
2. **The engines are hold-to-settle**, not selling early. Every cent of the
   loss came from bad **entries**, not premature exits.
3. **The missing entry-time indicator is signed BTC velocity over 60s.** All
   five big losers had signed_v60 between +$64 and +$122 — we're systematically
   buying after exhaustion moves into the favorite.
4. **The engines have a sign problem, not just a sizing problem.** In the
   mid-conviction stripe (entry 70-91c), the engines pick the wrong side often
   enough that the *inverse* of their decision has positive edge. Specifically:
   v5 EM/cursed-stripe (n=13) and Pine early-bar/mid-entry (n=11) both flip to
   positive inverse-edge ≥ +9.8c/contract under realistic execution.
5. **The arithmetic floor**: at entry X cents, breakeven WR = X%. The engines
   sit at WR 70-77% across the 88-91c "cursed" stripe — barely below threshold
   on the engine side, comfortably profitable on the inverse side.
6. **Drawdowns cluster temporally.** Without a bucket-level streak halt, any
   sizing scheme that scales above ~10ct gets destroyed during the 2026-05-16
   EM-cursed loss cluster. With a 2-loss-in-a-row halt per bucket, the cluster
   is dodged and the strategy is strictly better than 0.10 Kelly cap-100ct on
   both net and max-DD axes.

## Files

| File | Purpose |
|---|---|
| `common.py` | Schema-tolerant trade-log loader + bucketing + edge math. |
| `01_baseline_audit.py` | Per-engine reported vs realistic P&L; fill-rate skew on v5_unified. |
| `02_bucket_edge_table.py` | Bucket-conditional edge table; KEEP/INVERT decisions. |
| `03_sizing_simulation.py` | Sizing-rule Pareto frontier (fixed vs dollar-at-risk vs Kelly vs streak-halt). |
| `build_edge_table.py` | Extracts `edge_table.json` from the current trade logs. |
| `edge_table.json` | Versioned snapshot of the edge table (regenerable). |

## How to run

From the repo root:

```powershell
$env:PYTHONPATH = "src"
$py = "C:\Users\coleb\AppData\Local\Python\bin\python.exe"

& $py -m analysis.01_baseline_audit
& $py -m analysis.02_bucket_edge_table
& $py -m analysis.03_sizing_simulation
& $py -m analysis.build_edge_table             # writes analysis/edge_table.json
& $py -m analysis.build_edge_table --dry-run   # print to stdout
```

## Deployment notes

The recommended sizing rule, as stored in `edge_table.json` under
`sizing_recommendation`:

```
- $-at-risk discrete tiers, scaled to bankroll: $12/$6/$3/$1.20 per trade
  (edge >=30c / >=20c / >=10c / >=5c). Linear scale-up as bankroll grows.
- Min edge per ct: 5c (below this, abstain).
- Max position: 100 contracts (Kalshi book-depth ceiling).
- Bucket streak halt: 2 losses in a row -> skip the next 10 trades from
  that specific bucket. Reset streak on a win.
```

Projection on the 131-trade historical tape: **+$398 net, −$104 max DD, 43% WR**.

**Important caveats**:
- The bucket WRs are derived from the same data they're applied to. Wilson 95%
  LCB suggests pessimistic EV is mildly negative on the marginal buckets. Treat
  any bucket with n < 10 as exploratory size only.
- The +$220 number from 0.10 Kelly cap-100ct was misleading: max running DD is
  also ~$220, not the −$28.89 single-trade max-loss reported originally. The
  streak-halt strategy beats it on both axes specifically because it short-
  circuits the May-16 cluster — that's the load-bearing piece.
- The inverse side's fill rate on Kalshi is the next unknown. We've established
  the math; the deployment question is whether opposite-side limit orders fill
  when the engine wants to bet against the favorite.

## Regenerating the edge table

The edge table is data-derived; do not hand-edit `edge_table.json`. Re-run
`build_edge_table.py` after each week of live trading to update the bucket WRs
toward truth. The `input_fingerprint` field records the file sizes/mtimes/hash
of each trade log so a stale table is auto-detectable.

Bump `TABLE_VERSION` in `build_edge_table.py` whenever the bucketing logic in
`common.py` changes.
