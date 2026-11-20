"""生存者报告挂点单测：survivor_report_actions / _survivor_dir / _run_survivor_report /
三档调用顺序（先报告后打包）。

跑法：python3 -m unittest -v tests.test_survivor_job（在 tengod 根目录）
"""
import json
import os
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
        self._saved_pkg = J._run_package
        self._saved_rep = J._run_survivor_report
        J._run_package = lambda *a, **k: self.seq.append("package")
        J._run_survivor_report = lambda *a, **k: self.seq.append("report")
        self._patch = mock.patch("subprocess.run", side_effect=self._fake_run)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        J._run_package = self._saved_pkg
        J._run_survivor_report = self._saved_rep
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
        self.assertEqual(self.seq, ["report", "package"])

    def test_am_order(self):
        J._run_am_once(self.root, {}, self.state_path)
        self.assertEqual(self.seq, ["report", "package"])

    def test_intra_order(self):
        J._run_intra_once(self.root, {}, self.state_path)
        self.assertEqual(self.seq, ["report", "package"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
