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


class TestHalfDayUpdate(unittest.TestCase):
    """半日一更新（午盘 AM + 晚间 PM）：调度判定、60m 午盘叠加门禁、客户端同日 sha256 升级。"""

    def test_next_am_action_schedule(self):
        from datetime import datetime
        dt = lambda h, m: datetime(2026, 11, 19, h, m, 0, tzinfo=D.BJT)
        st = {"date": "2026-11-19", "am_attempts": 0, "am_done": False, "done": False}
        self.assertEqual(D.next_am_action(st, dt(11, 30)), "wait")
        self.assertEqual(D.next_am_action(st, dt(11, 40)), "run")
        # 刚跑过第 1 档，未满 12 分钟间隔 -> wait；到 11:55 满间隔 -> run
        st1 = {**st, "am_attempts": 1, "am_last_attempt_at": dt(11, 45).isoformat()}
        self.assertEqual(D.next_am_action(st1, dt(11, 55)), "wait")
        self.assertEqual(D.next_am_action(st1, dt(11, 58)), "run")
        # 午盘已 done -> wait，但不影响晚间 19:05 的 next_action
        st_done = {**st, "am_attempts": 1, "am_done": True}
        self.assertEqual(D.next_am_action(st_done, dt(12, 20)), "wait")
        self.assertEqual(D.next_action(st_done, dt(19, 5)), "run")
        # 到了晚间 19:05 之后，即使午盘没跑完也不再触发 AM
        self.assertEqual(D.next_am_action(st, dt(19, 6)), "wait")

    def test_overlay_am_bars_gates_and_synthesis(self):
        from datetime import datetime
        sys.path.insert(0, str(ROOT / "scripts"))
        import build_live_slice as BLS
        start_at = datetime.fromisoformat("2026-11-07T15:52:00+08:00")
        end_at = datetime.fromisoformat("2026-12-07T10:05:00+08:00")
        base_bars = {
            "000001": [{"date": "2026-11-18", "open": 10.0, "high": 10.5,
                        "low": 9.9, "close": 10.2, "volume": 100.0, "amount": 1000.0}]
        }
        # 1) h=2 股票数不足 -> 拒绝
        rows_h1_only = [("000001", "2026-11-19", 10.2, 10.4, 10.1, 10.3, 50.0, 1, 1)]
        _, _, err = BLS.overlay_am_bars(dict(base_bars), rows_h1_only, start_at, end_at,
                                        min_symbols_h2=1)
        self.assertIn("11:30(h=2)", err)

        # 2) 日期不比 zip 最新日新 -> 拒绝
        rows_stale = [("000001", "2026-11-18", 10.0, 10.6, 9.8, 10.4, 80.0, 2, 2)]
        _, _, err2 = BLS.overlay_am_bars(dict(base_bars), rows_stale, start_at, end_at,
                                         min_symbols_h2=1)
        self.assertIn("不比日线包", err2)

        # 3) 正常 11:30(h=2) 聚合行 -> 叠加成功
        bars = {"000001": list(base_bars["000001"])}
        rows_ok = [("000001", "2026-11-19", 10.25, 10.80, 10.10, 10.65, 120.0, 2, 2)]
        am_date, h2_cnt, err3 = BLS.overlay_am_bars(bars, rows_ok, start_at, end_at,
                                                    min_symbols_h2=1)
        self.assertEqual(err3, "")
        self.assertEqual(am_date, "2026-11-19")
        self.assertEqual(h2_cnt, 1)
        self.assertEqual(len(bars["000001"]), 2)
        self.assertEqual(bars["000001"][-1]["close"], 10.65)

    def test_client_update_same_day_sha_upgrade_and_am_window(self):
        from datetime import datetime
        sys.path.insert(0, str(WORKTREE / "tools"))
        import client_update as CU
        # 工作日 12:00 处于午盘高频窗口（20 分钟），20:00 处于晚间窗口（30 分钟）
        self.assertEqual(CU.interval_for(datetime(2026, 11, 19, 12, 0, tzinfo=CU.BJT)),
                         CU.AM_INTERVAL)
        self.assertEqual(CU.interval_for(datetime(2026, 11, 19, 20, 0, tzinfo=CU.BJT)),
                         CU.DAY_INTERVAL)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "update_state.json"
            state = {"date": "2026-11-19", "sha256": "am_sha_111"}
            man_same = {"date": "2026-11-19", "sha256": "am_sha_111",
                        "file": "update_20261119.zip"}
            # 同日期 + 同 sha256 -> 直接判 current，不触发下载
            res = CU._apply_daily(root, "https://example.invalid", "tok",
                                  man_same, state, state_path, say=False)
            self.assertEqual(res, "current")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestIntraday30(unittest.TestCase):
    """盘中 30 分钟档：调度判定、30m 聚合门禁与合成、客户机盘中巡检窗口。"""

    def test_next_intra_action_schedule(self):
        from datetime import datetime
        dt = lambda h, m: datetime(2026, 11, 19, h, m, 0, tzinfo=D.BJT)
        st = {"date": "2026-11-19", "intra_slots_done": []}
        self.assertEqual(D.next_intra_action(st, dt(9, 50)), "wait")
        self.assertEqual(D.next_intra_action(st, dt(10, 5)), "run")
        self.assertEqual(D.next_intra_action({**st, "intra_last_attempt_at": dt(10, 3).isoformat()},
                                             dt(10, 6)), "wait")
        done = {"date": "2026-11-19",
                "intra_slots_done": ["10:05", "10:35", "11:05"]}
        self.assertEqual(D.next_intra_action(done, dt(11, 20)), "wait")
        self.assertEqual(D.next_intra_action(done, dt(13, 35)), "run")
        # 15:05 已记档但没真正更新（如同步失败）：15:12 兜底档仍会跑
        after = {"date": "2026-11-19",
                 "intra_slots_done": ["10:05", "10:35", "11:05", "13:35", "14:05", "14:35", "15:05"]}
        self.assertEqual(D.next_intra_action(after, dt(15, 12)), "run")
        self.assertEqual(D.next_intra_action(after, dt(15, 30)), "wait")

    def test_publisher_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bazi_month_relation_tengod_dev_v1"
            root.mkdir()
            old_role = os.environ.pop("BAZI_ROLE", None)
            try:
                self.assertFalse(D._publisher_role(root))
                d = Path(tmp) / "exports" / "update_pkgs"
                d.mkdir(parents=True)
                (d / "latest.json").write_text("{}", encoding="utf-8")
                self.assertTrue(D._publisher_role(root))
                os.environ["BAZI_ROLE"] = "client"
                self.assertFalse(D._publisher_role(root))
            finally:
                os.environ.pop("BAZI_ROLE", None)
                if old_role is not None:
                    os.environ["BAZI_ROLE"] = old_role

    def test_overlay_intraday_gates_and_synthesis(self):
        from datetime import datetime
        sys.path.insert(0, str(ROOT / "scripts"))
        import build_live_slice as BLS
        start_at = datetime.fromisoformat("2026-11-07T15:52:00+08:00")
        end_at = datetime.fromisoformat("2026-12-07T10:05:00+08:00")
        base_bars = {"000001": [{"date": "2026-11-18", "open": 10.0, "high": 10.5,
                                 "low": 9.9, "close": 10.2, "volume": 100.0,
                                 "amount": 1000.0}]}
        # 1) 只数不足 -> 拒绝
        _, cnt, _, err = BLS.overlay_intraday_bars(
            dict(base_bars), [("000001", "2026-11-19", 10.2, 10.4, 10.1, 10.3, 50.0, 7)],
            start_at, end_at, min_symbols=2)
        self.assertEqual(cnt, 1)
        self.assertIn("采集尚未产出", err)
        # 2) 日期不比 zip 新 -> 拒绝
        _, _, _, err2 = BLS.overlay_intraday_bars(
            dict(base_bars), [("000001", "2026-11-18", 10.0, 10.6, 9.8, 10.4, 80.0, 8)],
            start_at, end_at, min_symbols=1)
        self.assertIn("不比日线包", err2)
        # 3) 正常聚合行 -> 叠加并回填 asof_time
        bars = {"000001": list(base_bars["000001"])}
        day, cnt3, asof_t, err3 = BLS.overlay_intraday_bars(
            bars, [("000001", "2026-11-19", 10.25, 10.80, 10.10, 10.65, 120.0, 3)],
            start_at, end_at, min_symbols=1)
        self.assertEqual(err3, "")
        self.assertEqual(day, "2026-11-19")
        self.assertEqual(asof_t, "11:00")
        self.assertEqual(bars["000001"][-1]["close"], 10.65)

    def test_overlay_intraday_rejects_gap(self):
        from datetime import datetime
        sys.path.insert(0, str(ROOT / "scripts"))
        import build_live_slice as BLS
        start_at = datetime.fromisoformat("2026-11-07T15:52:00+08:00")
        end_at = datetime.fromisoformat("2026-12-07T10:05:00+08:00")
        bars = {"000001": [{"date": "2026-11-17", "open": 10.0, "high": 10.5,
                            "low": 9.9, "close": 10.2, "volume": 100.0,
                            "amount": 1000.0}]}
        rows = [("000001", "2026-11-19", 10.2, 10.4, 10.1, 10.3, 50.0, 3)]
        _, _, _, err = BLS.overlay_intraday_bars(
            bars, rows, start_at, end_at, min_symbols=1,
            prev_trade_date="2026-11-18")
        self.assertIn("缺上一交易日", err)

    def test_client_intraday_window(self):
        from datetime import datetime
        sys.path.insert(0, str(WORKTREE / "tools"))
        import client_update as CU
        self.assertEqual(CU.interval_for(datetime(2026, 11, 19, 10, 0, tzinfo=CU.BJT)),
                         CU.AM_INTERVAL)
        self.assertEqual(CU.interval_for(datetime(2026, 11, 19, 14, 40, tzinfo=CU.BJT)),
                         CU.AM_INTERVAL)
        self.assertEqual(CU.interval_for(datetime(2026, 11, 19, 15, 30, tzinfo=CU.BJT)),
                         CU.NIGHT_INTERVAL)
