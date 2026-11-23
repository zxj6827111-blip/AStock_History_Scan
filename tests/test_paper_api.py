"""模拟盘只读 API 单测：dispatch 纯函数 + 真实 HTTP（登录拦截/401/路由）。

跑法（tengod 根目录）：python3 -m unittest -v tests.test_paper_api
"""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import sys
APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import paper_api  # noqa: E402


def make_latest():
    return {
        "schema": "paper_public_v1",
        "generated_at_bjt": "2026-11-23T15:20:00+08:00",
        "synced_at": "2026-11-23T15:12:00+08:00",
        "sync_ok": True, "stale": False,
        "summary": {"mode": "dry_run", "total": 1010000.0, "cash": 500000.0,
                    "market_value": 510000.0, "total_weight": 50.0,
                    "nav": 1.01, "day_pnl": 10000.0, "cum_pnl": 10000.0},
        "positions": [{"code": "000001.SZ", "name": "平安银行",
                       "strategy": "b8", "weight": 10.0, "market_value": 101000.0,
                       "entry_date": "2026-11-23", "entry_ref": 10.0,
                       "approx_entry": False, "stop": 9.5, "target": None}],
        "plans_recent": [{"plan_id": "ZH1|2026-11-24|abc", "signal_date":
                           "2026-11-23", "execute_trade_date": "2026-11-24",
                           "status": "planned", "regime": "Bear",
                           "segment_id": "S4", "n_buys": 1, "n_exits": 0,
                           "n_excluded": 0}],
        "trades_recent": [{"trade_date": "2026-11-24", "plan_id": "p",
                           "kind": "buy", "code": "000001.SZ", "name": "平安银行",
                           "strategy": "b8", "weight": 10.0, "status": "planned"}],
        "equity": [{"date": "2026-11-23", "nav": 1.01, "total": 1010000.0,
                    "day_pnl": 10000.0, "cum_pnl": 10000.0}],
        "warnings": [],
        "disclaimer": "d",
    }


class TestDispatch(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.public = Path(self._tmp.name)
        (self.public / "plans").mkdir()
        latest = make_latest()
        (self.public / "latest.json").write_text(
            json.dumps(latest, ensure_ascii=False), encoding="utf-8")
        (self.public / "nav.jsonl").write_text(
            json.dumps({"trade_date": "2026-11-23", "total": 1010000.0,
                        "nav": 1.01, "day_pnl": 10000.0, "cum_pnl": 10000.0},
                       ensure_ascii=False) + "\n", encoding="utf-8")
        (self.public / "plans" / "latest.json").write_text(
            json.dumps({"plan_id": "ZH1|2026-11-24|abc", "status": "planned",
                        "buys": [], "exits": [], "excluded": []},
                       ensure_ascii=False), encoding="utf-8")
        os.environ["BAZI_PAPER_PUBLIC_DIR"] = str(self.public)

    def tearDown(self):
        os.environ.pop("BAZI_PAPER_PUBLIC_DIR", None)
        self._tmp.cleanup()

    def test_latest_summary_positions_trades_equity(self):
        self.assertEqual(paper_api.latest()["schema"], "paper_public_v1")
        self.assertEqual(paper_api.summary()["summary"]["total"], 1010000.0)
        self.assertEqual(paper_api.positions()["positions"][0]["code"],
                         "000001.SZ")
        self.assertEqual(paper_api.trades()["trades"][0]["kind"], "buy")
        self.assertEqual(paper_api.equity()["equity"][0]["nav"], 1.01)

    def test_plans_detail_defaults_to_latest(self):
        out = paper_api.plans({})
        self.assertEqual(out["detail"]["plan_id"], "ZH1|2026-11-24|abc")
        self.assertEqual(len(out["plans"]), 1)

    def test_bad_date_raises(self):
        with self.assertRaises(paper_api.PaperError):
            paper_api.plans({"date": ["../../etc"]})

    def test_unknown_route_raises(self):
        with self.assertRaises(paper_api.PaperError):
            paper_api.dispatch("/api/paper/nope", {})

    def test_missing_dir_unavailable(self):
        os.environ["BAZI_PAPER_PUBLIC_DIR"] = str(self.public / "nope")
        with self.assertRaises(paper_api.PaperUnavailable):
            paper_api.latest()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None   # 302 不跟随：把重定向当 HTTPError 抛给调用方


class TestHttpAuth(unittest.TestCase):
    """真实 HTTP：未登录 401/重定向登录；登录后 /paper 与 /api/paper/* 正常。"""

    @classmethod
    def setUpClass(cls):
        import auth
        import server
        cls.auth = auth
        cls.server = server
        cls._orig_validate = auth.session_manager.validate_session
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.auth.session_manager.validate_session = cls._orig_validate
        cls.srv.shutdown()
        cls.srv.server_close()

    def _get(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(url, timeout=10) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def test_unauth_api_401(self):
        code, body, _ = self._get("/api/paper/latest")
        self.assertEqual(code, 401)
        self.assertIn("UNAUTHORIZED", json.loads(body)["error"])

    def test_unauth_page_redirects_login(self):
        code, _, headers = self._get("/paper")
        self.assertEqual(code, 302)
        self.assertIn("/login", headers.get("Location", ""))

    def test_authed_paper_redirect_and_api_503(self):
        with tempfile.TemporaryDirectory() as td:
            # 指到确定不存在的目录：鉴权已过、数据未生成 → 503（不是 401/500）
            os.environ["BAZI_PAPER_PUBLIC_DIR"] = str(Path(td) / "nope")
            self.auth.session_manager.validate_session = lambda *a, **k: True
            try:
                code, _, headers = self._get("/paper")
                self.assertEqual(code, 302)
                self.assertIn("/static/paper.html", headers.get("Location", ""))
                # /static/paper.html 本体存在且可读（受统一登录拦截保护）
                code2, body2, _ = self._get("/static/paper.html")
                self.assertEqual(code2, 200)
                self.assertIn(b"paper.js", body2)
                code3, body3, _ = self._get("/api/paper/latest")
                self.assertEqual(code3, 503)
                self.assertIn("error", json.loads(body3))
            finally:
                os.environ.pop("BAZI_PAPER_PUBLIC_DIR", None)
                self.auth.session_manager.validate_session = self._orig_validate


if __name__ == "__main__":
    unittest.main(verbosity=2)
