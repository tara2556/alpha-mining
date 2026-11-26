"""Local read-only dashboard for report folders: python -m finback.dashboard.

Serves only on 127.0.0.1. Every metric shown in the browser is recomputed here
from the ledger CSVs with Decimal, so the page never invents numbers of its own.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import webbrowser
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from finback.prices import estimate_beta
from finback.workbench import ApiError, Workbench

D = Decimal
WEB_DIR = Path(__file__).with_name("web")
STATIC = {
    "/": ("workbench.html", "text/html; charset=utf-8"),
    "/workbench.js": ("workbench.js", "text/javascript; charset=utf-8"),
    "/workbench.css": ("workbench.css", "text/css; charset=utf-8"),
    "/reports": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/vendor/lightweight-charts.js": ("vendor/lightweight-charts.standalone.production.js",
                                      "text/javascript; charset=utf-8"),
}
LEDGER_NAME = re.compile(r"^ledger_(.+)\.csv$")
# A ledger row reconciles when cash + quantity * close equals equity to this tolerance.
RECONCILE_TOLERANCE = D("0.000001")
LEDGER_FIELDS = ("date", "close", "quantity", "cash", "market_value", "equity", "fees",
                 "dividend_cash", "external_flow", "daily_return", "wealth_index", "drawdown",
                 "gross_exposure", "net_exposure", "benchmark_return")


def list_reports(root: Path) -> list[dict]:
    """Report folders under root that contain at least one ledger CSV."""
    if not root.is_dir():
        return []
    reports = []
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        ledgers = sorted(p.name for p in folder.iterdir() if LEDGER_NAME.match(p.name))
        if ledgers:
            run = _read_json(folder / "run.json")
            reports.append({
                "name": folder.name,
                "ledgers": len(ledgers),
                "synthetic": bool(run and run.get("synthetic")),
                "modified": max((folder / name).stat().st_mtime for name in ledgers),
            })
    return reports


def load_report(folder: Path) -> dict:
    """Ledgers, recomputed metrics, reconciliation and run metadata for one folder."""
    ledgers = []
    for path in sorted(folder.iterdir()):
        match = LEDGER_NAME.match(path.name)
        if match:
            ledger = _load_ledger(match.group(1), path)
            ledger["trades"] = _load_trades(folder / f"trades_{match.group(1)}.csv")
            if ledger["trades"] is not None:
                ledger["metrics"]["trades"] = len(ledger["trades"])
            ledgers.append(ledger)
    if not ledgers:
        raise ValueError(f"no ledger_*.csv in {folder.name}")
    run = _read_json(folder / "run.json")
    order = list((run or {}).get("labels", {}))
    ledgers.sort(key=lambda item: (order.index(item["label"]) if item["label"] in order
                                   else len(order), _label_sort_key(item["label"])))
    report_md = folder / "report.md"
    return {
        "name": folder.name,
        "run": run,
        "report_md": report_md.read_text(encoding="utf-8") if report_md.exists() else None,
        "ledgers": ledgers,
        "benchmark": _benchmark(ledgers[0]["rows"]),
    }


def _load_ledger(label: str, path: Path) -> dict:
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        missing = set(LEDGER_FIELDS) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path.name} missing columns: {', '.join(sorted(missing))}")
        rows = list(reader)
    if not rows:
        raise ValueError(f"{path.name} has no rows")
    try:
        values = [{k: D(row[k]) for k in LEDGER_FIELDS if k != "date"} for row in rows]
    except InvalidOperation as exc:
        raise ValueError(f"{path.name} contains a non-numeric value") from exc

    worst = max(abs(v["cash"] + v["quantity"] * v["close"] - v["equity"]) for v in values)
    beta = estimate_beta([(v["daily_return"], v["benchmark_return"]) for v in values[1:]])
    first, last = values[0], values[-1]
    return {
        "label": label,
        "rows": [{k: row[k] for k in LEDGER_FIELDS} for row in rows],
        "metrics": {
            "start_date": rows[0]["date"],
            "end_date": rows[-1]["date"],
            "days": len(rows),
            "initial_equity": str(_initial_equity(first)),
            "final_equity": str(last["equity"]),
            "net_external_flow": str(sum(v["external_flow"] for v in values)),
            "total_return": str(last["wealth_index"] - 1),
            "max_drawdown": str(min(v["drawdown"] for v in values)),
            "beta": None if beta is None else str(beta),
            "fees_paid": str(sum(v["fees"] for v in values)),
            "dividends": str(sum(v["dividend_cash"] for v in values)),
            "first_exposure": str(first["gross_exposure"]),
            "last_exposure": str(last["gross_exposure"]),
        },
        "reconciliation": {
            "ok": worst <= RECONCILE_TOLERANCE,
            "max_abs_diff": str(worst),
            "rule": "cash + quantity × close = equity",
        },
    }


def _load_trades(path: Path) -> list[dict] | None:
    """Trade list written by finback.backtest; None for reports that have none."""
    if not path.exists():
        return None
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def _initial_equity(first: dict[str, Decimal]) -> Decimal:
    """Equity before day one: first equity / (1 + first return) minus that day's flow."""
    return first["equity"] / (1 + first["daily_return"]) - first["external_flow"]


