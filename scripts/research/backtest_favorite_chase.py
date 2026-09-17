"""Backtest the favorite-chase rule on captured KXBTC15M data.

Reads one or more burn-in SQLite files (default: holdpure capture). For each
KXBTC15M market it:
  1. Resolves session_open_ms from market_dim.open_time (ISO).
  2. Finds the first trade at >= open + 8min with yes_price>=0.75 OR no_price>=0.75.
  3. Walks forward through kalshi_l2_event to find the next valid ask on the
     entered side (the simulated fill).
  4. Continues walking l2 events until either:
       - mid <= 0.50  -> stop exit at bid (taker fees on both legs)
       - close_time   -> settle from kalshi_lifecycle_event (status='determined')
  5. Reports per-trade P&L (cents, after taker fees on opens / stop exits) and
     aggregate stats.

Read-only — does not write to the source DB.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from kalshi_btc_engine_v2.policy.edge import kalshi_taker_fee_cents  # noqa: E402
from kalshi_btc_engine_v2.strategies.favorite_chase import (  # noqa: E402
    ENTRY_AFTER_MS,
    ENTRY_TRIGGER_PRICE,
    STOP_PRICE,
    BookTick,
    Side,
    TradeTick,
    ask_for_side,
    bid_for_side,
    detect_entry,
    detect_stop,
    mid_for_side,
)

DEFAULT_DBS = [
    r"D:\Trading\kalshi-btc-engine-v2\data\burnin_holdpure_2026_05_12.sqlite",
]

SERIES_FILTER = "KXBTC15M-%"


def parse_iso_to_ms(s: str | None) -> int | None:
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    return int(dt.datetime.fromisoformat(s).timestamp() * 1000)


def fmt_ts(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.UTC).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class TradeOutcome:
    market_ticker: str
    series_ticker: str
    session_open_ms: int
    session_close_ms: int
    side: Side
    entry_trigger_ts_ms: int
    entry_fill_ts_ms: int
    entry_price: float
    exit_ts_ms: int
    exit_price: float
    exit_reason: str  # 'stop' | 'settle_win' | 'settle_loss' | 'no_fill' | 'no_settle'
    gross_cents: int = 0  # (exit_price - entry_price) * 100, rounded
    entry_fee_cents: int = 0
    exit_fee_cents: int = 0
    net_cents: int = 0


@dataclass
class Aggregate:
    trades: list[TradeOutcome] = field(default_factory=list)
    sessions_seen: int = 0
    sessions_no_entry: int = 0
    sessions_no_fill: int = 0
    sessions_no_settle: int = 0

    @property
    def n(self) -> int:
        return len([t for t in self.trades if t.exit_reason not in ("no_fill", "no_settle")])

    @property
    def wins(self) -> int:
        return len([t for t in self.trades if t.net_cents > 0])

    @property
    def losses(self) -> int:
        return len([t for t in self.trades if t.net_cents < 0])

    @property
    def net_cents(self) -> int:
        return sum(t.net_cents for t in self.trades)

    @property
    def gross_cents(self) -> int:
        return sum(t.gross_cents for t in self.trades)

    @property
    def fees_cents(self) -> int:
        return sum(t.entry_fee_cents + t.exit_fee_cents for t in self.trades)


def lookup_settlement(con: sqlite3.Connection, ticker: str) -> str | None:
    """Return 'yes' or 'no' if the market is determined, else None.

    Tries the lifecycle event's `result` field first; falls back to raw_json.
    """
    row = con.execute(
        """
        SELECT raw_json FROM kalshi_lifecycle_event
        WHERE market_ticker = ? AND status = 'determined'
        ORDER BY event_id DESC LIMIT 1
        """,
        (ticker,),
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        j = json.loads(row[0])
        msg = j.get("msg", j)
        result = msg.get("result")
        if isinstance(result, str) and result.lower() in ("yes", "no"):
            return result.lower()
    except Exception:
        return None
    return None


def coerce_price(x: object) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v > 1.5:  # stored as cents
        v = v / 100.0
    return v


def iter_l2_after(
    con: sqlite3.Connection, ticker: str, after_ts_ms: int, until_ts_ms: int
) -> Iterable[BookTick]:
    """Yield BookTicks with exchange-ts > after_ts_ms and <= until_ts_ms+grace."""
    cur = con.execute(
        """
        SELECT COALESCE(exchange_ts_ms, received_ts_ms) AS ts_ms,
               best_yes_bid, best_yes_ask
        FROM kalshi_l2_event
        WHERE market_ticker = ?
          AND COALESCE(exchange_ts_ms, received_ts_ms) > ?
          AND COALESCE(exchange_ts_ms, received_ts_ms) <= ?
        ORDER BY COALESCE(exchange_ts_ms, received_ts_ms), event_id
        """,
        (ticker, after_ts_ms, until_ts_ms),
    )
    for row in cur:
        yield BookTick(
            ts_ms=int(row[0]),
            yes_bid=coerce_price(row[1]),
            yes_ask=coerce_price(row[2]),
        )


def find_entry(
    con: sqlite3.Connection, ticker: str, session_open_ms: int, session_close_ms: int
) -> tuple[TradeTick, Side] | None:
    """Find the first qualifying trade at >= open+8min that triggers a side.

    Push the >=75c filter into SQL so we only fetch one row per market.
    """
    trigger_after = session_open_ms + ENTRY_AFTER_MS
    row = con.execute(
        """
        SELECT COALESCE(exchange_ts_ms, received_ts_ms) AS ts_ms,
               yes_price, no_price
        FROM kalshi_trade_event
        WHERE market_ticker = ?
          AND COALESCE(exchange_ts_ms, received_ts_ms) >= ?
          AND COALESCE(exchange_ts_ms, received_ts_ms) <= ?
          AND (CAST(yes_price AS REAL) >= 0.75 OR CAST(no_price AS REAL) >= 0.75)
        ORDER BY COALESCE(exchange_ts_ms, received_ts_ms), event_id
        LIMIT 1
        """,
        (ticker, trigger_after, session_close_ms),
    ).fetchone()
    if row is None:
        return None
    yp = coerce_price(row[1])
    np_ = coerce_price(row[2])
    if yp is None and np_ is not None:
        yp = 1.0 - np_
    if np_ is None and yp is not None:
        np_ = 1.0 - yp
    trade = TradeTick(ts_ms=int(row[0]), yes_price=yp or 0.0, no_price=np_ or 0.0)
    side = detect_entry(trade, session_open_ms)
    if side is None:
        return None
    return trade, side


def find_first_fill(
    con: sqlite3.Connection, ticker: str, after_ts_ms: int, until_ts_ms: int, side: Side
) -> tuple[float, int] | None:
    """First L2 event after the trigger where the entered side has a valid ask.

    YES: best_yes_ask must be present.
    NO:  best_yes_bid must be present (NO ask = 1 - yes_bid).
    """
    if side is Side.YES:
        row = con.execute(
            """
            SELECT COALESCE(exchange_ts_ms, received_ts_ms) AS ts_ms,
                   best_yes_ask
            FROM kalshi_l2_event
            WHERE market_ticker = ?
              AND COALESCE(exchange_ts_ms, received_ts_ms) > ?
              AND COALESCE(exchange_ts_ms, received_ts_ms) <= ?
              AND best_yes_ask IS NOT NULL
            ORDER BY COALESCE(exchange_ts_ms, received_ts_ms), event_id
            LIMIT 1
            """,
            (ticker, after_ts_ms, until_ts_ms),
        ).fetchone()
        if not row:
            return None
        ask = coerce_price(row[1])
        if ask is None:
            return None
        return ask, int(row[0])
    # NO
    row = con.execute(
        """
        SELECT COALESCE(exchange_ts_ms, received_ts_ms) AS ts_ms,
               best_yes_bid
        FROM kalshi_l2_event
        WHERE market_ticker = ?
          AND COALESCE(exchange_ts_ms, received_ts_ms) > ?
          AND COALESCE(exchange_ts_ms, received_ts_ms) <= ?
          AND best_yes_bid IS NOT NULL
        ORDER BY COALESCE(exchange_ts_ms, received_ts_ms), event_id
        LIMIT 1
        """,
        (ticker, after_ts_ms, until_ts_ms),
    ).fetchone()
    if not row:
        return None
    yes_bid = coerce_price(row[1])
    if yes_bid is None:
        return None
    return 1.0 - yes_bid, int(row[0])


def find_stop(
    con: sqlite3.Connection, ticker: str, after_ts_ms: int, until_ts_ms: int, side: Side
) -> tuple[float, int] | None:
    """First L2 event after fill where the held side's mid <= 0.50.

    YES mid <= 0.5  ⇔ yes_bid + yes_ask <= 1.0
    NO mid <= 0.5   ⇔ yes_bid + yes_ask >= 1.0

    Returns (exit_bid_price, ts_ms) or None if no stop fires before close.
    """
    if side is Side.YES:
        row = con.execute(
            """
            SELECT COALESCE(exchange_ts_ms, received_ts_ms) AS ts_ms,
                   best_yes_bid, best_yes_ask
            FROM kalshi_l2_event
            WHERE market_ticker = ?
              AND COALESCE(exchange_ts_ms, received_ts_ms) > ?
              AND COALESCE(exchange_ts_ms, received_ts_ms) <= ?
              AND best_yes_bid IS NOT NULL AND best_yes_ask IS NOT NULL
              AND (CAST(best_yes_bid AS REAL) + CAST(best_yes_ask AS REAL)) <= 1.0
            ORDER BY COALESCE(exchange_ts_ms, received_ts_ms), event_id
            LIMIT 1
            """,
            (ticker, after_ts_ms, until_ts_ms),
        ).fetchone()
        if not row:
            return None
        yes_bid = coerce_price(row[1])
        if yes_bid is None:
            return None
        return yes_bid, int(row[0])
    # NO held
    row = con.execute(
        """
        SELECT COALESCE(exchange_ts_ms, received_ts_ms) AS ts_ms,
               best_yes_bid, best_yes_ask
        FROM kalshi_l2_event
        WHERE market_ticker = ?
          AND COALESCE(exchange_ts_ms, received_ts_ms) > ?
          AND COALESCE(exchange_ts_ms, received_ts_ms) <= ?
          AND best_yes_bid IS NOT NULL AND best_yes_ask IS NOT NULL
          AND (CAST(best_yes_bid AS REAL) + CAST(best_yes_ask AS REAL)) >= 1.0
        ORDER BY COALESCE(exchange_ts_ms, received_ts_ms), event_id
        LIMIT 1
        """,
        (ticker, after_ts_ms, until_ts_ms),
    ).fetchone()
    if not row:
        return None
    yes_ask = coerce_price(row[2])
    if yes_ask is None:
        return None
    return 1.0 - yes_ask, int(row[0])


def simulate_market(
    con: sqlite3.Connection,
    ticker: str,
    series_ticker: str,
    session_open_ms: int,
    session_close_ms: int,
) -> TradeOutcome | None:
    found = find_entry(con, ticker, session_open_ms, session_close_ms)
    if not found:
        return None
    trade, side = found

    grace_ms = 60_000
    until_ms = session_close_ms + grace_ms

    fill = find_first_fill(con, ticker, trade.ts_ms, until_ms, side)
    fill_price: float | None = None
    fill_ts_ms: int | None = None
    exit_reason: str | None = None
    exit_price: float | None = None
    exit_ts_ms: int | None = None

    if fill is not None:
        fill_price, fill_ts_ms = fill
        # Look for stop strictly after the fill, up to close_time.
        stop = find_stop(con, ticker, fill_ts_ms, session_close_ms, side)
        if stop is not None:
            exit_price, exit_ts_ms = stop
            exit_reason = "stop"

    if fill_price is None:
        return TradeOutcome(
            market_ticker=ticker,
            series_ticker=series_ticker,
            session_open_ms=session_open_ms,
            session_close_ms=session_close_ms,
            side=side,
            entry_trigger_ts_ms=trade.ts_ms,
            entry_fill_ts_ms=trade.ts_ms,
            entry_price=0.0,
            exit_ts_ms=trade.ts_ms,
            exit_price=0.0,
            exit_reason="no_fill",
        )

    if exit_reason is None:
        # No stop hit; settle.
        settled = lookup_settlement(con, ticker)
        if settled is None:
            return TradeOutcome(
                market_ticker=ticker,
                series_ticker=series_ticker,
                session_open_ms=session_open_ms,
                session_close_ms=session_close_ms,
                side=side,
                entry_trigger_ts_ms=trade.ts_ms,
                entry_fill_ts_ms=int(fill_ts_ms or trade.ts_ms),
                entry_price=fill_price,
                exit_ts_ms=session_close_ms,
                exit_price=0.0,
                exit_reason="no_settle",
            )
        won = settled == side.value
        exit_reason = "settle_win" if won else "settle_loss"
        exit_price = 1.0 if won else 0.0
        exit_ts_ms = session_close_ms

    # P&L. Fees on the taker entry; fees on the taker stop exit; no fee on
    # passive settlement.
    entry_cents = int(round(fill_price * 100))
    exit_cents = int(round((exit_price or 0.0) * 100))
    gross = exit_cents - entry_cents
    entry_fee = kalshi_taker_fee_cents(entry_cents, count=1)
    exit_fee = kalshi_taker_fee_cents(exit_cents, count=1) if exit_reason == "stop" else 0
    net = gross - entry_fee - exit_fee
    return TradeOutcome(
        market_ticker=ticker,
        series_ticker=series_ticker,
        session_open_ms=session_open_ms,
        session_close_ms=session_close_ms,
        side=side,
        entry_trigger_ts_ms=trade.ts_ms,
        entry_fill_ts_ms=int(fill_ts_ms or trade.ts_ms),
        entry_price=fill_price,
        exit_ts_ms=int(exit_ts_ms or session_close_ms),
        exit_price=float(exit_price or 0.0),
        exit_reason=exit_reason,
        gross_cents=gross,
        entry_fee_cents=entry_fee,
        exit_fee_cents=exit_fee,
        net_cents=net,
    )


def list_markets(con: sqlite3.Connection, ticker_like: str) -> list[tuple[str, str, int, int]]:
    rows = con.execute(
        """
        SELECT ticker, series_ticker, open_time, close_time
        FROM market_dim
        WHERE ticker LIKE ?
          AND open_time IS NOT NULL
          AND close_time IS NOT NULL
        """,
        (ticker_like,),
    ).fetchall()
    out: list[tuple[str, str, int, int]] = []
    for tk, series, ot, ct in rows:
        om = parse_iso_to_ms(ot)
        cm = parse_iso_to_ms(ct)
        if om is None or cm is None:
            continue
        out.append((str(tk), str(series), om, cm))
    out.sort(key=lambda x: x[2])
    return out


def run(dbs: list[Path], ticker_like: str, max_markets: int | None, jsonl: Path | None) -> Aggregate:
    agg = Aggregate()
    log_fp = jsonl.open("w", encoding="utf-8") if jsonl else None

    for db_path in dbs:
        if not db_path.exists():
            print(f"[skip] DB not found: {db_path}", file=sys.stderr)
            continue
        # immutable=1 lets sqlite skip WAL checks; safe for past-session data
        # even when a live capture is appending to the file.
        con = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
        try:
            markets = list_markets(con, ticker_like)
            print(f"[{db_path.name}] {len(markets)} markets matching '{ticker_like}'", file=sys.stderr)
            if max_markets:
                markets = markets[:max_markets]
            for i, (ticker, series, om, cm) in enumerate(markets, 1):
                agg.sessions_seen += 1
                if i % 25 == 0 or i == len(markets):
                    print(
                        f"  ... {i}/{len(markets)} (trades={agg.n} no_entry={agg.sessions_no_entry} net={agg.net_cents:+d}c)",
                        file=sys.stderr,
                        flush=True,
                    )
                outcome = simulate_market(con, ticker, series, om, cm)
                if outcome is None:
                    agg.sessions_no_entry += 1
                    continue
                if outcome.exit_reason == "no_fill":
                    agg.sessions_no_fill += 1
                if outcome.exit_reason == "no_settle":
                    agg.sessions_no_settle += 1
                agg.trades.append(outcome)
                if log_fp:
                    log_fp.write(
                        json.dumps(  # noqa

                            {
                                "ticker": outcome.market_ticker,
                                "series": outcome.series_ticker,
                                "session_open": fmt_ts(outcome.session_open_ms),
                                "session_close": fmt_ts(outcome.session_close_ms),
                                "side": outcome.side.value,
                                "entry_trigger_ts": fmt_ts(outcome.entry_trigger_ts_ms),
                                "entry_fill_ts": fmt_ts(outcome.entry_fill_ts_ms),
                                "entry_price": outcome.entry_price,
                                "exit_ts": fmt_ts(outcome.exit_ts_ms),
                                "exit_price": outcome.exit_price,
                                "exit_reason": outcome.exit_reason,
                                "gross_cents": outcome.gross_cents,
                                "entry_fee_cents": outcome.entry_fee_cents,
                                "exit_fee_cents": outcome.exit_fee_cents,
                                "net_cents": outcome.net_cents,
                            }
                        )
                        + "\n"
                    )
                    log_fp.flush()
        finally:
            con.close()
    if log_fp:
        log_fp.close()
    return agg


def summarize(agg: Aggregate) -> None:
    print()
    print(f"sessions_seen:     {agg.sessions_seen}")
    print(f"sessions_no_entry: {agg.sessions_no_entry}")
    print(f"sessions_no_fill:  {agg.sessions_no_fill}")
    print(f"sessions_no_settle:{agg.sessions_no_settle}")
    trades = [t for t in agg.trades if t.exit_reason not in ("no_fill", "no_settle")]
    print(f"completed_trades:  {len(trades)}")
    if not trades:
        return
    wins = [t for t in trades if t.net_cents > 0]
    losses = [t for t in trades if t.net_cents < 0]
    flats = [t for t in trades if t.net_cents == 0]
    by_reason: dict[str, int] = {}
    for t in trades:
        by_reason[t.exit_reason] = by_reason.get(t.exit_reason, 0) + 1
    print()
    print(f"  wins={len(wins)} losses={len(losses)} flats={len(flats)}")
    print(f"  win_rate={len(wins)/len(trades):.1%}")
    print(f"  net_cents={agg.net_cents:+d}  (=${agg.net_cents/100:+.2f})")
    print(f"  gross_cents={agg.gross_cents:+d}  fees_cents={agg.fees_cents:+d}")
    print(f"  avg_net_per_trade={agg.net_cents/len(trades):+.2f}c")
    print()
    print("  exit-reason breakdown:")
    for k, v in sorted(by_reason.items(), key=lambda x: -x[1]):
        bucket = [t for t in trades if t.exit_reason == k]
        net = sum(t.net_cents for t in bucket)
        print(f"    {k:14s} n={v:4d}  net={net:+d}c  avg={net/v:+.2f}c")
    print()
    print("  side breakdown:")
    for s in ("yes", "no"):
        bucket = [t for t in trades if t.side.value == s]
        if not bucket:
            continue
        w = sum(1 for t in bucket if t.net_cents > 0)
        net = sum(t.net_cents for t in bucket)
        print(f"    {s.upper():4s} n={len(bucket):4d}  wins={w}  net={net:+d}c  WR={w/len(bucket):.1%}")
    print()
    print("  entry-price distribution (paid ask, dollars):")
    prices = sorted(t.entry_price for t in trades)
    buckets = [(0.75, 0.78), (0.78, 0.81), (0.81, 0.85), (0.85, 0.90), (0.90, 0.95), (0.95, 1.01)]
    for lo, hi in buckets:
        sub = [t for t in trades if lo <= t.entry_price < hi]
        if not sub:
            continue
        w = sum(1 for t in sub if t.net_cents > 0)
        net = sum(t.net_cents for t in sub)
        print(f"    [{lo:.2f},{hi:.2f})  n={len(sub):4d}  wins={w}  net={net:+d}c  WR={w/len(sub):.1%}")
    print()
    print("  entry-delay distribution (minutes after session open):")
    delays = []
    for t in trades:
        delays.append((t.entry_trigger_ts_ms - t.session_open_ms) / 60_000)
    delay_buckets = [(8, 9), (9, 10), (10, 11), (11, 12), (12, 13), (13, 14), (14, 15.1)]
    for lo, hi in delay_buckets:
        sub = [t for t, d in zip(trades, delays) if lo <= d < hi]
        if not sub:
            continue
        w = sum(1 for t in sub if t.net_cents > 0)
        net = sum(t.net_cents for t in sub)
        print(f"    [{lo:.0f}m,{hi:.0f}m)  n={len(sub):4d}  wins={w}  net={net:+d}c  WR={w/len(sub):.1%}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--db",
        action="append",
        type=Path,
        help="SQLite capture DB (can be repeated). Default: holdpure.",
    )
    p.add_argument("--ticker-like", default=SERIES_FILTER)
    p.add_argument("--max-markets", type=int, default=None)
    p.add_argument("--jsonl", type=Path, default=None, help="Write per-trade JSONL log")
    args = p.parse_args()

    dbs = args.db or [Path(p) for p in DEFAULT_DBS]
    agg = run(dbs, args.ticker_like, args.max_markets, args.jsonl)
    summarize(agg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
