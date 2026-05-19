# Strategy discovery analysis (2026-05-18)

## Headline finding (after walk-forward validation)

**The bucket-conditional edge does NOT generalize out-of-sample.**

The 648-trade in-sample backtest projected +$2,293 with the recommended sizing.
Walk-forward (training on prior folds, applying to the next) produces **−$858
across 215 OOS trades**, with a running minimum of −$1,048. Every parameter
combination tested — including conservative tiers, larger min-bucket-n
thresholds, skip-only (no inverts), and live-engines-only — is OOS-negative.

The in-sample alpha was curve-fitting noise in small-N bucket WRs. **Do not
deploy this edge table as a live trading strategy.**

## What still holds (structural findings)

These are statements about the engines and the data, not about a trading rule,
and they don't depend on bucket WR estimates:

1. **Every live engine is net-negative under realistic execution.** Reported
   −$94 over 134 live settles → realistic −$215 with +2c slippage per entry.
   Across 648 trades (live + paper + shadow) the realistic baseline is −$527.
2. **T-30 sniper has a 2.9% fill rate** while EARLIER_MODERATE (the loss-
   driving leg) fills 45%. Execution adverse selection is real.
3. **The engines hold to settle** — they don't sell early. The losses are
   entry problems, not exit problems.
4. **The 88-91c mid-conviction entry stripe has below-breakeven WR in-sample**
   on both v5 and Pine. Whether that's a persistent edge or noise: this
   analysis cannot say. With 13 trades in the bucket it's well within
   binomial-noise distance of breakeven.

## What does NOT hold

- The +$220 / +$2,293 / +$398 projections from `03_sizing_simulation.py`.
  All were in-sample. Walk-forward kills them.
- The "INVERT" recommendation for any specific bucket. The decision is
  unstable: which side has the edge flips depending on which weeks of data
  are used to compute it.
- The "100% WR" buckets. These are n=3-10 samples each; binomial confidence
  intervals are wide enough that 92% is well within the 95% CI, which at
  avg entry 90c is a coin-flip on profitability.

## Walk-forward parameter sweep results

All OOS-negative:

| Config | OOS n | OOS WR | OOS net | Running min |
|---|---|---|---|---|
| default 12/30/60/100, 2d folds, n≥3, e≥5 | 215 | 54.4% | **−$858** | −$976 |
| default 12/30/60/100, 1d folds, n≥3, e≥5 | 207 | 51.7% | −$756 | −$1,048 |
| conservative 5/12/25/40, 1d, n≥10, e≥8 | 63 | 41.3% | −$97 | −$97 |
| strict 3/8/15/25, 1d, n≥15, e≥10 | 13 | 30.8% | −$44 | −$44 |
| ultra-strict 2/5/10/15, 1d, n≥20, e≥12 | 7 | 14.3% | −$19 | −$19 |
| skip-only 5/12/25/40, 1d, n≥5 (no inverts) | 107 | 63.6% | −$348 | −$352 |
| skip-only 3/8/15/25, 1d, n≥10 | 22 | 50.0% | −$24 | −$24 |

Tightening parameters reduces the magnitude of loss (by trading less) but
doesn't make it positive. There is no parameter region that survives OOS.

## Bucket stability

What actually IS stable across folds: which buckets the model picks (their
action: KEEP vs INVERT vs SKIP rarely flips). What is NOT stable: the
*magnitude* of the edge per bucket. Example: `pine/late/underdog/mom_lo`
shifts from edge +0.2c (essentially flat) in fold 1 to +20.6c in fold 2.
The strategy fires on the right buckets — and bets the wrong sizes.

## Files

| File | Purpose |
|---|---|
| `common.py` | Schema-tolerant trade-log loader + bucketing + edge math. Now loads 7 sources (648 settles). |
| `01_baseline_audit.py` | Per-engine reported vs realistic P&L; fill-rate skew. |
| `02_bucket_edge_table.py` | In-sample bucket-conditional edge table. **Diagnostic only.** |
| `03_sizing_simulation.py` | In-sample sizing-rule Pareto frontier. **Numbers are not OOS-valid.** |
| `04_walk_forward.py` | Walk-forward validation. The honest answer. |
| `build_edge_table.py` | Extracts `edge_table.json` from current trade logs. |
| `edge_table.json` | Latest in-sample table; tagged `oos_validation_status: FAILED`. |

## How to run

```powershell
$env:PYTHONPATH = "src"
$py = "C:\Users\coleb\AppData\Local\Python\bin\python.exe"

& $py -m analysis.01_baseline_audit
& $py -m analysis.02_bucket_edge_table
& $py -m analysis.03_sizing_simulation
& $py -m analysis.04_walk_forward           # the load-bearing check
& $py -m analysis.build_edge_table
```

## Where to go from here

The methodology is sound — bucket-conditional edge with realistic execution
and dollar-at-risk sizing is a reasonable framework. What it needs:

1. **Much more data.** 648 settles spread across 5 days isn't enough for
   stable bucket WRs. Even within the existing data, only one walk-forward
   fold (fold 2 with 413 trades) actually exercises the OOS gates; the
   first fold is the training set and the last has too few trades.
2. **Stronger inductive bias.** Replace empirical bucket WRs with a model
   that predicts edge from features (e.g., Brownian Bridge fair-value
   estimate in the gradient engine). Then the rule generalizes from
   structure rather than memorizing.
3. **Out-of-sample test BEFORE deployment.** Build an edge table from week
   N, deploy it as a paper overlay for week N+1, and only promote rules
   whose paper performance matches in-sample. The walk-forward script here
   gives the template.
4. **Drop the inverse layer** unless a specific bucket survives OOS for
   multiple folds with statistically significant negative engine-edge. The
   inverse logic doubles the risk of overfitting because every noisy
   negative-edge bucket becomes a "positive inverse-edge" opportunity.

## Bottom line for deployment

**Do not deploy any sizing/edge rule from this analysis without first
demonstrating OOS positivity on data the rule has never seen.** The work in
this folder is useful as a diagnostic of *how the engines have been losing*
and *which directions are worth structural investigation* — not as a
trading rule.

The honest path forward is more data + a structural model (Brownian Bridge
in the gradient engine, or T-30 sniper after the fill-rate problem is
solved), not curve-fitting bucket WRs at this sample size.
