"""栏目加载性能防回归：首屏轻量元信息、N2 元信息缓存、段筛选缓存。

跑法：python3 -m unittest -v tests.test_nav_performance（在 tengod 根目录）
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import api  # noqa: E402
import n2_api  # noqa: E402
import seg_api  # noqa: E402
import ten_god_rules  # noqa: E402


class TestMainMetaLite(unittest.TestCase):
    def setUp(self):
        api._META_CACHE.clear()
        self.con = sqlite3.connect(":memory:")
        self.con.execute("CREATE TABLE securities("
                         "security_type TEXT, delisted_date TEXT, has_natal_profile INTEGER)")
        self.con.execute("INSERT INTO securities VALUES ('A_SHARE', NULL, 1)")
        for table in ("stats_pillar60", "stats_branch12", "stats_relation_types",
                      "stats_relation_pillar", "relation_type_market_stats"):
            self.con.execute(f"CREATE TABLE {table}(x INTEGER)")
            self.con.execute(f"INSERT INTO {table} VALUES (1)")

    def tearDown(self):
        self.con.close()
        api._META_CACHE.clear()

    def test_lite_needs_no_heavy_archive_tables(self):
        # 故意不建 monthly_facts/monthly_relations：首屏轻量模式若扫描它们会直接失败。
        out = api.meta(self.con, "ashare", lite=True)
        self.assertTrue(out["stats_ready"])
        self.assertEqual(out["default_identity"], api.DEFAULT_IDENTITY)
        self.assertNotIn("monthly_facts", out)
        self.assertNotIn("monthly_relations", out)
        self.assertIn(("ashare", True), api._META_CACHE)


class _Result(list):
    def fetchall(self):
        return list(self)

    def fetchone(self):
        return self[0] if self else None


class _N2Connection:
    def __init__(self):
        self.calls = []

    def execute(self, sql):
        self.calls.append(sql)
        if "SELECT key, value FROM meta" in sql:
            return _Result()
        if "min(trade_date), max(trade_date)" in sql:
            return _Result([("2020-01-01", "2026-10-08")])
        if "max(trade_date) FROM daily_facts WHERE asset_class='A_SHARE'" in sql:
            return _Result([("2026-10-08",)])
        if "DISTINCT official_ten_god" in sql:
            raise AssertionError("十神下拉不应 DISTINCT 扫描关系模板表")
        raise AssertionError("unexpected SQL: " + sql)


class TestN2MetaCache(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.db = root / "n2.sqlite"
        self.db.write_bytes(b"")
        self.stats = root / "stats"
        self.stats.mkdir()
        for name in n2_api.STATS_FILES.values():
            (self.stats / name).write_text("x\n", encoding="utf-8")
        self.eligible = root / "eligible.csv"
        self.eligible.write_text("identity_code\n", encoding="utf-8")
        self.patches = [
            mock.patch.object(n2_api, "N2_DB", self.db),
            mock.patch.object(n2_api, "N2_STATS_DIR", self.stats),
            mock.patch.object(n2_api, "N2_ELIGIBLE_CSV", self.eligible),
            mock.patch.object(n2_api, "_read_stats", return_value=[]),
            mock.patch.object(n2_api, "disputed_set", return_value=frozenset()),
        ]
        for p in self.patches:
            p.start()
        n2_api._META_CACHE.update(stamp=None, data=None)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        n2_api._META_CACHE.update(stamp=None, data=None)
        self._tmp.cleanup()

    def test_frozen_ten_gods_and_file_stamp_invalidation(self):
        con = _N2Connection()
        first = n2_api.meta(con)
        self.assertEqual(first["ten_gods"], list(ten_god_rules.TEN_GODS))
        calls = len(con.calls)
        second = n2_api.meta(con)
        self.assertEqual(second, first)
        self.assertEqual(len(con.calls), calls)  # 第二次完全命中缓存

        self.db.write_bytes(b"x")
        self.db.touch()
        n2_api.meta(con)
        self.assertGreater(len(con.calls), calls)  # 文件指纹变化后必须重算


class _SegmentCursor:
    description = [("code",), ("s",), ("e",)]

    def fetchall(self):
        return [("000001.SZ", 0, 3)]


class _SegmentConnection:
    def __init__(self):
        self.calls = 0

    def execute(self, _sql, _params):
        self.calls += 1
        return _SegmentCursor()


class TestSegmentsCache(unittest.TestCase):
    def setUp(self):
        seg_api._SEGMENTS_CACHE.clear()

    def tearDown(self):
        seg_api._SEGMENTS_CACHE.clear()

    def test_cache_is_scoped_by_stamp_and_returns_clean_copies(self):
        con = _SegmentConnection()
        stamp = {"value": ("base",)}
        with mock.patch.object(seg_api, "_con", return_value=con), \
             mock.patch.object(seg_api, "_ensure_seg_view", return_value="seg_all"), \
             mock.patch.object(seg_api, "_seg_stamp", side_effect=lambda: stamp["value"]):
            first = seg_api._segments("己亥", "up", 5, 5.0, 5.0, 1, 10.0)
            second = seg_api._segments("己亥", "up", 5, 5.0, 5.0, 1, 10.0)
            self.assertEqual(con.calls, 1)
            self.assertEqual(first, second)
            self.assertIsNot(first[0], second[0])

            second[0]["_seg"] = {"status": "ongoing"}
            third = seg_api._segments("己亥", "up", 5, 5.0, 5.0, 1, 10.0)
            self.assertNotIn("_seg", third[0])

            stamp["value"] = ("new-data",)
            seg_api._segments("己亥", "up", 5, 5.0, 5.0, 1, 10.0)
            self.assertEqual(con.calls, 2)


if __name__ == "__main__":
    unittest.main()
