import json
import math
import time
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path
from unittest import TestCase, skipUnless

from finback.alpha import (
    FUNCTIONS, VARIABLES, AlphaError, adjusted_series, compile_alpha, evaluate_alpha,
)
from finback.prices import DailyPrice, load_prices

REAL_CSV = Path(__file__).resolve().parents[1] / "data/processed/finmind_0050_20200101_20251231.csv"


def bars(*closes, **extras):
    """Bars with open == close; extras maps index -> dict of DailyPrice fields."""
    out = []
    for i, c in enumerate(closes):
        kw = dict(extras.get(f"d{i}", {}))
        o = kw.pop("open", c)
        out.append(DailyPrice(date(2025, 1, 1) + timedelta(days=i), D(str(o)), D(str(c)), **kw))
    return out


def synthetic(n=90, split_at=50, dividend_at=30):
    """Wavy prices with a 1->4 split and a cash dividend; includes high/low/volume gaps."""
    out = []
    for i in range(n):
        base = 100 + 10 * math.sin(i / 5) + 0.3 * i + (3 if i % 7 == 0 else 0)
        if i >= split_at:
            base /= 4
        if i >= dividend_at:
            base -= 2 if i < split_at else 0.5
        close = D(str(round(base, 2)))
        opn = D(str(round(base * (1.004 if i % 2 else 0.996), 2)))
        hi, lo = max(opn, close) + D("0.5"), min(opn, close) - D("0.3")
        extra = {}
        if i % 11 != 3:
            extra.update(high=hi, low=lo)
        if i % 13 != 5:
            extra["volume"] = D(1000 + 37 * i) * (4 if i >= split_at else 1)
        if i == split_at:
            extra["split_ratio"] = D(4)
        if i == dividend_at:
            extra["dividend_per_share"] = D("2")
        out.append(DailyPrice(date(2024, 1, 1) + timedelta(days=i), opn, close, **extra))
    return out


def evaluate(expr, data, pool=None):
    return compile_alpha(expr, pool).evaluate(data)


class AdjustmentTests(TestCase):
    def test_split_and_dividend_adjustment_is_causal_and_continuous(self):
        data = bars(100, 102, 26, 26.5, 25, 26,
                    d2={"split_ratio": D(4)}, d4={"dividend_per_share": D("1")})
        s = adjusted_series(data)
        self.assertEqual(s["close"][:2], [100.0, 102.0])
        self.assertAlmostEqual(s["close"][2], 104.0)  # 102 * 4 * 26 / 102: +1.96%, not -75%
        self.assertAlmostEqual(s["returns"][2], 26 * 4 / 102 - 1)
        self.assertAlmostEqual(s["returns"][4], (25 + 1) / 26.5 - 1)  # dividend reinvested
        self.assertIsNone(s["returns"][0])
        self.assertAlmostEqual(s["open"][2], 104.0)
        self.assertEqual(s["volume"], [0.0] * 6)

    def test_volume_in_pre_split_units_and_high_low_fallback(self):
        data = bars(10, 11, 3,
                    d0={"volume": D(100), "high": D(12), "low": D(9)},
                    d1={"volume": D(200)},
                    d2={"volume": D(800), "split_ratio": D(4), "open": D("2.9")})
        s = adjusted_series(data)
        self.assertEqual(s["volume"], [100.0, 200.0, 200.0])
        self.assertEqual((s["high"][0], s["low"][0]), (12.0, 9.0))
        self.assertEqual((s["high"][1], s["low"][1]), (11.0, 11.0))
        self.assertAlmostEqual(s["high"][2], 12.0)  # max(open, close)=3 * factor 4
        self.assertAlmostEqual(s["low"][2], 11.6)

    def test_signal_does_not_see_split_as_crash(self):
        data = synthetic()
        ret = evaluate("returns", data)
        self.assertGreater(ret[50], -0.1)
        raw = float(data[50].close) / float(data[49].close) - 1
        self.assertLess(raw, -0.7)


