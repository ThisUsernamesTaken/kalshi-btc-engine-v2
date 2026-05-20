"""Veto shadow monitor: log what the model-veto would decide on a running live trader.

Tails one or more live-trader log files (live_v5_unified_trades.jsonl,
live_ta_trades.jsonl) and for every `*_trigger` event computes the
model-veto decision. Outputs to a separate log so we can audit the veto
without touching the live trader.

Usage:
    python scripts/live/veto_shadow_monitor.py \\
        --log-in  data/live_v5_unified_trades.jsonl \\
        --log-out data/veto_shadow.jsonl

Safe to run alongside any live trader: read-only on inputs, append-only on
output. No Kalshi API calls.
"""
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path
from typing import Iterator

# Locate the model_veto module
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / 'src'))

import bisect, math
from kalshi_btc_engine_v2.model_veto import veto_decision, model_action_decision  # noqa: E402

TRIGGER_KINDS = {
    'early_trigger', 'earlier_moderate_trigger',
    'late_trigger', 't30_sniper_trigger',
}

DEFAULT_THRESHOLD_C = 5
DEFAULT_FLIP_THRESHOLD_C = 30
DEFAULT_FLIP_SLIP_C = 2

# BTC history extracted from prior events in the log. (ts_ms, btc_price)
_BTC_HISTORY: list[tuple[int, float]] = []


def _add_btc_observation(e: dict) -> None:
    ts = e.get('ts_ms') or e.get('ts_minute_ms') or e.get('decided_at_ts_ms')
    bp = (e.get('btc_now') or e.get('btc_price') or e.get('spot_close')
          or e.get('cycle_open_price') or e.get('btc_price_at_entry'))
    if ts and bp:
        try: _BTC_HISTORY.append((int(ts), float(bp)))
        except (TypeError, ValueError): pass


def _vol_ann_from_history(end_ts_ms: int, window_ms: int = 300_000) -> float | None:
    """Adaptive annualized vol from BTC observations up to end_ts_ms."""
    if len(_BTC_HISTORY) < 5: return None
    ts_arr = [t for t, _ in _BTC_HISTORY]
    end_idx = bisect.bisect_right(ts_arr, end_ts_ms) - 1
    if end_idx < 5: return None
    start_ts = end_ts_ms - window_ms
    start_idx = bisect.bisect_left(ts_arr, start_ts)
    prices = [_BTC_HISTORY[i][1] for i in range(start_idx, end_idx + 1)
              if _BTC_HISTORY[i][1] > 0]
    if len(prices) < 3: return None
    lrets = [math.log(prices[i+1]/prices[i]) for i in range(len(prices)-1)]
    if not lrets: return None
    m = sum(lrets) / len(lrets)
    v = sum((r - m)**2 for r in lrets) / max(1, len(lrets) - 1)
    sigma_per_sample = math.sqrt(v)
    # average inter-arrival gives per-sec conversion
    dt_ms = (_BTC_HISTORY[end_idx][0] - _BTC_HISTORY[start_idx][0]) / max(1, end_idx - start_idx)
    if dt_ms <= 0: return None
    sigma_per_sec = sigma_per_sample / math.sqrt(dt_ms / 1000)
    sigma_ann = sigma_per_sec * math.sqrt(365 * 24 * 3600)
    return max(0.10, min(3.0, sigma_ann))


def _trigger_to_veto_args(event: dict) -> dict | None:
    """Map a trigger event to veto_decision kwargs. Returns None if
    required fields are missing."""
    k = event.get('kind', '')
    # All triggers carry: btc_now or btc_price, strike, secs_to_close,
    # entry_ask_cents (or fav_ask_cents), side_to_buy (or entry_side / fav_side)
    spot = event.get('btc_now') or event.get('btc_price')
    strike = event.get('strike')
    secs_to_close = event.get('secs_to_close')
    # Engine side & price: use the actual ask we'd pay (not limit_cents
    # which is "max willing to pay" -- typically 99 in v5_unified).
    side = event.get('side_to_buy') or event.get('entry_side') or event.get('fav_side')
    # Pick ask based on side
    if side == 'yes':
        price = (event.get('entry_ask_cents')
                 or event.get('yes_ask_cents')
                 or event.get('fav_ask_cents'))
    elif side == 'no':
        price = (event.get('entry_ask_cents')
                 or event.get('no_ask_cents')
                 or event.get('fav_ask_cents'))
    else:
        price = (event.get('entry_ask_cents')
                 or event.get('fav_ask_cents')
                 or event.get('limit_cents'))
    # Volatility: prefer adaptive from BTC history (matches live trader's
    # behavior with best_effort fallback). Fall back to rv_5m from trigger,
    # then to 0.5 default.
    sigma: float | None = None
    ts = event.get('ts_ms')
    if ts:
        sigma = _vol_ann_from_history(int(ts))
    if sigma is None:
        rv5m = event.get('rv_5m')
        if rv5m is not None and rv5m > 0:
            _factor = math.sqrt(365 * 24 * 3600) / (math.sqrt(60) * 100)  # = 7.25
            sigma = max(0.10, min(3.0, rv5m * _factor))
    if sigma is None:
        sigma = 0.5
    if not all([spot, strike, secs_to_close, side, price is not None]):
        return None
    # Best-of-both: include yes_ask / no_ask so we can compute flip details too.
    yes_ask = event.get('yes_ask_cents')
    no_ask  = event.get('no_ask_cents')
    return dict(
        spot_btc=float(spot),
        strike=float(strike),
        seconds_to_close=float(secs_to_close),
        sigma_annualized=float(sigma),
        engine_side=str(side),
        engine_price_cents=int(price),
        skip_threshold_cents=DEFAULT_THRESHOLD_C,
        flip_threshold_cents=DEFAULT_FLIP_THRESHOLD_C,
        flip_slip_cents=DEFAULT_FLIP_SLIP_C,
        yes_ask_cents=int(yes_ask) if yes_ask is not None else None,
        no_ask_cents=int(no_ask) if no_ask is not None else None,
    )


