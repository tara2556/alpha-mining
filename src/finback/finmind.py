"""Download daily prices and corporate actions from FinMind into an auditable CSV.

    python -m finback.finmind 0050 --start 2020-01-01 --end 2025-12-31

Raw responses are kept unmodified under data/raw/finmind/<symbol>/<UTC stamp>/.
The processed CSV holds raw (unadjusted) prices plus the events the engine applies:
cash dividends on the ex-date and integer forward splits before the open.
Anything the engine cannot model (stock dividends, capital reductions, reverse or
non-integer splits) stops the import instead of being silently mis-adjusted.

Set FINMIND_TOKEN for a higher rate limit; it is sent as a header and never saved.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

API = "https://api.finmindtrade.com/api/v4/data"
DATASETS = (
    "TaiwanStockPrice",
    "TaiwanStockDividendResult",
    "TaiwanStockSplitPrice",
    "TaiwanStockCapitalReductionReferencePrice",
)
CSV_FIELDS = ("date", "open", "high", "low", "close", "volume", "dividend_per_share",
              "split_ratio")
D = Decimal


def fetch(dataset: str, symbol: str, start: date, end: date) -> tuple[str, bytes]:
    query = urllib.parse.urlencode({"dataset": dataset, "data_id": symbol,
                                    "start_date": start.isoformat(),
                                    "end_date": end.isoformat()})
    url = f"{API}?{query}"
    headers = {"User-Agent": "finback-research/0.1"}
    token = os.environ.get("FINMIND_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                        timeout=60) as response:
                return url, response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (402, 429):
                raise RuntimeError("FinMind rate limit reached; set FINMIND_TOKEN or wait")
            if attempt == 2:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(3 * (attempt + 1))
    raise AssertionError("unreachable")


def records(payload: dict, dataset: str, symbol: str) -> list[dict]:
    if payload.get("status") != 200:
        raise ValueError(f"FinMind {dataset} failed: {payload.get('msg')}")
    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError(f"FinMind {dataset}: unexpected response shape")
    if any(row.get("stock_id") != symbol for row in data):
        raise ValueError(f"FinMind {dataset}: response contains another symbol")
    return data


def build_rows(raw: dict[str, list[dict]], start: date, end: date) -> tuple[list[dict], dict]:
    """Merge prices with corporate actions; raise on anything unsupported."""
    prices = raw["TaiwanStockPrice"]
    if not prices:
        raise ValueError("FinMind returned no prices for this symbol/range")
    rows: dict[str, dict] = {}
    for p in prices:
        day = p["date"]
        if day in rows:
            raise ValueError(f"duplicate price date {day}")
        o, h, l, c = (D(str(p[k])) for k in ("open", "max", "min", "close"))
        if min(o, h, l, c) <= 0:
            raise ValueError(f"{day}: non-positive price (halted or bad data): {p}")
        rows[day] = {"date": day, "open": o, "high": h, "low": l, "close": c,
                     "volume": D(str(p["Trading_Volume"])),
                     "dividend_per_share": D(0), "split_ratio": D(1)}

    if raw["TaiwanStockCapitalReductionReferencePrice"]:
        dates = [r["date"] for r in raw["TaiwanStockCapitalReductionReferencePrice"]]
        raise ValueError(f"capital reduction on {dates} is not supported yet")

    events = {"dividends": [], "splits": []}
    for r in raw["TaiwanStockDividendResult"]:
        kind = r["stock_or_cache_dividend"]
        if kind != "息":
            raise ValueError(f"{r['date']}: stock dividend ({kind}) is not supported yet")
        if r["date"] not in rows:
            raise ValueError(f"ex-dividend date {r['date']} has no price row")
        amount = D(str(r["stock_and_cache_dividend"]))
        rows[r["date"]]["dividend_per_share"] += amount
        events["dividends"].append({"ex_date": r["date"], "cash_per_share": str(amount),
                                    "before_price": r["before_price"],
                                    "reference_price": r["after_price"]})
    for r in raw["TaiwanStockSplitPrice"]:
        ratio = D(str(r["before_price"])) / D(str(r["after_price"]))
        whole = ratio.to_integral_value()
        if whole < 2 or abs(ratio - whole) > D("0.01") * whole:
            raise ValueError(f"{r['date']}: split ratio {ratio:.4f} ({r['type']}) unsupported")
        if r["date"] not in rows:
            raise ValueError(f"split date {r['date']} has no price row")
        rows[r["date"]]["split_ratio"] = whole
        events["splits"].append({"date": r["date"], "type": r["type"], "ratio": str(whole),
                                 "before_price": r["before_price"],
                                 "reference_price": r["after_price"]})

    ordered = [rows[k] for k in sorted(rows)]
    if not (start.isoformat() <= ordered[0]["date"] and ordered[-1]["date"] <= end.isoformat()):
        raise ValueError("FinMind returned dates outside the requested range")
    _check_gaps(ordered)
    return ordered, events


def _check_gaps(rows: list[dict]) -> None:
    """Unexplained overnight jumps over 30% usually mean a missed corporate action."""
    for prev, row in zip(rows, rows[1:]):
        adjusted = row["open"] * row["split_ratio"] + row["dividend_per_share"]
        move = adjusted / prev["close"] - 1
        if abs(move) > D("0.3"):
            raise ValueError(f"{row['date']}: open moved {move:.1%} vs previous close with "
                             "no recorded split/dividend; check corporate actions")


def download(symbol: str, start: date, end: date, root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    raw_dir = root / "data/raw/finmind" / symbol / stamp
    raw_dir.mkdir(parents=True)
    raw, sources = {}, []
    for dataset in DATASETS:
        url, body = fetch(dataset, symbol, start, end)
        path = raw_dir / f"{dataset}.json"
        path.write_bytes(body)
        raw[dataset] = records(json.loads(body), dataset, symbol)
        sources.append({"dataset": dataset, "url": url, "raw_file": str(path),
                        "sha256": hashlib.sha256(body).hexdigest(),
                        "rows": len(raw[dataset]),
                        "downloaded_at": datetime.now(timezone.utc).isoformat()})
        print(f"  {dataset}: {len(raw[dataset])} 筆")
    (raw_dir / "manifest.json").write_text(json.dumps(sources, indent=2), encoding="utf-8")

    rows, events = build_rows(raw, start, end)
    out = processed_path(root, symbol, start, end)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    metadata = {
        "provider": "FinMind",
        "symbol": symbol,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "first_date": rows[0]["date"],
        "last_date": rows[-1]["date"],
        "rows": len(rows),
        "price_basis": "unadjusted",
        "corporate_actions": events,
        "unsupported_checks": "stock dividends, capital reductions, reverse/non-integer "
                              "splits stop the import; >30% unexplained gaps stop it too",
        "sources": sources,
        "csv_sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
    }
    out.with_suffix(".metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return out


def processed_path(root: Path, symbol: str, start: date, end: date) -> Path:
    return root / "data/processed" / f"finmind_{symbol}_{start:%Y%m%d}_{end:%Y%m%d}.csv"


def main() -> None:
    parser = argparse.ArgumentParser(description="從 FinMind 下載台股日線與除權息事件")
    parser.add_argument("symbol")
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("start must not be after end")
    try:
        path = download(args.symbol, args.start, args.end, args.root)
    except (ValueError, RuntimeError, OSError) as exc:
        parser.error(str(exc))
    print(f"已寫入 {path}")


if __name__ == "__main__":
    main()
