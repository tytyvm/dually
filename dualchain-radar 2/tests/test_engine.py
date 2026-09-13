import dataclasses
import json
import math

import pytest

from radar.domain import Config, Snapshot
from radar.engine import Engine
from radar.cli import demo_events, write_report
from radar import execution, strategy


def event(i=0, **kwargs):
    base = Snapshot(chain="solana", token="token", pool="pool", ts=1800000000 + i * 30,
        price=.001 * (1 + i * .05), liquidity=50000, market_cap=100000, fdv=100000,
        created_at=1799999000, volume_m5=5000, buys_m5=40, sells_m5=10,
        buyers_m5=15 + i * 3, change_m5=.2)
    return dataclasses.replace(base, **kwargs)


def opened(e):
    for i in range(4):
        e.step(event(i))
    return e.state()["positions"]["solana:token"]


def test_two_chain_demo_has_winners_and_losers():
    e = Engine(":memory:", Config())
    for s in demo_events():
        e.step(s)
    for a in e.report()["accounts"].values():
        assert a["closed_trades"] == 2
        assert a["win_rate"] == .5
    assert not e.state()["positions"]


def test_entry_waits_for_later_observation_and_price():
    e = Engine(":memory:", Config())
    for i in range(3):
        e.step(event(i))
    assert not e.state()["positions"]
    assert e.state()["pending"]
    p = opened(e)
    expected = execution.buy(event(3), 25, Config())
    assert p["units"] == expected.amount


def test_duplicate_survives_restart(tmp_path):
    path = str(tmp_path / "test.db")
    e = Engine(path, Config())
    p = opened(e)
    cash = e.state()["accounts"]["solana"]["cash"]
    e.close()
    e = Engine(path, Config())
    assert e.step(event(3)) == "duplicate"
    assert e.state()["accounts"]["solana"]["cash"] == cash
    assert e.state()["positions"]["solana:token"] == p


def test_late_snapshot_does_not_rewind():
    e = Engine(":memory:", Config())
    e.step(event(3))
    assert e.step(event(2)) == "out_of_order"
    assert e.state()["clock"] == event(3).ts


def test_stale_and_future_rejected():
    e = Engine(":memory:", Config())
    assert e.step(event(), now=event().ts + 1000) == "stale_or_future"
    assert e.step(event(), now=event().ts - 1) == "stale_or_future"
    assert e.step(event(source_ts=event().ts - 500)) == "stale_source"


def test_stop_fills_at_later_worse_price():
    e = Engine(":memory:", Config())
    p = opened(e)
    e.step(event(4, price=.0008))
    assert e.state()["positions"]["solana:token"]["exit_reason"] == "stop"
    s = event(5, price=.0005)
    e.step(s)
    fill = [v for v in e.events() if v["kind"] == "sell"][0]
    q = execution.sell(s, p["units"], Config())
    assert fill["proceeds"] == q.amount - q.fee
    assert fill["pnl"] < -10


def test_zero_liquidity_not_fake_sale():
    e = Engine(":memory:", Config())
    opened(e)
    e.step(event(4, liquidity=0, price=0))
    assert e.state()["positions"]
    assert e.report()["accounts"]["solana"]["closed_trades"] == 0
    assert e.report()["accounts"]["solana"]["stale_positions"] == 1


def test_stale_position_blocks_entry_and_is_not_realized():
    e = Engine(":memory:", Config())
    opened(e)
    e.heartbeat(event(3).ts + 1000)
    a = e.state()["accounts"]["solana"]
    assert a["stale_positions"] == 1
    assert not e._entry_allowed(e.state(), "solana")
    assert e.report(now=event(3).ts + 1000)["accounts"]["solana"]["realized_pnl"] == 0


def test_same_token_on_different_chains_is_separate():
    e = Engine(":memory:", Config())
    for i in range(4):
        for ch in ("solana", "robinhood"):
            e.step(event(i, chain=ch))
    assert len(e.state()["positions"]) == 2


def test_pause_cancels_buy_and_does_not_clear_risk_halt():
    e = Engine(":memory:", Config())
    for i in range(3):
        e.step(event(i))
    e.pause()
    assert not e.state()["pending"]
    st = e.state()
    st["accounts"]["solana"]["halted"] = True
    with e.conn:
        e._save(st)
    e.pause(False)
    assert not e._entry_allowed(e.state(), "solana")


def test_expired_pending_cannot_fill():
    e = Engine(":memory:", Config())
    for i in range(3):
        e.step(event(i))
    e.step(event(20))
    assert not e.state()["positions"]


def test_position_limit_includes_pending():
    e = Engine(":memory:", Config(max_positions_per_chain=1))
    for i in range(3):
        for token in ("a", "b"):
            e.step(event(i, token=token, pool=token))
    assert len(e.state()["pending"]) == 1


def test_entry_impact_blocks_large_order():
    c = Config(order_usd=900, max_price_impact=.001)
    e = Engine(":memory:", c)
    for i in range(4):
        e.step(event(i))
    assert not e.state()["positions"]


def test_other_pool_does_not_price_position():
    e = Engine(":memory:", Config())
    opened(e)
    assert e.step(event(4, pool="wrong", price=100)) == "other_pool"
    assert e.state()["last"]["solana:token"]["pool"] == "pool"


