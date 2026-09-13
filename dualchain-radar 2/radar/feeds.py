"""Public REST adapter for both chains. Snapshot research, not a low-latency tape.

No wallet provenance, holder clustering, contract security or pre-migration
bonding-curve coverage is implied by this adapter. Unknown fields stay unknown.
"""
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx

from .domain import Snapshot, Config

log = logging.getLogger(__name__)
BASE = "https://api.geckoterminal.com/api/v2"


def number(value, default=None):
    try:
        return float(value) if value is not None else default
    except (ValueError, TypeError):
        return default


def parse_pool(chain, row, observed_at):
    a, rel = row["attributes"], row.get("relationships", {})
    token = rel.get("base_token", {}).get("data", {}).get("id", "")
    prefix = chain + "_"
    if not token.startswith(prefix):
        raise ValueError("missing base-token identity")
    token = token[len(prefix):]
    created = a.get("pool_created_at")
    created = int(datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()) if created else None
    tx = (a.get("transactions") or {}).get("m5") or {}
    # Do not replace unique buyer count with transaction count.
    buyers = tx.get("buyers")
    price = number(a.get("base_token_price_usd"))
    reserve = number(a.get("reserve_in_usd"))
    if price is None or reserve is None:
        raise ValueError("missing price or reserve")
    return Snapshot(
        chain=chain, token=token, pool=a["address"], ts=observed_at,
        price=price, liquidity=reserve,
        market_cap=number(a.get("market_cap_usd")), fdv=number(a.get("fdv_usd")),
        created_at=created, volume_m5=number((a.get("volume_usd") or {}).get("m5"), 0),
        buys_m5=int(tx.get("buys") or 0), sells_m5=int(tx.get("sells") or 0),
        buyers_m5=int(buyers) if buyers is not None else None,
        change_m5=number((a.get("price_change_percentage") or {}).get("m5"), 0) / 100,
        symbol=(a.get("name") or "?").split(" / ")[0], source="geckoterminal_rest",
        source_ts=None, # provider does not specify exact observation time; receipt time is not chain time
        dex=rel.get("dex", {}).get("data", {}).get("id", "unknown"),
    )


class GeckoFeed:
    def __init__(self, client=None):
        self.client = client or httpx.Client(base_url=BASE, timeout=15, headers={
            "Accept": "application/json;version=20230302", "User-Agent": "dualchain-radar/0.1"})
        self.next_request = 0
        self.backoff_until = 0

    def close(self):
        self.client.close()

    def get(self, path):
        if time.monotonic() < self.backoff_until:
            raise RuntimeError("provider backoff active")
        wait = self.next_request - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.next_request = time.monotonic() + 3.1 # shared limit across both chains
        response = self.client.get(path)
        if response.status_code == 429:
            retry = number(response.headers.get("retry-after"), 60)
            self.backoff_until = time.monotonic() + min(max(retry, 30), 300)
            raise RuntimeError("provider rate limited; no price substituted")
        response.raise_for_status()
        data = response.json().get("data")
        if not isinstance(data, list):
            raise ValueError("unexpected provider response")
        return data

    def discover(self, chain):
        rows = self.get(f"/networks/{chain}/new_pools?page=1")
        rows += self.get(f"/networks/{chain}/trending_pools?duration=5m")
        return self.parse(chain, rows)

    def pools(self, chain, pools):
        if not pools:
            return []
        ids = ",".join(quote(p, safe="") for p in pools)
        return self.parse(chain, self.get(f"/networks/{chain}/pools/multi/{ids}"))

    def parse(self, chain, rows):
        observed = int(time.time())
        out = {}
        for row in rows:
            try:
                s = parse_pool(chain, row, observed)
                out[s.pool] = s
            except (KeyError, ValueError, TypeError) as e:
                log.warning("Skipped invalid %s pool: %s", chain, type(e).__name__)
        return list(out.values())


def read_jsonl(path):
    with open(path) as f:
        for n, line in enumerate(f, 1):
            if line.strip():
                try:
                    yield Snapshot(**json.loads(line))
                except Exception as e:
                    raise ValueError(f"Invalid snapshot at line {n}: {e}") from e


def run_collector(engine, c: Config, once=False, duration=0, feed=None, stop_event=None, on_health=None):
    feed = feed or GeckoFeed()
    watch = {ch: {} for ch in c.chains}
    discovered_at = {ch: 0 for ch in c.chains}
    start = time.monotonic()
    health = {}
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                return health
            tick = time.monotonic()
            for ch in c.chains:
                if stop_event is not None and stop_event.is_set():
                    return health
                try:
                    discovery = []
                    if tick - discovered_at[ch] >= c.discovery_seconds or not watch[ch]:
                        discovery = feed.discover(ch)
                        discovered_at[ch] = tick
                        for s in discovery:
                            watch[ch][s.pool] = s
                    state = engine.state()
                    pinned = {p["pool"] for p in list(state["positions"].values()) + list(state["pending"].values())
                              if p["chain"] == ch}
                    # Pinned positions are fetched even after they leave discovery and exceed cap.
                    candidates = sorted(watch[ch].values(), key=lambda s: -s.volume_m5)
                    selected, tokens = [], set()
                    for s in candidates:
                        if s.pool in pinned:
                            tokens.add(s.token)
                    for s in candidates:
                        cap = s.market_cap if s.market_cap is not None else (s.fdv if c.allow_fdv_proxy else None)
                        if s.token in tokens or cap is None or not c.min_market_cap <= cap < c.max_market_cap:
                            continue
                        tokens.add(s.token)
                        selected.append(s.pool)
                        if len(selected) >= c.watch_pools_per_chain:
                            break
                    pool_ids = list(dict.fromkeys(sorted(pinned) + selected))
                    if not pool_ids:
                        # Record rejected discovery candidates too; unknown market cap remains explicit.
                        observations = discovery
                    else:
                        observations = []
                        for i in range(0, len(pool_ids), 30):
                            observations += feed.pools(ch, pool_ids[i:i + 30])
                        observed_pools = {s.pool for s in observations}
                        for pool in pinned - observed_pools:
                            log.error("%s position pool missing from response: %s", ch, pool)
                    for s in sorted(observations, key=lambda x: x.ts):
                        engine.step(s, now=int(time.time()))
                        watch[ch][s.pool] = s
                    # Do not let obsolete discovery fill memory forever.
                    watch[ch] = {p: s for p, s in watch[ch].items()
                                 if p in pinned or int(time.time()) - s.ts < 3600}
                    health[ch] = {"ok": True, "observations": len(observations), "at": int(time.time())}
                except Exception as e:
                    # A failed chain never prevents the other chain or stale-position risk checks.
                    log.error("%s collection failed (%s): %s", ch, type(e).__name__, str(e)[:180])
                    health[ch] = {"ok": False, "error": type(e).__name__, "at": int(time.time())}
                engine.heartbeat(int(time.time()))
                if on_health:
                    on_health(dict(health))
            log.info("collection %s", json.dumps(health))
            if once or duration and time.monotonic() - start >= duration:
                return health
            # Interruptible short sleeps; no long unresponsive scheduler wait.
            while time.monotonic() - tick < c.poll_seconds:
                if duration and time.monotonic() - start >= duration:
                    return health
                if stop_event is not None:
                    if stop_event.wait(1):
                        return health
                else:
                    time.sleep(1)
    finally:
        feed.close()
