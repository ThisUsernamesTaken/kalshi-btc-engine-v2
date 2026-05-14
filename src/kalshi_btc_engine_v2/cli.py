from __future__ import annotations

import argparse
import asyncio
import json
import os
from decimal import Decimal
from pathlib import Path

from kalshi_btc_engine_v2.adapters.kalshi import l2_event_to_record, snapshot_event_from_payload
from kalshi_btc_engine_v2.adapters.spot import (
    SpotQuote,
    fuse_spot_quotes,
    quote_to_record,
)
from kalshi_btc_engine_v2.backtest.runner import DEFAULT_DECISION_INTERVAL_MS
from kalshi_btc_engine_v2.capture import BurnInConfig, BurnInRunner
from kalshi_btc_engine_v2.core.time import utc_now_ms
from kalshi_btc_engine_v2.monitoring.continuity import continuity_json, sqlite_continuity_report
from kalshi_btc_engine_v2.replay.engine import DeterministicReplayer, replay_sample_json
from kalshi_btc_engine_v2.storage.schema import ddl_script
from kalshi_btc_engine_v2.storage.sqlite import connect, init_db, insert_record, upsert_market

LIVE_ENGINE_CREDS_PATH = Path(r"C:\Trading\btc-bias-engine\credentials\kalshi.env")


def _add_db_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", required=True, type=Path, help="SQLite database path")


def _resolve_kalshi_creds_into_env() -> bool:
    """If ``ENGINE_V2_KALSHI_KEY_ID`` is unset and the live-engine credential file
    exists, read it and export the v2 env vars. Returns True if creds resolved.

    This makes ``capture-burnin`` and other auth-requiring commands work without
    the caller manually exporting environment variables.
    """
    if os.environ.get("ENGINE_V2_KALSHI_KEY_ID"):
        return True
    if not LIVE_ENGINE_CREDS_PATH.exists():
        return False
    try:
        for raw_line in LIVE_ENGINE_CREDS_PATH.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key == "KALSHI_API_KEY" and not os.environ.get("ENGINE_V2_KALSHI_KEY_ID"):
                os.environ["ENGINE_V2_KALSHI_KEY_ID"] = value
            elif key == "KALSHI_PRIVATE_KEY_PATH" and not os.environ.get(
                "ENGINE_V2_KALSHI_PRIVATE_KEY_PATH"
            ):
                os.environ["ENGINE_V2_KALSHI_PRIVATE_KEY_PATH"] = value
    except OSError:
        return False
    return bool(os.environ.get("ENGINE_V2_KALSHI_KEY_ID"))


def command_init_db(args: argparse.Namespace) -> int:
    init_db(args.db)
    print(f"initialized {args.db}")
    return 0


def _insert_smoke_data(db_path: Path) -> tuple[int, int]:
    init_db(db_path)
    base_ts = utc_now_ms()
    market_ticker = f"KXBTC15M-SMOKE-{base_ts}"

    with connect(db_path) as conn:
        market_raw = {
            "ticker": market_ticker,
            "series_ticker": "KXBTC15M",
            "event_ticker": f"{market_ticker}-EVENT",
            "market_type": "binary",
            "title": "Smoke BTC Up or Down",
            "open_time": "2026-05-12T00:00:00Z",
            "close_time": "2026-05-12T00:15:00Z",
            "expiration_time": "2026-05-12T00:15:00Z",
            "settlement_source": "brti_proxy",
            "status": "open",
            "fee_type": "quadratic",
            "fee_multiplier": "0.07",
            "price_level_structure_json": "{}",
            "raw_json": json.dumps({"source": "smoke"}),
            "created_at_ms": base_ts,
            "updated_at_ms": base_ts,
        }
        upsert_market(conn, market_raw)

        snapshot = snapshot_event_from_payload(
            market_ticker=market_ticker,
            payload={
                "orderbook_fp": {
                    "yes_dollars": [["0.4800", "10"], ["0.4900", "20"]],
                    "no_dollars": [["0.5000", "15"]],
                }
            },
            seq=100,
            received_ts_ms=base_ts + 100,
        )
        insert_record(conn, "kalshi_l2_event", l2_event_to_record(snapshot))

        delta = snapshot_event_from_payload(
            market_ticker=market_ticker,
            payload={
                "orderbook_fp": {
                    "yes_dollars": [["0.4800", "10"], ["0.5000", "12"]],
                    "no_dollars": [["0.4900", "18"]],
                }
            },
            seq=101,
            received_ts_ms=base_ts + 350,
        )
        record = l2_event_to_record(delta)
        record["event_type"] = "delta"
        record["side"] = "yes"
        record["price"] = "0.5000"
        record["size"] = "12"
        insert_record(conn, "kalshi_l2_event", record)

        quotes = [
            SpotQuote(
                received_ts_ms=base_ts + 500,
                exchange_ts_ms=base_ts + 480,
                venue="coinbase",
                symbol="BTC-USD",
                bid=Decimal("103000.00"),
                ask=Decimal("103001.00"),
                mid=Decimal("103000.50"),
                last=Decimal("103000.75"),
                raw_json='{"source":"smoke"}',
            ),
            SpotQuote(
                received_ts_ms=base_ts + 540,
                exchange_ts_ms=base_ts + 520,
                venue="kraken",
                symbol="BTC/USD",
                bid=Decimal("102999.50"),
                ask=Decimal("103000.50"),
                mid=Decimal("103000.00"),
                last=Decimal("103000.25"),
                raw_json='{"source":"smoke"}',
            ),
            SpotQuote(
                received_ts_ms=base_ts + 560,
                exchange_ts_ms=base_ts + 540,
                venue="bitstamp",
                symbol="btcusd",
                bid=Decimal("103000.25"),
                ask=Decimal("103001.25"),
                mid=Decimal("103000.75"),
                last=Decimal("103000.40"),
                raw_json='{"source":"smoke"}',
            ),
        ]
        for quote in quotes:
            insert_record(conn, "spot_quote_event", quote_to_record(quote))

        fused = fuse_spot_quotes(quotes, now_ms=base_ts + 600)
        if fused is not None:
            insert_record(conn, "spot_quote_event", quote_to_record(fused.quote))

        insert_record(
            conn,
            "kalshi_trade_event",
            {
                "received_ts_ms": base_ts + 700,
                "exchange_ts_ms": base_ts + 690,
                "market_ticker": market_ticker,
                "trade_id": "smoke-trade-1",
                "side": "yes",
                "taker_side": "yes",
                "yes_price": "0.5000",
                "price": "0.5000",
                "count": "3",
                "raw_json": '{"source":"smoke"}',
            },
        )
        conn.commit()

    return base_ts, base_ts + 1_000


