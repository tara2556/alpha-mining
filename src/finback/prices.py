"""Daily price rows, ledger snapshots and shared statistics."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo


D = Decimal
TAIPEI = ZoneInfo("Asia/Taipei")


@dataclass(frozen=True)
class DailyPrice:
    date: date
    open: Decimal
    close: Decimal
    dividend_per_share: Decimal = D("0")
    external_flow: Decimal = D("0")
    # Shares held before this open become split_ratio times as many (1 = no split).
    split_ratio: Decimal = D("1")
    high: Decimal | None = None
    low: Decimal | None = None
    volume: Decimal | None = None

    def __post_init__(self) -> None:
        for name in ("open", "close", "dividend_per_share", "external_flow", "split_ratio",
                     "high", "low", "volume"):
            value = getattr(self, name)
            if value is not None and not value.is_finite():
                raise ValueError(f"{name} must be finite")
        if min(self.open, self.close) <= 0 or self.dividend_per_share < 0:
            raise ValueError("prices must be positive and dividends nonnegative")
        if self.split_ratio <= 0:
            raise ValueError("split_ratio must be positive")
        if self.high is not None and self.low is not None and not (
            self.low <= min(self.open, self.close) and max(self.open, self.close) <= self.high
        ):
            raise ValueError("high/low inconsistent with open/close")


@dataclass(frozen=True)
class Snapshot:
    date: date
    close: Decimal
    quantity: int
    cash: Decimal
    market_value: Decimal
    equity: Decimal
    fees: Decimal
    dividend_cash: Decimal
    external_flow: Decimal
    daily_return: Decimal
    wealth_index: Decimal
    drawdown: Decimal
    gross_exposure: Decimal
    net_exposure: Decimal
    benchmark_return: Decimal


def load_prices(path: Path) -> list[DailyPrice]:
    """Read raw daily prices; optional dividends and start-of-day cash flows."""
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not {"date", "open", "close"}.issubset(reader.fieldnames or []):
            raise ValueError("CSV requires date,open,close columns")
        rows = []
        for line, row in enumerate(reader, start=2):
            try:
                optional = {k: D(row[k]) for k in ("high", "low", "volume") if row.get(k)}
                rows.append(DailyPrice(
                    date.fromisoformat(row["date"]), D(row["open"]), D(row["close"]),
                    D(row.get("dividend_per_share") or "0"),
                    D(row.get("external_flow") or "0"),
                    D(row.get("split_ratio") or "1"),
                    **optional,
                ))
            except (ValueError, ArithmeticError, TypeError) as exc:
                raise ValueError(f"invalid CSV row {line}: {exc}") from exc
    return rows


def estimate_beta(pairs: list[tuple[Decimal, Decimal]]) -> Decimal | None:
    """OLS beta of (portfolio, benchmark) return pairs; None if undefined."""
    if len(pairs) < 2:
        return None
    mean_p = sum(p for p, _ in pairs) / len(pairs)
    mean_m = sum(m for _, m in pairs) / len(pairs)
    variance = sum((m - mean_m) ** 2 for _, m in pairs)
    if not variance:
        return None
    return sum((p - mean_p) * (m - mean_m) for p, m in pairs) / variance
