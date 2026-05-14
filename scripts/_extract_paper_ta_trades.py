"""Extract every paper-TA trade from the 2026-05-12 log into a clean table."""
import json
from datetime import datetime, timezone
from pathlib import Path

LOG = Path(r"C:\Trading\kalshi-btc-engine-v2\data\paper_ta_2026_05_12.jsonl")


def fmt_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%H:%M:%S")


def main() -> None:
    fills, settles = [], []
    for line in LOG.read_text().splitlines():
        rec = json.loads(line)
        if rec.get("kind") == "fill":
            fills.append(rec)
        elif rec.get("kind") == "settle":
            settles.append(rec)

    settle_by_key = {(s["ticker"], s["decided_at_ts_ms"]): s for s in settles}

    rows = []
    for f in fills:
        key = (f["ticker"], f["ts_minute_ms"])
        s = settle_by_key.get(key)
        side = "CALL/YES" if f["decided_side"] == "call" else "PUT/NO"
        entry_px = f["entry_price_cents"]
        fee = f["entry_fee_cents"]
        contracts = f["contracts"]
        ticker_short = f["ticker"].replace("KXBTC15M-", "")
        if s is None:
            exit_cond = "OPEN"
            pnl_per = None
            total = None
        else:
            outcome = s["outcome"]
            won = (outcome == "yes" and f["decided_side"] == "call") or (
                outcome == "no" and f["decided_side"] == "put"
            )
            exit_cond = f"settled {outcome.upper()} ({'WIN' if won else 'LOSS'})"
            pnl_per = s["net_cents"] / contracts if contracts else 0
            total = s["net_cents"]
        rows.append(
            {
                "ts": f["ts_minute_ms"],
                "time": fmt_ts(f["ts_minute_ms"]),
                "ticker": ticker_short,
                "side": side,
                "entry_px": entry_px,
                "fee": fee,
                "tier": f["tier_name"],
                "score": f["score"],
                "exit": exit_cond,
                "pnl_per": pnl_per,
                "contracts": contracts,
                "total": total,
            }
        )

    rows.sort(key=lambda r: r["ts"])

    header = (
        f"{'#':>2}  {'TIME(UTC)':9}  {'TICKER':22}  {'SIDE':9}  "
        f"{'ENTRY':>5}  {'FEE':>3}  {'TIER':6}  {'SCORE':>7}  "
        f"{'EXIT':28}  {'P/L/c':>6}  {'N':>2}  {'TOTAL':>6}"
    )
    print(header)
    print("-" * len(header))

    running = 0
    closed_wins = 0
    closed_losses = 0
    open_n = 0
    for i, r in enumerate(rows, 1):
        pnl_per_s = f"{r['pnl_per']:+.0f}" if r["pnl_per"] is not None else "  -- "
        total_s = f"{r['total']:+d}" if r["total"] is not None else "  -- "
        if r["total"] is not None:
            running += r["total"]
            if r["total"] > 0:
                closed_wins += 1
            else:
                closed_losses += 1
        else:
            open_n += 1
        print(
            f"{i:>2}  {r['time']:9}  {r['ticker']:22}  {r['side']:9}  "
            f"{r['entry_px']:>5}  {r['fee']:>3}  {r['tier']:6}  {r['score']:>7.2f}  "
            f"{r['exit']:28}  {pnl_per_s:>6}  {r['contracts']:>2}  {total_s:>6}"
        )

    print("-" * len(header))
    print(
        f"\nTotals: trades={len(rows)} closed={closed_wins + closed_losses} "
        f"(wins={closed_wins} losses={closed_losses}) open={open_n}"
    )
    print(f"Net P&L across all closed trades: {running:+d} cents (${running/100:+.2f})")


if __name__ == "__main__":
    main()
