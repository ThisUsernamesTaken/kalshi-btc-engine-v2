"""Shared helpers for the strategy-discovery analysis (2026-05-18).

Loads live trade logs from all engines, extracts entry-time features
(joining BTC price history derived from in-log events), and provides
the bucketing + edge math used across the analysis scripts.

Field-name resolution is schema-tolerant: handles v5_unified, v5_old,
live_ta (Pine Script), and live_ta_v2 settle-event shapes.
"""
from __future__ import annotations
import json, bisect, math
from collections import defaultdict
from pathlib import Path
from typing import Iterable

# Realistic execution cost: 2c over mid per entry. Empirically calibrated to
# the gap between reported and actual fill on live_v5_unified.
SLIP = 2

# Local data paths. The kalshi-btc-engine-v2/data/ is a symlink to D:\Trading\...
DATA_DIR = Path(__file__).resolve().parent.parent / 'data'

LIVE_TRADE_LOGS = [
    ('v5_unified',  'live_v5_unified_trades.jsonl'),
    ('v5_old',      'live_v5_trades.jsonl'),
    ('live_ta',     'live_ta_trades.jsonl'),
    ('live_ta_v2',  'live_ta_v2_trades.jsonl'),
]


def load_log(path: Path, label: str) -> list[dict]:
    """Load one trade log, return list of settle-records enriched with
    entry-time features (signed BTC velocity over 60s and 180s, etc.).

    BTC price history is derived from in-log events that carry a price
    field (btc_price / btc_now / spot_close / cycle_open_price)."""
    ev_by_ticker = defaultdict(list)
    btc_history: list[tuple[int, float]] = []
    if not path.exists():
        return []
    with path.open(encoding='utf-8', errors='ignore') as f:
        for line in f:
            try: e = json.loads(line)
            except json.JSONDecodeError: continue
            t = e.get('ticker')
            if t: ev_by_ticker[t].append(e)
            bp = (e.get('btc_price') or e.get('btc_now')
                  or e.get('spot_close') or e.get('cycle_open_price'))
            ts = e.get('ts_ms') or e.get('ts_minute_ms') or e.get('decided_at_ts_ms')
            if bp and ts: btc_history.append((ts, bp))
    btc_history.sort()
    ts_arr = [t for t, _ in btc_history]
    def btc_at(ts):
        idx = bisect.bisect_right(ts_arr, ts) - 1
        return btc_history[idx][1] if (ts_arr and idx >= 0) else None
    def vel(ts, win_ms):
        n = btc_at(ts); p = btc_at(ts - win_ms)
        return None if (n is None or p is None) else (n - p)

    rows = []
    for evs in ev_by_ticker.values():
        for s in evs:
            if s.get('kind') != 'settle':
                continue
            side = s.get('side')
            entry = s.get('entry_price_cents')
            if not side or entry is None:
                continue
            ctr = s.get('contracts', 0)
            gross = s.get('gross_cents', 0)
            result = (s.get('result') or s.get('outcome')
                      or (side if gross > 0 else ('no' if side == 'yes' else 'yes')))
            won = (result == side)
            entry_ts = (s.get('decided_at_ts_ms')
                        or s.get('entered_at_ms')
                        or s.get('ts_ms')
                        or 0)
            leg = (s.get('leg') or 'PINE').lower()
            trig = next((e for e in evs if e.get('kind') == f'{leg}_trigger'), None)
            v60 = vel(entry_ts, 60_000)
            v180 = vel(entry_ts, 180_000)
            sv60  = (v60  if side == 'yes' else -v60)  if v60  is not None else None
            sv180 = (v180 if side == 'yes' else -v180) if v180 is not None else None
            rows.append(dict(
                src=label, ticker=s['ticker'],
                leg=s.get('leg') or 'PINE',
                tier=s.get('tier') or s.get('tier_name'),
                side=side, entry=entry, ctr=ctr, net_c=s.get('net_cents', 0),
                gross_c=gross, won=won,
                sv60=sv60, sv180=sv180,
                gap_bps=(trig or {}).get('gap_bps'),
                cushion=(trig or {}).get('cushion_usd'),
                rv5m=(trig or {}).get('rv_5m'),
                v60_eng=(trig or {}).get('v60_usd'),
                stc=(trig or {}).get('secs_to_close'),
                sfo=(trig or {}).get('secs_from_open'),
                fav_ask=(trig or {}).get('fav_ask_cents'),
                balance=(trig or {}).get('balance_cents'),
                confidence=s.get('confidence'),
                bar=s.get('decided_at_bar'),
                entry_ts=entry_ts,
            ))
    return rows


