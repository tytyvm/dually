"""Deterministic baselines. Thresholds are hypotheses, not profitability claims."""
from .domain import Config, Snapshot


def eligibility(s: Snapshot, c: Config):
    if s.price <= 0:
        return "missing_price"
    cap = s.market_cap
    if cap is None and c.allow_fdv_proxy:
        cap = s.fdv
    if cap is None:
        return "unknown_market_cap"
    if not c.min_market_cap <= cap < c.max_market_cap:
        return "market_cap"
    if s.created_at is None or not c.min_age_s <= s.ts - s.created_at <= c.max_age_s:
        return "pool_age"
    if s.liquidity < c.min_liquidity:
        return "liquidity"
    if s.source_ts is not None and s.ts - s.source_ts > c.max_gap_s:
        return "stale_source"
    if s.volume_m5 < c.min_volume_m5:
        return "volume"
    if s.buyers_m5 is None:
        return "unknown_buyers"
    if s.buyers_m5 < c.min_buyers_m5:
        return "buyers"
    if s.buys_m5 / max(1, s.buys_m5 + s.sells_m5) < c.min_buy_fraction:
        return "buy_fraction"
    return None


def features(s: Snapshot, history: list[dict]):
    old = history[0] if history else s.data()
    high = max([r["price"] for r in history] + [s.price])
    return {
        "momentum_m5": s.change_m5,
        "observed_return": s.price / old["price"] - 1 if old["price"] else 0,
        "volume_liquidity": s.volume_m5 / max(s.liquidity, 1),
        "buy_fraction": s.buys_m5 / max(1, s.buys_m5 + s.sells_m5),
        "buyer_growth": (s.buyers_m5 or 0) / max(1, old.get("buyers_m5") or 0) - 1,
        "drawdown": s.price / high - 1 if high else 0,
        "age_hours": (s.ts - s.created_at) / 3600 if s.created_at else 0,
        "liquidity_log": __import__("math").log1p(s.liquidity),
    }


def signal(s: Snapshot, history: list[dict], c: Config):
    reason = eligibility(s, c)
    if reason:
        return False, reason, features(s, history)
    rows = [r for r in history if r["pool"] == s.pool and r["price"] > 0]
    f = features(s, rows)
    if len(rows) < c.min_observations or rows[-1]["ts"] - rows[0]["ts"] < c.warmup_s:
        return False, "warming_up", f
    if any(b["ts"] - a["ts"] > c.max_gap_s for a, b in zip(rows, rows[1:])):
        return False, "history_gap", f
    if c.strategy == "momentum":
        ok = (c.min_momentum_m5 <= s.change_m5 <= c.max_momentum_m5
              and f["observed_return"] >= .03 and f["buyer_growth"] > 0
              and s.price > rows[-2]["price"])
    else:
        previous_peak = max(r["price"] for r in rows[:-1])
        ok = (previous_peak >= rows[0]["price"] * 1.2
              and -.35 <= f["drawdown"] <= -.08
              and s.price >= rows[-2]["price"] * 1.03)
    return ok, c.strategy if ok else "no_setup", f

