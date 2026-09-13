from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .domain import Config, Snapshot
from . import execution, strategy


class Engine:
    """One atomic snapshot transaction: observations, orders, balances and audit log.

    No private keys. Pending orders execute only on a later observation, at that
    observation's price. Open positions remain visible when a feed disappears.
    """
    def __init__(self, path: str, config: Config, model=None):
        self.config = config
        self.model = model
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY CHECK(id=1), value TEXT);
            CREATE TABLE IF NOT EXISTS snapshots(id TEXT PRIMARY KEY, ts INTEGER, data TEXT);
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, ts INTEGER, kind TEXT, data TEXT);
            CREATE INDEX IF NOT EXISTS snapshot_time ON snapshots(ts);
        """)
        self.identity = {"config": config.fingerprint(), "model": model.get("id") if model else None}
        with self.conn:
            row = self.conn.execute("SELECT value FROM state WHERE id=1").fetchone()
            if row:
                if json.loads(row[0])["identity"] != self.identity:
                    raise ValueError("Database config/model differs; use a new database for a new experiment")
            else:
                initial = {
                    "identity": self.identity, "clock": 0, "paused": False,
                    "accounts": {ch: {"cash": config.initial_cash_per_chain, "day": -1,
                                      "day_start": config.initial_cash_per_chain,
                                      "peak": config.initial_cash_per_chain, "halted": False,
                                      "day_halted": False} for ch in config.chains},
                    "positions": {}, "pending": {}, "last": {}, "history": {}, "cooldown": {},
                }
                self.conn.execute("INSERT INTO state VALUES(1,?)", (json.dumps(initial),))

    def close(self):
        self.conn.close()

    def state(self):
        return json.loads(self.conn.execute("SELECT value FROM state WHERE id=1").fetchone()[0])

    def _save(self, state):
        self.conn.execute("UPDATE state SET value=? WHERE id=1", (json.dumps(state),))

    def pause(self, enabled=True):
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            st = self.state()
            st["paused"] = enabled
            if enabled:
                st["pending"].clear()
            self._save(st)
            self._log(st["clock"], "pause" if enabled else "resume", {})

    def _log(self, ts, kind, data):
        self.conn.execute("INSERT INTO audit(ts,kind,data) VALUES(?,?,?)", (ts, kind, json.dumps(data)))

    def events(self, limit=100):
        return [{"id": r[0], "ts": r[1], "kind": r[2], **json.loads(r[3])}
                for r in self.conn.execute("SELECT id,ts,kind,data FROM audit ORDER BY id DESC LIMIT ?", (limit,))]

    def equity(self, st, chain, now):
        value = st["accounts"][chain]["cash"]
        stale = 0
        for key, p in st["positions"].items():
            if p["chain"] != chain:
                continue
            raw = st["last"].get(key)
            if not raw or now - raw["ts"] > self.config.max_gap_s:
                stale += 1
                continue # risk valuation is zero, NOT a fabricated realized loss or fill
            try:
                q = execution.sell(Snapshot(**raw), p["units"], self.config)
                value += max(0, q.amount - q.fee)
            except ValueError:
                stale += 1
        return value, stale

    def _risk(self, st, now):
        c = self.config
        for ch, a in st["accounts"].items():
            eq, stale = self.equity(st, ch, now)
            day = now // 86400
            if a["day"] != day:
                a["day"] = day
                a["day_start"] = eq
                a["day_halted"] = False
            a["peak"] = max(a["peak"], eq)
            if eq <= a["day_start"] * (1 - c.daily_loss_fraction):
                a["day_halted"] = True
            if eq <= a["peak"] * (1 - c.max_drawdown_fraction):
                a["halted"] = True # latched; never automatically reset after a drawdown
            a["stale_positions"] = stale
            a["equity"] = eq

    def heartbeat(self, now):
        """Called even if all upstream requests fail; blocks entries against stale positions."""
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            st = self.state()
            self._risk(st, now)
            for k in list(st["pending"]):
                if st["pending"][k]["expires"] < now:
                    del st["pending"][k]
                    self._log(now, "cancel", {"key": k, "reason": "expired_without_feed"})
            self._save(st)

    def _entry_allowed(self, st, chain):
        a = st["accounts"][chain]
        return not (st["paused"] or a["halted"] or a["day_halted"] or a.get("stale_positions"))

    def step(self, s: Snapshot, now: int | None = None):
        c = self.config
        if s.chain not in c.chains:
            raise ValueError("chain not enabled")
        now = s.ts if now is None else now
        if now < s.ts or now - s.ts > c.max_gap_s:
            return "stale_or_future"
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            st = self.state()
            if self.conn.execute("SELECT 1 FROM snapshots WHERE id=?", (s.event_id,)).fetchone():
                return "duplicate"
            if s.ts < st["clock"]:
                return "out_of_order"
            if s.source_ts is not None and now - s.source_ts > c.max_gap_s:
                return "stale_source"
            st["clock"] = s.ts
            self.conn.execute("INSERT INTO snapshots VALUES(?,?,?)", (s.event_id, s.ts, json.dumps(s.data())))
            key = s.key
            p = st["positions"].get(key)
            pending = st["pending"].get(key)
            pinned = p or pending
            if pinned and pinned["pool"] != s.pool:
                self._save(st)
                return "other_pool"
            history = st["history"].get(key, [])
            if history and (history[-1]["pool"] != s.pool or s.ts - history[-1]["ts"] > c.max_gap_s):
                history = []
            history = [r for r in history if r["ts"] >= s.ts - max(c.warmup_s * 5, 600)]
            history.append(s.data())
            st["history"][key] = history[-120:]
            st["last"][key] = s.data()
            self._risk(st, s.ts)

            if p:
                self._position(st, s, p)
            elif pending:
                if s.ts > pending["expires"] or not self._entry_allowed(st, s.chain):
                    del st["pending"][key]
                    self._log(s.ts, "cancel", {"key": key, "reason": "expired_or_risk"})
                elif s.ts >= pending["due"]:
                    # Recheck hard conditions and price at arrival; no hindsight backfill.
                    reason = strategy.eligibility(s, c)
                    q = execution.buy(s, c.order_usd, c) if reason is None else None
                    a = st["accounts"][s.chain]
                    if q and q.impact <= c.max_price_impact and a["cash"] >= c.order_usd + q.fee:
                        a["cash"] -= c.order_usd + q.fee
                        st["positions"][key] = {
                            "chain": s.chain, "token": s.token, "pool": s.pool,
                            "opened": s.ts, "units": q.amount, "cost": c.order_usd + q.fee,
                            "entry_liquidity": s.liquidity, "peak_multiple": 1.0,
                            "strategy": c.strategy, "exit_due": None, "exit_reason": None,
                        }
                        self._log(s.ts, "buy", {"key": key, "cost": c.order_usd + q.fee,
                                  "units": q.amount, "impact": q.impact,
                                  "quote_type": "constant_product_estimate", "signal_ts": pending["signal_ts"]})
                    else:
                        self._log(s.ts, "cancel", {"key": key, "reason": reason or "cost_or_impact"})
                    del st["pending"][key]
            else:
                ok, reason, f = strategy.signal(s, st["history"][key], c)
                prediction = None
                if ok and self.model:
                    from .learning import predict
                    if s.ts <= self.model["trained_through"]:
                        ok, reason = False, "model_future_leakage"
                    elif self.model["config_fingerprint"] != c.fingerprint():
                        raise ValueError("model/config mismatch")
                    elif s.chain not in self.model["chains"]:
                        ok, reason = False, "model_chain_unseen"
                    else:
                        prediction = predict(self.model, f)
                        if prediction < self.model["threshold"]:
                            ok, reason = False, "model_filter"
                slots = sum(p["chain"] == s.chain for p in st["positions"].values())
                slots += sum(p["chain"] == s.chain for p in st["pending"].values())
                if ok:
                    if not self._entry_allowed(st, s.chain):
                        ok, reason = False, "risk_halt"
                    elif slots >= c.max_positions_per_chain:
                        ok, reason = False, "position_limit"
                    elif s.ts < st["cooldown"].get(key, 0):
                        ok, reason = False, "cooldown"
                    elif st["accounts"][s.chain]["cash"] < (sum(p["chain"] == s.chain for p in st["pending"].values()) + 1) * (c.order_usd + c.tx_fee(s.chain)):
                        ok, reason = False, "cash_reserved"
                self._log(s.ts, "decision", {"key": key, "enter": ok, "reason": reason,
                          "features": f, "prediction": prediction,
                          "cap_basis": "market_cap" if s.market_cap is not None else "fdv_proxy_or_unknown"})
                if ok:
                    st["pending"][key] = {"chain": s.chain, "pool": s.pool, "signal_ts": s.ts,
                                           "due": s.ts + c.latency_s, "expires": s.ts + c.pending_ttl_s}
                    self._log(s.ts, "entry_pending", {"key": key})
            self._risk(st, s.ts)
            # Bound ephemeral history; retain all snapshots in the append-only database.
            active = set(st["positions"]) | set(st["pending"])
            for k in list(st["last"]):
                if k not in active and s.ts - st["last"][k]["ts"] > 3600:
                    st["last"].pop(k, None)
                    st["history"].pop(k, None)
            for k in list(st["pending"]):
                if st["pending"][k]["expires"] < s.ts:
                    del st["pending"][k]
            self._save(st)
        return "processed"

    def _position(self, st, s, p):
        c, key = self.config, s.key
        try:
            q = execution.sell(s, p["units"], c)
        except ValueError:
            p["exit_reason"] = "liquidity_unavailable"
            p["exit_due"] = p["exit_due"] or s.ts + c.latency_s
            self._log(s.ts, "exit_blocked", {"key": key, "reason": "no_liquidity"})
            return
        if p["exit_due"] is not None and s.ts >= p["exit_due"]:
            a = st["accounts"][s.chain]
            if a["cash"] + q.amount < q.fee:
                self._log(s.ts, "exit_blocked", {"key": key, "reason": "fee_balance"})
                return
            proceeds = q.amount - q.fee
            a["cash"] += proceeds
            self._log(s.ts, "sell", {"key": key, "cost": p["cost"], "proceeds": proceeds,
                      "pnl": proceeds - p["cost"], "multiple": proceeds / p["cost"],
                      "reason": p["exit_reason"], "impact": q.impact,
                      "quote_type": "constant_product_estimate", "opened": p["opened"]})
            del st["positions"][key]
            st["cooldown"][key] = s.ts + c.cooldown_s
            return
        multiple = (q.amount - q.fee) / p["cost"]
        p["peak_multiple"] = max(p["peak_multiple"], multiple)
        reason = None
        if multiple <= 1 - c.stop_loss:
            reason = "stop"
        elif s.liquidity < p["entry_liquidity"] * c.liquidity_exit_fraction:
            reason = "liquidity_drop"
        elif multiple >= c.take_profit_multiple:
            reason = "target"
        elif p["peak_multiple"] >= c.trail_activation and multiple <= p["peak_multiple"] * (1 - c.trail_drawdown):
            reason = "trailing"
        elif s.ts - p["opened"] >= c.max_hold_s:
            reason = "time"
        elif st["paused"]:
            reason = "manual_pause"
        if reason and p["exit_due"] is None:
            p["exit_reason"], p["exit_due"] = reason, s.ts + c.latency_s
            self._log(s.ts, "exit_pending", {"key": key, "reason": reason, "observed_multiple": multiple})

    def report(self, now=None):
        st = self.state()
        now = st["clock"] if now is None else now
        rows = [json.loads(r[0]) for r in self.conn.execute("SELECT data FROM audit WHERE kind='sell'")]
        accounts = {}
        for ch, a in st["accounts"].items():
            eq, stale = self.equity(st, ch, now)
            closed = [r for r in rows if r["key"].startswith(ch + ":")]
            accounts[ch] = {**a, "estimated_equity": round(eq, 4), "stale_positions": stale,
                            "closed_trades": len(closed), "realized_pnl": round(sum(r["pnl"] for r in closed), 4),
                            "win_rate": sum(r["pnl"] > 0 for r in closed) / len(closed) if closed else None,
                            "net_without_best_trade": round(sum(r["pnl"] for r in closed) - max([r["pnl"] for r in closed] or [0]), 4)}
        return {"mode": "paper_only", "strategy": self.config.strategy, "clock": st["clock"],
                "paused": st["paused"], "accounts": accounts, "positions": st["positions"],
                "pending": st["pending"], "events": self.events(30),
                "warning": "Estimated constant-product fills; no live quotes, signing, or proven edge. Stale positions marked zero for risk only."}
