from datetime import date, timedelta
from decimal import Decimal as D
from unittest import TestCase

from finback.engine import CostModel, run_backtest
from finback.finmind import build_rows
from finback.prices import DailyPrice
from finback.strategies import BuyAndHold, FixedWeight, Momentum, SmaCross, build

FREE = CostModel(D(0), D(1), D(0), D(0))


def bars(*rows):
    """rows of close, or (open, close), or dicts of DailyPrice extras."""
    out = []
    for i, row in enumerate(rows):
        extra = {}
        if isinstance(row, dict):
            extra = dict(row)
            row = extra.pop("px")
        o, c = row if isinstance(row, tuple) else (row, row)
        out.append(DailyPrice(date(2025, 1, 1) + timedelta(days=i), D(str(o)), D(str(c)),
                              **extra))
    return out


class Script:
    """Returns scripted weights by number of bars seen; records each history length."""

    def __init__(self, plan):
        self.plan, self.seen = plan, []

    def target_weight(self, history):
        self.seen.append(len(history))
        return self.plan.get(len(history))


class EngineTests(TestCase):
    def test_signal_after_close_fills_next_open(self):
        strategy = Script({1: D("1")})  # decided after day 0 close
        result = run_backtest(bars((10, 11), (12, 13), (13, 13)), strategy, "s",
                              D("1000"), FREE)
        self.assertEqual(strategy.seen, [0, 1, 2])  # never sees a bar before it closes
        self.assertEqual(result.rows[0].quantity, 0)
        self.assertEqual(result.trades[0].date, "2025-01-02")
        self.assertEqual(result.trades[0].price, D("12"))  # day 1 open, not day 0 close
        self.assertEqual(result.rows[1].quantity, 83)

    def test_taiwan_costs_min_commission_and_sell_tax_are_floored(self):
        costs = CostModel(D("0.001425"), D("1"), D("20"), D("0.003"))
        strategy = Script({0: D("1"), 1: D("0")})
        result = run_backtest(bars(100, 100, 100), strategy, "s", D("10000"), costs)
        buy, sell = result.trades
        self.assertEqual((buy.quantity, buy.commission), (99, D("20")))  # 14.1 -> min 20
        self.assertEqual(sell.tax, D("29"))  # floor(9900 * 0.003 = 29.7)
        self.assertEqual(result.rows[-1].equity, D("10000") - 20 - 20 - 29)
        self.assertEqual(result.rows[-1].cash, result.rows[-1].equity)

    def test_buy_never_overdraws_cash(self):
        costs = CostModel(D("0.01"), D("1"), D("0"), D("0"))
        result = run_backtest(bars(100, 100), BuyAndHold(), "s", D("1000"), costs)
        self.assertEqual(result.trades[0].quantity, 9)  # 10 would need 1010
        self.assertGreaterEqual(result.rows[0].cash, 0)

    def test_lot_size(self):
        result = run_backtest(bars(10, 10), BuyAndHold(), "s", D("25000"), FREE, lot_size=1000)
        self.assertEqual(result.rows[0].quantity, 2000)

    def test_split_multiplies_shares_and_keeps_equity(self):
        prices = bars(100, {"px": (25, 25), "split_ratio": D(4)}, 26)
        result = run_backtest(prices, BuyAndHold(), "s", D("1000"), FREE)
        self.assertEqual([r.quantity for r in result.rows], [10, 40, 40])
        self.assertEqual(result.rows[1].equity, D("1000"))
        self.assertEqual(result.rows[1].benchmark_return, 0)
        self.assertEqual(result.rows[2].equity, D("1040"))

    def test_dividend_to_prior_holdings_and_reconciles(self):
        prices = bars(100, {"px": (98, 98), "dividend_per_share": D(2)}, 99)
        result = run_backtest(prices, BuyAndHold(), "s", D("1000"), FREE)
        self.assertEqual(result.rows[1].dividend_cash, D("20"))
        self.assertEqual(result.rows[1].equity, D("1000"))
        for row in result.rows:
            self.assertEqual(row.cash + row.quantity * row.close, row.equity)

    def test_rejects_invalid_inputs(self):
        for prices in ([], bars(100, 110)[::-1], bars(100) * 2):
            with self.assertRaises(ValueError):
                run_backtest(prices, BuyAndHold(), "s", D("1000"), FREE)
        with self.assertRaises(ValueError):
            DailyPrice(date(2025, 1, 1), D("NaN"), D("100"))
        overdrawn = bars(100, {"px": (100, 100), "external_flow": D("-600")})
        with self.assertRaises(ValueError):  # withdrawal larger than the remaining cash
            run_backtest(overdrawn, BuyAndHold(D("0.5")), "s", D("1000"), FREE)

    def test_later_prices_do_not_change_earlier_ledger(self):
        short = run_backtest(bars(100, 110), SmaCross(1, 2), "s", D("1000"), FREE)
        long = run_backtest(bars(100, 110, 1), SmaCross(1, 2), "s", D("1000"), FREE)
        self.assertEqual(short.rows, long.rows[:2])

    def test_rejects_bad_weight(self):
        with self.assertRaises(ValueError):
            run_backtest(bars(1, 1), Script({0: D("1.5")}), "s", D("100"), FREE)


