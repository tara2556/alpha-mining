from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class AssetClass(StrEnum):
    STOCK = "stock"
    FUTURE = "future"
    OPTION = "option"


@dataclass(frozen=True, slots=True)
class Instrument:
    """A tradable contract as it existed at a point in time.

    `symbol` is the vendor-independent identity used by the engine. Futures and
    options should identify a specific expiry contract, not a continuous series.
    """

    symbol: str
    asset_class: AssetClass
    currency: str = "TWD"
    multiplier: Decimal = Decimal("1")
    tick_size: Decimal = Decimal("0.01")

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("symbol must not be blank")
        if self.multiplier <= 0:
            raise ValueError("multiplier must be positive")
        if self.tick_size <= 0:
            raise ValueError("tick_size must be positive")

    def notional(self, price: Decimal, quantity: int) -> Decimal:
        return price * self.multiplier * quantity

