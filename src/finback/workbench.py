"""JSON API behind the research workbench (served by finback.dashboard).

Everything runs locally on files under the project root:
  data/processed/finmind_*.csv   price datasets (download via FinMind)
  configs/alphas.json            the alpha pool (formula name -> expression)
  reports/<name>/                saved backtests, also readable by the report page
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from pathlib import Path

from finback import finmind
from finback.engine import BacktestResult, CostModel, run_backtest, write_result
from finback.prices import DailyPrice, load_prices
from finback.strategies import AlphaRule, BuyAndHold, build

D = Decimal
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,39}$")
REPORT_NAME = re.compile(r"^[A-Za-z0-9_-]{1,60}$")
SYMBOL = re.compile(r"^[0-9A-Za-z]{2,10}$")


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class Workbench:
    def __init__(self, root: Path, reports: Path) -> None:
        self.root = root
        self.reports = reports
        self.data_dir = root / "data/processed"
        self.pool_file = root / "configs/alphas.json"

    # ---------- datasets ----------
    def datasets(self) -> list[dict]:
        out = []
        for csv_path in sorted(self.data_dir.glob("finmind_*.csv")):
            meta_path = csv_path.with_suffix(".metadata.json")
            if not meta_path.exists():
                continue
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            out.append({"id": csv_path.stem, "symbol": meta["symbol"],
                        "first_date": meta["first_date"], "last_date": meta["last_date"],
                        "rows": meta["rows"],
                        "splits": len(meta["corporate_actions"]["splits"]),
                        "dividends": len(meta["corporate_actions"]["dividends"])})
        return out

    def _prices(self, dataset: str, start: str | None = None,
                end: str | None = None) -> tuple[list[DailyPrice], dict]:
        if dataset not in {d["id"] for d in self.datasets()}:
            raise ApiError(f"找不到資料集：{dataset}", 404)
        path = self.data_dir / f"{dataset}.csv"
        meta = json.loads(path.with_suffix(".metadata.json").read_text(encoding="utf-8"))
        if hashlib.sha256(path.read_bytes()).hexdigest() != meta["csv_sha256"]:
            raise ApiError("CSV 與 metadata 雜湊不符，請重新下載", 409)
        prices = load_prices(path)
        lo = date.fromisoformat(start) if start else None
        hi = date.fromisoformat(end) if end else None
        prices = [p for p in prices if (lo is None or p.date >= lo) and (hi is None or p.date <= hi)]
        if len(prices) < 2:
            raise ApiError("選取期間內的交易日不足")
        return prices, meta

    def bars(self, dataset: str) -> dict:
        """Chart bars, back-adjusted for splits only (display; signals use causal data)."""
        prices, meta = self._prices(dataset)
        factor, factors = D(1), []
        for p in reversed(prices):
            factors.append(factor)
            factor /= p.split_ratio  # earlier bars are scaled by every later split
        factors.reverse()
        bars = []
        for p, f in zip(prices, factors):
            hi = p.high if p.high is not None else max(p.open, p.close)
            lo = p.low if p.low is not None else min(p.open, p.close)
            bars.append({"time": p.date.isoformat(), "open": float(p.open * f),
                         "high": float(hi * f), "low": float(lo * f),
                         "close": float(p.close * f),
                         "volume": float((p.volume or 0) / f)})
        return {"symbol": meta["symbol"], "adjustment": "split_back_adjusted", "bars": bars,
                "events": meta["corporate_actions"]}

    def download(self, body: dict) -> dict:
        symbol = str(body.get("symbol", ""))
        if not SYMBOL.match(symbol):
            raise ApiError("股票代號格式不正確")
        try:
            start, end = date.fromisoformat(body["start"]), date.fromisoformat(body["end"])
        except (KeyError, ValueError) as exc:
            raise ApiError("start / end 需為 YYYY-MM-DD") from exc
        if start > end:
            raise ApiError("開始日不可晚於結束日")
        path = finmind.download(symbol, start, end, self.root)
        return {"id": path.stem}

    # ---------- alpha pool ----------
    def pool(self) -> list[dict]:
        if not self.pool_file.exists():
            return []
        return json.loads(self.pool_file.read_text(encoding="utf-8"))["alphas"]

    def pool_map(self) -> dict[str, str]:
        return {a["name"]: a["expr"] for a in self.pool()}

    def alphas(self) -> dict:
        from finback.alpha import FUNCTIONS, VARIABLES
        return {"alphas": self.pool(), "functions": FUNCTIONS, "variables": VARIABLES}

    def save_alpha(self, name: str, body: dict) -> dict:
        from finback.alpha import AlphaError, compile_alpha
        if not NAME.match(name):
            raise ApiError("名稱只能用英文字母、數字、底線，且不能以數字開頭")
        expr = str(body.get("expr", "")).strip()
        alphas = [a for a in self.pool() if a["name"] != name]
        pool = {a["name"]: a["expr"] for a in alphas} | {name: expr}
        try:
            compile_alpha(expr, pool)
            for other in alphas:  # editing may break formulas that reference this one
                compile_alpha(other["expr"], pool)
        except AlphaError as exc:
            raise ApiError(f"公式錯誤：{exc}") from exc
        entry = {"name": name, "expr": expr, "description": str(body.get("description", ""))}
        existing = [a["name"] for a in self.pool()]
        if name in existing:
            alphas.insert(existing.index(name), entry)
        else:
            alphas.append(entry)
        self._write_pool(alphas)
        return entry

    def delete_alpha(self, name: str) -> dict:
        from finback.alpha import AlphaError, compile_alpha
        alphas = [a for a in self.pool() if a["name"] != name]
        if len(alphas) == len(self.pool()):
            raise ApiError(f"alpha 池中沒有 {name}", 404)
        pool = {a["name"]: a["expr"] for a in alphas}
        for other in alphas:
            try:
                compile_alpha(other["expr"], pool)
            except AlphaError as exc:
                raise ApiError(f"{other['name']} 引用了 {name}，無法刪除") from exc
        self._write_pool(alphas)
        return {"deleted": name}

    def _write_pool(self, alphas: list[dict]) -> None:
        self.pool_file.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps({"alphas": alphas}, indent=2, ensure_ascii=False) + "\n"
        fd, tmp = tempfile.mkstemp(dir=self.pool_file.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(tmp, self.pool_file)

    def evaluate(self, body: dict) -> dict:
        from finback.alpha import AlphaError, compile_alpha, evaluate_alpha
        prices, _ = self._prices(body.get("dataset", ""), body.get("start"), body.get("end"))
        split = _optional_date(body.get("split_date"))
        horizons = tuple(int(h) for h in body.get("horizons", (1, 5, 20)))
        if not horizons or any(not 1 <= h <= 250 for h in horizons):
            raise ApiError("預測天數需介於 1 到 250")
        try:
            values = compile_alpha(str(body.get("expr", "")), self.pool_map()).evaluate(prices)
        except AlphaError as exc:
            raise ApiError(f"公式錯誤：{exc}") from exc
        stats = evaluate_alpha(prices, values, horizons, split)
        return {"series": _series(prices, values), "stats": stats}

    # ---------- backtest ----------
    def backtest(self, body: dict) -> dict:
        prices, meta = self._prices(body.get("dataset", ""), body.get("start"), body.get("end"))
        split = _optional_date(body.get("split_date"))
        try:
            costs = CostModel(**{k: D(str(v)) for k, v in (body.get("costs") or {}).items()})
            cash = D(str(body.get("initial_cash", "1000000")))
            lot = int(body.get("lot_size", 1))
            spec = dict(body.get("strategy") or {})
            strategy = build(spec, self.pool_map())
            result = run_backtest(prices, strategy, "strategy", cash, costs, lot, meta["symbol"])
            bench = run_backtest(prices, BuyAndHold(), "bh", cash, costs, lot, meta["symbol"])
        except (ValueError, TypeError, ArithmeticError) as exc:
            raise ApiError(str(exc)) from exc
        response = {
            "symbol": meta["symbol"],
            "split_date": split.isoformat() if split else None,
            "strategy": _result_payload(result, prices, split),
            "benchmark": _result_payload(bench, prices, split),
        }
        if isinstance(strategy, AlphaRule):
            response["alpha"] = _series(prices, strategy.values)
        save_as = body.get("save_as")
        if save_as:
            response["saved"] = self._save(save_as, body, spec, meta, result, bench, cash, costs, lot)
        return response

    def _save(self, name, body, spec, meta, result, bench, cash, costs, lot) -> str:
        if not REPORT_NAME.match(str(name)):
            raise ApiError("報告名稱只能用英文字母、數字、- 與 _")
        folder = self.reports / name
        folder.mkdir(parents=True, exist_ok=True)
        for old in list(folder.glob("ledger_*.csv")) + list(folder.glob("trades_*.csv")):
            old.unlink()
        write_result(result, folder, "strategy")
        write_result(bench, folder, "bh")
        label = spec.get("name") or (f"alpha：{spec.get('expr')}" if spec.get("type") == "alpha"
                                     else spec.get("type", "策略"))
        run = {
            "kind": "strategy_backtest", "synthetic": False, "source_sha256": meta["csv_sha256"],
            "data_provenance": meta, "labels": {"strategy": label, "bh": "買入持有"},
            "request": body, "alpha_pool": self.pool() if spec.get("type") == "alpha" else None,
            "initial_cash": str(cash), "costs": {k: str(v) for k, v in vars(costs).items()},
            "lot_size": lot,
            "execution": "signal after close t, filled at open t+1; whole lots",
            "limitations": "single instrument, long-only, no slippage, no price-limit or "
                           "volume checks, no interest on cash, no terminal liquidation costs",
        }
        (folder / "run.json").write_text(json.dumps(run, indent=2, ensure_ascii=False) + "\n",
                                         encoding="utf-8")
        return name


def _optional_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ApiError("切分日需為 YYYY-MM-DD") from exc


def _series(prices: list[DailyPrice], values) -> list[dict]:
    return [{"time": p.date.isoformat()} | ({} if v is None else {"value": v})
            for p, v in zip(prices, values)]


def _result_payload(result: BacktestResult, prices, split: date | None) -> dict:
    rows = result.rows
    bad = max(abs(r.cash + r.quantity * r.close - r.equity) for r in rows)
    payload = {
        "wealth": [{"time": r.date.isoformat(), "value": float(r.wealth_index)} for r in rows],
        "drawdown": [{"time": r.date.isoformat(), "value": float(r.drawdown)} for r in rows],
        "exposure": [{"time": r.date.isoformat(), "value": float(r.gross_exposure)} for r in rows],
        "trades": [{k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(t).items()}
                   for t in result.trades],
        "final_equity": str(rows[-1].equity),
        "reconciled": bad <= D("0.000001"),
        "metrics": {"full": _stats(rows, result.trades, None, None)},
    }
    if split:
        payload["metrics"]["in_sample"] = _stats(rows, result.trades, None, split)
        payload["metrics"]["out_of_sample"] = _stats(rows, result.trades, split, None)
    return payload


def _stats(rows, trades, start: date | None, end: date | None) -> dict | None:
    """Return/risk of one segment; returns are chained daily so flows don't count."""
    seg = [r for r in rows if (start is None or r.date >= start) and (end is None or r.date < end)]
    if len(seg) < 2:
        return None
    rets = [float(r.daily_return) for r in seg]
    wealth = peak = 1.0
    max_dd = 0.0
    for r in rets:
        wealth *= 1 + r
        peak = max(peak, wealth)
        max_dd = min(max_dd, wealth / peak - 1)
    n = len(rets)
    mean = sum(rets) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (n - 1))
    years = n / 252
    seg_trades = [t for t in trades if seg[0].date.isoformat() <= t.date <= seg[-1].date.isoformat()]
    return {
        "start": seg[0].date.isoformat(), "end": seg[-1].date.isoformat(), "days": n,
        "total_return": wealth - 1,
        "cagr": wealth ** (1 / years) - 1 if wealth > 0 else None,
        "volatility": sd * math.sqrt(252),
        "sharpe": mean / sd * math.sqrt(252) if sd else None,
        "max_drawdown": max_dd,
        "exposure": sum(float(r.gross_exposure) for r in seg) / n,
        "trades": len(seg_trades),
        "costs": float(sum((t.commission + t.tax for t in seg_trades), D(0))),
    }