class FunctionTests(TestCase):
    def setUp(self):
        self.d = bars(1, 2, 4, 3, 5)

    def check(self, expr, expected, data=None):
        got = evaluate(expr, data or self.d)
        self.assertEqual(len(got), len(expected))
        for g, e in zip(got, expected):
            if e is None:
                self.assertIsNone(g, expr)
            else:
                self.assertIsNotNone(g, expr)
                self.assertAlmostEqual(g, e, msg=expr)

    def test_lags(self):
        self.check("delay(close, 2)", [None, None, 1, 2, 4])
        self.check("delta(close, 1)", [None, 1, 2, -1, 2])
        self.check("pct_change(close, 1)", [None, 1, 1, -0.25, 2 / 3])

    def test_rolling(self):
        self.check("ts_mean(close, 3)", [None, None, 7 / 3, 3, 4])
        self.check("sma(close, 3)", [None, None, 7 / 3, 3, 4])
        self.check("ts_sum(close, 2)", [None, 3, 6, 7, 8])
        self.check("ts_max(close, 3)", [None, None, 4, 4, 5])
        self.check("ts_min(close, 3)", [None, None, 1, 2, 3])
        self.check("ts_argmax(close, 3)", [None, None, 0, 1, 0])
        self.check("ts_argmin(close, 3)", [None, None, 2, 2, 1])
        self.check("ts_std(close, 3)", [None, None, math.sqrt(7 / 3), 1, 1])
        self.check("ts_rank(close, 3)", [None, None, 1, 0.5, 1])
        self.check("zscore(close, 3)", [None, None, (4 - 7 / 3) / math.sqrt(7 / 3), 0, 1])
        self.check("decay_linear(close, 2)", [None, 5 / 3, 10 / 3, 10 / 3, 13 / 3])
        self.check("ts_corr(close, close * 2 + 1, 3)", [None, None, 1, 1, 1])
        self.check("ts_corr(close, -close, 3)", [None, None, -1, -1, -1])

    def test_rolling_requires_full_window_of_values(self):
        # returns[0] is None, so a 2-day window first exists at t=2
        self.check("ts_sum(returns, 2)", [None, None, 2, 0.75, 5 / 12])

    def test_ema_and_rsi(self):
        # ema seeded with the first 2-day mean, alpha = 2/3
        self.check("ema(close, 2)", [None, 1.5, 19 / 6, 2 + 19 / 18, 10 / 3 + (2 + 19 / 18) / 3])
        # Wilder, n=2: gains 1,2 -> seed avg 1.5/0; then loss 1: g=0.75 l=0.5; then gain 2: g=1.375 l=0.25
        self.check("rsi(close, 2)", [None, None, 100, 100 - 100 / (1 + 1.5), 100 - 100 / (1 + 5.5)])
        self.check("rsi(close, 2)", [None, None, 50, 50], bars(3, 3, 3, 3))

    def test_elementwise_and_logic(self):
        self.check("abs(delta(close, 1))", [None, 1, 2, 1, 2])
        self.check("sign(delta(close, 1))", [None, 1, 1, -1, 1])
        self.check("log(close - 2)", [None, None, math.log(2), 0, math.log(3)])
        self.check("sqrt(close - 2)", [None, 0, math.sqrt(2), 1, math.sqrt(3)])
        self.check("max(close, 3)", [3, 3, 4, 3, 5])
        self.check("min(close, 3)", [1, 2, 3, 3, 3])
        self.check("clip(close, 2, 4)", [2, 2, 4, 3, 4])
        self.check("where(close > 2, 1, -1)", [-1, -1, 1, 1, 1])
        self.check("(close >= 2) and (close < 5)", [0, 1, 1, 1, 0])
        self.check("close < 2 or close == 4", [1, 0, 1, 0, 0])
        self.check("not (close != 3)", [0, 0, 0, 1, 0])
        self.check("1 < close < 4", [0, 1, 0, 1, 0])
        self.check("close ** 2 - +close", [0, 2, 12, 6, 20])
        self.check("-close / 2", [-0.5, -1, -2, -1.5, -2.5])

    def test_undefined_values_become_none(self):
        self.check("1 / (close - 2)", [-1, None, 0.5, 1, 1 / 3])
        self.check("(close - 3) ** 0.5", [None, None, 1, 0, math.sqrt(2)])
        self.check("10.0 ** (close * 200)", [1e200, None, None, None, None])
        self.check("zscore(close * 0 + 1, 3)", [None] * 5)
        self.check("where(delay(close, 1) > 1, close, 0)", [None, 0, 4, 3, 5])

    def test_every_function_is_documented_and_tested_for_prefix(self):
        covered = " ".join(PREFIX_EXPRESSIONS)
        for name in FUNCTIONS:
            self.assertIn(name + "(", covered)
        for name in VARIABLES:
            self.assertIn(name, PREFIX_EXPRESSIONS)


PREFIX_EXPRESSIONS = [
    *VARIABLES,
    "delay(close, 3)", "delta(open, 2)", "pct_change(close, 5)",
    "ts_mean(close, 5)", "sma(high, 4)", "ema(close, 6)", "decay_linear(low, 5)",
    "ts_std(returns, 10)", "ts_sum(volume, 3)", "ts_max(high, 7)", "ts_min(low, 7)",
    "ts_argmax(close, 10)", "ts_argmin(close, 10)", "ts_rank(close, 10)",
    "zscore(close, 20)", "ts_corr(close, volume, 10)", "rsi(close, 14)",
    "abs(returns)", "sign(delta(close, 1))", "log(volume)", "sqrt(close)",
    "max(open, close)", "min(open, close)", "clip(returns, -0.01, 0.01)",
    "where(rsi(close, 5) < 40, 1, 0)",
    "ts_mean(close, 5) / ts_mean(close, 20) - 1",
    "-zscore(ema(close, 3), 15) * (volume > ts_mean(volume, 5))",
    "ts_rank(ts_corr(high, low, 5), 8)",
]