def tail_jsonl(path: Path, poll_interval_s: float = 1.0) -> Iterator[dict]:
    """Yield JSON-decoded events as they're appended. Survives file rotation."""
    pos = 0
    if path.exists():
        # Start at current EOF (don't re-process history)
        pos = path.stat().st_size
    while True:
        try:
            cur_size = path.stat().st_size if path.exists() else pos
        except OSError:
            time.sleep(poll_interval_s); continue
        if cur_size < pos:
            # Rotated / truncated
            pos = 0
        if cur_size > pos:
            with path.open('rb') as f:
                f.seek(pos)
                chunk = f.read()
                pos = cur_size
            for line in chunk.decode('utf-8', errors='ignore').splitlines():
                if not line.strip(): continue
                try: yield json.loads(line)
                except json.JSONDecodeError: continue
        else:
            time.sleep(poll_interval_s)


def process_event(event: dict, out_fp) -> None:
    # Always accumulate BTC observations for adaptive RV
    _add_btc_observation(event)
    k = event.get('kind', '')
    if k not in TRIGGER_KINDS: return
    args = _trigger_to_veto_args(event)
    if args is None:
        out_fp.write(json.dumps({
            'kind': 'veto_shadow_skip',
            'ts_ms': event.get('ts_ms'),
            'ticker': event.get('ticker'),
            'trigger_kind': k,
            'reason': 'missing_required_fields',
        }, default=str) + '\n')
        out_fp.flush()
        return
    try:
        action, p_model, reason, flip = model_action_decision(**args)
    except Exception as e:
        out_fp.write(json.dumps({
            'kind': 'veto_shadow_error',
            'ts_ms': event.get('ts_ms'),
            'ticker': event.get('ticker'),
            'trigger_kind': k,
            'error': str(e)[:200],
        }, default=str) + '\n')
        out_fp.flush()
        return
    out_fp.write(json.dumps({
        'kind': 'veto_shadow_decision',
        'ts_ms': event.get('ts_ms'),
        'ticker': event.get('ticker'),
        'trigger_kind': k,
        'engine_side': args['engine_side'],
        'engine_price_cents': args['engine_price_cents'],
        'sigma_used': args['sigma_annualized'],
        'p_model_yes': p_model,
        'action': action,
        'would_skip': action in ('SKIP', 'FLIP'),  # back-compat
        'flip_details': flip,
        'reason': reason,
        'skip_threshold_cents': DEFAULT_THRESHOLD_C,
        'flip_threshold_cents': DEFAULT_FLIP_THRESHOLD_C,
    }, default=str) + '\n')
    out_fp.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--log-in', type=str, required=True,
                    help='Live trader log to tail (jsonl)')
    ap.add_argument('--log-out', type=str, required=True,
                    help='Where to write veto_shadow events (jsonl)')
    ap.add_argument('--poll-interval', type=float, default=1.0,
                    help='Tail poll interval in seconds (default 1.0)')
    ap.add_argument('--from-start', action='store_true',
                    help='Process the entire input log from the beginning, then exit.'
                         ' Useful for backtesting the shadow on historical data.')
    args = ap.parse_args()

    in_path = Path(args.log_in)
    out_path = Path(args.log_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Startup announcement
    with out_path.open('a', encoding='utf-8') as out_fp:
        out_fp.write(json.dumps({
            'kind': 'veto_shadow_startup',
            'ts_ms': int(time.time() * 1000),
            'log_in': str(in_path),
            'log_out': str(out_path),
            'threshold_cents': DEFAULT_THRESHOLD_C,
            'from_start': args.from_start,
        }, default=str) + '\n')
        out_fp.flush()

        if args.from_start:
            if not in_path.exists():
                print(f'ERROR: input log not found: {in_path}', file=sys.stderr)
                return 1
            n_seen = n_decided = n_would_skip = 0
            with in_path.open(encoding='utf-8', errors='ignore') as f:
                for line in f:
                    try: e = json.loads(line)
                    except: continue
                    n_seen += 1
                    if e.get('kind') in TRIGGER_KINDS:
                        process_event(e, out_fp)
                        n_decided += 1
            print(f'Processed {n_seen} lines; {n_decided} triggers evaluated')
        else:
            # Live tail
            print(f'Tailing {in_path} -> {out_path} (Ctrl+C to stop)')
            try:
                for e in tail_jsonl(in_path, args.poll_interval):
                    process_event(e, out_fp)
            except KeyboardInterrupt:
                pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
