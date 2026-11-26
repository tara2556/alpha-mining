"""Safe formula language ("alpha pool") for single-instrument daily signals.

Expressions such as ``ts_mean(close, 5) / ts_mean(close, 20) - 1`` are parsed with
``ast.parse(mode="eval")`` and checked against a strict whitelist; nothing is ever
passed to eval/exec. Every function is causal: the value at bar t depends only on
bars[0..t] (prefix invariance), and prices are adjusted forward-only so a split is
never seen as a crash. Signal math uses floats; accounting elsewhere stays Decimal.
"""

from __future__ import annotations

import ast
import math
import re
from bisect import bisect_right
from datetime import date
from typing import Callable, Mapping, Sequence

from finback.prices import DailyPrice

__all__ = [
    "AlphaError", "FUNCTIONS", "VARIABLES", "Alpha",
    "adjusted_series", "compile_alpha", "evaluate_alpha",
]

MAX_EXPR_LENGTH = 2000
MAX_NODES = 400          # AST nodes per expression
MAX_TOTAL_NODES = 4000   # across the expression and every referenced pool alpha
MAX_DEPTH = 100          # AST nesting depth per expression
MAX_HEIGHT = 150         # evaluation depth including pool references
MAX_REF_DEPTH = 16       # nesting of pool references
MAX_WINDOW = 1000
MAX_HORIZON = 1000
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

Series = list  # list[float | None]


class AlphaError(ValueError):
    """User-facing error: syntax, unknown name, bad window, cycle, ..."""


VARIABLES: dict[str, str] = {
    "open": "開盤價（還原：依過去的分割與股息向前調整）",
    "high": "最高價（還原；缺值時用 max(open, close)）",
    "low": "最低價（還原；缺值時用 min(open, close)）",
    "close": "收盤價（還原總報酬：分割不會被視為暴跌，股息再投入）",
    "volume": "成交量（換算成第一筆資料時的股數單位；缺值為 0）",
    "returns": "日報酬率 close[t]/close[t-1]-1（還原；第一天無值）",
}

FUNCTIONS: dict[str, str] = {
    "delay": "delay(x, n)：n 天前的 x",
    "delta": "delta(x, n)：x 減去 n 天前的 x",
    "pct_change": "pct_change(x, n)：x 相對 n 天前的變動率",
    "ts_mean": "ts_mean(x, n)：最近 n 天平均",
    "sma": "sma(x, n)：同 ts_mean，最近 n 天簡單平均",
    "ema": "ema(x, n)：指數移動平均（α=2/(n+1)，以前 n 天平均為起點）",
    "decay_linear": "decay_linear(x, n)：線性加權平均，越近權重越大",
    "ts_std": "ts_std(x, n)：最近 n 天樣本標準差（n≥2）",
    "ts_sum": "ts_sum(x, n)：最近 n 天總和",
    "ts_max": "ts_max(x, n)：最近 n 天最大值",
    "ts_min": "ts_min(x, n)：最近 n 天最小值",
    "ts_argmax": "ts_argmax(x, n)：最近 n 天最大值距今幾天（0 = 今天）",
    "ts_argmin": "ts_argmin(x, n)：最近 n 天最小值距今幾天（0 = 今天）",
    "ts_rank": "ts_rank(x, n)：今天的值在最近 n 天中的百分位（0 最低、1 最高，n≥2）",
    "zscore": "zscore(x, n)：(x - n 天平均) / n 天標準差（n≥2）",
    "ts_corr": "ts_corr(x, y, n)：x 與 y 最近 n 天的相關係數（n≥2）",
    "rsi": "rsi(x, n)：Wilder 相對強弱指標（0~100）",
    "abs": "abs(x)：絕對值",
    "sign": "sign(x)：正負號（1、0、-1）",
    "log": "log(x)：自然對數（x≤0 時無值）",
    "sqrt": "sqrt(x)：平方根（x<0 時無值）",
    "max": "max(a, b)：逐日取較大者",
    "min": "min(a, b)：逐日取較小者",
    "clip": "clip(x, lo, hi)：把 x 限制在 lo 與 hi 之間",
    "where": "where(cond, a, b)：條件成立（非 0）取 a，否則取 b",
}


# ---------------------------------------------------------------- price data

