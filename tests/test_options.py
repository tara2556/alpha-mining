import csv
import gzip
import hashlib
import io
import json
import tempfile
from datetime import date
from decimal import Decimal as D
from pathlib import Path
from unittest import TestCase

from finback.options_data import (CHAIN_FIELDS, OptionChain, TradingCalendar, _parse_chain,
                                  scheduled_expiry)
from finback.options_engine import OptionParams, run_options_backtest

DAYS = [date(2025, 1, d) for d in (6, 7, 8, 9, 10, 13, 14, 15, 16)]
EXPR = "close - 100"  # alpha > 0 -> calls, < 0 -> puts


def make_chain(folder: Path, closes, quotes) -> OptionChain:
    """closes: TAIEX close per DAYS entry; quotes: {(day, contract, strike, cp): (open, close, vol)}."""
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "taiex.csv").open("w", encoding="utf-8", newline="") as s:
        w = csv.writer(s)
        w.writerow(["date", "open", "high", "low", "close", "volume"])
        for d, c in zip(DAYS, closes):
            w.writerow([d, c, c, c, c, 1])
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CHAIN_FIELDS, lineterminator="\n")
    w.writeheader()
    for (d, contract, strike, cp), (o, c, v) in sorted(quotes.items()):
        w.writerow({"date": d, "contract": contract, "strike": strike, "call_put": cp, "open": o,
                    "high": max(o, c), "low": min(o, c) if o else c, "close": c, "volume": v,
                    "settlement_price": 0, "open_interest": 0})
    data = gzip.compress(buf.getvalue().encode(), mtime=0)
    (folder / "2025-01.csv.gz").write_bytes(data)
    meta = {"months": {"2025-01": {"start": "2025-01-01", "end": "2025-01-31",
                                   "csv_gz_sha256": hashlib.sha256(data).hexdigest()}},
            "underlying": {"csv_sha256": hashlib.sha256((folder / "taiex.csv").read_bytes()).hexdigest()},
            "coverage": {"start": "2025-01-06", "end": "2025-01-16"},
            "settlement_source": "approx_taiex_close"}
    (folder / "metadata.json").write_text(json.dumps(meta))
    return OptionChain(folder)


def listing(strikes=(100, 105, 110), contracts=("202501", "202502"), price=(2, 3, 10)):
    """Every contract/strike/side listed every day with (open, close, volume)."""
    return {(d, c, k, cp): price for d in DAYS for c in contracts for k in strikes
            for cp in ("call", "put")}


class OptionEngineTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.n = 0

    def tearDown(self):
        self.tmp.cleanup()

    def chain(self, closes, quotes):
        self.n += 1
        return make_chain(Path(self.tmp.name) / str(self.n), closes, quotes)

    def params(self, **kw):
        base = dict(initial_cash=D(10000), premium_fraction=D("0.5"), roll_days=0,
                    min_days_to_expiry=5)
        base.update(kw)
        return OptionParams(**base)

    def test_buys_atm_call_at_next_open_with_floor_sizing_and_fees(self):
        chain = self.chain([106] * 9, listing())
        r = run_options_backtest(chain, EXPR, self.params(), end=DAYS[1])
        t = r.trades[0]
        self.assertEqual((t.date, t.action, t.contract, t.strike, t.call_put),
                         ("2025-01-07", "open", "202501", D(105), "call"))
        # budget 5000 / (2*50 + 25) -> 40 contracts; premium 4000, commission 1000, tax 4
        self.assertEqual((t.contracts, t.premium, t.commission, t.tax), (40, D(4000), D(1000), D(4)))
        day1 = r.rows[1]
        self.assertEqual(day1.cash, D(4996))
        self.assertEqual(day1.position_value, D(3) * 50 * 40)
        self.assertEqual(r.rows[0].contracts, 0)  # nothing can fill on the signal day
        self.assertTrue(r.metrics["reconciled"])

    def test_missed_fill_without_quote_then_retries(self):
        q = listing()
        q[(DAYS[1], "202501", 105, "call")] = (0, 3, 0)  # no trade on day 1
        r = run_options_backtest(self.chain([106] * 9, q), EXPR, self.params(), end=DAYS[3])
        self.assertEqual([(t.date, t.action) for t in r.trades][:2],
                         [("2025-01-07", "missed"), ("2025-01-08", "open")])

    def test_itm_call_cash_settles_at_taiex_close_on_expiry(self):
        closes = [106, 106, 106, 106, 106, 106, 106, 112, 112]
        r = run_options_backtest(self.chain(closes, listing()), EXPR, self.params(), end=DAYS[7])
        exp = [t for t in r.trades if t.action == "expire"][0]
        self.assertEqual((exp.date, exp.price, exp.premium), ("2025-01-15", D(7), D(14000)))
        self.assertEqual((exp.commission, exp.tax), (D(1000), D(14)))
        self.assertEqual(r.rows[7].cash, D(4996) + 14000 - 1014)
        self.assertEqual(r.rows[7].contracts, 0)

    def test_otm_put_expires_worthless_without_fees(self):
        closes = [94, 94, 94, 94, 94, 94, 94, 104, 104]  # put 95 (ATM of 94 -> 95? nearest 95)
        r = run_options_backtest(self.chain(closes, listing(strikes=(90, 95, 100))), EXPR,
                                 self.params(), end=DAYS[7])
        opened = r.trades[0]
        self.assertEqual((opened.call_put, opened.strike), ("put", D(95)))
        exp = [t for t in r.trades if t.action == "expire"][0]
        self.assertEqual((exp.premium, exp.commission, exp.tax), (D(0), D(0), D(0)))
        self.assertEqual(r.metrics["expired_worthless"], 1)

    def test_rolls_to_next_month_before_expiry(self):
        r = run_options_backtest(self.chain([106] * 9, listing()), EXPR,
                                 self.params(roll_days=3), end=DAYS[8])
        actions = [(t.date, t.action, t.contract) for t in r.trades]
        self.assertIn(("2025-01-13", "close", "202501"), actions)
        self.assertIn(("2025-01-13", "open", "202502"), actions)
        self.assertFalse(any(a == "expire" for _, a, _ in actions))

    def test_signal_flip_closes_calls_then_buys_puts(self):
        closes = [106, 106, 94, 94, 94, 94, 94, 94, 94]
        r = run_options_backtest(self.chain(closes, listing(strikes=(90, 95, 100, 105))), EXPR,
                                 self.params(), end=DAYS[4])
        self.assertEqual([(t.date, t.action, t.call_put) for t in r.trades],
                         [("2025-01-07", "open", "call"), ("2025-01-09", "close", "call"),
                          ("2025-01-09", "open", "put")])
        self.assertTrue(r.metrics["reconciled"])

    def test_later_data_never_changes_earlier_rows(self):
        q1, q2 = listing(), listing()
        for key in q2:
            if key[0] >= DAYS[5]:
                q2[key] = (9, 1, 5)
        closes2 = [106, 106, 106, 106, 106, 50, 50, 50, 50]
        a = run_options_backtest(self.chain([106] * 9, q1), EXPR, self.params(), end=DAYS[8])
        b = run_options_backtest(self.chain(closes2, q2), EXPR, self.params(), end=DAYS[8])
        self.assertEqual(a.rows[:5], b.rows[:5])
        self.assertEqual([t for t in a.trades if t.date <= "2025-01-10"],
                         [t for t in b.trades if t.date <= "2025-01-10"])

    def test_cash_never_negative_and_params_validated(self):
        r = run_options_backtest(self.chain([106] * 9, listing()), EXPR,
                                 self.params(premium_fraction=D(1)), end=DAYS[8])
        self.assertTrue(all(row.cash >= 0 for row in r.rows))
        with self.assertRaises(ValueError):
            OptionParams(min_days_to_expiry=1, roll_days=3)
        with self.assertRaises(ValueError):
            OptionParams(long_entry=0, long_exit=1)


class ExpiryAndDataTests(TestCase):
    def test_scheduled_expiry_rules(self):
        self.assertEqual(scheduled_expiry("202506"), date(2025, 6, 18))
        self.assertEqual(scheduled_expiry("202506W2"), date(2025, 6, 11))
        self.assertEqual(scheduled_expiry("202510F2"), date(2025, 10, 10))
        with self.assertRaises(ValueError):
            scheduled_expiry("202506X1")

    def test_holiday_moves_expiry_to_next_trading_day(self):
        days = [date(2025, 10, d) for d in (1, 2, 3, 7, 8, 9, 13, 14, 15)]  # 10/10 holiday
        self.assertEqual(TradingCalendar(days).expiry("202510F2"), date(2025, 10, 13))

    def test_holiday_before_first_listed_day_is_not_trading(self):
        # 2025-01-01 is a holiday; the covered range starts on it, the first trading day is 1/2
        cal = TradingCalendar([date(2025, 1, 2), date(2025, 1, 3)], date(2025, 1, 1),
                              date(2025, 1, 3))
        self.assertEqual(cal.expiry("202501W1"), date(2025, 1, 2))

    def test_parse_chain_keeps_day_session_and_rejects_bad_rows(self):
        row = {"date": "2025-06-02", "option_id": "TXO", "contract_date": "202506",
               "strike_price": 21000.0, "call_put": "call", "open": 10.0, "max": 12.0,
               "min": 9.0, "close": 11.0, "volume": 5, "settlement_price": 0.0,
               "open_interest": 1, "trading_session": "position"}
        night = dict(row, trading_session="after_market")
        out = _parse_chain({"status": 200, "data": [row, night]}, "t")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["strike"], D("21000.0"))
        for bad in (dict(row, option_id="TEO"), dict(row, call_put="x"), dict(row, close=-1.0)):
            with self.assertRaises(ValueError):
                _parse_chain({"status": 200, "data": [bad]}, "t")
        with self.assertRaises(ValueError):
            _parse_chain({"status": 200, "data": [row, row]}, "t")  # duplicates