class PrefixInvarianceTests(TestCase):
    def test_value_at_t_depends_only_on_bars_up_to_t(self):
        data = synthetic()
        for expr in PREFIX_EXPRESSIONS:
            alpha = compile_alpha(expr)
            full = alpha.evaluate(data)
            self.assertEqual(len(full), len(data))
            self.assertTrue(any(v is not None for v in full), expr)
            for k in range(1, len(data) + 1):
                self.assertEqual(alpha.evaluate(data[:k])[-1], full[k - 1], f"{expr} at k={k}")

    def test_pool_alpha_is_prefix_invariant(self):
        data = synthetic()
        pool = {"mom": "pct_change(close, 10)", "rev": "-delta(close, 3)"}
        alpha = compile_alpha("0.5 * mom + 0.5 * zscore(rev, 5)", pool)
        full = alpha.evaluate(data)
        for k in range(1, len(data) + 1):
            self.assertEqual(alpha.evaluate(data[:k])[-1], full[k - 1])


class SafetyTests(TestCase):
    def test_rejections(self):
        bad = [
            "__import__('os')", "close.__class__", "(lambda: 1)()", "close[0]", "'a'",
            "ts_mean(close, -5)", "ts_mean(close, 2.5)", "ts_mean(close, close)",
            "ts_mean(close, 0)", "ts_mean(close, 1001)", "ts_mean(close, True)",
            "ts_std(close, 1)", "unknown_thing", "ts_mean(x=close, n=5)",
            "ts_mean(close, n=5)", "ts_mean(*[close, 5])", "[c for c in close]",
            "(x := 1)", "f'{close}'", "close if close else open", "ts_mean(close)",
            "close % 2", "close // 2", "close @ close", "open(1)", "ts_mean", "eval('1')",
            "True", "None", "1j", "1e999", "close; 1", "import os", "", "   ",
            "x" * 2001, "{1: 2}", "(1, 2)", "close in close", "close is close", "~close",
            "close << 1", "(" * 300 + "1" + ")" * 300,
        ]
        for expr in bad:
            with self.assertRaises(AlphaError, msg=expr):
                compile_alpha(expr)

    def test_alpha_error_is_value_error(self):
        self.assertTrue(issubclass(AlphaError, ValueError))

    def test_node_count_limit(self):
        with self.assertRaises(AlphaError):
            compile_alpha("+".join(["close"] * 500))
        compile_alpha("+".join(["close"] * 60))


class PoolTests(TestCase):
    def test_pool_references(self):
        data = bars(1, 2, 4, 3, 5)
        pool = {"mom": "delta(close, 1)", "twice": "mom * 2"}
        alpha = compile_alpha("twice + mom", pool)
        self.assertEqual(alpha.evaluate(data), [None, 3, 6, -3, 6])
        self.assertEqual(alpha.references, ("mom", "twice"))

    def test_cycles_and_unknown_names(self):
        with self.assertRaisesRegex(AlphaError, "a -> b -> a"):
            compile_alpha("a", {"a": "b + 1", "b": "a * 2"})
        with self.assertRaises(AlphaError):
            compile_alpha("me", {"me": "me"})
        with self.assertRaises(AlphaError):
            compile_alpha("a", {"a": "missing"})
        with self.assertRaises(AlphaError):
            compile_alpha("a", {"a": "close.real"})

    def test_pool_name_rules(self):
        for name in ("close", "ts_mean", "1abc", "a-b", ""):
            with self.assertRaises(AlphaError, msg=name):
                compile_alpha("1", {name: "close"})
        compile_alpha("1", {"_ok1": "close"})

    def test_unused_bad_pool_expr_is_not_compiled(self):
        self.assertEqual(compile_alpha("close", {"x": "1 +"}).references, ())


