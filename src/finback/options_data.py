"""TAIEX options (TXO) daily chains from FinMind, stored for lookahead-free backtests.

    python -m finback.options_data --start 2025-01-01 --end 2025-12-31

FinMind findings (checked 2026-11, free tier, no token):
- `TaiwanOptionDaily` data_id=TXO accepts a multi-day range; one month is ~25k rows (~11 MB JSON)
  covering both sessions. Only `trading_session == "position"` (day session) is kept.
- Contract codes: `YYYYMM` monthly (3rd Wednesday), `YYYYMMWn` Wednesday weeklies (n-th
  Wednesday; W3 does not exist because it is the monthly), `YYYYMMFn` Friday weeklies (n-th
  Friday, listed from mid-2025). A holiday expiry moves to the next trading day (e.g. 202510F2:
  2025-10-10 holiday -> traded until 2025-10-13).
- Contracts trade on their expiry day; `settlement_price` is 0 on that day and FinMind has no
  final-settlement dataset. TXO's official final settlement price is the average TAIEX of the
  last 30 minutes on expiry day, so the TAIEX close is used as an approximation
  (`settlement_source = "approx_taiex_close"`).
- About 60% of rows have open == 0 (no trade); fills must be skipped, never invented.
- Underlying: `TaiwanStockPrice` data_id=TAIEX.

Layout (all under the project root):
  data/raw/finmind/TXO/<YYYY-MM>.<stamp>.json.gz   exact response bytes (gzip), never overwritten
  data/processed/finmind_TXO/<YYYY-MM>.csv.gz       day-session rows of that month
  data/processed/finmind_TXO/taiex.csv              underlying OHLCV
  data/processed/finmind_TXO/metadata.json          sources, hashes, coverage
Downloads are resumable: months already processed are skipped unless --refresh.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import gzip
import hashlib
import io
import json
import re
import time
from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from finback import finmind
from finback.prices import DailyPrice

D = Decimal
MULTIPLIER = D(50)  # TWD per index point
FOLDER = "finmind_TXO"
CODE = re.compile(r"^(\d{4})(\d{2})(?:([WF])([1-5]))?$")
CHAIN_FIELDS = ("date", "contract", "strike", "call_put", "open", "high", "low", "close",
                "volume", "settlement_price", "open_interest")


@dataclass(frozen=True, slots=True)
class Quote:
    date: date
    contract: str
    strike: Decimal
    call_put: str  # "call" | "put"
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    settlement_price: Decimal
    open_interest: int

    @property
    def tradable_open(self) -> bool:
        return self.open > 0 and self.volume > 0

    @property
    def mark(self) -> Decimal | None:
        if self.close > 0:
            return self.close
        if self.settlement_price > 0:
            return self.settlement_price
        return None


# ---------- expiry rules ----------
def parse_contract(code: str) -> tuple[int, int, str, int]:
    """'202506' -> (2025, 6, 'M', 3); '202506W2' -> (2025, 6, 'W', 2); 'F1' Friday weeklies."""
    m = CODE.match(code)
    if not m:
        raise ValueError(f"unknown TXO contract code {code!r}")
    year, month, kind, n = int(m[1]), int(m[2]), m[3] or "M", int(m[4] or 3)
    if not 1 <= month <= 12:
        raise ValueError(f"bad month in contract {code!r}")
    return year, month, kind, n


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date | None:
    """n-th weekday (Mon=0) of a month, or None if the month has fewer."""
    first = date(year, month, 1)
    day = first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    return day if day.month == month else None


def scheduled_expiry(code: str) -> date:
    """Calendar expiry before holiday adjustment (known when the contract is listed)."""
    year, month, kind, n = parse_contract(code)
    day = nth_weekday(year, month, 4 if kind == "F" else 2, n)
    if day is None:
        raise ValueError(f"{code}: month has no {n}th expiry weekday")
    return day


class TradingCalendar:
    """Exchange trading days. Holidays are published in advance, so using them is not lookahead."""

    def __init__(self, days: list[date], start: date | None = None,
                 end: date | None = None) -> None:
        """days: every trading day in [start, end] (defaults: first and last listed day)."""
        self.days = sorted(days)
        self._set = set(self.days)
        self.start = start or (self.days[0] if self.days else None)
        self.end = end or (self.days[-1] if self.days else None)

    def is_trading_day(self, day: date) -> bool:
        if self.start and self.start <= day <= self.end:
            return day in self._set
        return day.weekday() < 5  # beyond the data: assume weekdays trade

    def expiry(self, code: str) -> date:
        day = scheduled_expiry(code)
        while not self.is_trading_day(day):
            day += timedelta(days=1)
        return day

    def next_day(self, day: date) -> date | None:
        i = bisect_left(self.days, day + timedelta(days=1))
        return self.days[i] if i < len(self.days) else None


# ---------- download ----------
def _month_spans(start: date, end: date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        last = calendar.monthrange(y, m)[1]
        yield (f"{y:04d}-{m:02d}", max(start, date(y, m, 1)), min(end, date(y, m, last)))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def _parse_chain(payload: dict, label: str) -> list[dict]:
    if payload.get("status") != 200 or not isinstance(payload.get("data"), list):
        raise ValueError(f"FinMind TXO {label}: {payload.get('msg')}")
    out = []
    for r in payload["data"]:
        if r.get("option_id") != "TXO":
            raise ValueError(f"TXO {label}: unexpected option_id {r.get('option_id')}")
        if r.get("trading_session") != "position":
            continue
        if r["call_put"] not in ("call", "put"):
            raise ValueError(f"TXO {label}: bad call_put {r['call_put']}")
        parse_contract(r["contract_date"])
        nums = [D(str(r[k])) for k in ("strike_price", "open", "max", "min", "close",
                                         "settlement_price")]
        if nums[0] <= 0 or any(x < 0 for x in nums) or r["volume"] < 0:
            raise ValueError(f"TXO {label}: negative or zero field in {r}")
        out.append({"date": r["date"], "contract": r["contract_date"], "strike": nums[0],
                    "call_put": r["call_put"], "open": nums[1], "high": nums[2], "low": nums[3],
                    "close": nums[4], "volume": int(r["volume"]), "settlement_price": nums[5],
                    "open_interest": int(r.get("open_interest") or 0)})
    out.sort(key=lambda x: (x["date"], x["contract"], x["strike"], x["call_put"]))
    keys = [(x["date"], x["contract"], x["strike"], x["call_put"]) for x in out]
    if len(keys) != len(set(keys)):
        raise ValueError(f"TXO {label}: duplicate day-session rows")
    return out


def download(start: date, end: date, root: Path, refresh: bool = False,
             pause: float = 2.0, log=print) -> Path:
    folder = root / "data/processed" / FOLDER
    raw_dir = root / "data/raw/finmind/TXO"
    folder.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    meta_path = folder / "metadata.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {
        "provider": "FinMind", "symbol": "TXO", "months": {}, "underlying": None}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    for label, lo, hi in _month_spans(start, end):
        out = folder / f"{label}.csv.gz"
        prev = meta["months"].get(label)
        complete = prev and prev["end"] >= hi.isoformat() and prev["start"] <= lo.isoformat()
        if out.exists() and complete and not refresh:
            log(f"  {label} 已存在，略過")
            continue
        url, body = finmind.fetch("TaiwanOptionDaily", "TXO", lo, hi)
        raw_path = raw_dir / f"{label}.{stamp}.json.gz"
        raw_path.write_bytes(gzip.compress(body))
        rows = _parse_chain(json.loads(body), label)
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=CHAIN_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        data = gzip.compress(buf.getvalue().encode("utf-8"), mtime=0)
        out.write_bytes(data)
        meta["months"][label] = {
            "start": lo.isoformat(), "end": hi.isoformat(), "rows": len(rows),
            "url": url, "raw_file": str(raw_path), "raw_sha256": hashlib.sha256(body).hexdigest(),
            "csv_gz_sha256": hashlib.sha256(data).hexdigest(),
            "downloaded_at": datetime.now(timezone.utc).isoformat()}
        meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        log(f"  {label}: {len(rows)} 筆日盤資料")
        time.sleep(pause)

    months = sorted(meta["months"])
    first = meta["months"][months[0]]["start"]
    last = meta["months"][months[-1]]["end"]
    url, body = finmind.fetch("TaiwanStockPrice", "TAIEX", date.fromisoformat(first),
                              date.fromisoformat(last))
    taiex_rows = finmind.records(json.loads(body), "TaiwanStockPrice", "TAIEX")
    (raw_dir / f"TAIEX.{stamp}.json.gz").write_bytes(gzip.compress(body))
    with (folder / "taiex.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["date", "open", "high", "low", "close", "volume"])
        for r in sorted(taiex_rows, key=lambda x: x["date"]):
            writer.writerow([r["date"], r["open"], r["max"], r["min"], r["close"],
                             r["Trading_Volume"]])
    meta["underlying"] = {
        "dataset": "TaiwanStockPrice", "symbol": "TAIEX", "url": url,
        "raw_sha256": hashlib.sha256(body).hexdigest(), "rows": len(taiex_rows),
        "csv_sha256": hashlib.sha256((folder / "taiex.csv").read_bytes()).hexdigest()}
    meta["coverage"] = {"start": first, "end": last}
    meta["settlement_source"] = "approx_taiex_close"
    meta["notes"] = ("Day session only. Final settlement approximated by the TAIEX close on expiry "
                     "day (official: average of the last 30 minutes).")
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    load_chain(root).verify_expiries()
    return folder


# ---------- loading ----------
class OptionChain:
    """Read-only access to stored chains; loads one month at a time."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
        taiex = folder / "taiex.csv"
        if hashlib.sha256(taiex.read_bytes()).hexdigest() != self.meta["underlying"]["csv_sha256"]:
            raise ValueError("taiex.csv 與 metadata 雜湊不符，請重新下載")
        with taiex.open(encoding="utf-8", newline="") as stream:
            self.underlying = [
                DailyPrice(date.fromisoformat(r["date"]), D(r["open"]), D(r["close"]),
                           high=D(r["high"]), low=D(r["low"]), volume=D(r["volume"]))
                for r in csv.DictReader(stream)]
        coverage = self.meta.get("coverage") or {}
        self.calendar = TradingCalendar(
            [p.date for p in self.underlying],
            date.fromisoformat(coverage["start"]) if coverage.get("start") else None,
            date.fromisoformat(coverage["end"]) if coverage.get("end") else None)
        self._cache: dict[str, dict[date, list[Quote]]] = {}

    def _month(self, label: str) -> dict[date, list[Quote]]:
        if label not in self._cache:
            info = self.meta["months"].get(label)
            by_day: dict[date, list[Quote]] = {}
            if info:
                data = (self.folder / f"{label}.csv.gz").read_bytes()
                if hashlib.sha256(data).hexdigest() != info["csv_gz_sha256"]:
                    raise ValueError(f"{label}.csv.gz 與 metadata 雜湊不符，請重新下載")
                for r in csv.DictReader(io.StringIO(gzip.decompress(data).decode("utf-8"))):
                    q = Quote(date.fromisoformat(r["date"]), r["contract"], D(r["strike"]),
                              r["call_put"], D(r["open"]), D(r["high"]), D(r["low"]),
                              D(r["close"]), int(r["volume"]), D(r["settlement_price"]),
                              int(r["open_interest"]))
                    by_day.setdefault(q.date, []).append(q)
            if len(self._cache) >= 2:
                self._cache.pop(next(iter(self._cache)))
            self._cache[label] = by_day
        return self._cache[label]

    def quotes(self, day: date) -> list[Quote]:
        return self._month(f"{day.year:04d}-{day.month:02d}").get(day, [])

    def verify_expiries(self) -> dict[str, str]:
        """Rule-derived expiry must equal the last day a contract trades (within coverage)."""
        last: dict[str, date] = {}
        for label in sorted(self.meta["months"]):
            for day, rows in self._month(label).items():
                for q in rows:
                    last[q.contract] = max(last.get(q.contract, day), day)
        end = date.fromisoformat(self.meta["coverage"]["end"])
        bad = {c: f"rule {self.calendar.expiry(c)} vs last trade {d}"
               for c, d in last.items() if self.calendar.expiry(c) <= end
               and self.calendar.expiry(c) != d}
        if bad:
            raise ValueError(f"到期日規則與資料不符：{bad}")
        return {c: self.calendar.expiry(c).isoformat() for c in sorted(last)}


def load_chain(root: Path) -> OptionChain:
    folder = root / "data/processed" / FOLDER
    if not (folder / "metadata.json").exists():
        raise FileNotFoundError("還沒有台指選擇權資料，請先下載")
    return OptionChain(folder)


def main() -> None:
    parser = argparse.ArgumentParser(description="從 FinMind 下載台指選擇權（TXO）日盤契約鏈")
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--refresh", action="store_true", help="重新下載已存在的月份")
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("start must not be after end")
    try:
        folder = download(args.start, args.end, args.root, args.refresh)
    except (ValueError, RuntimeError, OSError) as exc:
        parser.error(str(exc))
    print(f"已寫入 {folder}")


if __name__ == "__main__":
    main()
