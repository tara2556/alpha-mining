"""Buyer-side TXO backtest driven by an alpha on the TAIEX index.

Each trading day t:
  1. at the open: execute orders decided after close t-1 (closes first, then opens) at the
     contract's day-session OPEN; no valid open (0 or zero volume) -> "missed", never invented
  2. at the close: cash-settle a contract expiring today, mark the position to market
  3. after the close: evaluate the alpha (data <= t), update the long/short/flat state and pick
     the contract for tomorrow using only day-t information (listed strikes, rule expiry)

Long premium only: at most one position, never negative cash, max loss = premium + costs.
"""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path

from finback.alpha import compile_alpha
from finback.options_data import MULTIPLIER, OptionChain, Quote

D = Decimal


@dataclass(frozen=True)
class OptionParams:
    long_entry: float = 0.0
    long_exit: float = 0.0
    short_entry: float = 0.0      # go long puts when alpha < short_entry
    short_exit: float = 0.0       # leave puts when alpha > short_exit
    allow_puts: bool = True
    series: str = "monthly"       # "monthly" only, or "all" (adds Wednesday/Friday weeklies)
    moneyness_steps: int = 0      # 0 = ATM; positive = out of the money
    min_days_to_expiry: int = 10  # calendar days from the fill day to expiry
    roll_days: int = 2            # roll when expiry is fewer calendar days away
    premium_fraction: Decimal = D("0.1")
    commission: Decimal = D("25")  # per contract per side (assumption)
    tax_rate: Decimal = D("0.001")  # on premium and on settlement payment (assumption)
    initial_cash: Decimal = D("1000000")

    def __post_init__(self) -> None:
        if self.long_exit > self.long_entry:
            raise ValueError("long_exit 不可大於 long_entry")
        if self.short_exit < self.short_entry:
            raise ValueError("short_exit 不可小於 short_entry")
        if self.series not in ("monthly", "all"):
            raise ValueError("series 必須是 monthly 或 all")
        if not 0 < self.premium_fraction <= 1:
            raise ValueError("premium_fraction 需介於 0 與 1")
        if self.moneyness_steps < -20 or self.moneyness_steps > 20:
            raise ValueError("moneyness_steps 超出範圍")
        if self.min_days_to_expiry < 0 or self.roll_days < 0 or self.initial_cash <= 0:
            raise ValueError("天數與本金需為正")
        if self.min_days_to_expiry < self.roll_days:
            raise ValueError("min_days_to_expiry 需 ≥ roll_days，否則新契約會立刻又被換月")


@dataclass(frozen=True)
class Position:
    contract: str
    expiry: date
    strike: Decimal
    call_put: str
    contracts: int
    entry_price: Decimal

    @property
    def key(self) -> tuple[str, Decimal, str]:
        return self.contract, self.strike, self.call_put


@dataclass(frozen=True)
class OptionTrade:
    date: str
    action: str  # open | close | roll | expire | missed
    contract: str
    expiry: str
    strike: Decimal
    call_put: str
    contracts: int
    price: Decimal
    premium: Decimal
    commission: Decimal
    tax: Decimal
    cash_after: Decimal
    reason: str


@dataclass(frozen=True)
class OptionSnapshot:
    date: date
    underlying_close: Decimal
    alpha: float | None
    state: str
    contract: str
    strike: Decimal | None
    call_put: str
    contracts: int
    mark: Decimal | None
    stale_mark: bool
    cash: Decimal
    position_value: Decimal
    equity: Decimal
    fees: Decimal
    daily_return: Decimal
    wealth_index: Decimal
    drawdown: Decimal
    underlying_return: Decimal


@dataclass
class OptionResult:
    rows: list[OptionSnapshot]
    trades: list[OptionTrade]
    params: OptionParams
    expr: str
    settlement_source: str
    metrics: dict = field(default_factory=dict)


def _fee(params: OptionParams, contracts: int, notional: Decimal) -> tuple[Decimal, Decimal]:
    tax = (notional * params.tax_rate).to_integral_value(rounding=ROUND_FLOOR)
    return params.commission * contracts, tax


def _eligible(contract: str, series: str) -> bool:
    return series == "all" or contract.isdigit()


def select_contract(quotes: list[Quote], chain: OptionChain, day: date, fill_day: date,
                    spot: Decimal, side: str, params: OptionParams) -> tuple[str, Decimal, date] | None:
    """Nearest eligible expiry >= min days after fill_day; strike nearest spot, shifted OTM."""
    cp = "call" if side == "long" else "put"
    strikes: dict[str, set[Decimal]] = {}
    for q in quotes:
        if q.call_put == cp and _eligible(q.contract, params.series):
            strikes.setdefault(q.contract, set()).add(q.strike)
    choices = []
    for contract, listed in strikes.items():
        expiry = chain.calendar.expiry(contract)
        if (expiry - fill_day).days >= params.min_days_to_expiry and expiry > day:
            choices.append((expiry, contract, sorted(listed)))
    if not choices:
        return None
    expiry, contract, listed = min(choices)
    atm = min(range(len(listed)), key=lambda i: (abs(listed[i] - spot), listed[i]))
    step = params.moneyness_steps if cp == "call" else -params.moneyness_steps
    i = atm + step
    if not 0 <= i < len(listed):
        return None
    return contract, listed[i], expiry


