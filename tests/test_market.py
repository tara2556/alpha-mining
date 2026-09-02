from datetime import datetime
from decimal import Decimal
from unittest import TestCase

from finback import AssetClass, Bar, Instrument


class MarketValidationTests(TestCase):
    def test_naive_timestamp_is_rejected(self) -> None:
        stock = Instrument("2330", AssetClass.STOCK)

        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            Bar(
                stock,
                datetime(2026, 9, 2, 9, 0),
                Decimal("100"),
                Decimal("101"),
                Decimal("99"),
                Decimal("100"),
                1000,
            )

