#!/usr/bin/env python3
"""客户机「日更」按钮后端自测（标准库 unittest）。

红线：每个用例都把包根指到 tempfile.mkdtemp()，更新器是假脚本；
不联网、不跑真的 client_update.py、不碰生产 update_state.json / update.log。
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

import update_center as U  # noqa: E402

FAKE = '''# marker: update_loop.lock
import json, sys, time
from pathlib import Path
root = Path(__file__).resolve().parents[2]
spec = json.loads((root / "fake.json").read_text(encoding="utf-8"))
(root / "argv.json").write_text(json.dumps(sys.argv[1:], ensure_ascii=False), encoding="utf-8")
if spec.get("sleep"):
    time.sleep(float(spec["sleep"]))
print(spec.get("line", ""), flush=True)
if spec.get("state"):
    (root / "update_state.json").write_text(
        json.dumps(spec["state"], ensure_ascii=False), encoding="utf-8")
sys.exit(int(spec.get("rc", 0)))
'''

OLD_FAKE = "import sys\nprint('旧版更新器', flush=True)\nsys.exit(0)\n"


class UpdateCenterTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        # 新布局：脚本在仓库 tools/ 下，配置在查询包根；测试全用临时树。
        self.updater = self.root / "bazi_month_relation_tengod_dev_v1" / "tools" / "client_update.py"
        self.updater.parent.mkdir(parents=True)
        self.config = self.root / "update_client.json"
        U.set_root(self.root, updater=self.updater, config_path=self.config)
        self._wait_idle()

    def tearDown(self):
        self._wait_idle()
        self._tmp.cleanup()

    def _wait_idle(self, timeout: float = 8.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not U.status()["running"]:
                return
            time.sleep(0.05)
        self.fail("假更新器在超时内没有结束")

    def _install(self, source: str = FAKE, config: bool = True):
        self.updater.write_text(source, encoding="utf-8")
        if config:
            self.config.write_text(
                '{"server":"https://example.invalid","token":"x"}', encoding="utf-8")

    def _spec(self, **kw):
        (self.root / "fake.json").write_text(
            json.dumps(kw, ensure_ascii=False), encoding="utf-8")

    def _run(self):
        code, payload = U.start_daily()
        self.assertEqual(code, "STARTED", payload)
        self._wait_idle()
        return U.status()

    def test_not_installed(self):
        st = U.status()
        self.assertFalse(st["installed"])
        code, payload = U.start_daily()
        self.assertEqual(code, "NOT_INSTALLED")
        self.assertIn("联系开发方", payload["message"])

    def test_publisher_role(self):
        (self.root / "exports" / "update_pkgs").mkdir(parents=True)
        (self.root / "exports" / "update_pkgs" / "latest.json").write_text("{}", encoding="utf-8")
        st = U.status()
        self.assertEqual(st["role"], "publisher")
        self.assertFalse(st["installed"])

    def test_client_role_without_updater(self):
        self.assertEqual(U.status()["role"], "client")

    def test_old_updater_refused(self):
        self._install(OLD_FAKE)
        st = U.status()
        self.assertTrue(st["installed"])
        self.assertFalse(st["updater_ready"])
        code, payload = U.start_daily()
        self.assertEqual(code, "UPDATER_OLD")
        self.assertIn("新版", payload["message"])

    def test_updated(self):
        self._install()
        self._spec(line="[2026-11-10 12:00:00] 更新完成：数据已到 2026-11-09（37 个文件）",
                   state={"date": "2026-11-09", "entries": 37,
                          "applied_at_bjt": "2026-11-10T12:00:00+08:00"})
        st = self._run()
        self.assertEqual(st["last_result"], "updated")
        self.assertEqual(st["local_date"], "2026-11-09")
        self.assertIn("2026-11-09", st["last_message"])

    def test_current(self):
        self._install()
        self._spec(line="[2026-11-10 12:00:00] 已是最新（2026-11-09）")
        st = self._run()
        self.assertEqual(st["last_result"], "current")
        self.assertIn("已是最新", st["last_message"])

    def test_command_contract(self):
        """按钮必须把查询包根与配置路径显式传给更新器（跨组件契约，不能靠脚本缺省猜测）。"""
        self._install()
        self._spec(line="[2026-11-10 12:00:00] 已是最新（2026-11-09）")
        self._run()
        argv = json.loads((self.root / "argv.json").read_text(encoding="utf-8"))
        self.assertEqual(argv[0], "--once")
        self.assertIn("--root", argv)
        # macOS 的 /var → /private/var 是同一路径的两种写法，比较前统一 resolve。
        self.assertEqual(Path(argv[argv.index("--root") + 1]).resolve(),
                         Path(self.root).resolve())
        self.assertIn("--config", argv)
        self.assertEqual(Path(argv[argv.index("--config") + 1]).resolve(),
                         Path(self.config).resolve())

    def test_publisher_refuses_manual_run(self):
        (self.root / "exports" / "update_pkgs").mkdir(parents=True)
        (self.root / "exports" / "update_pkgs" / "latest.json").write_text("{}", encoding="utf-8")
        self._install()
        code, payload = U.start_daily()
        self.assertEqual(code, "PUBLISHER", payload)
        self.assertEqual(U.start_auto_loop()["started"], False)
        self.assertIn("发布端", U.start_auto_loop()["reason"])

    def test_legacy_root_updater_blocks_button_and_loop(self):
        self._install()
        (self.root / "client_update.py").write_text(OLD_FAKE, encoding="utf-8")
        st = U.status()
        self.assertTrue(st["legacy_root_updater"])
        code, payload = U.start_daily()
        self.assertEqual(code, "LEGACY_ROOT", payload)
        loop = U.start_auto_loop()
        self.assertFalse(loop["started"])
        self.assertIn("旧版", loop["reason"])

    def test_auto_loop_not_started_without_install(self):
        loop = U.start_auto_loop()
        self.assertFalse(loop["started"])
        self.assertIn("未安装", loop["reason"])

    def test_second_click_while_running(self):
        self._install()
        self._spec(line="已是最新（2026-11-09）", sleep=1.0)
        code, _ = U.start_daily()
        self.assertEqual(code, "STARTED")
        code2, payload = U.start_daily()
        self.assertEqual(code2, "ALREADY_RUNNING", payload)
        self._wait_idle()

    def test_interpret_mapping(self):
        cases = [
            ("[需处理] 更新口令不正确（401）", "auth"),
            ("取清单失败：URLError: timed out", "offline"),
            ("服务器还没有更新包（HTTP 404）", "no_package"),
            ("[失败] 更新未应用，保留旧数据：sha256 不符", "failed"),
            ("已有另一个更新器正在更新，本次跳过", "busy"),
            ("[2026-11-10 19:00:00] 更新完成：数据已到 2026-11-09（37 个文件）", "updated"),
            ("[2026-11-10 19:00:00] 已是最新（2026-11-09）", "current"),
        ]
        for line, want in cases:
            got, msg, _ = U._interpret(line, 0 if want in ("updated", "current") else 1)
            self.assertEqual(got, want, f"{line!r} -> {got!r}")
            self.assertTrue(msg)

    def test_unexpected_output_is_reported_as_failure(self):
        got, msg, _ = U._interpret("Traceback (most recent call last):\nKeyError: x", 1)
        self.assertEqual(got, "failed")
        self.assertIn("KeyError", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