def run_options_backtest(chain: OptionChain, expr: str, params: OptionParams,
                         pool: dict[str, str] | None = None, start: date | None = None,
                         end: date | None = None) -> OptionResult:
    bars_all = chain.underlying
    alpha_all = compile_alpha(expr, pool or {}).evaluate(bars_all)  # causal: value t uses <= t
    idx = [i for i, b in enumerate(bars_all)
           if (start is None or b.date >= start) and (end is None or b.date <= end)]
    if len(idx) < 2:
        raise ValueError("期間內交易日不足")
    covered = set(chain.meta["months"])
    if any(f"{bars_all[i].date:%Y-%m}" not in covered for i in idx):
        raise ValueError("選擇權資料未涵蓋整個回測期間，請先下載缺少的月份")

    cash = params.initial_cash
    pos: Position | None = None
    state = "flat"
    pending_close: str | None = None              # reason
    pending_open: tuple[str, Decimal, date, str] | None = None  # contract, strike, expiry, reason
    last_mark: Decimal | None = None
    prev_equity = cash
    wealth = peak = D(1)
    rows: list[OptionSnapshot] = []
    trades: list[OptionTrade] = []

    def log(day, action, p_contract, expiry, strike, cp, n, price, premium, comm, tax, reason):
        trades.append(OptionTrade(day.isoformat(), action, p_contract, expiry.isoformat(), strike,
                                  cp, n, price, premium, comm, tax, cash, reason))

    for k, i in enumerate(idx):
        bar = bars_all[i]
        day = bar.date
        quotes = chain.quotes(day)
        book = {(q.contract, q.strike, q.call_put): q for q in quotes}
        fees = D(0)

        # 1. open: closes first
        if pending_close and pos:
            q = book.get(pos.key)
            if q and q.tradable_open:
                premium = q.open * MULTIPLIER * pos.contracts
                comm, tax = _fee(params, pos.contracts, premium)
                cash += premium - comm - tax
                fees += comm + tax
                log(day, "close", pos.contract, pos.expiry, pos.strike, pos.call_put,
                    pos.contracts, q.open, premium, comm, tax, pending_close)
                pos, last_mark, pending_close = None, None, None
            else:
                log(day, "missed", pos.contract, pos.expiry, pos.strike, pos.call_put,
                    pos.contracts, D(0), D(0), D(0), D(0), f"賣出無報價：{pending_close}")
        if pending_open and pos is None:
            contract, strike, expiry, reason = pending_open
            cp = "call" if state == "long" else "put"
            q = book.get((contract, strike, cp))
            if q and q.tradable_open:
                unit = q.open * MULTIPLIER
                budget = cash * params.premium_fraction
                n = int((budget / (unit + params.commission)).to_integral_value(ROUND_FLOOR))
                while n > 0 and sum(_fee(params, n, unit * n)) + unit * n > cash:
                    n -= 1
                if n > 0:
                    comm, tax = _fee(params, n, unit * n)
                    cash -= unit * n + comm + tax
                    fees += comm + tax
                    pos = Position(contract, expiry, strike, cp, n, q.open)
                    last_mark = q.open
                    log(day, "open", contract, expiry, strike, cp, n, q.open, unit * n, comm, tax,
                        reason)
                else:
                    log(day, "missed", contract, expiry, strike, cp, 0, q.open, D(0), D(0), D(0),
                        "預算不足一口")
            else:
                log(day, "missed", contract, expiry, strike, cp, 0, D(0), D(0), D(0), D(0),
                    f"買進無報價：{reason}")
        pending_open = None

        # 2. close: expiry settlement, then mark to market
        if pos and pos.expiry == day:
            settle = bar.close  # approximation of the official final settlement price
            intrinsic = max(D(0), settle - pos.strike if pos.call_put == "call"
                            else pos.strike - settle)
            payment = intrinsic * MULTIPLIER * pos.contracts
            comm, tax = _fee(params, pos.contracts, payment) if payment else (D(0), D(0))
            cash += payment - comm - tax
            fees += comm + tax
            log(day, "expire", pos.contract, pos.expiry, pos.strike, pos.call_put, pos.contracts,
                intrinsic, payment, comm, tax, f"到期結算（加權收盤 {settle} 近似）")
            pos, last_mark, pending_close = None, None, None

        stale = False
        if pos:
            q = book.get(pos.key)
            mark = q.mark if q else None
            if mark is None:
                mark, stale = last_mark, True
            last_mark = mark
            position_value = mark * MULTIPLIER * pos.contracts
        else:
            mark, position_value = None, D(0)
        equity = cash + position_value
        daily = equity / prev_equity - 1
        wealth *= 1 + daily
        peak = max(peak, wealth)
        prev_close = bars_all[i - 1].close if i > 0 else bar.open
        a = alpha_all[i]

        # 3. after the close: state machine and orders for tomorrow
        if a is not None:
            if state == "long" and a < params.long_exit:
                state = "flat"
            elif state == "short" and a > params.short_exit:
                state = "flat"
            if state == "flat":
                if a > params.long_entry:
                    state = "long"
                elif params.allow_puts and a < params.short_entry:
                    state = "short"
        rows.append(OptionSnapshot(
            day, bar.close, a, state, pos.contract if pos else "", pos.strike if pos else None,
            pos.call_put if pos else "", pos.contracts if pos else 0, mark, stale, cash,
            position_value, equity, fees, daily, wealth, wealth / peak - 1,
            bar.close / prev_close - 1))
        prev_equity = equity

        if k + 1 >= len(idx):
            break
        fill_day = bars_all[idx[k + 1]].date
        want = {"long": "call", "short": "put"}.get(state)
        pending_close = None  # re-decided every evening (a missed close is retried if still due)
        if pos:
            if want != pos.call_put:
                pending_close = "訊號轉為空手" if want is None else "訊號反轉"
            elif (pos.expiry - fill_day).days < params.roll_days:
                pending_close = "到期前換月"
        if want and (pos is None or pending_close):
            pick = select_contract(quotes, chain, day, fill_day, bar.close, state, params)
            if pick:
                pending_open = (*pick, "換月" if pending_close == "到期前換月" else "訊號進場")

    result = OptionResult(rows, trades, params, expr, chain.meta.get("settlement_source", ""))
    result.metrics = _metrics(result)
    return result