def command_smoke_replay(args: argparse.Namespace) -> int:
    start_ms, end_ms = _insert_smoke_data(args.db)
    with connect(args.db) as conn:
        ticks = list(DeterministicReplayer(conn).run(start_ms=start_ms, end_ms=end_ms))
    print(replay_sample_json(ticks[:10]))
    return 0


def command_continuity_report(args: argparse.Namespace) -> int:
    init_db(args.db)
    with connect(args.db) as conn:
        stats = sqlite_continuity_report(conn, persist=args.persist)
    print(continuity_json(stats))
    return 0


def command_print_ddl(_: argparse.Namespace) -> int:
    print(ddl_script())
    return 0


def command_capture_burnin(args: argparse.Namespace) -> int:
    resolved = _resolve_kalshi_creds_into_env()
    if not resolved:
        print(
            "[capture-burnin] no Kalshi credentials in environment or live-engine file; "
            "WS will run public-only (likely 401 unless venue allows it)."
        )
    else:
        print("[capture-burnin] using Kalshi credentials from environment")
    config = BurnInConfig(
        db_path=args.db,
        hours=args.hours,
        market_ticker=args.market_ticker,
    )
    asyncio.run(BurnInRunner(config).run())
    return 0


def command_db_stats(args: argparse.Namespace) -> int:
    if not args.db.exists():
        print(json.dumps({"error": f"db not found: {args.db}"}))
        return 1
    with connect(args.db) as conn:
        tables = [
            "market_dim",
            "kalshi_l2_event",
            "kalshi_trade_event",
            "spot_quote_event",
            "spot_trade_event",
            "capture_health_event",
            "kalshi_lifecycle_event",
            "kalshi_user_order_event",
            "kalshi_fill_event",
            "kalshi_position_event",
        ]
        counts: dict[str, int | str] = {}
        for table in tables:
            try:
                counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            except Exception as exc:  # noqa: BLE001
                counts[table] = f"err: {exc}"
        health_rows = conn.execute(
            "SELECT event_kind, COUNT(*) FROM capture_health_event GROUP BY event_kind"
        ).fetchall()
        health_hist = {str(row[0]): int(row[1]) for row in health_rows}
        time_span = conn.execute(
            "SELECT MIN(received_ts_ms), MAX(received_ts_ms) FROM kalshi_l2_event"
        ).fetchone()
        l2_first_ms = int(time_span[0]) if time_span and time_span[0] else None
        l2_last_ms = int(time_span[1]) if time_span and time_span[1] else None
        runtime_s = (l2_last_ms - l2_first_ms) / 1000.0 if l2_first_ms and l2_last_ms else 0.0
        l2_rate = counts.get("kalshi_l2_event", 0)
        if isinstance(l2_rate, int) and runtime_s > 0:
            l2_per_s: float | None = l2_rate / runtime_s
        else:
            l2_per_s = None
        markets = conn.execute(
            "SELECT DISTINCT market_ticker FROM kalshi_l2_event ORDER BY market_ticker"
        ).fetchall()
        market_tickers = [str(m[0]) for m in markets]
    print(
        json.dumps(
            {
                "row_counts": counts,
                "health_histogram": health_hist,
                "kalshi_l2_first_ms": l2_first_ms,
                "kalshi_l2_last_ms": l2_last_ms,
                "kalshi_l2_runtime_seconds": runtime_s,
                "kalshi_l2_events_per_second": l2_per_s,
                "markets_observed": market_tickers,
            },
            indent=2,
        )
    )
    return 0


