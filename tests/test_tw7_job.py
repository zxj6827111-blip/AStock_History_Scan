"""TW7 单测：日更后今日关注的纯函数分支；跨年 zip 归档；解释器选择。"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

WORKTREE = Path(__file__).resolve().parents[1]
ROOT = WORKTREE.parent
sys.path.insert(0, str(WORKTREE / "app"))
sys.path.insert(0, str(ROOT / "today_watch_v1" / "scripts"))

import daily_live_job as D  # noqa: E402
import tw13_daily_live as L  # noqa: E402
import tw51_generate as G  # noqa: E402


class TestTodayWatchActions(unittest.TestCase):
    def test_success_new_target(self):
        self.assertEqual(D.today_watch_actions(True, "2026-10-30", "2026-10-29", False),
                         ["sessions", "generate", "score"])

    def test_skip_cases(self):
        self.assertEqual(D.today_watch_actions(True, "2026-10-30", "2026-10-30", False), [])
        self.assertEqual(D.today_watch_actions(False, "2026-10-30", "2026-10-29", False), [])
        self.assertEqual(D.today_watch_actions(True, "2026-10-30", "2026-10-29", True), [])


class TestArchiveTargets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.live = base / "live"
        self.arch = base / "archive"
        self.live.mkdir()
        self.arch.mkdir()
        self.old_live, self.old_arch = G.C.LIVE_DIR, G.C.ARCHIVE_DIR
        G.C.LIVE_DIR, G.C.ARCHIVE_DIR = self.live, self.arch

    def tearDown(self):
        G.C.LIVE_DIR, G.C.ARCHIVE_DIR = self.old_live, self.old_arch
        self.tmp.cleanup()

    def test_targets(self):
        (self.live / "2026.zip").write_bytes(b"zip-2026")
        (self.live / "2027.zip").write_bytes(b"zip-2027")
        sessions = pd.DataFrame({"date": ["2026-10-30", "2026-12-31", "2027-01-05"]})
        self.assertEqual(G.archive_targets("2026-10-30", sessions), [])
        self.assertEqual(G.archive_targets("2027-01-05", sessions),
                         [(self.live / "2026.zip", self.arch / "2026.zip")])
        self.assertEqual(G.archive_targets("2026-12-31", sessions),
                         [(self.live / "2026.zip", self.arch / "2026.zip")])
        done = G.maybe_archive("2026-12-31", sessions)
        self.assertEqual(len(done), 1)
        self.assertEqual((self.arch / "2026.zip").read_bytes(), b"zip-2026")
        self.assertTrue((self.arch / "INDEX.json").exists())

    def test_december_refreshes_archive(self):
        """12 月连续两天、zip 内容不同：归档必须是后一天的内容。"""
        sessions = pd.DataFrame({"date": ["2026-12-01", "2026-12-02"]})
        (self.live / "2026.zip").write_bytes(b"day-1")
        G.maybe_archive("2026-12-01", sessions)
        self.assertEqual((self.arch / "2026.zip").read_bytes(), b"day-1")

        (self.live / "2026.zip").write_bytes(b"day-2")
        G.maybe_archive("2026-12-02", sessions)
        self.assertEqual((self.arch / "2026.zip").read_bytes(), b"day-2")

    def test_zip_path_prefers_live_source(self):
        (self.live / "2026.zip").write_bytes(b"new")
        (self.arch / "2026.zip").write_bytes(b"old")
        self.assertEqual(L._zip_path(2026), self.live / "2026.zip")
        (self.live / "2026.zip").unlink()
        self.assertEqual(L._zip_path(2026), self.arch / "2026.zip")


class TestPythonSelection(unittest.TestCase):
    def test_env_default_pythonpath(self):
        old = os.environ.pop("PYTHONPATH", None)
        try:
            self.assertEqual(D._today_watch_env()["PYTHONPATH"], D.TW_DEFAULT_PYTHONPATH)
            os.environ["PYTHONPATH"] = "/custom/site-packages"
            self.assertEqual(D._today_watch_env()["PYTHONPATH"], "/custom/site-packages")
        finally:
            os.environ.pop("PYTHONPATH", None)
            if old is not None:
                os.environ["PYTHONPATH"] = old

    def test_pick_first_working_interpreter(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bad = tmp / "py_bad"
            good = tmp / "py_good"
            bad.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            good.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            bad.chmod(0o755)
            good.chmod(0o755)
            env = {"PATH": "/usr/bin:/bin"}
            self.assertEqual(D._python_for_today_watch([str(bad), str(good)], env=env), str(good))
            self.assertEqual(D._python_for_today_watch([str(bad), str(tmp / "missing")], env=env), "")


class TestDailyLockTakeover(unittest.TestCase):
    """2026-11-04 回归：日更锁改到每档前才抢。占锁的常驻进程一旦消失，
    待命实例必须能在下一档接管（旧行为是启动时抢一次、抢不到就永久让位）。"""

    HOLDER = ("import fcntl,sys,time;"
              "fh=open(sys.argv[1],'w');fcntl.flock(fh,fcntl.LOCK_EX);"
              "print('held',flush=True);time.sleep(float(sys.argv[2]))")

    def _hold_elsewhere(self, lock: Path, secs: float = 30):
        p = subprocess.Popen([sys.executable, "-c", self.HOLDER, str(lock), str(secs)],
                             stdout=subprocess.PIPE, text=True)
        self.assertEqual(p.stdout.readline().strip(), "held", "对照子进程没占到锁，测试无效")
        return p

    def test_free_lock_is_acquired(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock, log = Path(tmp) / "live_daily.lock", Path(tmp) / "live_daily.log"
            self.assertTrue(D._acquire_lock(lock, log))
            self.assertFalse(log.exists(), "抢到锁不该写日志")

    def test_busy_then_takeover_after_holder_dies(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock, log = Path(tmp) / "live_daily.lock", Path(tmp) / "live_daily.log"
            holder = self._hold_elsewhere(lock)
            self.assertFalse(D._acquire_lock(lock, log), "别人占锁时不该抢跑")
            self.assertIn("日更锁被其它实例占用", log.read_text(encoding="utf-8"))
            holder.terminate()
            holder.wait(timeout=10)
            holder.stdout.close()
            self.assertTrue(D._acquire_lock(lock, log), "占锁进程死了就该接管")


if __name__ == "__main__":
    unittest.main(verbosity=2)
