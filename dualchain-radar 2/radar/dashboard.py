"""Single-process dashboard supervisor. One collector thread, persistent experiments."""
import dataclasses
import json
import os
import threading
import time
import uuid
from pathlib import Path

from .domain import Config
from .engine import Engine
from .feeds import run_collector

EDITABLE = {"strategy", "order_usd", "max_market_cap", "min_liquidity", "take_profit_multiple",
            "stop_loss", "max_hold_s", "max_positions_per_chain", "allow_fdv_proxy"}


class Dashboard:
    def __init__(self, directory, config, collector=run_collector):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.manifest = self.directory / "dashboard.json"
        self.lock = threading.RLock()
        self.thread = None
        self.stop = threading.Event()
        self.collector = collector
        self.error = None
        if self.manifest.exists():
            self.meta = json.loads(self.manifest.read_text())
        else:
            self.meta = {"active": "paper", "collect_requested": False, "health": {},
                         "experiments": {"paper": {"config": dataclasses.asdict(config),
                                                   "created": int(time.time())}}}
            engine = self.engine()
            engine.pause(True)
            engine.close()
            self.save()

    @property
    def config(self):
        c = dict(self.meta["experiments"][self.meta["active"]]["config"])
        c["chains"] = tuple(c["chains"])
        return Config(**c)

    def engine(self, experiment=None):
        key = experiment or self.meta["active"]
        cfg = dict(self.meta["experiments"][key]["config"])
        cfg["chains"] = tuple(cfg["chains"])
        return Engine(str(self.directory / (key + ".db")), Config(**cfg))

    def save(self):
        tmp = self.manifest.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.meta, indent=2))
        os.replace(tmp, self.manifest)

    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, resume=True):
        with self.lock:
            if self.running() and self.stop.is_set():
                raise ValueError("Collector is stopping; wait a moment and try again")
            if resume:
                e = self.engine()
                try:
                    e.pause(False)
                finally:
                    e.close()
            self.meta["collect_requested"] = True
            self.save()
            if self.running():
                return
            self.error = None
            self.stop.clear()
            self.thread = threading.Thread(target=self._worker, name="paper-collector", daemon=True)
            self.thread.start()

    def _worker(self):
        e = None
        try:
            e = self.engine()
            self.collector(e, self.config, stop_event=self.stop, on_health=self.record_health)
        except Exception as exc:
            with self.lock:
                self.error = type(exc).__name__
        finally:
            if e:
                e.close()

    def record_health(self, health):
        with self.lock:
            self.meta["health"] = health
            self.save()
            e = self.engine()
            try:
                now = int(time.time())
                r = e.report(now=now)
                e.conn.execute("CREATE TABLE IF NOT EXISTS dashboard_equity(ts INTEGER, chain TEXT, value REAL, PRIMARY KEY(ts,chain))")
                with e.conn:
                    for ch, a in r["accounts"].items():
                        e.conn.execute("INSERT OR REPLACE INTO dashboard_equity VALUES(?,?,?)", (now, ch, a["estimated_equity"]))
            finally:
                e.close()

    def pause(self):
        with self.lock:
            e = self.engine()
            try:
                e.pause(True)
            finally:
                e.close()

    def stop_collection(self):
        with self.lock:
            e = self.engine()
            try:
                if e.state()["positions"] or e.state()["pending"]:
                    raise ValueError("Pause and exit positions before stopping collection")
                e.pause(True)
            finally:
                e.close()
            self.meta["collect_requested"] = False
            self.save()
            self.stop.set()

    def shutdown(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=20)

    def new_experiment(self, changes):
        with self.lock:
            if self.running():
                raise ValueError("Stop collection before starting a new experiment")
            if set(changes) - EDITABLE:
                raise ValueError("Unknown setting")
            e = self.engine()
            try:
                if e.state()["positions"] or e.state()["pending"]:
                    raise ValueError("Close existing positions first; previous records will be preserved")
            finally:
                e.close()
            if not isinstance(changes.get("allow_fdv_proxy", False), bool):
                raise ValueError("FDV option must be true or false")
            for name, value in changes.items():
                if name not in ("strategy", "allow_fdv_proxy") and (isinstance(value, bool) or not isinstance(value, (int,float))):
                    raise ValueError("Settings must be numeric")
            for name in ("max_hold_s", "max_positions_per_chain"):
                if name in changes and (not isinstance(changes[name], int) or changes[name] < 1):
                    raise ValueError(f"{name} must be a positive whole number")
            cfg = dataclasses.replace(self.config, **changes)
            if cfg.max_market_cap > 500000:
                raise ValueError("This dashboard is limited to entries below $500,000")
            key = "paper-" + uuid.uuid4().hex[:12]
            self.meta["experiments"][key] = {"config": dataclasses.asdict(cfg), "created": int(time.time())}
            self.meta["active"] = key
            self.meta["health"] = {}
            self.meta["collect_requested"] = False
            self.save()
            e = self.engine()
            e.pause(True)
            e.close()
            return key

    def status(self, experiment=None):
        with self.lock:
            if experiment and experiment not in self.meta["experiments"]:
                raise ValueError("Unknown experiment")
            e = self.engine(experiment)
            try:
                now = int(time.time())
                r = e.report(now=now)
                st = e.state()
                for key, p in r["positions"].items():
                    latest = st["last"].get(key, {})
                    p["symbol"] = latest.get("symbol", p["token"])
                    p["last_seen"] = latest.get("ts")
                    p["stale"] = not latest or now - latest["ts"] > e.config.max_gap_s
                r["events"] = e.events(150)
                r["observation_count"] = e.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
                r["recent_tokens"] = list(st["last"].values())[-80:]
                r["equity_history"] = []
                if e.conn.execute("SELECT 1 FROM sqlite_master WHERE name='dashboard_equity'").fetchone():
                    r["equity_history"] = [{"ts": a, "chain": b, "value": v} for a,b,v in e.conn.execute(
                        "SELECT ts,chain,value FROM dashboard_equity ORDER BY ts DESC LIMIT 600")][::-1]
                r.update({"demo": False, "active_experiment": self.meta["active"],
                    "viewed_experiment": experiment or self.meta["active"],
                    "experiments": [{"id": k, "strategy": v["config"]["strategy"], "created": v["created"]}
                                    for k,v in self.meta["experiments"].items()],
                    "settings": {k: getattr(e.config,k) for k in sorted(EDITABLE)},
                    "collector": {"running": self.running(), "stopping": self.stop.is_set() and self.running(),
                                  "error": self.error, "health": self.meta["health"]},
                    "server_time": now})
                return r
            finally:
                e.close()

    def demo(self):
        from .cli import demo_events
        e = Engine(":memory:", Config())
        history = []
        try:
            events = list(demo_events())
            base = events[-1]
            for i in range(4):
                for chain in ("solana", "robinhood"):
                    events.append(dataclasses.replace(base, chain=chain, token="DEMO-open", pool="DEMO-open-pool",
                        ts=base.ts+30*(i+1), price=.002*(1+.05*i), buyers_m5=20+i*4,
                        symbol="OPEN-DEMO", market_cap=120000))
            for s in events:
                e.step(s)
                r = e.report()
                history += [{"ts": s.ts, "chain": ch, "value": a["estimated_equity"]} for ch,a in r["accounts"].items()]
            r = e.report()
            for k,p in r["positions"].items():
                p.update({"symbol":"OPEN-DEMO", "last_seen":r["clock"], "stale":False})
            r.update({"demo": True, "observation_count": len(events), "events": e.events(150),
                      "recent_tokens": list(e.state()["last"].values()), "equity_history": history,
                      "collector": {"running": False, "health": {}, "error": None},
                      "server_time": r["clock"], "settings": {k:getattr(e.config,k) for k in EDITABLE},
                      "experiments": [], "active_experiment": "demo", "viewed_experiment": "demo"})
            return r
        finally:
            e.close()
