from __future__ import annotations

from kalshi_btc_engine_v2.ecology.toxicity import (
    ToxicityConfig,
    update_toxicity,
    vpin_from_history,
)


def test_no_buckets_yet_returns_none():
    state, vpin = update_toxicity(
        None,
        buy_contracts=10.0,
        sell_contracts=10.0,
        config=ToxicityConfig(bucket_size_contracts=100.0),
    )
    assert vpin is None
    assert state.pending_buy + state.pending_sell == 20.0


def test_balanced_flow_yields_zero_vpin():
    cfg = ToxicityConfig(bucket_size_contracts=50.0, recent_buckets=5)
    state = None
    last = None
    for _ in range(20):
        state, last = update_toxicity(state, buy_contracts=10.0, sell_contracts=10.0, config=cfg)
    assert last is not None
    assert last < 0.05


def test_one_sided_flow_yields_high_vpin():
    cfg = ToxicityConfig(bucket_size_contracts=50.0, recent_buckets=5)
    state = None
    last = None
    for _ in range(20):
        state, last = update_toxicity(state, buy_contracts=20.0, sell_contracts=0.0, config=cfg)
    assert last is not None
    assert last > 0.95


def test_vpin_from_history_helper():
    flow = [(50.0, 50.0)] * 5
    assert (vpin_from_history(flow, config=ToxicityConfig(bucket_size_contracts=100.0))) is not None
    one_sided = [(100.0, 0.0)] * 5
    out = vpin_from_history(one_sided, config=ToxicityConfig(bucket_size_contracts=100.0))
    assert out is not None
    assert out > 0.95
