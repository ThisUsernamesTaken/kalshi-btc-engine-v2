"""Live paper / shadow runner for engine_v3.

Connects to Kalshi (read-only by default), polls BTC from Bitstamp, monitors
active KXBTC15M markets, and evaluates engine_v3.evaluate_entry() at the
T-30 window. Logs all decisions to data/engine_v3_paper_decisions.jsonl.

MODES:
  --mode paper   : track decisions, simulate fills at current ask, no real orders
  --mode shadow  : like paper, AND cross-reference production trader's log
                   (data/live_v5_unified_trades.jsonl) to flag divergences

REAL ORDERS:
  Real order placement is OFF by default. Even with --i-have-real-money-authorized,
  must ALSO pass --live to actually place orders. Triple gate.

Output JSONL kinds:
  startup, discover, ws_subscribe, btc_poll_error,
  v3_evaluate, v3_paper_fill, v3_paper_settle, v3_shadow_compare,
  cusum_alert, cusum_kill, posterior_update.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Optional

import aiohttp

# Reuse credentials helper + Kalshi client from production trader
_V1_ROOT = Path(r"D:\Trading\btc-bias-engine")
if str(_V1_ROOT) not in sys.path:
    sys.path.insert(0, str(_V1_ROOT))
from kalshi_client import KalshiClient  # noqa: E402
from kalshi_ws import KalshiWebSocket  # noqa: E402

# engine_v3 module
sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine_v3 import (
    EngineV3Config, BayesianPosterior, CUSUMTracker, BTCPriceTracker,
    MarketSnapshot, evaluate_entry, settle_pnl_cents,
)

KALSHI_CREDS_PATH = Path(r"D:\Trading\btc-bias-engine\credentials\kalshi.env")
BITSTAMP_TICKER_URL = "https://www.bitstamp.net/api/v2/ticker/btcusd/"
PROD_LOG_PATH = Path(r"C:\Trading\kalshi-btc-engine-v2\data\live_v5_unified_trades.jsonl")
DEFAULT_OUT = Path(r"C:\Trading\kalshi-btc-engine-v2\data\engine_v3_paper_decisions.jsonl")


def load_kalshi_creds() -> tuple[str, str]:
    env = {}
    with KALSHI_CREDS_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    key_id = env.get("KALSHI_API_KEY") or os.environ.get("KALSHI_API_KEY")
    pem_path = env.get("KALSHI_PRIVATE_KEY_PATH") or os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not key_id or not pem_path:
        raise RuntimeError("kalshi.env missing")
    pem = Path(pem_path).read_text(encoding="utf-8")
    return key_id, pem


async def btc_poller(buf: BTCPriceTracker, session: aiohttp.ClientSession, log_fp,
                     stop: dict, poll_interval_s: float = 2.0):
    """Background loop: REST poll Bitstamp, append to buffer."""
    consecutive_errors = 0
    while not stop["flag"]:
        try:
            async with session.get(BITSTAMP_TICKER_URL,
                                    timeout=aiohttp.ClientTimeout(total=5)) as r:
                r.raise_for_status()
                data = await r.json()
            price = float(data.get("last") or 0)
            if price > 0:
                buf.add(int(time.time() * 1000), price)
                consecutive_errors = 0
        except Exception as e:  # noqa: BLE001
            consecutive_errors += 1
            if consecutive_errors in (1, 10, 100):
                log_fp.write(json.dumps({
                    "kind": "btc_poll_error", "ts_ms": int(time.time()*1000),
                    "error": repr(e), "consecutive": consecutive_errors,
                }) + "\n")
                log_fp.flush()
        await asyncio.sleep(poll_interval_s)


async def fetch_floor_strike(client: KalshiClient, ticker: str) -> Optional[float]:
    try:
        c = await client.get_contract(ticker)
        # Extract floor_strike from raw fields if available
        raw = getattr(c, "raw_json", None)
        if raw:
            try:
                rj = json.loads(raw) if isinstance(raw, str) else raw
                return float(rj.get("floor_strike")) if rj.get("floor_strike") else None
            except Exception:
                pass
        # Some Kalshi contracts expose subtitle with target — best-effort
        return None
    except Exception:
        return None


async def discover_open_markets(client: KalshiClient) -> list[dict]:
    """Find KXBTC15M markets that are open or about to open."""
    try:
        markets = await client.get_markets(series_ticker="KXBTC15M", status="open")
    except Exception:
        return []
    out = []
    now_ms = int(time.time() * 1000)
    for m in (markets or []):
        try:
            close_ms = int(dt.datetime.fromisoformat(
                m.close_time.replace("Z", "+00:00")).timestamp() * 1000)
        except Exception:
            continue
        if close_ms < now_ms:
            continue
        if close_ms - now_ms > 15 * 60 * 1000:  # > 15 min away, too early
            continue
        out.append({"ticker": m.ticker, "close_ts_ms": close_ms})
    return out


def load_recent_shadow_records() -> list[dict]:
    """Load recent production trader trigger records for shadow comparison."""
    if not PROD_LOG_PATH.exists():
        return []
    out = []
    cutoff_ms = int(time.time() * 1000) - 3600_000  # last hour
    try:
        with PROD_LOG_PATH.open(encoding="utf-8") as f:
            # Tail the last ~5000 lines
            lines = f.readlines()[-5000:]
        for ln in lines:
            try:
                r = json.loads(ln)
                if r.get("ts_ms", 0) < cutoff_ms:
                    continue
                if r.get("kind") in ("t30_sniper_trigger", "t30_sniper_fill",
                                     "t30_sniper_no_fill", "earlier_moderate_fill",
                                     "settle"):
                    out.append(r)
            except Exception:
                continue
    except Exception:
        return []
    return out


async def main_async() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("paper", "shadow"), default="paper")
    parser.add_argument("--decision-log", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--poll-interval-s", type=float, default=1.5)
    parser.add_argument("--posterior-prior",
                        choices=("cold", "backtest"), default="backtest",
                        help="cold = Beta(2,2); backtest = Beta(45,1) from "
                             "44/44 historical win rate")
    parser.add_argument("--enable-vwap-lock", action="store_true", default=True)
    parser.add_argument("--enable-jump-filter", action="store_true", default=True)
    parser.add_argument("--enable-bayesian-sizing", action="store_true", default=True)
    parser.add_argument("--enable-cusum", action="store_true", default=True)
    parser.add_argument("--fixed-contracts", type=int, default=None,
                        help="If set, override Bayesian sizing with fixed N contracts.")
    parser.add_argument("--bankroll-dollars", type=float, default=30.0)
    parser.add_argument("--i-have-real-money-authorized", action="store_true",
                        help="Required to even consider --live")
    parser.add_argument("--live", action="store_true",
                        help="Actually place real orders. Requires --i-have-real-money-authorized.")
    args = parser.parse_args()

    LIVE_ENABLED = (args.live and args.i_have_real_money_authorized)
    if args.live and not args.i_have_real_money_authorized:
        print("[engine_v3_paper] --live requires --i-have-real-money-authorized; ignoring --live.")

    args.decision_log.parent.mkdir(parents=True, exist_ok=True)
    log_fp = args.decision_log.open("a", encoding="utf-8")

    key_id, pem = load_kalshi_creds()

    config = EngineV3Config(bankroll_dollars=args.bankroll_dollars)
    if not args.enable_jump_filter:
        config.jump_sigma_threshold = 999.0
    if not args.enable_vwap_lock:
        config.vwap_settlement_window_s = 0
    if not args.enable_cusum:
        config.cusum_kill_losses = 99
    if args.fixed_contracts is not None:
        config.fixed_contracts = args.fixed_contracts
        config.posterior_mean_min = 0.0  # don't gate on posterior if fixed-size
    elif not args.enable_bayesian_sizing:
        config.fixed_contracts = 5
        config.posterior_mean_min = 0.0

    # Prior choice
    if args.posterior_prior == "backtest":
        prior_a, prior_b = 45.0, 1.0
    else:
        prior_a, prior_b = config.prior_wins, config.prior_losses
    posterior = BayesianPosterior(alpha=prior_a, beta=prior_b)
    cusum = CUSUMTracker(config.cusum_window, config.cusum_kill_losses,
                          config.cusum_alert_losses)

    btc_buf = BTCPriceTracker(window_s=320)
    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *a: stop.__setitem__("flag", True))
    try:
        signal.signal(signal.SIGTERM, lambda *a: stop.__setitem__("flag", True))
    except Exception:
        pass

    startup = {
        "kind": "startup", "ts_ms": int(time.time() * 1000),
        "engine": "engine_v3_paper",
        "mode": args.mode,
        "live_enabled": LIVE_ENABLED,
        "decision_log": str(args.decision_log),
        "config": {
            "t30_window_open_s": config.t30_window_open_s,
            "t30_window_close_s": config.t30_window_close_s,
            "fav_bid_min": config.fav_bid_min,
            "posterior_mean_min": config.posterior_mean_min,
            "kelly_fraction": config.kelly_fraction,
            "bankroll_dollars": config.bankroll_dollars,
            "max_contracts_per_trade": config.max_contracts_per_trade,
            "min_contracts_per_trade": config.min_contracts_per_trade,
            "fixed_contracts": config.fixed_contracts,
            "cusum_window": config.cusum_window,
            "cusum_kill_losses": config.cusum_kill_losses,
            "jump_lookback_s": config.jump_lookback_s,
            "jump_sigma_threshold": config.jump_sigma_threshold,
            "vwap_settlement_window_s": config.vwap_settlement_window_s,
        },
        "posterior_prior": {"alpha": posterior.alpha, "beta": posterior.beta},
    }
    log_fp.write(json.dumps(startup) + "\n")
    log_fp.flush()
    print(f"[engine_v3_paper] STARTED mode={args.mode} live={LIVE_ENABLED} "
          f"prior=Beta({posterior.alpha},{posterior.beta}) "
          f"sizing={'fixed='+str(config.fixed_contracts) if config.fixed_contracts else 'KELLY'}",
          flush=True)

    async with aiohttp.ClientSession() as session, KalshiClient(key_id, pem) as client:
        # Background BTC poller
        btc_task = asyncio.create_task(btc_poller(btc_buf, session, log_fp, stop))

        tracked: dict[str, dict[str, Any]] = {}  # ticker -> state
        last_discover_t = 0.0
        last_status_t = time.time()
        n_evals = 0
        n_paper_fills = 0
        n_paper_settles = 0

        try:
            while not stop["flag"]:
                now_wall = time.time()
                now_ms = int(now_wall * 1000)

                # Discover periodically
                if now_wall - last_discover_t > 30:
                    last_discover_t = now_wall
                    discovered = await discover_open_markets(client)
                    for d in discovered:
                        tk = d["ticker"]
                        if tk not in tracked:
                            tracked[tk] = {
                                "close_ts_ms": d["close_ts_ms"],
                                "strike": 0.0,
                                "evaluated": False,
                                "paper_entered": False,
                                "paper_side": None,
                                "paper_contracts": 0,
                                "paper_entry_price_c": 0,
                                "settled": False,
                            }
                            log_fp.write(json.dumps({
                                "kind": "discover", "ts_ms": now_ms,
                                "ticker": tk, "close_ts_ms": d["close_ts_ms"],
                            }) + "\n")
                            log_fp.flush()

                # Evaluate each tracked market
                for ticker, state in list(tracked.items()):
                    close_ts = state["close_ts_ms"]
                    secs_to_close = (close_ts - now_ms) / 1000.0

                    # Cleanup settled
                    if secs_to_close < -300:  # 5 min past close
                        if not state["settled"] and state["paper_entered"]:
                            # Try to fetch settlement result
                            try:
                                c = await client.get_contract(ticker)
                                result = c.result if c else None
                            except Exception:
                                result = None
                            if result in ("yes", "no"):
                                pnl_c = settle_pnl_cents(
                                    state["paper_entry_price_c"],
                                    state["paper_contracts"],
                                    state["paper_side"], result,
                                )
                                won = state["paper_side"] == result
                                posterior.update(won)
                                cusum.update(won)
                                n_paper_settles += 1
                                log_fp.write(json.dumps({
                                    "kind": "v3_paper_settle", "ts_ms": now_ms,
                                    "ticker": ticker,
                                    "side": state["paper_side"],
                                    "contracts": state["paper_contracts"],
                                    "entry_price_cents": state["paper_entry_price_c"],
                                    "result": result, "won": won,
                                    "pnl_cents": pnl_c,
                                    "posterior_alpha": posterior.alpha,
                                    "posterior_beta": posterior.beta,
                                    "posterior_mean": posterior.mean(),
                                    "cusum_loss_count": cusum.loss_count(),
                                }) + "\n")
                                log_fp.flush()
                                print(f"[v3] SETTLE {ticker[-25:]} {state['paper_side']}@"
                                      f"{state['paper_entry_price_c']}c result={result} "
                                      f"pnl={pnl_c}c (posterior_mean={posterior.mean():.3f})",
                                      flush=True)
                                if cusum.should_kill():
                                    log_fp.write(json.dumps({
                                        "kind": "cusum_kill", "ts_ms": now_ms,
                                        "losses": cusum.loss_count(),
                                    }) + "\n")
                                    log_fp.flush()
                                    print(f"[v3] CUSUM KILL FIRED ({cusum.loss_count()} losses)",
                                          flush=True)
                            state["settled"] = True
                        if secs_to_close < -3600:
                            tracked.pop(ticker, None)
                        continue

                    # Only evaluate in the entry window
                    if not (config.t30_window_close_s <= secs_to_close <= config.t30_window_open_s):
                        continue
                    if state["evaluated"]:
                        continue

                    # Need strike
                    if state["strike"] == 0:
                        s = await fetch_floor_strike(client, ticker)
                        if s and s > 0:
                            state["strike"] = s

                    # Fetch current book
                    try:
                        ob = await client.get_orderbook(ticker)
                    except Exception:
                        continue
                    yb_c = int(round((ob.yes_bid or 0) * 100)) if hasattr(ob, "yes_bid") else None
                    ya_c = int(round((ob.yes_ask or 0) * 100)) if hasattr(ob, "yes_ask") else None
                    if not yb_c or not ya_c:
                        continue

                    snap = MarketSnapshot(
                        ticker=ticker, secs_to_close=secs_to_close,
                        close_ts_ms=close_ts, strike=state["strike"],
                        yes_bid=yb_c, yes_ask=ya_c,
                        no_bid=100 - ya_c, no_ask=100 - yb_c,
                        btc_now=btc_buf.latest()[1] if btc_buf.latest() else None,
                    )
                    dec = evaluate_entry(snap, btc_buf, posterior, cusum, config)
                    n_evals += 1
                    log_fp.write(json.dumps({
                        "kind": "v3_evaluate", "ts_ms": now_ms,
                        "ticker": ticker, "action": dec.action, "reason": dec.reason,
                        "secs_to_close": round(secs_to_close, 1),
                        "yes_bid": yb_c, "yes_ask": ya_c,
                        "no_bid": snap.no_bid, "no_ask": snap.no_ask,
                        "fav_bid": dec.fav_bid_cents, "fav_ask": dec.fav_ask_cents,
                        "btc": snap.btc_now, "strike": state["strike"],
                        "posterior_mean": dec.posterior_mean,
                        "posterior_skeptical_p": dec.posterior_skeptical_p,
                        "cusum_loss_count": dec.cusum_loss_count,
                        "vwap_locked_avg": dec.vwap_locked_avg,
                        "vwap_locked_direction": dec.vwap_locked_direction,
                        "jump_detected": dec.jump_detected,
                        "kelly_quarter": dec.kelly_quarter,
                        "side": dec.side, "contracts": dec.contracts,
                        "limit_cents": dec.limit_cents,
                    }) + "\n")
                    log_fp.flush()
                    state["evaluated"] = True
                    if dec.action == "ENTER":
                        # Paper fill at current ask
                        state["paper_entered"] = True
                        state["paper_side"] = dec.side
                        state["paper_contracts"] = dec.contracts
                        state["paper_entry_price_c"] = dec.fav_ask_cents
                        n_paper_fills += 1
                        log_fp.write(json.dumps({
                            "kind": "v3_paper_fill", "ts_ms": now_ms,
                            "ticker": ticker, "side": dec.side,
                            "contracts": dec.contracts,
                            "entry_price_cents": dec.fav_ask_cents,
                            "limit_cents": dec.limit_cents,
                            "secs_to_close": round(secs_to_close, 1),
                            "kelly_quarter": dec.kelly_quarter,
                            "posterior_mean": dec.posterior_mean,
                        }) + "\n")
                        log_fp.flush()
                        print(f"[v3] PAPER FILL {ticker[-25:]} {dec.side} "
                              f"{dec.contracts}@{dec.fav_ask_cents}c "
                              f"(secs_left={secs_to_close:.0f})", flush=True)

                # Shadow comparison
                if args.mode == "shadow" and now_wall - last_status_t > 60:
                    shadow_recs = load_recent_shadow_records()
                    if shadow_recs:
                        log_fp.write(json.dumps({
                            "kind": "v3_shadow_compare", "ts_ms": now_ms,
                            "prod_recent_count": len(shadow_recs),
                            "v3_evals": n_evals,
                            "v3_paper_fills": n_paper_fills,
                            "v3_paper_settles": n_paper_settles,
                            "posterior": {"alpha": posterior.alpha, "beta": posterior.beta,
                                          "mean": posterior.mean()},
                            "cusum_losses": cusum.loss_count(),
                        }) + "\n")
                        log_fp.flush()

                # Status print
                if now_wall - last_status_t > 30:
                    last_status_t = now_wall
                    print(f"[v3] STATUS tracked={len(tracked)} evals={n_evals} "
                          f"fills={n_paper_fills} settles={n_paper_settles} "
                          f"posterior=Beta({posterior.alpha:.0f},{posterior.beta:.0f})"
                          f"={posterior.mean():.3f} cusum_losses={cusum.loss_count()}",
                          flush=True)

                await asyncio.sleep(args.poll_interval_s)
        finally:
            stop["flag"] = True
            btc_task.cancel()
            try:
                await btc_task
            except Exception:
                pass

    log_fp.close()
    return 0


def main():
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