def command_compare_gates(args: argparse.Namespace) -> int:
    """Run gated vs ungated backtest on the same DB and print side-by-side findings."""
    from kalshi_btc_engine_v2.backtest.runner import (
        BacktestConfig,
        Backtester,
        aggregate_summary_to_dict,
    )
    from kalshi_btc_engine_v2.policy.sizing import SizingConfig
    from kalshi_btc_engine_v2.policy.veto import VetoConfig
    from kalshi_btc_engine_v2.risk.guards import RiskConfig

    gated_cfg = BacktestConfig(
        bankroll_dollars=args.bankroll,
        decision_interval_ms=args.decision_interval_ms,
        min_returns_for_decision=args.min_returns,
        risk_config=RiskConfig(max_risk_per_window_dollars=args.window_cap_dollars),
        sizing_config=SizingConfig(
            fractional_kelly=args.fractional_kelly,
            max_contracts=args.max_contracts,
        ),
    )
    ungated_cfg = BacktestConfig(
        bankroll_dollars=args.bankroll,
        decision_interval_ms=args.decision_interval_ms,
        min_returns_for_decision=args.min_returns,
        risk_config=RiskConfig(
            max_risk_per_window_dollars=10_000.0,
            per_ticker_entry_lock_enabled=False,
            oversell_hardening_enabled=False,
            max_entries_per_window=10_000,
        ),
        sizing_config=SizingConfig(
            fractional_kelly=args.fractional_kelly,
            max_contracts=args.max_contracts,
        ),
        veto_config=VetoConfig(
            min_venue_quorum=1,
            max_venue_disagreement_bp=1000.0,
            min_depth_multiplier=0.0,
            max_fragility_score=1000.0,
        ),
        enable_cooldowns=False,
        enable_error_tracker=False,
        ungated=True,
        min_edge_cents_override=args.ungated_min_edge,
    )

    gated_bt = Backtester(config=gated_cfg)
    try:
        gated_summary = gated_bt.run_db(args.db)
    finally:
        gated_bt.close()

    ungated_bt = Backtester(config=ungated_cfg)
    try:
        ungated_summary = ungated_bt.run_db(args.db)
    finally:
        ungated_bt.close()

    gated_dict = aggregate_summary_to_dict(gated_summary)
    ungated_dict = aggregate_summary_to_dict(ungated_summary)
    delta_pnl = ungated_dict["net_pnl_cents"] - gated_dict["net_pnl_cents"]
    gated_trades = gated_dict["decisions_buy"] + gated_dict["decisions_exit"]
    ungated_trades = ungated_dict["decisions_buy"] + ungated_dict["decisions_exit"]
    print(
        json.dumps(
            {
                "gating_profitable_for_window": delta_pnl < 0,
                "gated_net_pnl_cents": gated_dict["net_pnl_cents"],
                "ungated_net_pnl_cents": ungated_dict["net_pnl_cents"],
                "gating_saved_cents": -delta_pnl,
                "gated_trades": gated_trades,
                "ungated_trades": ungated_trades,
                "trade_ratio_ungated_over_gated": (
                    ungated_trades / gated_trades if gated_trades else None
                ),
                "gated": gated_dict,
                "ungated": ungated_dict,
            },
            indent=2,
            default=str,
        )
    )
    return 0


def command_divergence_stats(args: argparse.Namespace) -> int:
    from kalshi_btc_engine_v2.backtest.divergence_stats import divergence_stats

    if not args.decision_log.exists():
        print(json.dumps({"error": f"decision log not found: {args.decision_log}"}))
        return 1
    stats = divergence_stats(args.decision_log)
    print(json.dumps(stats.to_dict(), indent=2, default=str))
    return 0


def command_trade_patterns(args: argparse.Namespace) -> int:
    from kalshi_btc_engine_v2.backtest.trade_patterns import (
        TradePatternConfig,
        detect_patterns,
    )

    if not args.decision_log.exists():
        print(json.dumps({"error": f"decision log not found: {args.decision_log}"}))
        return 1
    cfg = TradePatternConfig(
        quick_flip_max_s=args.quick_flip_max_s,
        chase_window_s=args.chase_window_s,
        chase_min_entries=args.chase_min_entries,
        flip_flop_window_s=args.flip_flop_window_s,
    )
    report = detect_patterns(args.decision_log, config=cfg)
    print(json.dumps(report.to_dict(), indent=2, default=str))
    return 0