def adjusted_series(bars: Sequence[DailyPrice]) -> dict[str, list[float]]:
    """Causal (forward) total-return adjustment; uses only past events.

    adj_close[0] = close[0];
    adj_close[t] = adj_close[t-1] * split_ratio[t] * (close[t] + dividend[t]) / close[t-1].
    open/high/low are scaled by adj_close[t] / close[t]; volume is divided by the
    cumulative split product so it stays in first-bar share units.
    """
    out: dict[str, list] = {k: [] for k in ("open", "high", "low", "close", "volume", "returns")}
    prev_close = prev_adj = None
    cum_split = 1.0
    for i, bar in enumerate(bars):
        o, c = float(bar.open), float(bar.close)
        if i == 0:
            adj = c
        else:
            ratio = float(bar.split_ratio)
            cum_split *= ratio
            adj = prev_adj * ratio * (c + float(bar.dividend_per_share)) / prev_close
        factor = adj / c
        high = float(bar.high) if bar.high is not None else max(o, c)
        low = float(bar.low) if bar.low is not None else min(o, c)
        out["open"].append(o * factor)
        out["high"].append(high * factor)
        out["low"].append(low * factor)
        out["close"].append(adj)
        out["volume"].append(float(bar.volume) / cum_split if bar.volume is not None else 0.0)
        out["returns"].append(None if i == 0 else adj / prev_adj - 1)
        prev_close, prev_adj = c, adj
    return out


# ---------------------------------------------------------- series primitives

def _clean(v) -> float | None:
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return v if math.isfinite(v) else None


_MATH_ERRORS = (ValueError, ZeroDivisionError, OverflowError, TypeError)


def _map(f: Callable, *cols: Series) -> Series:
    out = []
    for vals in zip(*cols):
        if None in vals:
            out.append(None)
            continue
        try:
            out.append(_clean(f(*vals)))
        except _MATH_ERRORS:
            out.append(None)
    return out


def _rolling(xs: Series, n: int, f: Callable[[list], float | None]) -> Series:
    """f(window) where window = xs[t-n+1..t], all non-None; else None."""
    out: Series = []
    last_none = -1
    for t, x in enumerate(xs):
        if x is None:
            last_none = t
        if t - n + 1 <= last_none or t + 1 < n:
            out.append(None)
            continue
        try:
            out.append(_clean(f(xs[t - n + 1:t + 1])))
        except _MATH_ERRORS:
            out.append(None)
    return out


def _lag(xs: Series, n: int, f: Callable[[float, float], float]) -> Series:
    out: Series = []
    for t, x in enumerate(xs):
        past = xs[t - n] if t >= n else None
        if x is None or past is None:
            out.append(None)
            continue
        try:
            out.append(_clean(f(x, past)))
        except _MATH_ERRORS:
            out.append(None)
    return out


def _mean(w):
    return math.fsum(w) / len(w)


def _std(w):
    m = _mean(w)
    return math.sqrt(math.fsum((v - m) ** 2 for v in w) / (len(w) - 1))


def _zscore(w):
    sd = _std(w)
    return None if sd == 0 else (w[-1] - _mean(w)) / sd


def _argmax(w):  # bars since the most recent maximum
    n = len(w)
    return float(n - 1 - max(range(n), key=lambda i: (w[i], i)))


def _argmin(w):  # bars since the most recent minimum
    n = len(w)
    return float(n - 1 - min(range(n), key=lambda i: (w[i], -i)))


def _rank(w):
    last = w[-1]
    less = sum(1 for v in w if v < last)
    ties = sum(1 for v in w if v == last) - 1
    return (less + 0.5 * ties) / (len(w) - 1)


def _decay(w):
    n = len(w)
    return math.fsum(v * (i + 1) for i, v in enumerate(w)) / (n * (n + 1) / 2)


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = math.fsum(xs) / n, math.fsum(ys) / n
    sxx = math.fsum((x - mx) ** 2 for x in xs)
    syy = math.fsum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    r = math.fsum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sxx * syy)
    return _clean(max(-1.0, min(1.0, r)))


