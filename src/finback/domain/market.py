from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from finback.domain.instruments import Instrument


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is OrderSide.BUY else -1


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


@dataclass(frozen=True, slots=True)
class Bar:
    instrument: Instrument
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int

    def __post_init__(self) -> None:
        _require_aware(self.timestamp, "timestamp")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("OHLC prices must be positive")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("OHLC prices are inconsistent")
        if self.low > self.high:
            raise ValueError("low must not exceed high")
        if self.volume < 0:
            raise ValueError("volume must not be negative")


@dataclass(frozen=True, slots=True)
class Order:
    id: str
    instrument: Instrument
    side: OrderSide
    quantity: int
    submitted_at: datetime
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = None

    def __post_init__(self) -> None:
        _require_aware(self.submitted_at, "submitted_at")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit orders require limit_price")
        if self.limit_price is not None and self.limit_price <= 0:
            raise ValueError("limit_price must be positive")


@dataclass(frozen=True, slots=True)
class Fill:
    order_id: str
    instrument: Instrument
    side: OrderSide
    quantity: int
    price: Decimal
    filled_at: datetime
    fees: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        _require_aware(self.filled_at, "filled_at")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.price <= 0:
            raise ValueError("price must be positive")
        if self.fees < 0:
            raise ValueError("fees must not be negative")

