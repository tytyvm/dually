"""Small regularized logistic baseline, trained only on chronological snapshots.

Research score, NOT calibrated probability. Entire training labels finish before
holdout begins (purge). No automatic model promotion. Targets mean estimated net
exit proceeds / entry cost reaches target before stop within the observation window.
"""
import hashlib
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

from . import execution, strategy
from .domain import Config

FEATURES = ("momentum_m5", "observed_return", "volume_liquidity", "buy_fraction",
            "buyer_growth", "drawdown", "age_hours", "liquidity_log")


def sigmoid(x):
    return 1 / (1 + math.exp(-max(-35, min(35, x))))


def predict(model, values):
    xs = [(values[k] - m) / s for k, m, s in zip(FEATURES, model["means"], model["scales"])]
    return sigmoid(model["bias"] + sum(w * x for w, x in zip(model["weights"], xs)))


def examples(snapshots, c: Config):
    groups = defaultdict(list)
    for s in snapshots:
        groups[(s.chain, s.token, s.pool)].append(s)
    rows, censored = [], 0
    for items in groups.values():
        items.sort(key=lambda s: s.ts)
        history = []
        next_sample = 0
        for i, s in enumerate(items):
            if history and s.ts - history[-1]["ts"] > c.max_gap_s:
                history = []
            history = [h for h in history if h["ts"] >= s.ts - max(600, c.warmup_s * 5)]
            history.append(s.data())
            ok, _, f = strategy.signal(s, history, c)
            if not ok or s.ts < next_sample:
                continue
            future = items[i + 1:]
            entry = next((v for v in future if v.ts >= s.ts + c.latency_s), None)
            if entry is None or entry.ts > s.ts + c.pending_ttl_s or strategy.eligibility(entry, c):
                censored += 1
                continue
            q = execution.buy(entry, c.order_usd, c)
            if q.impact > c.max_price_impact:
                continue
            cost = c.order_usd + q.fee
            last, label, label_end = entry.ts, None, None
            for v in future:
                if v.ts <= entry.ts:
                    continue
                if v.ts - last > c.max_gap_s:
                    break
                last = v.ts
                if v.price <= 0 or v.liquidity <= 0:
                    label, label_end = 0, v.ts
                    break
                out = execution.sell(v, q.amount, c)
                multiple = (out.amount - out.fee) / cost
                if multiple <= 1 - c.stop_loss:
                    label, label_end = 0, v.ts
                    break
                if v.ts - entry.ts >= c.max_hold_s:
                    label, label_end = 0, v.ts
                    break
                if multiple >= c.take_profit_multiple:
                    # Need a later observation for execution, not the signal's ideal price.
                    landing = next((z for z in future if z.ts >= v.ts + c.latency_s), None)
                    if landing and landing.ts - v.ts <= c.max_gap_s and landing.price > 0 and landing.liquidity > 0:
                        exit_q = execution.sell(landing, q.amount, c)
                        label = int((exit_q.amount - exit_q.fee) / cost >= c.take_profit_multiple)
                        label_end = landing.ts
                    break
            if label is None:
                censored += 1
                continue
            rows.append({"ts": s.ts, "end": label_end, "x": f, "y": label, "chain": s.chain,
                         "token": s.token})
            next_sample = entry.ts + c.max_hold_s # reduce overlapping decisions on same token
    return sorted(rows, key=lambda r: r["ts"]), censored


def train(snapshots, c: Config, out: str):
    rows, censored = examples(snapshots, c)
    if len(rows) < 100:
        raise ValueError(f"Need >=100 labeled setups; found {len(rows)} ({censored} censored). Collect more real data; do not train on the demo.")
    split = rows[int(len(rows) * .75)]["ts"]
    training = [r for r in rows if r["end"] < split]
    testing = [r for r in rows if r["ts"] >= split]
    if len(training) < 50 or len(testing) < 20 or len({r["y"] for r in training}) < 2:
        raise ValueError("Insufficient purged training/holdout data or only one outcome class")
    xs = [[r["x"][k] for k in FEATURES] for r in training]
    means = [statistics.mean(v) for v in zip(*xs)]
    scales = [max(statistics.pstdev(v), 1e-6) for v in zip(*xs)]
    xs = [[(v - m) / s for v, m, s in zip(row, means, scales)] for row in xs]
    w, b = [0.] * len(FEATURES), 0.
    for _ in range(600):
        grad, gb = [0.] * len(w), 0.
        for row, example in zip(xs, training):
            err = sigmoid(b + sum(a * v for a, v in zip(w, row))) - example["y"]
            gb += err
            for j, v in enumerate(row):
                grad[j] += err * v
        n = len(training)
        w = [a - .03 * (g / n + .01 * a) for a, g in zip(w, grad)]
        b -= .03 * gb / n
    model = {"features": list(FEATURES), "weights": w, "bias": b, "means": means, "scales": scales,
             "trained_through": max(r["end"] for r in training), "threshold": .5,
             "config_fingerprint": c.fingerprint(), "chains": sorted({r["chain"] for r in training}),
             "target": "estimated_target_before_stop_or_timeout", "calibrated": False}
    ps = [predict(model, r["x"]) for r in testing]
    selected = [r for r, p in zip(testing, ps) if p >= .5]
    base = statistics.mean(r["y"] for r in training)
    model["metrics"] = {"train": len(training), "holdout": len(testing), "censored": censored,
                        "brier": statistics.mean((p - r["y"]) ** 2 for r, p in zip(testing, ps)),
                        "baseline_brier": statistics.mean((base - r["y"]) ** 2 for r in testing),
                        "holdout_positive_rate": statistics.mean(r["y"] for r in testing),
                        "selected": len(selected),
                        "precision": statistics.mean(r["y"] for r in selected) if selected else None,
                        "holdout_start": split, "holdout_end": max(r["end"] for r in testing)}
    model["id"] = hashlib.sha256(json.dumps(model, sort_keys=True).encode()).hexdigest()
    Path(out).write_text(json.dumps(model, indent=2))
    return model["metrics"]