def _ts_corr(xs: Series, ys: Series, n: int) -> Series:
    out: Series = []
    last_none = -1
    for t in range(len(xs)):
        if xs[t] is None or ys[t] is None:
            last_none = t
        if t - n + 1 <= last_none or t + 1 < n:
            out.append(None)
            continue
        a, b = xs[t - n + 1:t + 1], ys[t - n + 1:t + 1]
        mx, my = _mean(a), _mean(b)
        sxx = math.fsum((x - mx) ** 2 for x in a)
        syy = math.fsum((y - my) ** 2 for y in b)
        if sxx <= 0 or syy <= 0:
            out.append(None)
            continue
        r = math.fsum((x - mx) * (y - my) for x, y in zip(a, b)) / math.sqrt(sxx * syy)
        out.append(_clean(max(-1.0, min(1.0, r))))
    return out


def _ema(xs: Series, n: int) -> Series:
    a = 2.0 / (n + 1)
    out: Series = []
    prev = None
    run = 0
    for t, x in enumerate(xs):
        if x is None:  # a gap restarts the warm-up
            prev, run = None, 0
            out.append(None)
            continue
        run += 1
        if prev is None:
            if run >= n:
                prev = _mean(xs[t - n + 1:t + 1])
        else:
            prev = a * x + (1 - a) * prev
        prev = _clean(prev)
        out.append(prev)
    return out


def _rsi(xs: Series, n: int) -> Series:
    out: Series = [None] * len(xs)
    avg_gain = avg_loss = None
    seed: list[tuple[float, float]] = []
    for t in range(1, len(xs)):
        a, b = xs[t - 1], xs[t]
        if a is None or b is None:  # a gap restarts the warm-up
            avg_gain = avg_loss = None
            seed = []
            continue
        d = b - a
        gain, loss = max(d, 0.0), max(-d, 0.0)
        if avg_gain is None:
            seed.append((gain, loss))
            if len(seed) < n:
                continue
            avg_gain = math.fsum(g for g, _ in seed) / n
            avg_loss = math.fsum(l for _, l in seed) / n
            seed = []
        else:
            avg_gain = (avg_gain * (n - 1) + gain) / n
            avg_loss = (avg_loss * (n - 1) + loss) / n
        if avg_loss == 0:
            out[t] = 50.0 if avg_gain == 0 else 100.0
        else:
            out[t] = _clean(100.0 - 100.0 / (1.0 + avg_gain / avg_loss))
    return out


def _sign(x):
    return 1.0 if x > 0 else -1.0 if x < 0 else 0.0


def _log(x):
    return math.log(x) if x > 0 else None


def _sqrt(x):
    return math.sqrt(x) if x >= 0 else None


def _clip(x, lo, hi):
    return None if lo > hi else min(max(x, lo), hi)


def _where(cond: Series, a: Series, b: Series) -> Series:
    return [None if c is None else (x if c != 0 else y) for c, x, y in zip(cond, a, b)]


# Signature: "x" = series argument, "n" = window (positive int constant).
# (signature, minimum window, implementation taking evaluated args)
_SPECS: dict[str, tuple[str, int, Callable]] = {
    "delay": ("xn", 1, lambda x, n: _lag(x, n, lambda cur, past: past)),
    "delta": ("xn", 1, lambda x, n: _lag(x, n, lambda cur, past: cur - past)),
    "pct_change": ("xn", 1, lambda x, n: _lag(x, n, lambda cur, past: cur / past - 1)),
    "ts_mean": ("xn", 1, lambda x, n: _rolling(x, n, _mean)),
    "sma": ("xn", 1, lambda x, n: _rolling(x, n, _mean)),
    "ema": ("xn", 1, _ema),
    "decay_linear": ("xn", 1, lambda x, n: _rolling(x, n, _decay)),
    "ts_std": ("xn", 2, lambda x, n: _rolling(x, n, _std)),
    "ts_sum": ("xn", 1, lambda x, n: _rolling(x, n, math.fsum)),
    "ts_max": ("xn", 1, lambda x, n: _rolling(x, n, max)),
    "ts_min": ("xn", 1, lambda x, n: _rolling(x, n, min)),
    "ts_argmax": ("xn", 1, lambda x, n: _rolling(x, n, _argmax)),
    "ts_argmin": ("xn", 1, lambda x, n: _rolling(x, n, _argmin)),
    "ts_rank": ("xn", 2, lambda x, n: _rolling(x, n, _rank)),
    "zscore": ("xn", 2, lambda x, n: _rolling(x, n, _zscore)),
    "ts_corr": ("xxn", 2, _ts_corr),
    "rsi": ("xn", 1, _rsi),
    "abs": ("x", 0, lambda x: _map(abs, x)),
    "sign": ("x", 0, lambda x: _map(_sign, x)),
    "log": ("x", 0, lambda x: _map(_log, x)),
    "sqrt": ("x", 0, lambda x: _map(_sqrt, x)),
    "max": ("xx", 0, lambda a, b: _map(max, a, b)),
    "min": ("xx", 0, lambda a, b: _map(min, a, b)),
    "clip": ("xxx", 0, lambda x, lo, hi: _map(_clip, x, lo, hi)),
    "where": ("xxx", 0, _where),
}
assert set(_SPECS) == set(FUNCTIONS)