def _metrics(result: OptionResult) -> dict:
    rows, trades = result.rows, result.trades
    opens = [t for t in trades if t.action == "open"]
    closes = [t for t in trades if t.action in ("close", "expire")]
    # Independent cash check: replay every trade's cash effect from the initial cash.
    cash = result.params.initial_cash
    for t in trades:
        if t.action == "open":
            cash -= t.premium + t.commission + t.tax
        elif t.action in ("close", "expire"):
            cash += t.premium - t.commission - t.tax
        if cash != t.cash_after:
            break
    cash_ok = cash == rows[-1].cash and all(r.cash >= 0 for r in rows)
    worst = max(abs(r.cash + r.position_value - r.equity) for r in rows)
    return {
        "final_equity": str(rows[-1].equity),
        "total_return": str(rows[-1].wealth_index - 1),
        "max_drawdown": str(min(r.drawdown for r in rows)),
        "entries": len(opens),
        "calls": sum(1 for t in opens if t.call_put == "call"),
        "puts": sum(1 for t in opens if t.call_put == "put"),
        "expired": sum(1 for t in trades if t.action == "expire"),
        "expired_worthless": sum(1 for t in trades if t.action == "expire" and t.premium == 0),
        "missed": sum(1 for t in trades if t.action == "missed"),
        "stale_marks": sum(1 for r in rows if r.stale_mark),
        "premium_paid": str(sum((t.premium for t in opens), D(0))),
        "premium_received": str(sum((t.premium for t in closes), D(0))),
        "costs": str(sum((t.commission + t.tax for t in trades), D(0))),
        "days_in_market": sum(1 for r in rows if r.contracts),
        "reconciled": worst == 0 and cash_ok,
        "settlement_source": result.settlement_source,
    }


def write_options_result(result: OptionResult, folder: Path, label: str) -> None:
    """Report-viewer-compatible ledger + trades, plus the full options ledger."""
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / f"ledger_{label}.csv").open("w", encoding="utf-8", newline="") as stream:
        w = csv.writer(stream)
        w.writerow(["date", "close", "quantity", "cash", "market_value", "equity", "fees",
                    "dividend_cash", "external_flow", "daily_return", "wealth_index", "drawdown",
                    "gross_exposure", "net_exposure", "benchmark_return"])
        for r in result.rows:
            exposure = r.position_value / r.equity if r.equity else D(0)
            w.writerow([r.date, r.underlying_close, r.contracts, r.cash, r.position_value,
                        r.equity, r.fees, 0, 0, r.daily_return, r.wealth_index, r.drawdown,
                        exposure, exposure, r.underlying_return])
    with (folder / f"options_ledger_{label}.csv").open("w", encoding="utf-8", newline="") as stream:
        w = csv.DictWriter(stream, fieldnames=list(OptionSnapshot.__dataclass_fields__))
        w.writeheader()
        w.writerows(asdict(r) for r in result.rows)
    with (folder / f"trades_{label}.csv").open("w", encoding="utf-8", newline="") as stream:
        w = csv.DictWriter(stream, fieldnames=list(OptionTrade.__dataclass_fields__))
        w.writeheader()
        w.writerows(asdict(t) for t in result.trades)
