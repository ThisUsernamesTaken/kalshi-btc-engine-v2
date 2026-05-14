"""Latency-budget diagnostic for a captured burn-in SQLite.

Answers the question: is the engine's feature-to-decision latency wider
than the half-life of the features it is claiming as edge?

This is the diagnostic the May 2026 deep-research report flagged as
missing. If queue imbalance / OFI / trade-burst signals decay on a
100ms-2s horizon and the engine's path from exchange event to decision
is wider, the residual edge is illusory regardless of how well the math
is set up.

What we can measure from the captured DB:

1. Network latency: ``received_ts_ms - exchange_ts_ms`` for L2 deltas
   and trades. This is exchange-to-our-host wire time. If the median
   is materially below the 100ms half-life floor, microstructure
   features can in principle carry information.

2. L2 event inter-arrival per market: how often the book updates. If
   the median delta-to-delta interval per market is wider than typical
   feature half-lives, no amount of fast decisioning helps because the
   raw signal isn't refreshed.

3. Decision-cadence ceiling: the configured ``decision_interval_ms``
   (default 1000ms) bounds how stale features are when fed to the
   decision engine. The script reports this for comparison.

What we cannot measure from the captured DB alone:

- Decision-build wall time (event → snapshot → decision). Requires
  instrumentation in the live engine. Out of scope for this script;
  flagged in the registry.

Output: a JSON summary plus a short text histogram. Designed to be
piped to a file or scrutinised at a glance.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from pathlib import Path


def _percentiles(values: list[float], qs: tuple[float, ...]) -> dict[str, float]:
    if not values:
        return {f"p{int(q*100)}": float("nan") for q in qs}
    s = sorted(values)
    out: dict[str, float] = {}
    for q in qs:
        idx = int(q * (len(s) - 1))
        out[f"p{int(q*100)}"] = float(s[idx])
    return out


def _ascii_histogram(values: list[float], bins: list[float], label: str) -> str:
    if not values:
        return f"{label}: (no data)\n"
    counts = [0] * len(bins)
    overflow = 0
    for v in values:
        placed = False
        for i, ceil in enumerate(bins):
            if v < ceil:
                counts[i] += 1
                placed = True
                break
        if not placed:
            overflow += 1
    total = len(values)
    out = [f"{label}  (n={total})"]
    prev = 0.0
    max_count = max(counts + [overflow]) or 1
    bar_w = 40
    for ceil, count in zip(bins, counts, strict=True):
        pct = 100.0 * count / total
        bar = "#" * int(bar_w * count / max_count)
        out.append(f"  [{prev:>7.1f},{ceil:>7.1f}) {count:>7}  {pct:5.1f}%  {bar}")
        prev = ceil
    if overflow:
        pct = 100.0 * overflow / total
        bar = "#" * int(bar_w * overflow / max_count)
        out.append(f"  [{prev:>7.1f},+inf  ) {overflow:>7}  {pct:5.1f}%  {bar}")
    return "\n".join(out) + "\n"


def network_latency_ms(conn: sqlite3.Connection, table: str) -> list[float]:
    rows = conn.execute(f"""
        SELECT received_ts_ms - exchange_ts_ms
        FROM {table}
        WHERE exchange_ts_ms IS NOT NULL
          AND received_ts_ms IS NOT NULL
        """).fetchall()
    return [float(r[0]) for r in rows if r[0] is not None and r[0] >= 0]


def l2_interarrival_ms_by_market(
    conn: sqlite3.Connection,
    limit_per_market: int = 5000,
) -> dict[str, list[float]]:
    markets = [
        str(r[0])
        for r in conn.execute(
            "SELECT DISTINCT market_ticker FROM kalshi_l2_event ORDER BY market_ticker"
        ).fetchall()
    ]
    out: dict[str, list[float]] = {}
    for ticker in markets:
        rows = conn.execute(
            """
            SELECT COALESCE(exchange_ts_ms, received_ts_ms)
            FROM kalshi_l2_event
            WHERE market_ticker = ?
            ORDER BY event_id
            LIMIT ?
            """,
            (ticker, limit_per_market),
        ).fetchall()
        ts = [int(r[0]) for r in rows if r[0] is not None]
        if len(ts) < 2:
            continue
        deltas = [float(ts[i] - ts[i - 1]) for i in range(1, len(ts)) if ts[i] >= ts[i - 1]]
        if deltas:
            out[ticker] = deltas
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="latency-budget",
        description="Measure network and L2 inter-arrival latency from a captured "
        "burn-in SQLite. Used to evaluate whether microstructure-residual edges "
        "are achievable given current data-plane latency.",
    )
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument(
        "--decision-interval-ms",
        type=int,
        default=1000,
        help="Engine decision-cadence ceiling (default 1000ms). Reported for "
        "comparison against measured feature half-lives.",
    )
    parser.add_argument(
        "--feature-half-life-ms",
        type=int,
        default=500,
        help="Assumed half-life of microstructure features (default 500ms). "
        "If decision-cadence or interarrival exceeds 2x this, the residual "
        "edge is unlikely to be tradeable.",
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="Emit only JSON (skip the text histogram).",
    )
    args = parser.parse_args()

    if not args.db.exists():
        print(json.dumps({"error": f"db not found: {args.db}"}))
        return 1

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        l2_net = network_latency_ms(conn, "kalshi_l2_event")
        trade_net = network_latency_ms(conn, "kalshi_trade_event")
        interarrival_by_market = l2_interarrival_ms_by_market(conn)
    finally:
        conn.close()

    bins = [10.0, 25.0, 50.0, 100.0, 200.0, 500.0, 1000.0, 2000.0, 5000.0]

    summary = {
        "decision_interval_ms": args.decision_interval_ms,
        "assumed_feature_half_life_ms": args.feature_half_life_ms,
        "kalshi_l2_network_latency": {
            "n": len(l2_net),
            "mean_ms": statistics.mean(l2_net) if l2_net else None,
            "median_ms": statistics.median(l2_net) if l2_net else None,
            **_percentiles(l2_net, (0.50, 0.90, 0.95, 0.99)),
        },
        "kalshi_trade_network_latency": {
            "n": len(trade_net),
            "mean_ms": statistics.mean(trade_net) if trade_net else None,
            "median_ms": statistics.median(trade_net) if trade_net else None,
            **_percentiles(trade_net, (0.50, 0.90, 0.95, 0.99)),
        },
        "l2_interarrival_per_market": {
            ticker: {
                "n": len(deltas),
                "median_ms": statistics.median(deltas),
                **_percentiles(deltas, (0.50, 0.90, 0.95, 0.99)),
            }
            for ticker, deltas in interarrival_by_market.items()
        },
    }

    # The effective minimum staleness of a feature when it reaches the
    # decision engine is approximately:
    #   network_p50            (time from exchange to our host)
    # + decision_interval / 2  (avg wait until the next decision tick)
    # This is a lower bound; queueing, multi-event coalescing, and decision-
    # build wall time push it higher.
    l2_net_p50 = summary["kalshi_l2_network_latency"]["p50"] or 0.0
    interarrival_p50s = [v["median_ms"] for v in summary["l2_interarrival_per_market"].values()]
    summary["median_market_interarrival_ms"] = (
        statistics.median(interarrival_p50s) if interarrival_p50s else None
    )
    floor = float(l2_net_p50) + float(args.decision_interval_ms) / 2.0
    summary["effective_staleness_floor_ms"] = floor
    summary["staleness_vs_half_life_ratio"] = (
        floor / args.feature_half_life_ms if args.feature_half_life_ms else None
    )
    summary["verdict"] = (
        "feasible"
        if floor < args.feature_half_life_ms
        else "marginal" if floor < 2 * args.feature_half_life_ms else "infeasible"
    )

    print(json.dumps(summary, indent=2, default=str))

    if not args.json_only:
        print()
        print(_ascii_histogram(l2_net, bins, "L2 network latency ms (received - exchange)"))
        print(_ascii_histogram(trade_net, bins, "Trade network latency ms"))
        # Pick representative market for interarrival histogram
        if interarrival_by_market:
            ticker = max(interarrival_by_market, key=lambda t: len(interarrival_by_market[t]))
            print(
                _ascii_histogram(
                    interarrival_by_market[ticker],
                    [50.0, 100.0, 200.0, 500.0, 1000.0, 2000.0, 5000.0, 10000.0],
                    f"L2 interarrival ms for {ticker} (largest sample)",
                )
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