def _truth(v: float) -> bool:
    return v != 0


_BINOPS: dict[type, tuple[str, Callable]] = {
    ast.Add: ("+", lambda a, b: a + b),
    ast.Sub: ("-", lambda a, b: a - b),
    ast.Mult: ("*", lambda a, b: a * b),
    ast.Div: ("/", lambda a, b: a / b),  # ZeroDivisionError -> None
    ast.Pow: ("**", math.pow),           # complex/overflow -> ValueError/OverflowError -> None
}
_CMPOPS: dict[type, tuple[str, Callable]] = {
    ast.Lt: ("<", lambda a, b: a < b),
    ast.LtE: ("<=", lambda a, b: a <= b),
    ast.Gt: (">", lambda a, b: a > b),
    ast.GtE: (">=", lambda a, b: a >= b),
    ast.Eq: ("==", lambda a, b: a == b),
    ast.NotEq: ("!=", lambda a, b: a != b),
}


# ------------------------------------------------------------------ compiler

class _Ctx:
    __slots__ = ("vars", "cache", "n")

    def __init__(self, variables: dict[str, list], n: int):
        self.vars, self.cache, self.n = variables, {}, n


class _Node:
    __slots__ = ("key", "height", "fn")

    def __init__(self, key: str, height: int, fn: Callable[[_Ctx], Series]):
        if height > MAX_HEIGHT:
            raise AlphaError("運算式巢狀太深（expression nested too deeply）")
        self.key, self.height, self.fn = key, height, fn

    def run(self, ctx: _Ctx) -> Series:
        value = ctx.cache.get(self.key)
        if value is None:
            value = self.fn(ctx)
            ctx.cache[self.key] = value
        return value


def _bool(v: bool) -> float:
    return 1.0 if v else 0.0


