"""Single-instrument, long-only, daily strategy backtester for Taiwan cash equities.

Timeline for each trading day t (Asia/Taipei):
  1. before the open: external cash flows, split adjustment, cash dividends to shares
     held before t
  2. at the open: fill the target decided after the previous close (never today's close)
  3. at the close: mark to market, then ask the strategy for tomorrow's target

The strategy only ever sees bars[: t + 1], so a decision made at t's close can use
t's close but is filled at t+1's open. The first decision is asked before day 0's
open with an empty history.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, time
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from typing import Protocol

from finback import Account, AssetClass, Fill, Instrument, OrderSide
from finback.prices import TAIPEI, DailyPrice, Snapshot, estimate_beta

D = Decimal


class Strategy(Protocol):
    """Return the target stock weight (0..1 of equity) for the next open, or None to hold.

    `history` holds every bar up to and including the latest close. It is empty for
    the decision taken before the first open.
    """

    def target_weight(self, history: Sequence[DailyPrice]) -> Decimal | None: ...


@dataclass(frozen=True)
class CostModel:
    """Taiwan cash-equity costs. Rates are inputs; nothing here is a broker quote.

    Commission = max(floor(notional × rate × discount), minimum) per fill.
    Sell tax = floor(notional × sell_tax_rate). Amounts are floored to whole TWD.
    """

    commission_rate: Decimal = D("0.001425")
    commission_discount: Decimal = D("1")
    min_commission: Decimal = D("20")
    sell_tax_rate: Decimal = D("0.003")

    def __post_init__(self) -> None:
        for name in ("commission_rate", "commission_discount", "min_commission",
                     "sell_tax_rate"):
            value = getattr(self, name)
            if not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if self.commission_rate * self.commission_discount >= 1 or self.sell_tax_rate >= 1:
            raise ValueError("cost rates must be below 1")

    def commission(self, notional: Decimal) -> Decimal:
        raw = (notional * self.commission_rate * self.commission_discount).to_integral_value(
            rounding=ROUND_FLOOR)
        return max(raw, self.min_commission)

    def tax(self, notional: Decimal) -> Decimal:
        return (notional * self.sell_tax_rate).to_integral_value(rounding=ROUND_FLOOR)


@dataclass(frozen=True)
class Trade:
    date: str
    side: str
    quantity: int
    price: Decimal
    notional: Decimal
    commission: Decimal
    tax: Decimal
    target_weight: Decimal
    quantity_after: int
    cash_after: Decimal


@dataclass(frozen=True)
class BacktestResult:
    name: str
    rows: tuple[Snapshot, ...]
    trades: tuple[Trade, ...]
    total_return: Decimal
    max_drawdown: Decimal
    beta: Decimal | None
    metrics: dict = field(default_factory=dict)


def run_backtest(
    prices: Sequence[DailyPrice],
    strategy: Strategy,
    name: str,
    initial_cash: Decimal = D("1000000"),
    costs: CostModel = CostModel(),
    lot_size: int = 1,
    symbol: str = "RESEARCH",
) -> BacktestResult:
    if not prices:
        raise ValueError("at least one daily price is required")
    if any(a.date >= b.date for a, b in zip(prices, prices[1:])):
        raise ValueError("dates must be unique and strictly increasing")
    if not initial_cash.is_finite() or initial_cash <= 0:
        raise ValueError("initial_cash must be positive and finite")
    if lot_size < 1:
        raise ValueError("lot_size must be at least 1")

    instrument = Instrument(symbol, AssetClass.STOCK)
    account = Account(initial_cash)
    quantity = 0
    previous_equity = initial_cash
    wealth = peak = D("1")
    rows: list[Snapshot] = []
    trades: list[Trade] = []
    # Optional hook: precompute causal signals once instead of re-deriving them per day.
    # A strategy using it must still only read values up to len(history) - 1.
    prepare = getattr(strategy, "prepare", None)
    if prepare is not None:
        prepare(prices)
    pending = _checked_weight(strategy.target_weight(prices[:0]))

    for i, bar in enumerate(prices):
        if bar.split_ratio != 1 and bar.dividend_per_share:
            raise ValueError(f"{bar.date}: split and dividend on the same day are unsupported")
        denominator = previous_equity + bar.external_flow
        if denominator <= 0 or account.cash + bar.external_flow < 0:
            raise ValueError(f"{bar.date}: external flow exhausts equity or exceeds cash")
        account.cash += bar.external_flow

        if bar.split_ratio != 1:
            quantity = _apply_split(account, instrument, quantity, bar)
        dividend_cash = quantity * bar.dividend_per_share
        account.cash += dividend_cash

        fees = D("0")
        if pending is not None:
            trade = _rebalance(account, instrument, quantity, bar, pending, costs, lot_size)
            if trade:
                trades.append(trade)
                quantity = trade.quantity_after
                fees = trade.commission + trade.tax
            pending = None

        equity = account.equity({symbol: bar.close})
        daily_return = equity / denominator - 1
        wealth *= 1 + daily_return
        peak = max(peak, wealth)
        market_value = quantity * bar.close
        benchmark_return = (
            bar.close / bar.open - 1 if i == 0
            else bar.split_ratio * (bar.close + bar.dividend_per_share) / prices[i - 1].close - 1
        )
        rows.append(Snapshot(
            bar.date, bar.close, quantity, account.cash, market_value, equity, fees,
            dividend_cash, bar.external_flow, daily_return, wealth, wealth / peak - 1,
            market_value / equity, market_value / equity, benchmark_return,
        ))
        previous_equity = equity
        if i + 1 < len(prices):
            pending = _checked_weight(strategy.target_weight(prices[: i + 1]))

    beta = estimate_beta([(r.daily_return, r.benchmark_return) for r in rows[1:]])
    buys = [t for t in trades if t.side == "buy"]
    metrics = {
        "trades": len(trades),
        "buys": len(buys),
        "sells": len(trades) - len(buys),
        "commission": str(sum((t.commission for t in trades), D(0))),
        "tax": str(sum((t.tax for t in trades), D(0))),
        "turnover": str(sum((t.notional for t in trades), D(0))),
        "days_invested": sum(1 for r in rows if r.quantity),
    }
    return BacktestResult(name, tuple(rows), tuple(trades), wealth - 1,
                          min(r.drawdown for r in rows), beta, metrics)


def _checked_weight(weight: Decimal | None) -> Decimal | None:
    if weight is None:
        return None
    weight = D(weight)
    if not weight.is_finite() or not 0 <= weight <= 1:
        raise ValueError(f"strategy returned weight {weight}; long-only needs 0..1")
    return weight


def _apply_split(account: Account, instrument: Instrument, quantity: int,
                 bar: DailyPrice) -> int:
    new_quantity = quantity * bar.split_ratio
    if new_quantity != new_quantity.to_integral_value():
        raise ValueError(f"{bar.date}: split leaves fractional shares; unsupported")
    position = account.positions.get(instrument.symbol)
    if position and position.quantity:
        position.quantity = int(new_quantity)
        position.average_price /= bar.split_ratio
    return int(new_quantity)


def _rebalance(account: Account, instrument: Instrument, quantity: int, bar: DailyPrice,
               weight: Decimal, costs: CostModel, lot: int) -> Trade | None:
    """Move to `weight` of open equity in whole lots; buys never overdraw cash."""
    price = bar.open
    equity_open = account.cash + quantity * price
    desired = int((weight * equity_open / price / lot).to_integral_value(ROUND_FLOOR)) * lot
    filled_at = datetime.combine(bar.date, time(9), tzinfo=TAIPEI)
    if desired > quantity:
        n = desired - quantity
        rate = costs.commission_rate * costs.commission_discount
        n = min(n, int((account.cash / (price * (1 + rate)) / lot).to_integral_value(
            ROUND_FLOOR)) * lot)
        while n > 0 and n * price + costs.commission(n * price) > account.cash:
            n -= lot
        if n <= 0:
            return None
        side, commission, tax = OrderSide.BUY, costs.commission(n * price), D(0)
    elif desired < quantity:
        n = quantity - desired
        side = OrderSide.SELL
        commission, tax = costs.commission(n * price), costs.tax(n * price)
    else:
        return None
    account.apply_fill(Fill(f"{bar.date}-{side}", instrument, side, n, price, filled_at,
                            commission + tax))
    after = quantity + side.sign * n
    return Trade(bar.date.isoformat(), str(side), n, price, n * price, commission, tax,
                 weight, after, account.cash)


def write_result(result: BacktestResult, folder: Path, label: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / f"ledger_{label}.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(Snapshot.__dataclass_fields__))
        writer.writeheader()
        writer.writerows(asdict(row) for row in result.rows)
    with (folder / f"trades_{label}.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(Trade.__dataclass_fields__))
        writer.writeheader()
        writer.writerows(asdict(trade) for trade in result.trades)