def command_per_market_report(args: argparse.Namespace) -> int:
    from kalshi_btc_engine_v2.backtest.per_market_report import (
        per_market_report,
        report_to_dict,
    )

    if not args.db.exists():
        print(json.dumps({"error": f"db not found: {args.db}"}))
        return 1
    if not args.decision_log.exists():
        print(json.dumps({"error": f"decision log not found: {args.decision_log}"}))
        return 1
    stats = per_market_report(args.decision_log, args.db)
    print(json.dumps(report_to_dict(stats), indent=2, default=str))
    return 0


def command_hold_counterfactual(args: argparse.Namespace) -> int:
    from dataclasses import asdict

    from kalshi_btc_engine_v2.backtest.counterfactual import hold_to_settlement

    if not args.db.exists():
        print(json.dumps({"error": f"db not found: {args.db}"}))
        return 1
    if not args.decision_log.exists():
        print(json.dumps({"error": f"decision log not found: {args.decision_log}"}))
        return 1
    report = hold_to_settlement(args.decision_log, args.db)
    payload = {
        "entries": report.entries,
        "settled_entries": report.settled_entries,
        "directionally_correct": report.directionally_correct,
        "directional_accuracy_pct": (
            round(100.0 * report.directionally_correct / report.settled_entries, 1)
            if report.settled_entries
            else None
        ),
        "hold_to_settlement_gross_cents": report.hold_to_settlement_gross_cents,
        "hold_to_settlement_fees_cents": report.hold_to_settlement_fees_cents,
        "hold_to_settlement_net_cents": report.hold_to_settlement_net_cents,
        "actual_exit_estimated_cents": report.actual_exit_estimated_cents,
        "delta_cents": report.delta_cents,
        "delta_interpretation": (
            "exit rules saved money"
            if (report.delta_cents or 0) < 0
            else "exit rules cost money vs hold" if (report.delta_cents or 0) > 0 else None
        ),
        "trades": [asdict(t) for t in report.trades],
    }
    print(json.dumps(payload, indent=2, default=str))
    return 0


def command_backfill_market_dim(args: argparse.Namespace) -> int:
    from kalshi_btc_engine_v2.backtest.backfill import backfill_from_lifecycle

    if not args.db.exists():
        print(json.dumps({"error": f"db not found: {args.db}"}))
        return 1
    result = backfill_from_lifecycle(args.db)
    print(json.dumps(result, indent=2))
    return 0


def command_settled_markets(args: argparse.Namespace) -> int:
    from kalshi_btc_engine_v2.backtest.settlement import scan_settled_markets

    if not args.db.exists():
        print(json.dumps({"error": f"db not found: {args.db}"}))
        return 1
    settled = scan_settled_markets(args.db)
    payload = {
        "settled_count": len(settled),
        "yes_wins": sum(1 for s in settled if s.yes_won == 1),
        "no_wins": sum(1 for s in settled if s.yes_won == 0),
        "markets": [
            {
                "ticker": s.market_ticker,
                "yes_won": s.yes_won,
                "settlement_value_dollars": s.settlement_value_dollars,
                "close_time": s.close_time,
            }
            for s in settled
        ],
    }
    print(json.dumps(payload, indent=2, default=str))
    return 0


def command_walk_forward(args: argparse.Namespace) -> int:
    from kalshi_btc_engine_v2.backtest.runner import (
        BacktestConfig,
        Backtester,
        aggregate_summary_to_dict,
    )
    from kalshi_btc_engine_v2.backtest.walk_forward import (
        WalkForwardConfig,
        run_walk_forward,
    )
    from kalshi_btc_engine_v2.policy.sizing import SizingConfig
    from kalshi_btc_engine_v2.risk.guards import RiskConfig

    if not args.db.exists():
        print(json.dumps({"error": f"db not found: {args.db}"}))
        return 1
    with connect(args.db) as conn:
        span = conn.execute(
            "SELECT MIN(received_ts_ms), MAX(received_ts_ms) FROM kalshi_l2_event"
        ).fetchone()
    if not span or span[0] is None:
        print(json.dumps({"error": "no kalshi_l2_event rows captured yet"}))
        return 1
    start_ms = int(span[0])
    end_ms = int(span[1])

    def _factory() -> Backtester:
        return Backtester(
            config=BacktestConfig(
                bankroll_dollars=args.bankroll,
                decision_interval_ms=args.decision_interval_ms,
                min_returns_for_decision=args.min_returns,
                risk_config=RiskConfig(max_risk_per_window_dollars=args.window_cap_dollars),
                sizing_config=SizingConfig(
                    fractional_kelly=args.fractional_kelly,
                    max_contracts=args.max_contracts,
                ),
            )
        )

    report = run_walk_forward(
        args.db,
        available_start_ms=start_ms,
        available_end_ms=end_ms,
        backtester_factory=_factory,
        config=WalkForwardConfig(
            train_days=args.train_days,
            validate_days=args.validate_days,
            test_days=args.test_days,
            step_days=args.step_days,
        ),
    )
    print(
        json.dumps(
            {
                "total_windows": len(report.windows),
                "total_net_pnl_cents": report.total_net_pnl_cents(),
                "total_fills": report.total_fills(),
                "per_window": [
                    {
                        "index": r.window.index,
                        "test_start_ms": r.window.test_start_ms,
                        "test_end_ms": r.window.test_end_ms,
                        "summary": aggregate_summary_to_dict(r.summary),
                    }
                    for r in report.windows
                ],
            },
            indent=2,
            default=str,
        )
    )
    return 0


