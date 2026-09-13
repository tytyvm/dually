"""Conservative paper-only constant-product approximation, NOT an executable DEX quote.

Assumes half of pool TVL is quote reserve. Invalid for concentrated-liquidity pools,
bonding curves and asymmetric pools. These limitations are retained in every fill.
"""
from dataclasses import dataclass
from .domain import Config, Snapshot


@dataclass
class Quote:
    amount: float
    impact: float
    fee: float


def buy(s: Snapshot, dollars: float, c: Config):
    if s.price <= 0 or s.liquidity <= 0:
        raise ValueError("no paper liquidity")
    reserve = s.liquidity / 2
    effective = dollars * (1 - c.pool_fee_bps / 10000)
    impact = effective / (reserve + effective)
    units = (reserve / s.price) * effective / (reserve + effective)
    units *= 1 - c.extra_slippage_bps / 10000
    return Quote(units, impact, c.tx_fee(s.chain))


def sell(s: Snapshot, units: float, c: Config):
    if s.price <= 0 or s.liquidity <= 0:
        raise ValueError("no paper liquidity")
    reserve = s.liquidity / 2
    effective_units = units * (1 - c.pool_fee_bps / 10000)
    token_reserve = reserve / s.price
    impact = effective_units / (token_reserve + effective_units)
    dollars = reserve * effective_units / (token_reserve + effective_units)
    dollars *= 1 - c.extra_slippage_bps / 10000
    return Quote(dollars, impact, c.tx_fee(s.chain))


def sign_and_send(*args, **kwargs):
    raise RuntimeError("Live orders are not implemented. This build cannot sign or send transactions.")

