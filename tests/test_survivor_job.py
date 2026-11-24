"""生存者报告挂点单测：survivor_report_actions / _survivor_dir / _run_survivor_report /
三档调用顺序（先报告后打包）。

跑法：python3 -m unittest -v tests.test_survivor_job（在 tengod 根目录）
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

D = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(D))

import daily_live_job as J  # noqa: E402


class TestSurvivorReportActions(unittest.TestCase):
    def test_runs_when_enabled_and_script_present(self):
        self.assertTrue(J.survivor_report_actions(False, True))

    def test_disabled_flag_wins(self):
        self.assertFalse(J.survivor_report_actions(True, True))

    def test_missing_script_never_runs(self):
        self.assertFalse(J.survivor_report_actions(False, False))


class TestSurvivorDir(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("BAZI_SURVIVOR_DIR")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("BAZI_SURVIVOR_DIR", None)
        else:
            os.environ["BAZI_SURVIVOR_DIR"] = self._saved

    def test_default_sibling_of_root(self):
        root = Path("/tmp/fake_dev_v1")
        self.assertEqual(J._survivor_dir(root), Path("/tmp/survivor_report_v1"))

    def test_env_override(self):
        os.environ["BAZI_SURVIVOR_DIR"] = "/tmp/sandbox_survivor"
        self.assertEqual(J._survivor_dir(Path("/tmp/anything")), Path("/tmp/sandbox_survivor"))


class TestRunSurvivorReport(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sdir = Path(self._tmp.name) / "survivor_report_v1"
        (self.sdir / "scripts").mkdir(parents=True)
        self.log = Path(self._tmp.name) / "live_daily.log"
        self.root = Path(self._tmp.name) / "dev_v1"
        self.root.mkdir()
        self._saved_env = os.environ.get("BAZI_SURVIVOR_DIR")
        os.environ["BAZI_SURVIVOR_DIR"] = str(self.sdir)
        self._saved_py = J._python_for_today_watch  # 保存函数引用，不是调用结果
        J._python_for_today_watch = lambda: sys.executable  # 测试环境只要求能跑 python

    def tearDown(self):
        J._python_for_today_watch = self._saved_py
        if self._saved_env is None:
            os.environ.pop("BAZI_SURVIVOR_DIR", None)
        else:
            os.environ["BAZI_SURVIVOR_DIR"] = self._saved_env
        self._tmp.cleanup()

    def _script(self, body: str):
        (self.sdir / "scripts" / "sr_gen_report.py").write_text(body, encoding="utf-8")

    def test_success_logs_rc0(self):
        self._script("import json; print(json.dumps({'ok': True}))")
        J._run_survivor_report(self.root, self.log)
        text = self.log.read_text(encoding="utf-8")
        self.assertIn("生存者报告 rc=0", text)

    def test_failure_logged_not_raised(self):
        self._script("import sys; sys.exit(2)")
        J._run_survivor_report(self.root, self.log)
        text = self.log.read_text(encoding="utf-8")
        self.assertIn("生存者报告 rc=2", text)

    def test_disabled_skip(self):
        os.environ["BAZI_SURVIVOR_REPORT"] = "0"
        try:
            J._run_survivor_report(self.root, self.log)
            self.assertIn("跳过（disabled）", self.log.read_text(encoding="utf-8"))
        finally:
            os.environ.pop("BAZI_SURVIVOR_REPORT", None)

    def test_missing_script_skip(self):
        J._run_survivor_report(self.root, self.log)
        self.assertIn("脚本缺失", self.log.read_text(encoding="utf-8"))


class TestOrderReportBeforePackage(unittest.TestCase):
    """三档调序回归：切片重建成功后必须「先出报告、后打包」（包里带报告）。

    管线子进程全部用假 subprocess.run 顶掉，只看 _run_survivor_report 与
    _run_package 的调用先后（2026-11-20 调序：包里有报告，先打就把旧的打进去）。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "dev_v1"
        self.root.mkdir()
        self.state_path = self.root.parent / "live_daily_state.json"
        self.log = self.root.parent / "live_daily.log"
        # sync_live_30m/60m 的脚本与 30m meta：让盘中档能走到 建库→报告→打包
        slot88 = self.root.parent / "slot88_full_market_v1"
        (slot88 / "scripts").mkdir(parents=True)
        (slot88 / "scripts" / "sync_live_30m.py").write_text("", encoding="utf-8")
        (slot88 / "scripts" / "sync_live_60m.py").write_text("", encoding="utf-8")
        (slot88 / "data").mkdir()
        (slot88 / "data" / "live_30m_meta.json").write_text(json.dumps(
            {"asof": J._now().strftime("%F"), "asof_time": "10:00"}), encoding="utf-8")
        self.seq: list[str] = []
        self.triggers: list[str] = []
        self._saved_pkg = J._run_package
        self._saved_rep = J._run_survivor_report
        self._saved_paper = J.paper_engine

        class _FakePaper:
            def tick(inner, now):
                self.seq.append("tick")

            def snapshot_now(inner, kind, market_as_of=None):
                self.seq.append("snapshot")

            def finalize_from_report(inner, info):
                self.seq.append("finalize_pm")

            def finalize_intraday(inner, info):
                self.seq.append("finalize_intraday")

        J.paper_engine = _FakePaper()

        def _fake_report(*a, **k):
            trigger = a[2] if len(a) > 2 else ""
            self.seq.append("report")
            self.triggers.append(trigger)
            if trigger == "PM":
                J.paper_engine.finalize_from_report({})
            elif trigger == "INTRADAY":
                J.paper_engine.finalize_intraday({})

        J._run_package = lambda *a, **k: self.seq.append("package")
        J._run_survivor_report = _fake_report
        self._patch = mock.patch("subprocess.run", side_effect=self._fake_run)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        J._run_package = self._saved_pkg
        J._run_survivor_report = self._saved_rep
        J.paper_engine = self._saved_paper
        self._tmp.cleanup()

    @staticmethod
    def _fake_run(cmd, *args, **kw):
        argv = " ".join(str(c) for c in cmd)
        if "import paramiko" in argv:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "build_live_slice" in argv and "--overlay-live" in argv:
            return SimpleNamespace(returncode=0,
                                   stdout=json.dumps({"latest": J._now().strftime("%F")}),
                                   stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def test_pm_order(self):
        J._run_once(self.root, {}, self.state_path)
        self.assertEqual(self.seq, ["report", "finalize_pm", "tick", "snapshot", "package"])
        self.assertEqual(self.triggers, ["PM"])

    def test_am_order(self):
        J._run_am_once(self.root, {}, self.state_path)
        self.assertEqual(self.seq, ["report", "tick", "snapshot", "package"])
        self.assertEqual(self.triggers, ["AM"])

    def test_intra_order(self):
        J._run_intra_once(self.root, {}, self.state_path)
        self.assertEqual(self.seq,
                         ["report", "finalize_intraday", "tick", "snapshot", "package"])
        self.assertEqual(self.triggers, ["INTRADAY"])


class TestFeishuHook(unittest.TestCase):
    """飞书推送挂点：rc=0 新报告→入队+drain；skipped→仅 drain；rc≠0→故障卡；
    推送路径任何异常都不外抛（打包判定不受影响）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sdir = Path(self._tmp.name) / "survivor_report_v1"
        (self.sdir / "scripts").mkdir(parents=True)
        self.log = Path(self._tmp.name) / "live_daily.log"
        self.root = Path(self._tmp.name) / "dev_v1"
        self.root.mkdir()
        self._saved_env = os.environ.get("BAZI_SURVIVOR_DIR")
        os.environ["BAZI_SURVIVOR_DIR"] = str(self.sdir)
        self._saved_py = J._python_for_today_watch
        J._python_for_today_watch = lambda: sys.executable
        self.enqueue_args: list[list] = []
        self.drained: list[bool] = []
        self.failure_args: list[tuple] = []
        self._saved_fe = J._feishu_enqueue
        self._saved_fd = J._feishu_drain
        self._saved_ff = J._feishu_failure
        J._feishu_enqueue = lambda root, lp, args: self.enqueue_args.append(list(args))
        J._feishu_drain = lambda root, lp: self.drained.append(True)

        def _fake_failure(root, lp, tg, err):
            self.failure_args.append((tg, err))
            self.drained.append(True)  # 与真实 _feishu_failure 行为一致：失败也触发补发检查

        J._feishu_failure = _fake_failure

    def tearDown(self):
        J._feishu_enqueue = self._saved_fe
        J._feishu_drain = self._saved_fd
        J._feishu_failure = self._saved_ff
        J._python_for_today_watch = self._saved_py
        if self._saved_env is None:
            os.environ.pop("BAZI_SURVIVOR_DIR", None)
        else:
            os.environ["BAZI_SURVIVOR_DIR"] = self._saved_env
        self._tmp.cleanup()

    def _script(self, body: str):
        (self.sdir / "scripts" / "sr_gen_report.py").write_text(body, encoding="utf-8")

    def test_success_enqueues_report_and_drains(self):
        self._script("import json; print(json.dumps({'ok': True, 'json': '/r/x.json',"
                     " 'slot': 'survivor_x', 'built_at': 'bt9'}))")
        J._run_survivor_report(self.root, self.log, "PM")
        self.assertEqual(self.enqueue_args,
                         [["--report", "/r/x.json", "--trigger", "PM",
                           "--built-at", "bt9"]])
        self.assertEqual(self.drained, [True])
        self.assertEqual(self.failure_args, [])

    def test_skipped_only_drains(self):
        self._script("import json; print(json.dumps({'ok': True, 'skipped': True,"
                     " 'json': '/r/x.json', 'slot': 's', 'built_at': 'b'}))")
        J._run_survivor_report(self.root, self.log, "AM")
        self.assertEqual(self.enqueue_args, [])
        self.assertEqual(self.drained, [True])
        self.assertEqual(self.failure_args, [])

    def test_no_report_path_logs_skip(self):
        self._script("import json; print(json.dumps({'ok': True}))")
        J._run_survivor_report(self.root, self.log, "PM")
        self.assertEqual(self.enqueue_args, [])
        self.assertEqual(self.drained, [])
        self.assertIn("无报告路径", self.log.read_text(encoding="utf-8"))

    def test_failure_enqueues_failure_card(self):
        self._script("import sys; sys.exit(3)")
        J._run_survivor_report(self.root, self.log, "INTRADAY")
        self.assertEqual(self.enqueue_args, [])
        self.assertEqual(self.drained, [True])
        self.assertEqual(len(self.failure_args), 1)
        trig, err = self.failure_args[0]
        self.assertEqual(trig, "INTRADAY")
        self.assertIn("rc=3", err)

    def test_timeout_enqueues_failure(self):
        self._script("pass")

        def fake_run(cmd, *a, **k):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=300)

        with mock.patch("subprocess.run", side_effect=fake_run):
            J._run_survivor_report(self.root, self.log, "PM")
        self.assertEqual(len(self.failure_args), 1)
        self.assertIn("超时", self.failure_args[0][1])

    def test_push_exception_isolated(self):
        def boom(*a, **k):
            raise RuntimeError("push boom")

        self._script("import json; print(json.dumps({'ok': True, 'json': '/r/x.json'}))")
        with mock.patch.object(J, "_feishu_enqueue", side_effect=boom), \
                mock.patch.object(J, "_feishu_drain", side_effect=boom):
            J._run_survivor_report(self.root, self.log, "PM")  # 不应外抛
        self.assertIn("飞书推送路径异常", self.log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