_BACKTEST_PRESETS = {
    # 2026-05-12 empirical best on the first 4h burn-in. q_cal extreme veto
    # blocks overconfident wrong bets; adverse=-100 disables the
    # adverse_revaluation stop so winning trades play out. Result: +$0.31 net
    # vs default −$1.15. See HANDOFF.md "Tuning experiment 4" for context.
    "qcalveto_neverbail": {
        "q_cal_min": 0.10,
        "q_cal_max": 0.90,
        "adverse_ev_cents": -100.0,
    },
    "qcalveto_neverbail_safe": {
        "q_cal_min": 0.10,
        "q_cal_max": 0.90,
        "adverse_ev_cents": -100.0,
        "spot_circuit_breaker_bp": 30.0,
    },
    # Equivalent strategy using regime gating instead of q-boundary. Same
    # final P&L on the 4h slice. Choose based on whether regime classifier
    # or q-boundary feels more principled in your design.
    "regimefilter_neverbail": {
        "tradeable_regimes": "info_absorption_trend,reflexive_squeeze",
        "adverse_ev_cents": -100.0,
    },
    "regimefilter_neverbail_safe": {
        "tradeable_regimes": "info_absorption_trend,reflexive_squeeze",
        "adverse_ev_cents": -100.0,
        "spot_circuit_breaker_bp": 30.0,
    },
    # 2026-05-12 — pure hold-to-settlement. Disables BOTH the EV-flip
    # adverse_revaluation branch AND the profit_capture branch, leaving only
    # the three rare-bail classes:
    #   1. feed_degraded (operational; cannot be disabled)
    #   2. spot_circuit_breaker (structural break)
    #   3. time_stop / hold_to_settlement near close (mechanical close-out)
    # Plus the q_cal extreme veto and the size-1 fee-floor (defaults).
    # This implements the report's central recommendation: hold is default,
    # bail is rare and objective. Tested via the burn-in registry in
    # docs/EXPERIMENT_REGISTRY_2026_05_12.md.
    "hold_to_settle_pure": {
        "q_cal_min": 0.10,
        "q_cal_max": 0.90,
        "adverse_ev_cents": -100.0,
        "spot_circuit_breaker_bp": 30.0,
        "profit_capture_enabled": False,
    },
}


def _apply_preset(args: argparse.Namespace) -> None:
    if not getattr(args, "preset", None):
        return
    preset = _BACKTEST_PRESETS.get(args.preset)
    if preset is None:
        raise SystemExit(f"unknown preset {args.preset!r}; choose from {list(_BACKTEST_PRESETS)}")
    # Default sentinel values per argparse defaults; only override if matching
    _defaults = {
        "q_cal_min": 0.0,
        "q_cal_max": 1.0,
        "adverse_ev_cents": -0.6,
        "spot_circuit_breaker_bp": 0.0,
        "tradeable_regimes": None,
        "profit_capture_enabled": True,
    }
    for key, value in preset.items():
        if getattr(args, key, _defaults.get(key)) == _defaults.get(key):
            setattr(args, key, value)


