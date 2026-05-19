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

from kalshi_btc_engine_v2.model_veto import veto_decision  # noqa: E402

TRIGGER_KINDS = {
    'early_trigger', 'earlier_moderate_trigger',
    'late_trigger', 't30_sniper_trigger',
}

DEFAULT_THRESHOLD_C = 5


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
    # Volatility: use rv_5m from trigger if available, else 0.5 default
    # NOTE: the engine's rv_5m is sigma_per_sec_log * sqrt(60) * 100, NOT
    # annualized. We must apply the same conversion the live trader does.
    import math as _math
    rv5m = event.get('rv_5m')
    if rv5m is None or rv5m <= 0:
        sigma = 0.5
    else:
        _factor = _math.sqrt(365 * 24 * 3600) / (_math.sqrt(60) * 100)  # = 7.25
        sigma = max(0.10, min(3.0, rv5m * _factor))
    if not all([spot, strike, secs_to_close, side, price is not None]):
        return None
    return dict(
        spot_btc=float(spot),
        strike=float(strike),
        seconds_to_close=float(secs_to_close),
        sigma_annualized=float(sigma),
        engine_side=str(side),
        engine_price_cents=int(price),
        threshold_cents=DEFAULT_THRESHOLD_C,
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
        skip, p_model, reason = veto_decision(**args)
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
        'would_skip': skip,
        'reason': reason,
        'threshold_cents': DEFAULT_THRESHOLD_C,
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
