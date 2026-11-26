"""全系统 UI 整体优化契约与回归测试（导航壳、双页互通、Bug 修复、#open-today 保持隐藏）。

跑法（tengod 根目录）：python3 -m unittest -v tests.test_ui_holistic_upgrade
"""
import re
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"
STATIC_DIR = APP_DIR / "static"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


class TestUiHolisticContract(unittest.TestCase):
    """P0/P1/P2 静态资产契约检查（不改动 .py 业务算法与数据库）。"""

    def test_nav_shell_exists_and_exposes_api(self):
        js = (STATIC_DIR / "nav_shell.js").read_text(encoding="utf-8")
        for sym in (
            "window.BaziNavShell",
            "mountHeader",
            "applyTheme",
            "bindThemeToggle",
            "attachStockSuggest",
            "fetchCrossStatus",
            "renderStockBadges",
            "setLastStock",
            "getLastStock",
            "updateNavBadges",
        ):
            self.assertIn(sym, js)
        # 不把隐藏的今日关注放进全局导航
        self.assertNotIn("today.html", js)
        # /n2 重定向会丢失 query，直链 /static/n2.html?code= 且支持动态 dataset getter
        self.assertIn("/static/n2.html?code=", js)
        self.assertIn('typeof opts.dataset === "function"', js)
        # 个股档案按钮不拼接标的名称/指数后缀，统一展示相同版本号徽章
        self.assertNotIn("data-nav-stock-label", js)
        self.assertNotIn("rv-nav-mode-tag", js)
        self.assertIn("UI v1.9.3 · 2026-11-26", js)

    def test_index_today_remains_hidden_and_nav_grouped(self):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        self.assertRegex(html, r'id="open-today"[^>]*class="[^"]*\bhidden\b')
        self.assertNotIn("back-stock-cur", html)
        self.assertNotIn('id="return-board-bar"', html)
        self.assertIn("UI v1.9.3 · 2026-11-26", html)
        self.assertIn("nav_shell.js", html)
        self.assertIn("seg-status-pills", html)
        self.assertIn("seg-export-btn", html)
        # 全站页面版心宽度统一对齐 1780px
        for fname in ("survivor.html", "paper.html", "n2.html", "gua.css", "dayun.html"):
            txt = (STATIC_DIR / fname).read_text(encoding="utf-8")
            self.assertIn("max-width: 1780px", txt, msg=fname)
        gua_html = (STATIC_DIR / "gua.html").read_text(encoding="utf-8")
        self.assertIn('class="gua-disclaimer hidden"', gua_html)
        # 右侧抽屉模块重排：drawer-jump 应在 drawer-seg-box 之前，drawer-gua-box 在 drawer-windows-box 之后
        self.assertLess(html.index('id="drawer-jump"'), html.index('id="drawer-seg-box"'))
        self.assertLess(html.index('id="drawer-windows-box"'), html.index('id="drawer-gua-box"'))
        # 100%段允许复用 #o-search（style.css 不再在 body.segmode 下隐藏 #o-search-wrap）
        css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
        self.assertNotRegex(css, r"body\.segmode\s+#o-search-wrap\s*\{[^}]*display\s*:\s*none")
        self.assertIn(".rv-chart-card", css)
        self.assertIn(".xm-badge", css)

    def test_standalone_pages_mount_global_nav_shell(self):
        for page, active in (
            ("survivor.html", "survivor"),
            ("paper.html", "paper"),
            ("n2.html", "n2"),
            ("gua.html", "gua"),
        ):
            html = (STATIC_DIR / page).read_text(encoding="utf-8")
            self.assertIn('id="global-nav"', html, msg=page)
            self.assertIn(f'data-active="{active}"', html, msg=page)
            self.assertIn("nav_shell.js", html, msg=page)
        self.assertNotIn('id="head-meta"', (STATIC_DIR / "gua.html").read_text(encoding="utf-8"))

    def test_survivor_and_paper_deep_integration(self):
        sv_html = (STATIC_DIR / "survivor.html").read_text(encoding="utf-8")
        sv_js = (STATIC_DIR / "survivor.js").read_text(encoding="utf-8")
        self.assertIn("sv-paper-bridge", sv_html)
        self.assertIn("sv-sec-nav", sv_html)
        self.assertIn('data-list="bwatch"', sv_html)
        self.assertIn("/api/paper/latest", sv_js)
        self.assertIn("/api/paper/manual/capabilities", sv_js)
        self.assertIn("top_movers", sv_js)

        pp_html = (STATIC_DIR / "paper.html").read_text(encoding="utf-8")
        pp_js = (STATIC_DIR / "paper.js").read_text(encoding="utf-8")
        self.assertIn("pp-goto-survivor", pp_html)
        self.assertIn("renderPlanGroupsHtml", pp_js)
        self.assertIn("/api/paper/plans", pp_js)
        self.assertIn("pp-code-link", pp_js)
        self.assertIn("pp-sv-link", pp_js)
        self.assertIn("最大回撤", pp_js)

    def test_app_js_bug_fixes_and_overview_seg_upgrades(self):
        app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        # Bug 1：updateDrawerWindowView 旬模式表头必须为 5 列，段模式单独分支为 7 列
        self.assertIn("上旬<span class=\"th-sub\">1–10日</span>", app_js)
        self.assertIn("第1段<span class=\"th-sub\">第1–5日</span>", app_js)
        # Bug 2：showWindowDrawer 不得再把 INDEX/000001.SH 静默替换成 600519.SH 贵州茅台
        fn_start = app_js.index("function showWindowDrawer(")
        fn_end = app_js.index("function pillarNote(", fn_start)
        fn_body = app_js[fn_start:fn_end]
        self.assertNotIn("600519.SH", fn_body)
        self.assertNotIn("贵州茅台", fn_body)
        # 100%段导出与状态筛选、个股总览窗位节奏与跨模块状态
        self.assertIn("/api/seg/export?", app_js)
        self.assertIn("segStatus", app_js)
        self.assertIn("本月月内窗位节奏", app_js)
        self.assertIn("实战跟踪状态", app_js)


class TestUiHolisticHttp(unittest.TestCase):
    """临时 HTTP 服务检查各静态入口均可访问（不碰生产 8737 端口）。"""

    @classmethod
    def setUpClass(cls):
        import auth
        import server
        cls.auth = auth
        cls._orig_validate = auth.session_manager.validate_session
        auth.session_manager.validate_session = lambda *a, **k: True
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
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read()

    def test_all_ui_routes_serve_200(self):
        for path, marker in (
            ("/", b"nav_shell.js"),
            ("/static/nav_shell.js", b"BaziNavShell"),
            ("/static/survivor.html", b"sv-paper-bridge"),
            ("/static/paper.html", b"pp-goto-survivor"),
            ("/static/n2.html", b"n2-goto-profile"),
            ("/gua", b"gua-goto-profile"),
        ):
            code, body = self._get(path)
            self.assertEqual(code, 200, msg=path)
            self.assertIn(marker, body, msg=path)


if __name__ == "__main__":
    unittest.main()