def _benchmark(rows: list[dict]) -> list[str]:
    """Total-return index of the traded instrument, from the first open."""
    wealth, index = D(1), []
    for row in rows:
        wealth *= 1 + D(row["benchmark_return"])
        index.append(str(wealth))
    return index


def _label_sort_key(label: str) -> tuple:
    return (0, int(label), "") if label.isdigit() else (1, 0, label)


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def make_handler(root: Path, workbench: Workbench | None = None
                 ) -> type[BaseHTTPRequestHandler]:
    """root = reports folder. workbench enables the research API (data, alphas, backtests)."""

    class Handler(BaseHTTPRequestHandler):
        def _allowed(self, write: bool) -> bool:
            # DNS-rebinding guard: only answer requests addressed to this local server.
            port = self.server.server_port
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if self.headers.get("Host") not in hosts:
                self._json({"error": "forbidden host"}, HTTPStatus.FORBIDDEN)
                return False
            if write:
                # CSRF guard: other sites cannot send JSON here without a CORS preflight,
                # which this server never approves; also reject foreign Origin headers.
                origin = self.headers.get("Origin")
                if origin is not None and origin.removeprefix("http://") not in hosts:
                    self._json({"error": "forbidden origin"}, HTTPStatus.FORBIDDEN)
                    return False
                if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                    self._json({"error": "JSON required"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                    return False
            return True

        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            if not self._allowed(write=False):
                return
            url = urlparse(self.path)
            path = url.path
            if path in STATIC:
                name, content_type = STATIC[path]
                return self._send(HTTPStatus.OK, (WEB_DIR / name).read_bytes(), content_type)
            if path == "/api/reports":
                return self._json(list_reports(root))
            if path.startswith("/api/reports/"):
                name = unquote(path.removeprefix("/api/reports/"))
                # Only folders that list_reports would show; blocks path traversal.
                if name not in {r["name"] for r in list_reports(root)}:
                    return self._json({"error": f"找不到報告：{name}"}, HTTPStatus.NOT_FOUND)
                try:
                    return self._json(load_report(root / name))
                except (ValueError, OSError, json.JSONDecodeError) as exc:
                    return self._json({"error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)
            if workbench is not None:
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                routes = {
                    "/api/datasets": lambda: workbench.datasets(),
                    "/api/bars": lambda: workbench.bars(query.get("dataset", "")),
                    "/api/alphas": lambda: workbench.alphas(),
                }
                if path in routes:
                    return self._call(routes[path])
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            self._write("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._write("PUT")

        def do_DELETE(self) -> None:  # noqa: N802
            self._write("DELETE")

        def _write(self, method: str) -> None:
            if not self._allowed(write=True):
                return
            if workbench is None:
                return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            length = int(self.headers.get("Content-Length") or 0)
            if length > 1_000_000:
                return self._json({"error": "request too large"},
                                  HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "invalid JSON"}, HTTPStatus.BAD_REQUEST)
            if not isinstance(body, dict):
                return self._json({"error": "JSON object required"}, HTTPStatus.BAD_REQUEST)
            path = urlparse(self.path).path
            alpha = (unquote(path.removeprefix("/api/alphas/"))
                     if path.startswith("/api/alphas/") else None)
            if method == "POST" and path == "/api/backtest":
                return self._call(lambda: workbench.backtest(body))
            if method == "POST" and path == "/api/alpha/evaluate":
                return self._call(lambda: workbench.evaluate(body))
            if method == "POST" and path == "/api/data/download":
                return self._call(lambda: workbench.download(body))
            if method == "PUT" and alpha:
                return self._call(lambda: workbench.save_alpha(alpha, body))
            if method == "DELETE" and alpha:
                return self._call(lambda: workbench.delete_alpha(alpha))
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def _call(self, fn) -> None:
            try:
                return self._json(fn())
            except ApiError as exc:
                return self._json({"error": str(exc)}, HTTPStatus(exc.status))
            except (ValueError, RuntimeError, OSError) as exc:
                return self._json({"error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)

        def _json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # quiet by default
            pass

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="研究工作台與回測報告（本機）")
    parser.add_argument("--root", type=Path, default=Path("."), help="專案根目錄")
    parser.add_argument("--reports", type=Path, help="預設 <root>/reports")
    parser.add_argument("--port", type=int, default=8050)
    parser.add_argument("--no-browser", action="store_true", help="不要自動開啟瀏覽器")
    args = parser.parse_args()
    project = args.root.resolve()
    reports = (args.reports or project / "reports").resolve()
    reports.mkdir(parents=True, exist_ok=True)
    handler = make_handler(reports, Workbench(project, reports))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"專案：{project}\n工作台：{url}\n報告：{url}reports（Ctrl+C 結束）")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
