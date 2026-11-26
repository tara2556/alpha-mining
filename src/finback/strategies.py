"""Built-in strategies. Each decides after a close; the engine fills at the next open.

To add a strategy: write a class with `target_weight(history) -> Decimal | None`
and register it in STRATEGIES. Return None to keep the current holdings; return a
weight (0..1) to trade to that share of equity at the next open.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from finback.prices import DailyPrice

D = Decimal


def sma(history: Sequence[DailyPrice], window: int) -> Decimal:
    return sum(bar.close for bar in history[-window:]) / window


@dataclass(frozen=True)
class BuyAndHold:
    """Buy `weight` at the first open, then never trade again."""

    weight: Decimal = D("1")

    def target_weight(self, history: Sequence[DailyPrice]) -> Decimal | None:
        return self.weight if not history else None


@dataclass(frozen=True)
class FixedWeight:
    """Hold `weight`; rebalance at the open after the first trading day of each period."""

    weight: Decimal = D("0.5")
    period: str = "month"  # "month" or "quarter"

    def __post_init__(self) -> None:
        if self.period not in ("month", "quarter"):
            raise ValueError("period must be month or quarter")

    def _key(self, bar: DailyPrice) -> tuple[int, int]:
        m = bar.date.month
        return bar.date.year, m if self.period == "month" else (m - 1) // 3

    def target_weight(self, history: Sequence[DailyPrice]) -> Decimal | None:
        if not history:
            return self.weight
        if len(history) >= 2 and self._key(history[-1]) != self._key(history[-2]):
            return self.weight
        return None


@dataclass(frozen=True)
class SmaCross:
    """Hold `weight` while SMA(fast) > SMA(slow) of closes, otherwise cash.

    Trades only when the regime flips (or on the first day both averages exist),
    so daily price drift does not cause tiny rebalancing trades.
    """

    fast: int = 20
    slow: int = 60
    weight: Decimal = D("1")

    def __post_init__(self) -> None:
        if not 1 <= self.fast < self.slow:
            raise ValueError("need 1 <= fast < slow")

    def _signal(self, history: Sequence[DailyPrice]) -> bool | None:
        if len(history) < self.slow:
            return None
        return sma(history, self.fast) > sma(history, self.slow)

    def target_weight(self, history: Sequence[DailyPrice]) -> Decimal | None:
        today = self._signal(history)
        if today is None or today == self._signal(history[:-1]):
            return None
        return self.weight if today else D("0")


@dataclass(frozen=True)
class Momentum:
    """Hold `weight` when the close is above the close `lookback` days ago, else cash."""

    lookback: int = 120
    weight: Decimal = D("1")

    def __post_init__(self) -> None:
        if self.lookback < 1:
            raise ValueError("lookback must be positive")

    def _signal(self, history: Sequence[DailyPrice]) -> bool | None:
        if len(history) <= self.lookback:
            return None
        return history[-1].close > history[-1 - self.lookback].close

    def target_weight(self, history: Sequence[DailyPrice]) -> Decimal | None:
        today = self._signal(history)
        if today is None or today == self._signal(history[:-1]):
            return None
        return self.weight if today else D("0")


class AlphaRule:
    """Hold `weight` while a formula alpha says so, with hysteresis.

    Flat -> long when alpha > entry; long -> flat when alpha < exit (exit <= entry).
    An undefined alpha value (warm-up) keeps the current state. Trades only on flips.
    """

    def __init__(self, expr: str, entry=0, exit=0, weight=1,
                 pool: dict[str, str] | None = None) -> None:
        from finback.alpha import compile_alpha  # formula module; imported lazily

        self.expr = expr
        self.entry, self.exit, self.weight = float(entry), float(exit), D(str(weight))
        if self.exit > self.entry:
            raise ValueError("exit threshold must not exceed entry threshold")
        if not 0 <= self.weight <= 1:
            raise ValueError("weight must be between 0 and 1")
        self.alpha = compile_alpha(expr, pool or {})
        self._prices: Sequence[DailyPrice] | None = None
        self._decisions: list[Decimal | None] = []
        self.values: list[float | None] = []

    def prepare(self, prices: Sequence[DailyPrice]) -> None:
        self._prices = prices
        self.values = self.alpha.evaluate(prices)
        self._decisions = self.decisions(self.values)

    def decisions(self, values: Sequence[float | None]) -> list[Decimal | None]:
        out, long = [], False
        for v in values:
            before = long
            if v is not None:
                if not long and v > self.entry:
                    long = True
                elif long and v < self.exit:
                    long = False
            out.append(None if long == before else (self.weight if long else D(0)))
        return out

    def target_weight(self, history: Sequence[DailyPrice]) -> Decimal | None:
        if not history:
            return None
        t = len(history) - 1
        prepared = self._prices
        if prepared is None or t >= len(prepared) or prepared[t] is not history[-1]:
            # Not prepared for this series: compute on the visible history only.
            return self.decisions(self.alpha.evaluate(history))[-1]
        return self._decisions[t]


STRATEGIES = {
    "buy_and_hold": BuyAndHold,
    "fixed_weight": FixedWeight,
    "sma_cross": SmaCross,
    "momentum": Momentum,
    "alpha": AlphaRule,
}


def build(spec: dict, pool: dict[str, str] | None = None):
    """Build from a config entry such as {"type": "sma_cross", "fast": 20, "slow": 60}.

    `pool` maps alpha names to formulas, used by {"type": "alpha", "expr": "..."}.
    """
    params = {k: v for k, v in spec.items() if k not in ("type", "label", "name")}
    kind = spec.get("type")
    if kind not in STRATEGIES:
        raise ValueError(f"unknown strategy type {kind!r}; choose from {sorted(STRATEGIES)}")
    if "weight" in params and kind != "alpha":
        params["weight"] = D(str(params["weight"]))
    if kind == "alpha":
        params.setdefault("pool", pool)
    try:
        return STRATEGIES[kind](**params)
    except TypeError as exc:
        raise ValueError(f"bad parameters for {kind}: {exc}") from exc
