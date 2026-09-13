from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import tomllib
from dataclasses import dataclass
from pathlib import Path

CHAINS = ("solana", "robinhood")


@dataclass(frozen=True)
class Config:
    chains: tuple[str, ...] = CHAINS
    strategy: str = "momentum"
    initial_cash_per_chain: float = 1000
    order_usd: float = 25
    max_positions_per_chain: int = 3
    max_market_cap: float = 500000
    min_market_cap: float = 20000
    allow_fdv_proxy: bool = False
    min_liquidity: float = 20000
    min_age_s: int = 120
    max_age_s: int = 86400
    min_volume_m5: float = 3000
    min_buyers_m5: int = 10
    min_buy_fraction: float = .60
    min_momentum_m5: float = .10
    max_momentum_m5: float = 1.20
    min_observations: int = 3
    warmup_s: int = 60
    max_gap_s: int = 180
    latency_s: int = 2
    pending_ttl_s: int = 180
    cooldown_s: int = 1800
    stop_loss: float = .20
    take_profit_multiple: float = 2
    trail_activation: float = 1.6
    trail_drawdown: float = .20
    max_hold_s: int = 1800
    liquidity_exit_fraction: float = .50
    daily_loss_fraction: float = .05
    max_drawdown_fraction: float = .15
    max_price_impact: float = .02
    pool_fee_bps: float = 100
    extra_slippage_bps: float = 100
    tx_fee_usd_solana: float = .10
    tx_fee_usd_robinhood: float = .10
    poll_seconds: int = 60
    discovery_seconds: int = 300
    watch_pools_per_chain: int = 15

    def __post_init__(self):
        if not self.chains or any(c not in CHAINS for c in self.chains):
            raise ValueError("chains must contain solana and/or robinhood")
        if len(set(self.chains)) != len(self.chains):
            raise ValueError("duplicate chain")
        if self.strategy not in ("momentum", "pullback"):
            raise ValueError("unknown strategy")
        for f in dataclasses.fields(self):
            v = getattr(self, f.name)
            if isinstance(v, (float, int)) and not isinstance(v, bool):
                if not math.isfinite(v) or v < 0:
                    raise ValueError(f"invalid {f.name}")
        for name in ("stop_loss", "trail_drawdown", "daily_loss_fraction", "max_drawdown_fraction",
                     "max_price_impact", "liquidity_exit_fraction", "min_buy_fraction"):
            if not 0 < getattr(self, name) < 1:
                raise ValueError(f"{name} must be between 0 and 1")
        if not 0 < self.order_usd < self.initial_cash_per_chain:
            raise ValueError("order must be positive and smaller than starting cash")
        if not 0 < self.min_market_cap < self.max_market_cap or self.min_liquidity <= 0:
            raise ValueError("invalid market limits")
        if self.take_profit_multiple <= 1 or self.trail_activation <= 1:
            raise ValueError("profit multiples must exceed one")
        if self.pool_fee_bps + self.extra_slippage_bps >= 10000:
            raise ValueError("invalid fee assumptions")
        if self.latency_s <= 0 or self.pending_ttl_s < self.latency_s:
            raise ValueError("invalid order latency")
        if self.min_observations < 3 or self.warmup_s <= 0 or self.max_positions_per_chain < 1:
            raise ValueError("invalid observation/position limits")
        if self.watch_pools_per_chain > 30 or self.watch_pools_per_chain < 1:
            raise ValueError("watch_pools_per_chain must be 1..30")
        if self.poll_seconds < 30 or self.discovery_seconds < self.poll_seconds:
            raise ValueError("REST polling must be >=30 seconds, discovery >= polling")

    @classmethod
    def load(cls, path: str):
        values = tomllib.loads(Path(path).read_text())
        if "chains" in values:
            values["chains"] = tuple(values["chains"])
        return cls(**values)

    def fingerprint(self):
        return hashlib.sha256(json.dumps(dataclasses.asdict(self), sort_keys=True).encode()).hexdigest()

    def tx_fee(self, chain):
        return getattr(self, f"tx_fee_usd_{chain}")


@dataclass(frozen=True)
class Snapshot:
    chain: str
    token: str
    pool: str
    ts: int # time received / available to the strategy, never a future candle's opening time
    price: float
    liquidity: float
    market_cap: float | None
    fdv: float | None
    created_at: int | None
    volume_m5: float
    buys_m5: int
    sells_m5: int
    buyers_m5: int | None
    change_m5: float
    symbol: str = "?"
    source: str = "external"
    source_ts: int | None = None
    dex: str = "unknown"

    def __post_init__(self):
        if self.chain not in CHAINS or not self.token or not self.pool or self.ts <= 0:
            raise ValueError("invalid event identity")
        for name in ("price", "liquidity", "market_cap", "fdv", "volume_m5", "buyers_m5",
                     "buys_m5", "sells_m5"):
            v = getattr(self, name)
            if v is not None and (not math.isfinite(v) or v < 0):
                raise ValueError(f"invalid {name}")
        if not math.isfinite(self.change_m5) or self.change_m5 < -1:
            raise ValueError("invalid price change")
        if self.source_ts is not None and self.source_ts > self.ts:
            raise ValueError("source timestamp is in the future")
        if self.chain == "robinhood":
            object.__setattr__(self, "token", self.token.lower())
            object.__setattr__(self, "pool", self.pool.lower())

    @property
    def key(self):
        return self.chain + ":" + self.token

    @property
    def event_id(self):
        return f"{self.chain}:{self.pool}:{self.ts}"

    def data(self):
        return dataclasses.asdict(self)

