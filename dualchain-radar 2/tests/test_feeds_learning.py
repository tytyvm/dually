import dataclasses
import json
import time

import httpx
import pytest

from radar.domain import Config
from radar.feeds import parse_pool, GeckoFeed, run_collector
from radar.engine import Engine
from radar.cli import demo_events
from radar.learning import train, examples, predict


def pool(chain="solana"):
    return {"attributes": {"address": "PoolABC", "name": "TEST / SOL",
        "base_token_price_usd": "0.001", "reserve_in_usd": "40000", "market_cap_usd": None,
        "fdv_usd": "200000", "pool_created_at": "2026-09-12T00:00:00Z",
        "volume_usd": {"m5": "5000"}, "transactions": {"m5": {"buys": 30, "sells": 10}},
        "price_change_percentage": {"m5": "25"}},
        "relationships": {"base_token": {"data": {"id": chain + "_TokenABC"}},
                          "dex": {"data": {"id": "example"}}}}


@pytest.mark.parametrize("chain", ["solana", "robinhood"])
def test_parser_keeps_missing_data_unknown(chain):
    s = parse_pool(chain, pool(chain), 1800000000)
    assert s.market_cap is None
    assert s.fdv == 200000
    assert s.buyers_m5 is None
    assert s.source_ts is None
    assert s.change_m5 == .25
    assert s.token == ("TokenABC" if chain == "solana" else "tokenabc")


def test_rate_limit_is_not_empty_success():
    client = httpx.Client(base_url="https://example.test", transport=httpx.MockTransport(
        lambda r: httpx.Response(429, headers={"Retry-After": "1"})))
    feed = GeckoFeed(client)
    with pytest.raises(RuntimeError, match="rate limited"):
        feed.get("/test")
    with pytest.raises(RuntimeError, match="backoff"):
        feed.get("/test")


def test_chain_failure_does_not_skip_other_chain():
    class Feed:
        calls = []
        def discover(self, chain):
            self.calls.append(chain)
            if chain == "solana":
                raise RuntimeError("offline")
            return []
        def close(self):
            pass
    feed = Feed()
    e = Engine(":memory:", Config())
    health = run_collector(e, Config(), once=True, feed=feed)
    assert feed.calls == ["solana", "robinhood"]
    assert health["solana"]["ok"] is False
    assert health["robinhood"]["ok"] is True


def test_pinned_pool_is_fetched_after_discovery_disappears():
    e = Engine(":memory:", Config())
    for s in list(demo_events())[:16]:
        e.step(s)
    assert e.state()["positions"]
    class Feed:
        calls = []
        def discover(self, ch):
            return []
        def pools(self, ch, pools):
            self.calls.append((ch, pools))
            return []
        def close(self):
            pass
    feed = Feed()
    run_collector(e, Config(), once=True, feed=feed)
    assert len(feed.calls) == 2
    assert all(len(pools) == 2 for ch, pools in feed.calls)


def test_training_refuses_small_sample(tmp_path):
    with pytest.raises(ValueError, match="100"):
        train(list(demo_events()), Config(), str(tmp_path / "model.json"))


def test_training_purges_outcome_overlap_and_writes_reproducible_model(tmp_path):
    events = []
    for batch in range(40):
        for s in demo_events():
            events.append(dataclasses.replace(s, ts=s.ts + batch * 1000,
                          created_at=s.created_at + batch * 1000, token=s.token + str(batch),
                          pool=s.pool + str(batch)))
    out = tmp_path / "model.json"
    metrics = train(events, Config(), str(out))
    model = json.loads(out.read_text())
    assert model["trained_through"] < metrics["holdout_start"]
    assert metrics["train"] >= 100
    assert metrics["holdout"] >= 20
    assert model["calibrated"] is False
    rows, _ = examples(events, Config())
    assert 0 <= predict(model, rows[0]["x"]) <= 1


def test_gapped_future_is_censored_not_a_win():
    events = list(demo_events())
    events = [dataclasses.replace(s, ts=s.ts + (1000 if s.ts >= 1800000120 else 0)) for s in events]
    rows, censored = examples(events, Config())
    assert censored > 0
    assert not rows

