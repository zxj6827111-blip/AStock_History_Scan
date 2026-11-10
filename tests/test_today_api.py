"""今日关注 API 单测（TW6 G6a）。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKTREE / "app"))

import today_api as T  # noqa: E402


class TestTodayApi(unittest.TestCase):
    def test_latest_shape(self):
        out = T.dispatch("/api/today/latest", {"list": ["L1"]})
        self.assertEqual(out["asof"], "2026-10-30")
        self.assertEqual(out["list"], "L1")
        self.assertEqual(len(out["rows"]), out["list_total"])
        self.assertTrue(out["rows"])
        self.assertIn("p5_cal", out["rows"][0])
        self.assertEqual(out["calendar"]["month_term"], "寒露")
        self.assertEqual(out["labels"]["S1"], "TW_LIST_BEATS_SAME_TIER_RANDOM")

    def test_tier_filter(self):
        base = T.dispatch("/api/today/latest", {"list": ["L1"]})
        tier = base["rows"][0]["cap_band"]
        self.assertIn(tier, base["tier_options"])
        out = T.dispatch("/api/today/latest", {"list": ["L1"], "tier": [tier]})
        self.assertTrue(all(r["cap_band"] == tier for r in out["rows"]))
        self.assertEqual(out["list_total"], base["list_total"])

    def test_unknown_tier_is_400(self):
        # 名单里冻结的旧档位不再是页面口径，按新档名过滤；旧名要报错而不是静默返回空表
        with self.assertRaises(T.TodayError):
            T.dispatch("/api/today/latest", {"list": ["L1"], "tier": ["微盘"]})

    def test_asof(self):
        out = T.dispatch("/api/today/latest", {"list": ["L2"], "asof": ["2026-10-15"]})
        self.assertEqual(out["asof"], "2026-10-15")
        self.assertTrue(out["rows"])

    def test_ledger_and_dates(self):
        ledger = T.dispatch("/api/today/ledger", {"days": ["20"]})
        self.assertIn("summary", ledger)
        self.assertIn("forward", ledger["summary"])
        dates = T.dispatch("/api/today/dates", {})
        self.assertEqual(dates["dates"][0]["asof"], "2026-10-30")

    def test_export_headers(self):
        data, filename = T.export_csv({"list": ["L1"], "tier": ["all"]})
        text = data.decode("utf-8-sig")
        header = text.splitlines()[0]
        self.assertIn("5 日跑赢概率", header)
        self.assertIn("三层历史", header)
        self.assertTrue(filename.endswith(".csv"))

    def test_missing_is_503(self):
        old = T.TODAY_DIR
        with tempfile.TemporaryDirectory() as tmp:
            T.TODAY_DIR = Path(tmp)
            try:
                with self.assertRaises(T.TodayUnavailable):
                    T.dispatch("/api/today/latest", {})
            finally:
                T.TODAY_DIR = old

    def test_stale_returns_latest_with_flag(self):
        old = T.TODAY_DIR
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "params.json").write_text(
                json.dumps({"features": {"tiers": ["超大盘", "大盘", "中盘", "小盘", "微盘"]}}),
                encoding="utf-8")
            (root / "ledger" / "picks").mkdir(parents=True)
            payload = {
                "schema": "today_watch_picks_v1", "asof": "2026-01-05",
                "entry": "下一交易日开盘", "calendar": {}, "labels": {}, "data": {},
                "n_universe": 1, "n_pool": 1, "backfill": True,
                "lists": {"L1": [{"rank": 1, "code": "000001.SZ", "tier": "大盘"}]},
            }
            (root / "ledger" / "picks" / "2026-01-05.json").write_text(
                json.dumps(payload), encoding="utf-8")
            T.TODAY_DIR = root
            try:
                out = T.dispatch("/api/today/latest", {})
                self.assertTrue(out["stale"])
                self.assertGreater(out["stale_days"], 3)
                self.assertEqual(len(out["rows"]), 1)
            finally:
                T.TODAY_DIR = old

    def test_bad_params(self):
        with self.assertRaises(T.TodayError):
            T.dispatch("/api/today/latest", {"list": ["L4"]})
        with self.assertRaises(T.TodayError):
            T.dispatch("/api/today/ledger", {"days": ["7"]})


if __name__ == "__main__":
    unittest.main(verbosity=2)
