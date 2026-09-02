from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from finback.domain.instruments import Instrument
from finback.domain.market import Fill


@dataclass(slots=True)
class Position:
    instrument: Instrument
    quantity: int = 0
    average_price: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")

    def apply(self, fill: Fill) -> Decimal:
        signed_quantity = fill.side.sign * fill.quantity
        old_quantity = self.quantity

        if old_quantity == 0 or old_quantity * signed_quantity > 0:
            new_quantity = old_quantity + signed_quantity
            old_cost = self.average_price * abs(old_quantity)
            fill_cost = fill.price * abs(signed_quantity)
            self.average_price = (old_cost + fill_cost) / abs(new_quantity)
            self.quantity = new_quantity
            return Decimal("0")

        closing_quantity = min(abs(old_quantity), abs(signed_quantity))
        direction = Decimal("1") if old_quantity > 0 else Decimal("-1")
        pnl = (
            (fill.price - self.average_price)
            * closing_quantity
            * direction
            * self.instrument.multiplier
        )
        self.realized_pnl += pnl
        self.quantity = old_quantity + signed_quantity

        if self.quantity == 0:
            self.average_price = Decimal("0")
        elif old_quantity * self.quantity < 0:
            self.average_price = fill.price
        return pnl

    def market_value(self, price: Decimal) -> Decimal:
        return self.instrument.notional(price, self.quantity)

    def unrealized_pnl(self, price: Decimal) -> Decimal:
        return (
            (price - self.average_price)
            * self.quantity
            * self.instrument.multiplier
        )


@dataclass(slots=True)
class Account:
    initial_cash: Decimal
    cash: Decimal = field(init=False)
    positions: dict[str, Position] = field(default_factory=dict)
    fees_paid: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if self.initial_cash < 0:
            raise ValueError("initial_cash must not be negative")
        self.cash = self.initial_cash

    def apply_fill(self, fill: Fill) -> None:
        position = self.positions.setdefault(
            fill.instrument.symbol, Position(instrument=fill.instrument)
        )
        cash_flow = fill.instrument.notional(fill.price, fill.quantity) * fill.side.sign
        self.cash -= cash_flow + fill.fees
        self.fees_paid += fill.fees
        position.apply(fill)

    def equity(self, prices: dict[str, Decimal]) -> Decimal:
        market_value = sum(
            (
                position.market_value(prices[symbol])
                for symbol, position in self.positions.items()
                if position.quantity != 0
            ),
            start=Decimal("0"),
        )
        return self.cash + market_value

