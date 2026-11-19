#!/usr/bin/env python3
"""客户机「代码更新」按钮后端自测（标准库 unittest）。

红线：每个用例都把仓库根/查询包根指到 tempfile 目录，git 是假桩（或纯本地真仓）；
不联网、不碰生产仓库、不碰生产《客户机更新说明.md》。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

import code_update as CU  # noqa: E402

DOC = "客户机更新说明.md"


class FakeGit:
    """假 git：按子命令回剧本输出，记录全部 argv 供契约断言。

    行为尽量贴真 git：merge 成功后本地 HEAD 前移到 remote，后续 rev-parse HEAD 就是新值。
    """

    def __init__(self, head="aaa1111", remote="bbb2222", behind="0",
                 subject="旧提交说明", dirty="", fetch_fail=False,
                 merge_fail=None, sleep=0.0):
        self.head, self.remote = head, remote
        self.behind_raw, self.subject = behind, subject
        self.dirty, self.fetch_fail = dirty, fetch_fail
        self.merge_fail, self.sleep = merge_fail, sleep
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]):
        self.calls.append(list(args))
        if self.sleep:
            time.sleep(self.sleep)
        if args[0] == "fetch":
            return (1, "", "fatal: 无法访问 'origin'") if self.fetch_fail else (0, "", "")
        if args[0] == "rev-parse":
            return (0, self.remote if "origin/main" in args else self.head, "")
        if args[0] == "rev-list":
            # 贴真 git：merge 成功后本地已追平远端，behind 归 0。
            return (0, ("0" if self.head == self.remote else self.behind_raw), "")
        if args[0] == "log":
            return (0, self.subject, "")
        if args[0] == "status":
            return (0, self.dirty, "")
        if args[0] == "merge":
            if self.merge_fail:
                return (1, "", self.merge_fail)
            self.head = self.remote  # --ff-only 成功：HEAD 前移
            return (0, "Fast-forward", "")
        return (0, "", "")

    def count(self, verb: str) -> int:
        return sum(1 for c in self.calls if c[0] == verb)


class CodeUpdateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        # 布局仿客户机：repo/ 是 git 仓库（含 .git 与说明文件），pkgroot/ 是查询包根。
        self.repo = tmp / "repo"
        self.pkgroot = tmp / "pkgroot"
        (self.repo / ".git").mkdir(parents=True)
        (self.repo / DOC).write_text("旧说明 v1", encoding="utf-8")
        self.pkgroot.mkdir(parents=True)
        self.fake = FakeGit()
        CU.set_root(self.repo, git_runner=self.fake, query_root=self.pkgroot,
                    running_head=self.fake.head)
        self._wait_idle()

    def tearDown(self):
        self._wait_idle()
        self._tmp.cleanup()

    def _wait_idle(self, timeout: float = 10.0):
        """只等「没有一轮在跑」（setUp/tearDown 用）：与 last_result 是否有值无关。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not CU.status()["updating"]:
                return CU.status()
            time.sleep(0.05)
        self.fail("假 git 在超时内没有结束")

    def _wait_result(self, timeout: float = 10.0):
        """等一轮跑到终态（_run 用）：last_result 离开 None/running。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = CU.status()
            if st["last_result"] not in (None, "running"):
                return st
            time.sleep(0.05)
        self.fail("假 git 在超时内没有结束")

    def _run(self):
        code, payload = CU.start_update()
        self.assertEqual(code, "STARTED", payload)
        return self._wait_result()

    def test_not_git_degrades(self):
        """不是 git 安装（老客户包/沙箱副本）：如实降级，绝不假装能更新。"""
        shutil.rmtree(self.repo / ".git")
        st = CU.status()
        self.assertFalse(st["available"])
        self.assertIn("git", st["reason"] or "")
        code, payload = CU.start_update()
        self.assertEqual(code, "NOT_GIT")
        self.assertIn("客户机更新说明", payload["message"])

    def test_publisher_refuses(self):
        (self.pkgroot / "exports" / "update_pkgs").mkdir(parents=True)
        (self.pkgroot / "exports" / "update_pkgs" / "latest.json").write_text(
            "{}", encoding="utf-8")
        st = CU.status()
        self.assertEqual(st["role"], "publisher")
        code, payload = CU.start_update()
        self.assertEqual(code, "PUBLISHER")
        self.assertIn("发布端", payload["message"])

    def test_dirty_refused_and_never_auto_discard(self):
        self.fake.dirty = " M app/api.py\n?? new.txt"
        code, payload = CU.start_update()
        self.assertEqual(code, "DIRTY_WORKTREE")
        self.assertIn("本地改动", payload["message"])
        self.assertIn("联系开发方", payload["message"])
        # 一次 merge 都没跑过：脏树绝不碰 refs。
        self.assertEqual(self.fake.count("merge"), 0)

    def test_data_update_running_guard(self):
        orig = CU._data_update_running
        CU._data_update_running = lambda: True
        try:
            code, payload = CU.start_update()
            self.assertEqual(code, "DATA_UPDATE_RUNNING")
            self.assertIn("数据日更", payload["message"])
        finally:
            CU._data_update_running = orig

    def test_updated(self):
        self.fake.behind_raw = "2"
        self.fake.subject = "feat(ui): 新增某功能"
        st = self._run()
        self.assertEqual(st["last_result"], "updated")
        self.assertIn("bbb2222", st["last_message"])
        self.assertIn("2", st["last_message"])
        # 说明文件已同步到查询包根（仿手工流程 2.2 的 Copy-Item）。
        self.assertEqual((self.pkgroot / DOC).read_text(encoding="utf-8"), "旧说明 v1")
        # 更新后重新盘点：behind 归零、待重启生效。
        st2 = CU.status(force=True)
        self.assertEqual(st2["behind"], 0)
        self.assertTrue(st2["restarting_needed"])

    def test_doc_sync_failure_ignored(self):
        """说明文件同步失败不能影响更新结果本身。"""
        self.fake.behind_raw = "1"
        (self.pkgroot / DOC).mkdir()  # 同名目录挡路：copyfile 必失败
        st = self._run()
        self.assertEqual(st["last_result"], "updated")

    def test_current(self):
        st = self._run()
        self.assertEqual(st["last_result"], "current")
        self.assertIn("已是最新", st["last_message"])
        self.assertEqual(self.fake.count("merge"), 0)

    def test_merge_failure_keeps_old_code(self):
        self.fake.behind_raw = "3"
        self.fake.merge_fail = "fatal: Not possible to fast-forward, aborting."
        st = self._run()
        self.assertEqual(st["last_result"], "failed")
        self.assertIn("fast-forward", st["last_message"])
        # HEAD 没动（假 git 的 head 只在 merge 成功时前移），旧代码原样保留。
        self.assertEqual(self.fake.head, "aaa1111")

    def test_command_contract(self):
        """必须且只能对 origin/main 做 fetch 与 --ff-only 合并（不接受任意参数）。"""
        self.fake.behind_raw = "1"
        self._run()
        verbs = [c[0] for c in self.fake.calls]
        self.assertIn("fetch", verbs)
        self.assertIn("merge", verbs)
        self.assertEqual(self.fake.calls[verbs.index("fetch")],
                         ["fetch", "origin", "main"])
        self.assertEqual(self.fake.calls[verbs.index("merge")],
                         ["merge", "--ff-only", "origin/main"])
        forbidden = {"reset", "clean", "checkout", "push", "remote"}
        self.assertFalse(forbidden & set(verbs))

    def test_second_click_while_running(self):
        self.fake.behind_raw = "1"
        self.fake.sleep = 0.6
        code, _ = CU.start_update()
        self.assertEqual(code, "STARTED")
        code2, payload = CU.start_update()
        self.assertEqual(code2, "ALREADY_RUNNING", payload)
        self._wait_idle()

    def test_status_cache_ttl_and_force(self):
        CU.status()                       # 首查：fetch 一次
        n1 = self.fake.count("fetch")
        CU.status()                       # 缓存内：不再 fetch
        n2 = self.fake.count("fetch")
        CU.status(force=True)             # force：强制重查
        n3 = self.fake.count("fetch")
        self.assertEqual(n2, n1)
        self.assertEqual(n3, n1 + 1)

    def test_fetch_failure_reported_not_crash(self):
        self.fake.fetch_fail = True
        st = CU.status(force=True)
        self.assertFalse(st["fetch_ok"])
        self.assertIsNone(st["behind"])
        self.assertTrue(st["local_head"])  # 本地盘点不依赖网络，照常尽力
        code, _ = CU.start_update()
        self.assertEqual(code, "STARTED")
        st = self._wait_idle()
        self.assertEqual(st["last_result"], "failed")
        self.assertIn("无法访问", st["last_message"])

    def test_restarting_needed_uses_frozen_head(self):
        self.fake.behind_raw = "1"
        self._run()
        # setUp 冻结的 RUNNING_HEAD 是旧 HEAD：更新后必判「待重启」。
        self.assertTrue(CU.status(force=True)["restarting_needed"])
        # 模拟人重启了服务：RUNNING_HEAD 换成新 HEAD，徽章归位。
        CU.set_root(self.repo, git_runner=self.fake, query_root=self.pkgroot,
                    running_head="bbb2222")
        self.assertFalse(CU.status(force=True)["restarting_needed"])

    def test_sandbox_layout_repo_not_found(self):
        """沙箱副本（app 被拷到无 .git 的地方）：REPO_DIR 上层没有 .git 时降级。"""
        CU.set_root(self.pkgroot / "app", git_runner=self.fake, query_root=self.pkgroot)
        st = CU.status()
        self.assertFalse(st["available"])
        self.assertIn("客户机更新说明", st["reason"])


class RealGitRoundtripTest(unittest.TestCase):
    """真本地 git 往返：bare origin + clone + push 新提交，全程不联网。"""

    def setUp(self):
        if shutil.which("git") is None:
            self.skipTest("本机没有 git")
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.origin, self.dev = tmp / "origin.git", tmp / "dev"
        self.client = tmp / "bazi_month_relation_tengod_dev_v1"
        self.pkgroot = tmp / "pkgroot"
        self.pkgroot.mkdir()
        cfg = ["-c", "user.name=测试", "-c", "user.email=t@e.x"]
        run = lambda repo, *a: subprocess.run(  # noqa: E731
            ["git", "-C", str(repo)] + cfg + list(a),
            check=True, capture_output=True, text=True)
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.origin)],
                       check=True, capture_output=True)
        subprocess.run(["git", "clone", str(self.origin), str(self.dev)],
                       check=True, capture_output=True)
        (self.dev / DOC).write_text("说明 v1", encoding="utf-8")
        run(self.dev, "add", ".")
        run(self.dev, "commit", "-m", "v1 基线")
        run(self.dev, "push", "origin", "main")
        subprocess.run(["git", "clone", str(self.origin), str(self.client)],
                       check=True, capture_output=True)
        self.head_v1 = subprocess.run(
            ["git", "-C", str(self.client), "rev-parse", "--short", "HEAD"],
            check=True, capture_output=True, text=True).stdout.strip()
        CU.set_root(self.client, git_runner=None, query_root=self.pkgroot,
                    running_head=self.head_v1)

    def tearDown(self):
        self._tmp.cleanup()

    def _wait_idle(self, timeout: float = 20.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = CU.status()
            if not st["updating"] and st["last_result"] not in (None, "running"):
                return st
            time.sleep(0.05)
        self.fail("本地 git 更新在超时内没有结束")

    def test_pull_applies_new_commit(self):
        st = CU.status(force=True)
        self.assertTrue(st["available"])
        self.assertEqual(st["behind"], 0)
        self.assertEqual(st["local_head"], self.head_v1)
        # 开发方出新版本：dev 上提交并 push。
        (self.dev / DOC).write_text("说明 v2", encoding="utf-8")
        (self.dev / "new_feature.py").write_text("print('v2')\n", encoding="utf-8")
        cfg = ["-c", "user.name=测试", "-c", "user.email=t@e.x"]
        run = lambda repo, *a: subprocess.run(  # noqa: E731
            ["git", "-C", str(repo)] + cfg + list(a),
            check=True, capture_output=True, text=True)
        run(self.dev, "add", ".")
        run(self.dev, "commit", "-m", "v2 新功能")
        run(self.dev, "push", "origin", "main")
        head_v2 = subprocess.run(
            ["git", "-C", str(self.dev), "rev-parse", "--short", "HEAD"],
            check=True, capture_output=True, text=True).stdout.strip()

        st = CU.status(force=True)
        self.assertEqual(st["behind"], 1)
        self.assertIn("v2 新功能", st["remote_subject"] or "")

        code, payload = CU.start_update()
        self.assertEqual(code, "STARTED", payload)
        st = self._wait_idle()
        self.assertEqual(st["last_result"], "updated")
        self.assertIn(head_v2, st["last_message"])
        # 磁盘上真的变了：新文件出现、说明文件更新并同步到查询包根。
        self.assertEqual((self.client / "new_feature.py").read_text(encoding="utf-8"),
                         "print('v2')\n")
        self.assertEqual((self.pkgroot / DOC).read_text(encoding="utf-8"), "说明 v2")
        st = CU.status(force=True)
        self.assertEqual(st["behind"], 0)
        self.assertTrue(st["restarting_needed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
