from datetime import datetime
from decimal import Decimal
from unittest import TestCase
from zoneinfo import ZoneInfo

from finback import Account, AssetClass, Fill, Instrument, OrderSide


TAIPEI = ZoneInfo("Asia/Taipei")
NOW = datetime(2026, 9, 2, 9, 0, tzinfo=TAIPEI)


class AccountTests(TestCase):
    def setUp(self) -> None:
        self.stock = Instrument(
            symbol="2330",
            asset_class=AssetClass.STOCK,
            multiplier=Decimal("1"),
            tick_size=Decimal("1"),
        )

    def test_round_trip_updates_cash_realized_pnl_and_fees(self) -> None:
        account = Account(initial_cash=Decimal("100000"))
        account.apply_fill(
            Fill("buy", self.stock, OrderSide.BUY, 100, Decimal("100"), NOW, Decimal("10"))
        )
        account.apply_fill(
            Fill("sell", self.stock, OrderSide.SELL, 100, Decimal("110"), NOW, Decimal("15"))
        )

        position = account.positions["2330"]
        self.assertEqual(position.quantity, 0)
        self.assertEqual(position.realized_pnl, Decimal("1000"))
        self.assertEqual(account.cash, Decimal("100975"))
        self.assertEqual(account.fees_paid, Decimal("25"))
        self.assertEqual(account.equity({}), Decimal("100975"))

    def test_future_multiplier_affects_pnl(self) -> None:
        future = Instrument(
            symbol="TXF-202609",
            asset_class=AssetClass.FUTURE,
            multiplier=Decimal("200"),
            tick_size=Decimal("1"),
        )
        account = Account(initial_cash=Decimal("10000000"))
        account.apply_fill(Fill("b", future, OrderSide.BUY, 1, Decimal("22000"), NOW))
        account.apply_fill(Fill("s", future, OrderSide.SELL, 1, Decimal("22010"), NOW))

        self.assertEqual(account.positions[future.symbol].realized_pnl, Decimal("2000"))

    def test_reversal_opens_new_position_at_fill_price(self) -> None:
        account = Account(initial_cash=Decimal("100000"))
        account.apply_fill(Fill("b", self.stock, OrderSide.BUY, 100, Decimal("100"), NOW))
        account.apply_fill(Fill("s", self.stock, OrderSide.SELL, 150, Decimal("110"), NOW))

        position = account.positions["2330"]
        self.assertEqual(position.quantity, -50)
        self.assertEqual(position.average_price, Decimal("110"))
        self.assertEqual(position.realized_pnl, Decimal("1000"))

