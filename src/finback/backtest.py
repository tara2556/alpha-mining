"""Run the strategies in a config file: python -m finback.backtest configs/strategies/x.json

Downloads the FinMind data on first use (or with --refresh), checks the CSV against
its metadata hash, runs every strategy on identical prices and writes one ledger and
one trade list per strategy to reports/<name>/, readable by finback.dashboard.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

from finback import finmind
from finback.engine import CostModel, run_backtest, write_result
from finback.prices import load_prices
from finback.strategies import build

D = Decimal
LABEL = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def load_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    for key in ("name", "symbol", "start", "end", "strategies"):
        if key not in config:
            raise ValueError(f"config missing {key!r}")
    if not LABEL.match(config["name"]):
        raise ValueError("name may only use letters, digits, _ and -")
    labels = [s.get("label", "") for s in config["strategies"]]
    if not labels or any(not LABEL.match(l) for l in labels) or len(set(labels)) != len(labels):
        raise ValueError("each strategy needs a unique label of letters, digits, _ or -")
    return config


def ensure_data(config: dict, root: Path, refresh: bool) -> tuple[Path, dict]:
    start, end = date.fromisoformat(config["start"]), date.fromisoformat(config["end"])
    path = finmind.processed_path(root, config["symbol"], start, end)
    if refresh or not path.exists():
        print(f"從 FinMind 下載 {config['symbol']} {start} ~ {end}")
        finmind.download(config["symbol"], start, end, root)
    metadata = json.loads(path.with_suffix(".metadata.json").read_text(encoding="utf-8"))
    if hashlib.sha256(path.read_bytes()).hexdigest() != metadata["csv_sha256"]:
        raise ValueError(f"{path} differs from its metadata; rerun with --refresh")
    return path, metadata


def main() -> None:
    parser = argparse.ArgumentParser(description="執行設定檔中的策略回測")
    parser.add_argument("config", type=Path)
    parser.add_argument("--refresh", action="store_true", help="重新從 FinMind 下載資料")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, help="預設 reports/<name>")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        data_path, metadata = ensure_data(config, args.root, args.refresh)
        prices = load_prices(data_path)
        costs = CostModel(**{k: D(str(v)) for k, v in config.get("costs", {}).items()})
        initial_cash = D(str(config.get("initial_cash", "1000000")))
        lot_size = int(config.get("lot_size", 1))
        pool_file = args.root / "configs/alphas.json"
        pool = ({a["name"]: a["expr"] for a in json.loads(pool_file.read_text(encoding="utf-8"))["alphas"]}
                if pool_file.exists() else {})
        results = []
        for spec in config["strategies"]:
            strategy = build(spec, pool)
            results.append((spec, run_backtest(prices, strategy, spec["label"], initial_cash,
                                               costs, lot_size, config["symbol"])))
    except (ValueError, KeyError, ArithmeticError, OSError, RuntimeError) as exc:
        parser.error(str(exc))

    output = args.output or args.root / "reports" / config["name"]
    output.mkdir(parents=True, exist_ok=True)
    for old in list(output.glob("ledger_*.csv")) + list(output.glob("trades_*.csv")):
        old.unlink()  # a rerun with fewer strategies must not leave stale ledgers behind
    run = {
        "kind": "strategy_backtest",
        "synthetic": False,
        "config_file": str(args.config.resolve()),
        "config_sha256": hashlib.sha256(args.config.read_bytes()).hexdigest(),
        "config": config,
        "source": str(data_path.resolve()),
        "source_sha256": metadata["csv_sha256"],
        "data_provenance": metadata,
        "labels": {s["label"]: s.get("name", s["label"]) for s in config["strategies"]},
        "initial_cash": str(initial_cash),
        "costs": {k: str(v) for k, v in vars(costs).items()},
        "lot_size": lot_size,
        "execution": "signal after close t, filled at open t+1; whole lots; buys never "
                     "overdraw cash; first decision fills at the first open",
        "cash_flows": "cash dividends on ex-date to shares held before; integer forward "
                      "splits applied before the open",
        "limitations": "single instrument, long-only, no slippage, no price-limit or volume "
                       "checks, no interest on cash, no terminal liquidation costs",
    }
    (output / "run.json").write_text(json.dumps(run, indent=2, ensure_ascii=False) + "\n",
                                     encoding="utf-8")
    with (output / "summary.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["label", "name", "final_equity", "total_return", "max_drawdown",
                         "beta", "trades", "commission", "tax"])
        print(f"\n{config['symbol']} {prices[0].date} ~ {prices[-1].date}，{len(prices)} 個交易日")
        print(f"{'策略':<16}{'期末淨值':>16}{'累積報酬':>10}{'最大回撤':>10}{'交易':>6}")
        for spec, result in results:
            write_result(result, output, spec["label"])
            m = result.metrics
            writer.writerow([spec["label"], spec.get("name", ""), result.rows[-1].equity,
                             result.total_return, result.max_drawdown, result.beta,
                             m["trades"], m["commission"], m["tax"]])
            print(f"{spec.get('name', spec['label']):<16}{result.rows[-1].equity:>16,.0f}"
                  f"{result.total_return:>10.2%}{result.max_drawdown:>10.2%}{m['trades']:>6}")
    print(f"\n結果：{output.resolve()}\n檢視：python -m finback.dashboard")


if __name__ == "__main__":
    main()