class _Compiler:
    def __init__(self, pool: Mapping[str, str] | None):
        self.pool = dict(pool or {})
        for name, expr in self.pool.items():
            if not isinstance(name, str) or not NAME_RE.match(name):
                raise AlphaError(f"alpha 名稱不合法：{name!r}（只能用英文字母、數字、底線，且不可以數字開頭）")
            if name in VARIABLES or name in FUNCTIONS:
                raise AlphaError(f"alpha 名稱 {name!r} 與內建變數或函式重複")
            if not isinstance(expr, str):
                raise AlphaError(f"alpha {name!r} 的運算式必須是字串")
        self.compiled: dict[str, _Node] = {}
        self.stack: list[str] = []
        self.total_nodes = 0
        self.references: set[str] = set()

    def source(self, expr: str, label: str | None) -> _Node:
        where = f"（{label}）" if label else ""
        if not isinstance(expr, str):
            raise AlphaError("運算式必須是字串")
        if not expr.strip():
            raise AlphaError(f"運算式是空的{where}")
        if len(expr) > MAX_EXPR_LENGTH:
            raise AlphaError(f"運算式太長{where}：最多 {MAX_EXPR_LENGTH} 字元")
        try:
            tree = ast.parse(expr.strip(), mode="eval")
        except SyntaxError as exc:
            raise AlphaError(f"語法錯誤{where}：{exc.msg}（第 {exc.offset} 字元附近）") from None
        except (ValueError, RecursionError, MemoryError):
            raise AlphaError(f"無法解析運算式{where}") from None
        count = sum(1 for _ in ast.walk(tree))
        if count > MAX_NODES:
            raise AlphaError(f"運算式太複雜{where}：最多 {MAX_NODES} 個節點")
        self.total_nodes += count
        if self.total_nodes > MAX_TOTAL_NODES:
            raise AlphaError("引用的 alpha 合計太複雜")
        return self.build(tree.body, 1)

    def build(self, node: ast.AST, depth: int) -> _Node:
        if depth > MAX_DEPTH:
            raise AlphaError("運算式巢狀太深（expression nested too deeply）")
        if isinstance(node, ast.Constant):
            value = self.constant(node)
            return _Node(repr(value), 1, lambda ctx: [value] * ctx.n)
        if isinstance(node, ast.Name):
            return self.name(node.id)
        if isinstance(node, ast.BinOp):
            op = _BINOPS.get(type(node.op))
            if op is None:
                raise AlphaError(f"不支援的運算子：{type(node.op).__name__}")
            a, b = self.build(node.left, depth + 1), self.build(node.right, depth + 1)
            symbol, f = op
            return _Node(f"({a.key}{symbol}{b.key})", 1 + max(a.height, b.height),
                         lambda ctx: _map(f, a.run(ctx), b.run(ctx)))
        if isinstance(node, ast.UnaryOp):
            x = self.build(node.operand, depth + 1)
            if isinstance(node.op, ast.USub):
                return _Node(f"(-{x.key})", x.height + 1, lambda ctx: _map(lambda v: -v, x.run(ctx)))
            if isinstance(node.op, ast.UAdd):
                return x
            if isinstance(node.op, ast.Not):
                return _Node(f"(not {x.key})", x.height + 1,
                             lambda ctx: _map(lambda v: _bool(not _truth(v)), x.run(ctx)))
            raise AlphaError(f"不支援的運算子：{type(node.op).__name__}")
        if isinstance(node, ast.Compare):
            return self.compare(node, depth)
        if isinstance(node, ast.BoolOp):
            parts = [self.build(v, depth + 1) for v in node.values]
            if isinstance(node.op, ast.And):
                symbol, f = " and ", lambda *vs: _bool(all(_truth(v) for v in vs))
            else:
                symbol, f = " or ", lambda *vs: _bool(any(_truth(v) for v in vs))
            return _Node("(" + symbol.join(p.key for p in parts) + ")",
                         1 + max(p.height for p in parts),
                         lambda ctx: _map(f, *(p.run(ctx) for p in parts)))
        if isinstance(node, ast.Call):
            return self.call(node, depth)
        raise AlphaError(f"不允許的語法：{type(node).__name__}")

    @staticmethod
    def constant(node: ast.Constant) -> float:
        if type(node.value) not in (int, float):
            raise AlphaError(f"只允許數字常數，不允許 {type(node.value).__name__}")
        try:
            value = float(node.value)
        except OverflowError:
            value = math.inf
        if not math.isfinite(value):
            raise AlphaError("數字常數太大")
        return value

    def compare(self, node: ast.Compare, depth: int) -> _Node:
        operands = [self.build(node.left, depth + 1)]
        operands += [self.build(c, depth + 1) for c in node.comparators]
        ops = []
        for op in node.ops:
            spec = _CMPOPS.get(type(op))
            if spec is None:
                raise AlphaError(f"不支援的比較：{type(op).__name__}")
            ops.append(spec)
        key = operands[0].key + "".join(s + o.key for (s, _), o in zip(ops, operands[1:]))

        def f(*vs):  # chained comparisons: a < b < c == (a < b) and (b < c)
            return _bool(all(fn(vs[i], vs[i + 1]) for i, (_, fn) in enumerate(ops)))

        return _Node(f"({key})", 1 + max(o.height for o in operands),
                     lambda ctx: _map(f, *(o.run(ctx) for o in operands)))

    def name(self, ident: str) -> _Node:
        if ident in VARIABLES:
            return _Node(ident, 1, lambda ctx: ctx.vars[ident])
        if ident in FUNCTIONS:
            raise AlphaError(f"{ident} 是函式，請用 {ident}(...) 呼叫")
        if ident not in self.pool:
            raise AlphaError(f"未知的名稱：{ident}")
        if ident in self.stack:
            cycle = self.stack[self.stack.index(ident):] + [ident]
            raise AlphaError("alpha 循環引用：" + " -> ".join(cycle))
        self.references.add(ident)
        if ident not in self.compiled:
            if len(self.stack) >= MAX_REF_DEPTH:
                raise AlphaError("alpha 引用層數太深")
            self.stack.append(ident)
            self.compiled[ident] = self.source(self.pool[ident], ident)
            self.stack.pop()
        return self.compiled[ident]

    def call(self, node: ast.Call, depth: int) -> _Node:
        if not isinstance(node.func, ast.Name):
            raise AlphaError("只能呼叫內建函式名稱")
        fname = node.func.id
        if fname not in _SPECS:
            raise AlphaError(f"未知的函式：{fname}")
        if node.keywords:
            raise AlphaError(f"{fname}：不支援關鍵字參數，請依序傳入")
        signature, min_window, impl = _SPECS[fname]
        if len(node.args) != len(signature):
            raise AlphaError(f"{fname} 需要 {len(signature)} 個參數，收到 {len(node.args)} 個（{FUNCTIONS[fname]}）")
        series: list[_Node] = []
        windows: list[int] = []
        keys: list[str] = []
        for kind, arg in zip(signature, node.args):
            if kind == "n":
                n = self.window(fname, arg, min_window)
                windows.append(n)
                keys.append(str(n))
            else:
                child = self.build(arg, depth + 1)
                series.append(child)
                keys.append(child.key)
        height = 1 + max(s.height for s in series)
        return _Node(f"{fname}({','.join(keys)})", height,
                     lambda ctx: impl(*(s.run(ctx) for s in series), *windows))

    @staticmethod
    def window(fname: str, arg: ast.AST, minimum: int) -> int:
        if not (isinstance(arg, ast.Constant) and type(arg.value) is int):
            raise AlphaError(f"{fname} 的天數必須是正整數常數")
        n = arg.value
        if n < max(minimum, 1) or n > MAX_WINDOW:
            raise AlphaError(f"{fname} 的天數必須介於 {max(minimum, 1)} 與 {MAX_WINDOW} 之間")
        return n