def load_all() -> list[dict]:
    rows = []
    for label, fname in LIVE_TRADE_LOGS:
        rows.extend(load_log(DATA_DIR / fname, label))
    return rows


# ---- Bucketing ---------------------------------------------------------
def sv60_bucket(v) -> str:
    if v is None: return 'NA'
    if v <= -30:  return 'rev'      # BTC reverted >$30 against our side
    if v <=  30:  return 'flat'
    if v <=  80:  return 'mom_lo'   # momentum into our side
    return 'mom_hi'                 # strong momentum exhaustion zone

def entry_bucket(e: int) -> str:
    if e <= 15: return 'lottery'    # near-resolution underdog tail
    if e <= 70: return 'underdog'
    if e <= 84: return 'mid'
    if e <= 91: return 'cursed'     # mid-conviction stripe: bimodal losses
    return 'high'                   # near-resolved consensus

def pine_bar_segment(bar) -> str:
    if bar is None: return 'unk'
    if bar <= 4: return 'early'
    if bar <= 6: return 'sweet'     # the bars-5/6 cycle sweet spot
    return 'late'

def bucket_key(r: dict) -> tuple:
    if r['src'] == 'live_ta':
        return ('pine', pine_bar_segment(r.get('bar')),
                entry_bucket(r['entry']), sv60_bucket(r.get('sv60')))
    leg = r['leg']
    leg_g = ('EM' if leg == 'EARLIER_MODERATE'
             else 'LATE' if leg == 'LATE'
             else 'T30' if leg == 'T30_SNIPER'
             else leg)
    return ('v5', leg_g, entry_bucket(r['entry']), sv60_bucket(r.get('sv60')))


# ---- Edge math ---------------------------------------------------------
def edge_per_ct(WR: float, avg_entry: float, slip: int = SLIP) -> float:
    """Realistic edge per contract (cents) for taking the engine's side."""
    return 100 * WR - avg_entry - slip

def inv_edge_per_ct(WR: float, avg_entry: float, slip: int = SLIP) -> float:
    """Realistic edge per contract (cents) for taking the OPPOSITE side."""
    return 100 * (1 - WR) - (100 - avg_entry) - slip

def kelly_fraction(WR: float, avg_entry: float, slip: int = SLIP) -> float:
    """Kelly fraction of bankroll for a positive-EV binary bet."""
    cost = avg_entry + slip
    if cost <= 0: return 0.0
    b = (100 - cost) / cost
    if b <= 0: return 0.0
    f = (b * WR - (1 - WR)) / b
    return max(0.0, f)


def build_bucket_info(rows: Iterable[dict], min_n: int = 3) -> dict:
    """Aggregate trades by bucket, decide KEEP / INVERT / SKIP, and
    annotate each bucket with edge, Kelly fraction, and n."""
    agg = defaultdict(lambda: {'n': 0, 'w': 0, 'entries': []})
    for r in rows:
        k = bucket_key(r)
        agg[k]['n'] += 1
        agg[k]['w'] += r['won']
        agg[k]['entries'].append(r['entry'])
    info = {}
    for k, v in agg.items():
        n, w = v['n'], v['w']
        if n < min_n: continue
        WR = w / n
        ae = sum(v['entries']) / n
        eo = edge_per_ct(WR, ae)
        ei = inv_edge_per_ct(WR, ae)
        if eo >= ei and eo > 0:
            info[k] = dict(action='KEEP', WR_engine=WR, WR_effective=WR,
                           avg_entry=ae, edge=eo, kelly=kelly_fraction(WR, ae),
                           n=n)
        elif ei > 0:
            info[k] = dict(action='INVERT', WR_engine=WR, WR_effective=1 - WR,
                           avg_entry=ae, edge=ei,
                           kelly=kelly_fraction(1 - WR, 100 - ae), n=n)
    return info
