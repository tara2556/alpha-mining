import csv
import json
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import date, timedelta
from decimal import Decimal as D
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import TestCase

from finback.dashboard import list_reports, load_report, make_handler
from finback.engine import CostModel, run_backtest, write_result
from finback.prices import DailyPrice
from finback.strategies import BuyAndHold


def prices(*closes):
    return [DailyPrice(date(2025, 1, 1) + timedelta(days=i), D("100"), D(str(close)))
            for i, close in enumerate(closes)]


class DashboardTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        bars = prices(100, 110, 90, 95)
        fee = CostModel(D("0.01"), D("1"), D("0"), D("0"))
        free = CostModel(D("0"), D("1"), D("0"), D("0"))
        self.result = run_backtest(bars, BuyAndHold(D("0.5")), "50", D("1000"), fee)
        write_result(self.result, self.root / "exp", "50")
        write_result(run_backtest(bars, BuyAndHold(), "100", D("1000"), free),
                     self.root / "exp", "100")
        (self.root / "exp" / "run.json").write_text(json.dumps({"synthetic": True}))
        (self.root / "empty").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_lists_only_folders_with_ledgers(self):
        reports = list_reports(self.root)
        self.assertEqual([r["name"] for r in reports], ["exp"])
        self.assertTrue(reports[0]["synthetic"])

    def test_metrics_match_engine_result(self):
        report = load_report(self.root / "exp")
        self.assertEqual([l["label"] for l in report["ledgers"]], ["50", "100"])
        m = report["ledgers"][0]["metrics"]
        self.assertEqual(D(m["total_return"]), self.result.total_return)
        self.assertEqual(D(m["max_drawdown"]), self.result.max_drawdown)
        self.assertEqual(D(m["beta"]), self.result.beta)
        self.assertEqual(D(m["final_equity"]), self.result.rows[-1].equity)
        self.assertEqual(D(m["initial_equity"]), D("1000"))
        self.assertTrue(report["ledgers"][0]["reconciliation"]["ok"])
        # First benchmark point is open-to-close; later points compound close-to-close.
        self.assertAlmostEqual(D(report["benchmark"][-1]), D("0.95"), places=20)

    def test_reconciliation_flags_tampered_equity(self):
        path = self.root / "exp" / "ledger_50.csv"
        with path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        rows[2]["equity"] = str(D(rows[2]["equity"]) + 1)
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        rec = load_report(self.root / "exp")["ledgers"][0]["reconciliation"]
        self.assertFalse(rec["ok"])
        self.assertEqual(D(rec["max_abs_diff"]), 1)

    def test_server_rejects_unlisted_and_traversal_names(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.root))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(f"{base}/api/reports/exp") as res:
                self.assertEqual(json.load(res)["name"], "exp")
            with urllib.request.urlopen(f"{base}/") as res:
                self.assertIn("finback", res.read().decode("utf-8"))
            for name in ("empty", "..%2F..%2Fetc", "%2E%2E"):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(f"{base}/api/reports/{name}")
                self.assertEqual(ctx.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()


class WorkbenchServerTests(TestCase):
    def setUp(self):
        from finback.workbench import Workbench
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "configs").mkdir()
        (root / "configs/alphas.json").write_text(json.dumps(
            {"alphas": [{"name": "m", "expr": "pct_change(close, 2)", "description": ""}]}))
        handler = make_handler(root / "reports", Workbench(root, root / "reports"))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.root = root

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req) as res:
                return res.status, json.load(res)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_save_validate_and_delete_alpha(self):
        status, _ = self.request("PUT", "/api/alphas/n", {"expr": "m * 2"})
        self.assertEqual(status, 200)
        status, body = self.request("PUT", "/api/alphas/bad", {"expr": "__import__('os')"})
        self.assertEqual(status, 400)
        status, body = self.request("DELETE", "/api/alphas/m", {})
        self.assertEqual(status, 400)  # n references m
        self.assertIn("引用", body["error"])
        names = [a["name"] for a in json.loads(
            (self.root / "configs/alphas.json").read_text(encoding="utf-8"))["alphas"]]
        self.assertEqual(names, ["m", "n"])

    def test_rejects_foreign_origin_non_json_and_foreign_host(self):
        status, _ = self.request("PUT", "/api/alphas/x", {"expr": "close"},
                                 {"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        req = urllib.request.Request(self.base + "/api/alphas/x", data=b"{}", method="PUT",
                                     headers={"Content-Type": "text/plain"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 415)
        status, _ = self.request("GET", "/api/alphas", headers={"Host": "evil.example:80"})
        self.assertEqual(status, 403)