class EvaluateAlphaTests(TestCase):
    def series(self, n=60):
        # deterministic pseudo-random open-to-open moves
        opens, p = [], 100.0
        for i in range(n):
            p *= 1 + ((i * 7919) % 101 - 50) / 2000
            opens.append(round(p, 4))
        return [DailyPrice(date(2024, 1, 1) + timedelta(days=i), D(str(o)), D(str(o)))
                for i, o in enumerate(opens)]

    def test_perfectly_predictive_alpha(self):
        data = self.series()
        opens = [float(b.open) for b in data]
        values = [opens[t + 2] / opens[t + 1] - 1 if t + 2 < len(data) else None
                  for t in range(len(data))]
        split = data[40].date
        result = evaluate_alpha(data, values, horizons=(1, 5), split_date=split, quantiles=4)
        ins, oos = result["in_sample"]["horizons"]["1"], result["out_of_sample"]["horizons"]["1"]
        self.assertAlmostEqual(ins["ic"], 1.0)
        self.assertAlmostEqual(ins["rank_ic"], 1.0)
        self.assertAlmostEqual(oos["ic"], 1.0)
        self.assertEqual(ins["hit_rate"], 1.0)
        self.assertEqual(ins["n"], 38)  # t=0..37: t+2 must still be before the split (purged)
        self.assertEqual(oos["n"], 18)
        self.assertGreater(ins["mean_fwd_when_positive"], 0)
        self.assertLess(ins["mean_fwd_when_negative"], 0)

        in_vals = sorted(v for v in values[:40] if v is not None)
        self.assertEqual(len(result["quantile_edges"]), 3)
        self.assertAlmostEqual(result["quantile_edges"][1],
                               (in_vals[19] + in_vals[20]) / 2)  # in-sample median only
        q = ins["quantile_returns"]
        self.assertEqual([b["bucket"] for b in q], [1, 2, 3, 4])
        self.assertEqual(sum(b["n"] for b in q), 38)
        means = [b["mean_fwd"] for b in q]
        self.assertEqual(means, sorted(means))
        self.assertEqual(result["split_date"], split.isoformat())
        self.assertAlmostEqual(result["coverage"], 58 / 60)
        self.assertIn("overlap", result["note"].lower())
        json.dumps(result, allow_nan=False)

    def test_edges_ignore_out_of_sample_values(self):
        data = self.series(40)
        values = [float(i) for i in range(40)]
        a = evaluate_alpha(data, values, split_date=data[20].date)
        values2 = values[:20] + [1e6] * 20
        b = evaluate_alpha(data, values2, split_date=data[20].date)
        self.assertEqual(a["quantile_edges"], b["quantile_edges"])
        self.assertTrue(all(e < 20 for e in a["quantile_edges"]))

    def test_no_split_and_degenerate_inputs(self):
        data = self.series(10)
        result = evaluate_alpha(data, [1.0] * 10)
        self.assertIsNone(result["out_of_sample"])
        self.assertIsNone(result["split_date"])
        h1 = result["in_sample"]["horizons"]["1"]
        self.assertIsNone(h1["ic"])  # zero variance
        self.assertEqual(h1["n"], 8)
        self.assertIsNone(result["in_sample"]["horizons"]["20"]["ic"])
        self.assertEqual(result["in_sample"]["horizons"]["20"]["n"], 0)
        json.dumps(result, allow_nan=False)
        empty = evaluate_alpha(data, [None] * 10, split_date=data[5].date)
        self.assertIsNone(empty["quantile_edges"])
        json.dumps(empty, allow_nan=False)
        with self.assertRaises(AlphaError):
            evaluate_alpha(data, [1.0] * 9)
        with self.assertRaises(AlphaError):
            evaluate_alpha(data, [1.0] * 10, horizons=(0,))
        with self.assertRaises(AlphaError):
            evaluate_alpha(data, [1.0] * 10, quantiles=1)

    def test_forward_return_uses_adjusted_open(self):
        data = bars(100, 100, 25, 25, d2={"split_ratio": D(4)})
        result = evaluate_alpha(data, [1.0, -1.0, 1.0, None], horizons=(1,))
        h1 = result["in_sample"]["horizons"]["1"]
        self.assertEqual(h1["n"], 2)
        self.assertEqual(h1["mean_fwd"], 0.0)  # split is not a -75% return


@skipUnless(REAL_CSV.exists(), "real 0050 CSV not available")
class RealDataTests(TestCase):
    def test_smoke_on_0050(self):
        data = load_prices(REAL_CSV)
        pool = {"mom20": "pct_change(close, 20)", "rev5": "-zscore(close, 5)"}
        expr = ("0.5*mom20 + 0.5*rev5 + where(rsi(close,14) < 30, 1, 0) "
                "+ ts_rank(ts_mean(close, 5) / ts_mean(close, 250) - 1, 120) "
                "+ ts_corr(returns, volume, 60) + zscore(ema(close, 50), 200)")
        alpha = compile_alpha(expr, pool)
        start = time.perf_counter()
        values = alpha.evaluate(data)
        self.assertLess(time.perf_counter() - start, 1.0)
        self.assertEqual(len(values), len(data))
        split_idx = next(i for i, b in enumerate(data) if b.split_ratio != 1)
        ret = compile_alpha("returns").evaluate(data)
        self.assertGreater(ret[split_idx], -0.2)
        result = evaluate_alpha(data, values, split_date=date(2024, 1, 1))
        json.dumps(result, allow_nan=False)
        self.assertGreater(result["in_sample"]["horizons"]["5"]["n"], 500)