class Alpha:
    """A compiled, validated alpha expression."""

    def __init__(self, expr: str, node: _Node, references: Sequence[str]):
        self.expr = expr
        self.references: tuple[str, ...] = tuple(sorted(references))  # pool names used
        self._node = node

    def evaluate(self, bars: Sequence[DailyPrice]) -> list[float | None]:
        """One value per bar; None where undefined. Value t uses only bars[0..t]."""
        if not bars:
            return []
        ctx = _Ctx(adjusted_series(bars), len(bars))
        return [_clean(v) for v in self._node.run(ctx)]

    def __repr__(self) -> str:
        return f"Alpha({self.expr!r})"


def compile_alpha(expr: str, pool: Mapping[str, str] | None = None) -> Alpha:
    """Validate and compile ``expr``; names not built in are looked up in ``pool``."""
    compiler = _Compiler(pool)
    node = compiler.source(expr, None)
    return Alpha(expr, node, compiler.references)


# ---------------------------------------------------------------- evaluation

def _average_ranks(xs: Sequence[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _quantile_edges(sorted_values: list[float], q: int) -> list[float]:
    m = len(sorted_values)
    edges = []
    for k in range(1, q):
        pos = k * (m - 1) / q
        lo = int(math.floor(pos))
        hi = min(lo + 1, m - 1)
        edges.append(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo))
    return edges


def _avg(xs: Sequence[float]) -> float | None:
    return _clean(math.fsum(xs) / len(xs)) if xs else None


def _stats(pairs: list[tuple[float, float]], edges: list[float] | None, q: int) -> dict:
    a = [p[0] for p in pairs]
    f = [p[1] for p in pairs]
    n = len(pairs)
    nonzero = [(x, y) for x, y in pairs if x != 0 and y != 0]
    hits = sum(1 for x, y in nonzero if (x > 0) == (y > 0))
    quantile_returns = None
    if edges is not None:
        buckets: list[list[float]] = [[] for _ in range(q)]
        for x, y in pairs:
            buckets[bisect_right(edges, x)].append(y)
        quantile_returns = [
            {"bucket": i + 1, "n": len(b), "mean_fwd": _avg(b)} for i, b in enumerate(buckets)
        ]
    return {
        "n": n,
        "ic": _pearson(a, f),
        "rank_ic": _pearson(_average_ranks(a), _average_ranks(f)) if n >= 3 else None,
        "hit_rate": _clean(hits / len(nonzero)) if nonzero else None,
        "mean_fwd": _avg(f),
        "mean_fwd_when_positive": _avg([y for x, y in pairs if x > 0]),
        "mean_fwd_when_negative": _avg([y for x, y in pairs if x < 0]),
        "quantile_returns": quantile_returns,
    }


def evaluate_alpha(
    bars: Sequence[DailyPrice],
    values: Sequence[float | None],
    horizons: Sequence[int] = (1, 5, 20),
    split_date: date | None = None,
    quantiles: int = 5,
) -> dict:
    """Predictive statistics of an alpha series against forward returns.

    The signal at close t is traded at open t+1, so the h-day forward return is
    adj_open[t+1+h] / adj_open[t+1] - 1. In-sample rows (date < split_date) whose
    forward window reaches a bar on/after split_date are dropped (purged), so the
    in-sample numbers never use out-of-sample prices. Quantile edges come from
    in-sample alpha values only and are reused for the out-of-sample segment.
    """
    n_bars = len(bars)
    if len(values) != n_bars:
        raise AlphaError("alpha 值的數量必須與 K 棒數量相同")
    hs: list[int] = []
    for h in horizons:
        if type(h) is not int or not 1 <= h <= MAX_HORIZON:
            raise AlphaError(f"持有天數必須是 1 到 {MAX_HORIZON} 的整數：{h!r}")
        if h not in hs:
            hs.append(h)
    if not hs:
        raise AlphaError("至少需要一個持有天數")
    if type(quantiles) is not int or not 2 <= quantiles <= 20:
        raise AlphaError("分組數必須是 2 到 20 的整數")

    alpha = [_clean(v) for v in values]
    adj_open = adjusted_series(bars)["open"]

    def fwd(t: int, h: int) -> float | None:
        j = t + 1 + h
        if j >= n_bars:
            return None
        return _clean(adj_open[j] / adj_open[t + 1] - 1)

    in_idx = [t for t in range(n_bars) if split_date is None or bars[t].date < split_date]
    oos_idx = None if split_date is None else [t for t in range(n_bars) if bars[t].date >= split_date]

    in_values = sorted(alpha[t] for t in in_idx if alpha[t] is not None)
    edges = _quantile_edges(in_values, quantiles) if in_values else None

    def segment(idx: list[int], purge: bool) -> dict:
        defined = sum(1 for t in idx if alpha[t] is not None)
        result = {
            "start": bars[idx[0]].date.isoformat() if idx else None,
            "end": bars[idx[-1]].date.isoformat() if idx else None,
            "bars": len(idx),
            "coverage": _clean(defined / len(idx)) if idx else None,
            "horizons": {},
        }
        for h in hs:
            pairs = []
            for t in idx:
                if alpha[t] is None:
                    continue
                if purge and (t + 1 + h >= n_bars or bars[t + 1 + h].date >= split_date):
                    continue
                r = fwd(t, h)
                if r is not None:
                    pairs.append((alpha[t], r))
            result["horizons"][str(h)] = _stats(pairs, edges, quantiles)
        return result

    defined_all = sum(1 for v in alpha if v is not None)
    return {
        "bars": n_bars,
        "coverage": _clean(defined_all / n_bars) if n_bars else None,
        "horizons": hs,
        "split_date": split_date.isoformat() if split_date else None,
        "quantiles": quantiles,
        "quantile_edges": edges,
        "in_sample": segment(in_idx, purge=split_date is not None),
        "out_of_sample": segment(oos_idx, purge=False) if oos_idx is not None else None,
        "note": (
            "訊號於收盤 t 產生、t+1 開盤成交；h 日報酬 = 還原開盤價 open[t+1+h]/open[t+1]-1。"
            "h>1 的報酬區間彼此重疊，IC 等統計的顯著性會被高估。"
            "分組門檻只用樣本內 alpha 值計算；樣本內會剔除報酬區間跨入樣本外的資料。"
            " (Overlapping horizons > 1 inflate apparent significance; quantile edges use"
            " in-sample alpha only; in-sample pairs whose forward window reaches the"
            " out-of-sample period are purged.)"
        ),
    }