def command_backtest(args: argparse.Namespace) -> int:
    from kalshi_btc_engine_v2.backtest.runner import (
        BacktestConfig,
        Backtester,
        aggregate_summary_to_dict,
    )
    from kalshi_btc_engine_v2.models.regime import RegimeConfig
    from kalshi_btc_engine_v2.policy.exits import ExitConfig
    from kalshi_btc_engine_v2.policy.sizing import SizingConfig
    from kalshi_btc_engine_v2.policy.veto import VetoConfig
    from kalshi_btc_engine_v2.risk.guards import RiskConfig

    _apply_preset(args)

    exit_cfg = ExitConfig(
        adverse_ev_cents=args.adverse_ev_cents,
        spot_circuit_breaker_bp=args.spot_circuit_breaker_bp,
        profit_capture_enabled=args.profit_capture_enabled,
    )
    sizing_cfg = SizingConfig(
        fractional_kelly=args.fractional_kelly,
        max_contracts=args.max_contracts,
        fee_floor_max_contracts=args.fee_floor_max_contracts,
        fee_floor_off_center_band=args.fee_floor_off_center_band,
        fee_floor_min_edge_cents=args.fee_floor_min_edge_cents,
    )
    regime_cfg = RegimeConfig(mean_revert_min_divergence=args.regime_divergence_min)
    q_cal_min = args.q_cal_min
    q_cal_max = args.q_cal_max
    tradeable_regimes = (
        tuple(r.strip() for r in args.tradeable_regimes.split(",") if r.strip())
        if args.tradeable_regimes
        else None
    )

    if args.ungated:
        risk_cfg = RiskConfig(
            max_risk_per_window_dollars=10_000.0,
            per_ticker_entry_lock_enabled=False,
            oversell_hardening_enabled=False,
            max_entries_per_window=10_000,
        )
        veto_cfg = VetoConfig(
            min_venue_quorum=1,
            max_venue_disagreement_bp=1000.0,
            min_depth_multiplier=0.0,
            max_fragility_score=1000.0,
        )
        config = BacktestConfig(
            bankroll_dollars=args.bankroll,
            decision_interval_ms=args.decision_interval_ms,
            min_returns_for_decision=args.min_returns,
            risk_config=risk_cfg,
            sizing_config=sizing_cfg,
            veto_config=veto_cfg,
            exit_config=exit_cfg,
            regime_config=regime_cfg,
            enable_cooldowns=False,
            enable_error_tracker=False,
            ungated=True,
            min_edge_cents_override=args.min_edge_override,
            q_cal_min=q_cal_min,
            q_cal_max=q_cal_max,
            tradeable_regimes_override=tradeable_regimes,
        )
    else:
        config = BacktestConfig(
            bankroll_dollars=args.bankroll,
            decision_interval_ms=args.decision_interval_ms,
            min_returns_for_decision=args.min_returns,
            risk_config=RiskConfig(max_risk_per_window_dollars=args.window_cap_dollars),
            sizing_config=sizing_cfg,
            exit_config=exit_cfg,
            regime_config=regime_cfg,
            min_edge_cents_override=args.min_edge_override,
            q_cal_min=q_cal_min,
            q_cal_max=q_cal_max,
            tradeable_regimes_override=tradeable_regimes,
        )
    bt = Backtester(config=config, decision_log_path=args.decision_log)
    try:
        summary = bt.run_db(args.db, start_ms=args.start_ms, end_ms=args.end_ms)
    finally:
        bt.close()
    print(json.dumps(aggregate_summary_to_dict(summary), indent=2, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="engine-v2")
    subcommands = parser.add_subparsers(dest="command", required=True)

    init_parser = subcommands.add_parser("init-db", help="Initialize the SQLite warehouse")
    _add_db_arg(init_parser)
    init_parser.set_defaults(func=command_init_db)

    smoke_parser = subcommands.add_parser("smoke-replay", help="Insert and replay a sample slice")
    _add_db_arg(smoke_parser)
    smoke_parser.set_defaults(func=command_smoke_replay)

    continuity_parser = subcommands.add_parser(
        "continuity-report", help="Report sequence continuity"
    )
    _add_db_arg(continuity_parser)
    continuity_parser.add_argument("--persist", action="store_true", help="Persist summary row")
    continuity_parser.set_defaults(func=command_continuity_report)

    ddl_parser = subcommands.add_parser("print-ddl", help="Print exact SQLite DDL")
    ddl_parser.set_defaults(func=command_print_ddl)

    burnin_parser = subcommands.add_parser(
        "capture-burnin", help="Run paper-only market and spot capture burn-in"
    )
    _add_db_arg(burnin_parser)
    burnin_parser.add_argument("--hours", required=True, type=float, help="Burn-in duration")
    burnin_parser.add_argument(
        "--market-ticker", help="Optional KXBTC15M ticker override instead of discovery"
    )
    burnin_parser.set_defaults(func=command_capture_burnin)

    backtest_parser = subcommands.add_parser(
        "backtest",
        help="Run the event-driven backtester against a captured SQLite",
    )
    _add_db_arg(backtest_parser)
    backtest_parser.add_argument("--start-ms", type=int, default=0)
    backtest_parser.add_argument("--end-ms", type=int, default=None)
    backtest_parser.add_argument("--bankroll", type=float, default=200.0)
    backtest_parser.add_argument(
        "--decision-interval-ms",
        type=int,
        default=DEFAULT_DECISION_INTERVAL_MS,
        help="Decision cadence in event-time milliseconds (default 250ms). "
        "Latency-budget diagnostics showed 1000ms was marginal for "
        "microstructure half-lives.",
    )
    backtest_parser.add_argument("--min-returns", type=int, default=30)
    backtest_parser.add_argument("--window-cap-dollars", type=float, default=15.0)
    backtest_parser.add_argument("--fractional-kelly", type=float, default=0.20)
    backtest_parser.add_argument("--max-contracts", type=int, default=100)
    backtest_parser.add_argument(
        "--decision-log",
        type=Path,
        default=None,
        help="Optional JSONL path for per-decision audit log",
    )
    backtest_parser.add_argument(
        "--ungated",
        action="store_true",
        help="Disable all gates (regime, cooldown, ticker-lock, veto, $15/window cap) "
        "for counterfactual analysis. ALWAYS paper-only — does not unlock live orders.",
    )
    backtest_parser.add_argument(
        "--min-edge-override",
        type=float,
        default=None,
        help="Override per-window minimum edge threshold in cents (default uses "
        "WINDOW_POLICIES). Useful with --ungated to trade on any positive edge.",
    )
    backtest_parser.add_argument(
        "--adverse-ev-cents",
        type=float,
        default=-0.6,
        help="Adverse-revaluation exit threshold in cents (default -0.6). Set "
        "more negative (e.g. -3.0) to give trades room to breathe; counterfactual "
        "suggests current threshold strips winning trades.",
    )
    backtest_parser.add_argument(
        "--spot-circuit-breaker-bp",
        type=float,
        default=0.0,
        help="Exit when spot moves against the entry side by this many basis "
        "points from spot_at_entry. Default 0 disables the circuit breaker.",
    )
    backtest_parser.add_argument(
        "--profit-capture-enabled",
        dest="profit_capture_enabled",
        action="store_true",
        default=True,
        help="Enable the profit_capture early-exit branch (default ON).",
    )
    backtest_parser.add_argument(
        "--no-profit-capture",
        dest="profit_capture_enabled",
        action="store_false",
        help="Disable the profit_capture branch. Combined with a very-negative "
        "--adverse-ev-cents this yields hold-to-settlement-pure semantics.",
    )
    backtest_parser.add_argument(
        "--fee-floor-max-contracts",
        type=int,
        default=3,
        help="Apply the fee-floor veto when sized contracts <= N (default 3). "
        "At 1-3 contracts the rounded entry fee is a flat 2c regardless of P; "
        "small-size off-center entries are dominated by fee drag.",
    )
    backtest_parser.add_argument(
        "--fee-floor-off-center-band",
        type=float,
        default=0.10,
        help="Half-width of the near-center band around P=0.5 inside which the "
        "fee-floor veto does NOT apply (default 0.10).",
    )
    backtest_parser.add_argument(
        "--fee-floor-min-edge-cents",
        type=float,
        default=4.0,
        help="Minimum edge_net_cents required to allow a small-size off-center "
        "entry (default 4.0c). Set 0.0 to disable the veto entirely.",
    )
    backtest_parser.add_argument(
        "--q-cal-min",
        type=float,
        default=0.0,
        help="Minimum calibrated probability to allow an entry. Empirically "
        "0.10 vetoes extreme-confidence entries the model gets wrong.",
    )
    backtest_parser.add_argument(
        "--q-cal-max",
        type=float,
        default=1.0,
        help="Maximum calibrated probability to allow an entry. Empirically "
        "0.90 vetoes extreme-confidence entries the model gets wrong.",
    )
    backtest_parser.add_argument(
        "--regime-divergence-min",
        type=float,
        default=0.5,
        help="Minimum |divergence_logit| to classify as mean_revert_dislocation "
        "(default 0.5 — empirically too low; 4h burn-in showed median=4.95).",
    )
    backtest_parser.add_argument(
        "--tradeable-regimes",
        type=str,
        default=None,
        help="Comma-separated list of regime labels considered tradeable "
        "(default = all 3: info_absorption_trend,reflexive_squeeze,mean_revert_dislocation). "
        "Use this to test per-regime gating, e.g. 'info_absorption_trend' alone.",
    )
    backtest_parser.add_argument(
        "--preset",
        type=str,
        default=None,
        choices=sorted(_BACKTEST_PRESETS),
        help="Apply a named parameter preset. 'qcalveto_neverbail' is the "
        "empirically best config on the first 4h burn-in (+$0.31 net vs "
        "default −$1.15). 'regimefilter_neverbail' is the regime-classifier "
        "equivalent (identical P&L on the 4h slice). '*_safe' variants add a "
        "30bp spot circuit breaker. CLI args override preset.",
    )
    backtest_parser.set_defaults(func=command_backtest)

    db_stats_parser = subcommands.add_parser(
        "db-stats", help="Show row counts and health summary for a captured SQLite"
    )
    _add_db_arg(db_stats_parser)
    db_stats_parser.set_defaults(func=command_db_stats)

    settled_parser = subcommands.add_parser(
        "settled-markets",
        help="Scan captured SQLite for settled markets and report realized outcomes",
    )
    _add_db_arg(settled_parser)
    settled_parser.set_defaults(func=command_settled_markets)

    backfill_parser = subcommands.add_parser(
        "backfill-market-dim",
        help="Backfill market_dim rows for rolled markets using captured lifecycle events",
    )
    _add_db_arg(backfill_parser)
    backfill_parser.set_defaults(func=command_backfill_market_dim)

    counterfactual_parser = subcommands.add_parser(
        "hold-counterfactual",
        help="Compute hold-to-settlement P&L for each entry vs the engine's "
        "actual exit, to evaluate exit-rule quality",
    )
    _add_db_arg(counterfactual_parser)
    counterfactual_parser.add_argument(
        "--decision-log",
        type=Path,
        required=True,
        help="Decision log JSONL emitted by `backtest --decision-log`",
    )
    counterfactual_parser.set_defaults(func=command_hold_counterfactual)

    per_market_parser = subcommands.add_parser(
        "per-market-report",
        help="Per-market breakdown of a decision log: entries, exits, hold time, "
        "exit modes, settlement, and hold-to-settlement counterfactual delta",
    )
    _add_db_arg(per_market_parser)
    per_market_parser.add_argument(
        "--decision-log",
        type=Path,
        required=True,
        help="Decision log JSONL emitted by `backtest --decision-log`",
    )
    per_market_parser.set_defaults(func=command_per_market_report)

    patterns_parser = subcommands.add_parser(
        "trade-patterns",
        help="Detect quick_flip / chase / flip_flop patterns in a decision log",
    )
    patterns_parser.add_argument(
        "--decision-log",
        type=Path,
        required=True,
        help="Decision log JSONL emitted by `backtest --decision-log`",
    )
    patterns_parser.add_argument(
        "--quick-flip-max-s",
        type=float,
        default=30.0,
        help="Hold time threshold below which an exit counts as a quick_flip "
        "(default 30s; v2 empirical avg hold is 9s — anything <30s on a 15-min "
        "market is suspicious)",
    )
    patterns_parser.add_argument(
        "--chase-window-s",
        type=float,
        default=60.0,
        help="Window for counting same-side re-entries as a chase pattern",
    )
    patterns_parser.add_argument(
        "--chase-min-entries",
        type=int,
        default=2,
        help="Number of same-side entries within window to count as a chase",
    )
    patterns_parser.add_argument(
        "--flip-flop-window-s",
        type=float,
        default=60.0,
        help="Window for counting opposite-side re-entries as a flip_flop",
    )
    patterns_parser.set_defaults(func=command_trade_patterns)

    divergence_parser = subcommands.add_parser(
        "divergence-stats",
        help="Distribution of divergence_logit values from a decision log; "
        "use to recalibrate regime classifier thresholds against real data",
    )
    divergence_parser.add_argument(
        "--decision-log",
        type=Path,
        required=True,
        help="Decision log JSONL emitted by `backtest --decision-log`",
    )
    divergence_parser.set_defaults(func=command_divergence_stats)

    compare_parser = subcommands.add_parser(
        "compare-gates",
        help="Run gated and ungated backtests on the same DB and report whether "
        "selectivity gates were profitable on this slice",
    )
    _add_db_arg(compare_parser)
    compare_parser.add_argument("--bankroll", type=float, default=200.0)
    compare_parser.add_argument(
        "--decision-interval-ms",
        type=int,
        default=DEFAULT_DECISION_INTERVAL_MS,
    )
    compare_parser.add_argument("--min-returns", type=int, default=30)
    compare_parser.add_argument("--window-cap-dollars", type=float, default=15.0)
    compare_parser.add_argument("--fractional-kelly", type=float, default=0.20)
    compare_parser.add_argument("--max-contracts", type=int, default=100)
    compare_parser.add_argument(
        "--ungated-min-edge",
        type=float,
        default=0.1,
        help="Minimum net edge in cents required for the ungated mode to fire "
        "an entry (default 0.1 — essentially any positive edge).",
    )
    compare_parser.set_defaults(func=command_compare_gates)

    wf_parser = subcommands.add_parser(
        "walk-forward",
        help="Run rolling walk-forward backtest windows over a captured SQLite",
    )
    _add_db_arg(wf_parser)
    wf_parser.add_argument("--train-days", type=int, default=5)
    wf_parser.add_argument("--validate-days", type=int, default=1)
    wf_parser.add_argument("--test-days", type=int, default=1)
    wf_parser.add_argument("--step-days", type=int, default=1)
    wf_parser.add_argument("--bankroll", type=float, default=200.0)
    wf_parser.add_argument(
        "--decision-interval-ms",
        type=int,
        default=DEFAULT_DECISION_INTERVAL_MS,
    )
    wf_parser.add_argument("--min-returns", type=int, default=30)
    wf_parser.add_argument("--window-cap-dollars", type=float, default=15.0)
    wf_parser.add_argument("--fractional-kelly", type=float, default=0.20)
    wf_parser.add_argument("--max-contracts", type=int, default=100)
    wf_parser.set_defaults(func=command_walk_forward)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