class StrategyTests(TestCase):
    def test_sma_cross_trades_only_on_flips(self):
        closes = [10] * 3 + [12, 13, 14] + [9, 8, 7]
        strategy = SmaCross(fast=2, slow=3)
        decisions = [strategy.target_weight(bars(*closes)[:n]) for n in range(len(closes) + 1)]
        self.assertEqual(decisions[:3], [None, None, None])
        self.assertEqual(decisions[3], D(0))  # first defined signal: not above
        self.assertEqual(decisions[4], D(1))  # flips up
        self.assertEqual(decisions[5:7], [None, None])
        self.assertEqual(decisions[7], D(0))  # flips down

    def test_fixed_weight_rebalances_after_month_change(self):
        prices = [DailyPrice(d, D(100), D(100)) for d in
                  (date(2025, 1, 30), date(2025, 1, 31), date(2025, 2, 3), date(2025, 2, 4))]
        strategy = FixedWeight(D("0.5"))
        out = [strategy.target_weight(prices[:n]) for n in range(5)]
        self.assertEqual(out, [D("0.5"), None, None, D("0.5"), None])

    def test_momentum_and_build(self):
        self.assertEqual(Momentum(lookback=2).target_weight(bars(10, 11, 12)), D(1))
        self.assertIsInstance(build({"type": "sma_cross", "fast": 5, "slow": 10}), SmaCross)
        with self.assertRaises(ValueError):
            build({"type": "nope"})


class FinMindTests(TestCase):
    def raw(self, **overrides):
        price = lambda d, o, c: {"date": d, "stock_id": "X", "open": o, "max": max(o, c),
                                 "min": min(o, c), "close": c, "Trading_Volume": 1}
        raw = {"TaiwanStockPrice": [price("2025-01-02", 100, 100), price("2025-01-03", 25, 25)],
               "TaiwanStockDividendResult": [],
               "TaiwanStockSplitPrice": [{"date": "2025-01-03", "type": "分割",
                                          "before_price": 100, "after_price": 25.01}],
               "TaiwanStockCapitalReductionReferencePrice": []}
        raw.update(overrides)
        return raw

    def test_split_ratio_rounded_from_reference_prices(self):
        rows, events = build_rows(self.raw(), date(2025, 1, 1), date(2025, 1, 31))
        self.assertEqual(rows[1]["split_ratio"], 4)
        self.assertEqual(events["splits"][0]["ratio"], "4")

    def test_unrecorded_gap_and_stock_dividend_stop_import(self):
        with self.assertRaisesRegex(ValueError, "no recorded split"):
            build_rows(self.raw(TaiwanStockSplitPrice=[]), date(2025, 1, 1), date(2025, 1, 31))
        stock_div = [{"date": "2025-01-02", "stock_or_cache_dividend": "權",
                      "stock_and_cache_dividend": 1}]
        with self.assertRaisesRegex(ValueError, "stock dividend"):
            build_rows(self.raw(TaiwanStockDividendResult=stock_div),
                       date(2025, 1, 1), date(2025, 1, 31))