@pytest.mark.parametrize("changes,reason", [
    ({"market_cap": None}, "unknown_market_cap"),
    ({"market_cap": 500000}, "market_cap"),
    ({"buyers_m5": None}, "unknown_buyers"),
    ({"liquidity": 10}, "liquidity"),
    ({"created_at": None}, "pool_age"),
])
def test_missing_or_ineligible_never_enters(changes, reason):
    assert strategy.eligibility(event(**changes), Config()) == reason


def test_fdv_is_explicit_opt_in():
    assert strategy.eligibility(event(market_cap=None), Config(allow_fdv_proxy=True)) is None


@pytest.mark.parametrize("changes", [{"price": float('nan')}, {"liquidity": -1}, {"chain": "fake"}])
def test_bad_snapshot(changes):
    with pytest.raises(ValueError):
        event(**changes)


@pytest.mark.parametrize("changes", [{"order_usd": -1}, {"stop_loss": 1.0}, {"latency_s": 0},
                                     {"poll_seconds": 1}, {"chains": ("solana", "solana")}])
def test_bad_config(changes):
    with pytest.raises(ValueError):
        Config(**changes)


def test_config_change_requires_new_database(tmp_path):
    path = str(tmp_path / "a.db")
    Engine(path, Config()).close()
    with pytest.raises(ValueError, match="differs"):
        Engine(path, Config(order_usd=30))


def test_live_execution_fails_closed():
    with pytest.raises(RuntimeError, match="not implemented"):
        execution.sign_and_send()


def test_html_escapes_external_token_text(tmp_path):
    e = Engine(":memory:", Config())
    e.step(event(token="<script>alert(1)</script>"))
    out = tmp_path / "report.html"
    write_report(e.report(), out)
    assert "<script>" not in out.read_text()


def test_pullback_requires_recovery():
    c = Config(strategy="pullback")
    prices = [.001, .0014, .0011, .0012]
    hist = [event(i, price=p).data() for i, p in enumerate(prices)]
    assert strategy.signal(Snapshot(**hist[-1]), hist, c)[0]
    assert not strategy.signal(Snapshot(**hist[-2]), hist[:-1], c)[0]


def test_account_cash_equals_start_plus_closed_pnl():
    e = Engine(":memory:", Config())
    for s in demo_events():
        e.step(s)
    for a in e.report()["accounts"].values():
        assert a["cash"] == pytest.approx(1000 + a["realized_pnl"], abs=.0001)


def test_database_transaction_rolls_back_on_failure(monkeypatch):
    e = Engine(":memory:", Config())
    original = e.state()
    def fail(*args):
        raise RuntimeError("injected")
    monkeypatch.setattr(e, "_save", fail)
    with pytest.raises(RuntimeError):
        e.step(event())
    assert e.state() == original
    assert e.conn.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 0


def test_heartbeat_expires_pending_without_a_feed():
    e = Engine(":memory:", Config())
    for i in range(3):
        e.step(event(i))
    assert e.state()["pending"]
    e.heartbeat(event(3).ts + 1000)
    assert not e.state()["pending"]


def test_daily_halt_latches_until_next_utc_day():
    e = Engine(":memory:", Config(order_usd=200))
    opened(e)
    e.step(event(4, price=.0005))
    assert e.state()["accounts"]["solana"]["day_halted"]
    e.step(event(5, price=.00115))
    assert e.state()["accounts"]["solana"]["day_halted"]
    st = e.state()
    assert not st["accounts"]["solana"]["halted"]
    e.heartbeat((event(5).ts // 86400 + 1) * 86400)
    assert not e.state()["accounts"]["solana"]["day_halted"]


def test_drawdown_halt_stays_after_recovery():
    e = Engine(":memory:", Config(order_usd=300))
    opened(e)
    e.step(event(4, price=.0001))
    assert e.state()["accounts"]["solana"]["halted"]
    e.step(event(5, price=.0012))
    assert e.state()["accounts"]["solana"]["halted"]


def test_time_exit_and_trailing_exit():
    e = Engine(":memory:", Config(max_hold_s=30))
    opened(e)
    e.step(event(4))
    assert e.state()["positions"]["solana:token"]["exit_reason"] == "time"
    e2 = Engine(":memory:", Config(take_profit_multiple=5))
    opened(e2)
    e2.step(event(4, price=.002))
    e2.step(event(5, price=.0014))
    assert e2.state()["positions"]["solana:token"]["exit_reason"] == "trailing"


def test_buyback_does_not_rewrite_previous_profit():
    e = Engine(":memory:", Config(cooldown_s=0))
    opened(e)
    e.step(event(4, price=.003))
    e.step(event(5, price=.003))
    first = [v for v in e.events() if v["kind"] == "sell"][0]
    e.step(event(6, price=.0032))
    e.step(event(7, price=.0033))
    assert e.state()["positions"]
    assert [v for v in e.events() if v["kind"] == "sell"][0] == first


def test_model_cannot_filter_before_training_cutoff():
    e = Engine(":memory:", Config(), model={"id": "test", "trained_through": 1900000000})
    for i in range(4):
        e.step(event(i))
    assert not e.state()["positions"]
    assert any(v.get("reason") == "model_future_leakage" for v in e.events())
