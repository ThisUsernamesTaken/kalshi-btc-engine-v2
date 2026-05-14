"""Full review of the live trader's performance.

Walks live_ta_trades.jsonl, cross-references unresolved positions against
the capture DB for lifecycle outcomes, computes:
  - Per-trade P&L (gross, fees, net)
  - Cumulative realized P&L
  - Win rate
  - Tier histogram (STRONG/MEDIUM/WEAK/MIMIC)
  - Side histogram (YES/NO bets)
  - Average hold-to-settle delta
  - Any open positions
  - Halt reasons fired
  - Stale-data skips, rejections, errors
"""
import json
import math
import sqlite3
import sys
from collections import Counter
from pathlib import Path

LOG = Path(r"C:\Trading\kalshi-btc-engine-v2\data\live_ta_trades.jsonl")
DB = r"C:\Trading\kalshi-btc-engine-v2\data\burnin_holdpure_2026_05_12.sqlite"


def fee_cents(price_c: int, n: int) -> int:
    if n <= 0 or price_c <= 0 or price_c >= 100:
        return 0
    p = price_c / 100.0
    return math.ceil(0.07 * n * p * (1 - p) * 100 - 1e-12)


def main() -> int:
    if not LOG.exists():
        print(f"no live trade log at {LOG}")
        return 1

    fills: list[dict] = []
    settles_by_order: dict[str, dict] = {}
    startups: list[dict] = []
    halts: list[dict] = []
    skip_kinds: Counter = Counter()
    other_kinds: Counter = Counter()

    for line in LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        k = r.get("kind")
        if k == "fill" and not r.get("dry_run"):
            fills.append(r)
        elif k == "settle":
            settles_by_order[r["order_id"]] = r
        elif k == "startup":
            startups.append(r)
        elif k in ("decision_halt", "decision_low_balance_halt"):
            halts.append(r)
        elif k in (
            "decision_skip_cycle_dup", "decision_stale_data_skip",
            "decision_balance_error", "decision_no_market",
            "order_rejected", "order_error", "order_no_fill",
        ):
            skip_kinds[k] += 1
        else:
            other_kinds[k] += 1

    print(f"=== Live trader review — log: {LOG.name} ===\n")
    print(f"Startups: {len(startups)}")
    for i, s in enumerate(startups, 1):
        print(f"  #{i} ts={s['ts_ms']} dry_run={s.get('dry_run')} "
              f"contracts={s.get('contracts_per_trade')} "
              f"loss_cap=${(s.get('daily_loss_cap_cents') or 0)/100:.2f} "
              f"min_bal=${(s.get('min_balance_cents') or 0)/100:.2f} "
              f"halt_at_start={s.get('halt_reason_at_start')}")
    print()
    print(f"Real fills logged: {len(fills)}")
    print(f"Settles logged:    {len(settles_by_order)}")
    print(f"Halts:             {len(halts)}")
    print(f"Skips: {dict(skip_kinds)}")
    if other_kinds:
        print(f"Other kinds: {dict(other_kinds)}")
    print()

    # Cross-reference unresolved fills against capture DB
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

    print(f"{'#':>3}  {'time(UTC)':>16}  {'ticker':<28}  {'side':<4}  "
          f"{'ct':>3}  {'@c':>3}  {'fee':>3}  {'tier':<7}  {'score':>7}  "
          f"{'outcome':<7}  {'gross':>6}  {'net':>6}")
    print("-" * 130)

    total_gross = 0
    total_fees = 0
    total_net = 0
    wins = 0
    losses = 0
    unresolved = 0
    tier_counter = Counter()
    side_counter = Counter()
    open_positions: list[dict] = []

    for i, f in enumerate(fills, 1):
        ts = f.get("ts_minute_ms") or f.get("ts_ms") or 0
        import datetime as dt
        ts_iso = dt.datetime.fromtimestamp(ts/1000, tz=dt.UTC).strftime("%m-%d %H:%M:%S")
        ticker = f.get("ticker", "?")
        side = f.get("side") or ("yes" if f.get("decided_side") == "call" else "no")
        n = int(f.get("contracts", 0))
        cost = int(f.get("entry_price_cents", 0))
        fee = int(f.get("entry_fee_cents", 0))
        tier = f.get("tier_name", "?")
        score = f.get("score", 0) or 0
        tier_counter[tier] += 1
        side_counter[side] += 1

        s = settles_by_order.get(f.get("order_id"))
        outcome = None
        gross = None
        net = None

        if s:
            outcome = s.get("outcome")
            gross = int(s.get("gross_cents", 0))
            net = int(s.get("net_cents", 0))
        else:
            # Look up settlement directly from capture lifecycle
            row = conn.execute(
                """
                SELECT raw_json FROM kalshi_lifecycle_event
                WHERE market_ticker = ? AND status = 'determined'
                ORDER BY event_id DESC LIMIT 1
                """,
                (ticker,),
            ).fetchone()
            if row and row[0]:
                try:
                    msg = json.loads(row[0]).get("msg", {})
                    outcome = msg.get("result")
                except Exception:
                    pass
            if outcome:
                gross = n * (100 - cost) if side == outcome else -n * cost
                net = gross - fee

        if outcome is None:
            unresolved += 1
            open_positions.append({
                "ticker": ticker, "side": side, "n": n, "cost": cost, "fee": fee,
            })
            print(f"{i:>3}  {ts_iso:>16}  {ticker:<28}  {side:<4}  {n:>3}  {cost:>3}  "
                  f"{fee:>3}  {tier:<7}  {score:>7.2f}  {'OPEN':<7}  {'?':>6}  {'?':>6}")
            continue

        total_gross += gross
        total_fees += fee
        total_net += net
        if net > 0:
            wins += 1
        else:
            losses += 1

        print(f"{i:>3}  {ts_iso:>16}  {ticker:<28}  {side:<4}  {n:>3}  {cost:>3}  "
              f"{fee:>3}  {tier:<7}  {score:>7.2f}  {outcome:<7}  {gross:+6d}  {net:+6d}")

    conn.close()
    print("-" * 130)
    print(f"Trades:  total={len(fills)}  resolved={len(fills)-unresolved}  open={unresolved}")
    print(f"W/L:     {wins}/{losses}  (hit-rate={100*wins/(wins+losses):.1f}% over resolved)" if (wins+losses) > 0 else "W/L: 0/0")
    print(f"Gross P&L:  {total_gross:+d}c  (${total_gross/100:+.2f})")
    print(f"Fees:       {total_fees}c  (${total_fees/100:.2f})")
    print(f"Net P&L:    {total_net:+d}c  (${total_net/100:+.2f})")
    print()
    print(f"Side mix: {dict(side_counter)}")
    print(f"Tier mix: {dict(tier_counter)}")
    print()

    if open_positions:
        print("OPEN POSITIONS:")
        for p in open_positions:
            risk = p["n"] * p["cost"]
            max_gain = p["n"] * (100 - p["cost"])
            print(f"  {p['ticker']} {p['side']} {p['n']}@{p['cost']}c  risk=-${risk/100:.2f} max_gain=+${max_gain/100:.2f}")

    if halts:
        print()
        print("HALT EVENTS:")
        for h in halts[-5:]:
            print(f"  ts={h.get('ts_ms')} kind={h.get('kind')} reason={h.get('halt_reason')}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
