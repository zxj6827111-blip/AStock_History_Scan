"""P5 数据完整性：全库十神复算、统计守恒、只读约束、未来月不伪造行情。

运行：python3 -m unittest tests.test_data_integrity -v
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

import api            # noqa: E402
import ten_god_rules as R  # noqa: E402
import ten_god_service as tg  # noqa: E402


class FactStatusTest(unittest.TestCase):
    """PLAN P2.2：必须能区分无历史事实与无有效行情。"""

    def test_classifier(self):
        self.assertEqual(tg.fact_status(False, "UP"), tg.FACT_NO_HISTORY)
        self.assertEqual(tg.fact_status(True, None), tg.FACT_NO_PRICE)
        self.assertEqual(tg.fact_status(True, "UP"), tg.FACT_HISTORICAL)

    def test_calendar_fact_status_and_boundary(self):
        con = api.connect("etf")
        try:
            cal = api.calendar_now(con, "510050.SH", "etf")
            now = api.datetime.now(api.TZ).isoformat(timespec="microseconds")
            row = con.execute("SELECT month_id FROM calendar_months"
                              " WHERE start_at<=? AND end_at>? ORDER BY start_at DESC LIMIT 1",
                              (now, now)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(cal["current"]["month_id"], row["month_id"])
            # 当前节气月可能尚未收盘：事实状态必须与 has_data 一致，不能假设有数据
            cur_entry = [m for m in cal["months"] if m["offset"] == 0][0]
            expected = (tg.FACT_HISTORICAL if cur_entry["has_data"]
                        else tg.FACT_NO_HISTORY)
            self.assertEqual(cal["current"]["fact_status"], expected)
            self.assertEqual(cal["current"]["ten_god"]["fact_status"], expected)
            nxt = [m for m in cal["months"] if m["offset"] == 1][0]
            self.assertEqual(nxt["fact_status"], tg.FACT_NO_HISTORY)
            self.assertGreater(nxt["start_at"], now)
            for m in cal["months"]:
                self.assertTrue(m["start_at"].endswith("+08:00"), m["month_id"])
                self.assertTrue(m["end_at"].endswith("+08:00"), m["month_id"])
        finally:
            con.close()

    def test_index_comparison_reason(self):
        con = api.connect("ashare")
        try:
            d = api.ten_god_stats(con, "000001.SH", "ALL")
            for g in d["groups"]:
                if g["n"]:
                    self.assertIsNone(g["index_comparison"])
                    self.assertEqual(g["index_comparison_reason"], "NO_MATCHED_BENCHMARK")
        finally:
            con.close()
        con = api.connect("etf")
        try:
            d = api.ten_god_stats(con, "510050.SH", "ALL")
            for g in d["groups"]:
                self.assertIsNotNone(g["index_comparison"])
                self.assertIsNone(g["index_comparison_reason"])
                self.assertEqual(g["fact_status"], tg.FACT_HISTORICAL)
        finally:
            con.close()


class ReadOnlyTest(unittest.TestCase):
    def test_write_is_rejected(self):
        """原库必须只读：任何写入尝试都应报错。"""
        for ds in ("ashare", "etf"):
            con = api.connect(ds)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    con.execute("CREATE TABLE __should_not_exist__(x)")
                with self.assertRaises(sqlite3.OperationalError):
                    con.execute("UPDATE monthly_facts SET stem_ten_god='比肩' WHERE 0")
            finally:
                con.close()


class FullRecomputeTest(unittest.TestCase):
    """两库全部非空行：推导十神 == 库中 stem_ten_god；日主 == 日柱天干。"""

    def _check(self, dataset):
        con = api.connect(dataset)
        try:
            rows = con.execute("""
                SELECT f.identity_code, f.month_id, f.flow_pillar, f.flow_stem,
                       f.day_master_stem, f.stem_ten_god, p.day_stem
                FROM monthly_facts f
                JOIN natal_profiles p ON p.identity_code=f.identity_code
                WHERE f.stem_ten_god IS NOT NULL""").fetchall()
            bad, dm_bad = [], []
            for r in rows:
                dm = r["day_master_stem"]
                if dm != r["day_stem"]:
                    dm_bad.append((r["identity_code"], r["month_id"], dm, r["day_stem"]))
                if R.ten_god(dm, r["flow_stem"]) != r["stem_ten_god"]:
                    bad.append((r["identity_code"], r["month_id"], r["flow_pillar"],
                                r["stem_ten_god"]))
            return len(rows), bad, dm_bad
        finally:
            con.close()

    def test_etf_all_rows(self):
        n, bad, dm_bad = self._check("etf")
        self.assertGreater(n, 75000)
        self.assertEqual(bad, [])
        self.assertEqual(dm_bad, [])

    def test_ashare_all_rows(self):
        n, bad, dm_bad = self._check("ashare")
        self.assertGreater(n, 848000)
        self.assertEqual(bad, [])
        self.assertEqual(dm_bad, [])


class ConservationTest(unittest.TestCase):
    def test_only_ten_god_names(self):
        for ds in ("ashare", "etf"):
            con = api.connect(ds)
            try:
                names = {r[0] for r in con.execute(
                    "SELECT DISTINCT stem_ten_god FROM monthly_facts"
                    " WHERE stem_ten_god IS NOT NULL")}
                self.assertEqual(names, set(R.TEN_GODS), ds)
                total = con.execute("SELECT COUNT(*) FROM monthly_facts").fetchone()[0]
                nulls = con.execute("SELECT COUNT(*) FROM monthly_facts"
                                    " WHERE stem_ten_god IS NULL").fetchone()[0]
                prof0 = con.execute("SELECT COUNT(*) FROM monthly_facts"
                                    " WHERE profile_present=0").fetchone()[0]
                self.assertEqual(nulls, prof0, f"{ds}: 空十神必须恰好等于缺档案行")
                self.assertEqual(total - nulls, con.execute(
                    "SELECT COUNT(*) FROM monthly_facts WHERE stem_ten_god IS NOT NULL"
                ).fetchone()[0])
            finally:
                con.close()

    def test_no_mixed_identity(self):
        for ds in ("ashare", "etf"):
            con = api.connect(ds)
            try:
                mixed = con.execute("""
                  SELECT COUNT(*) FROM (
                    SELECT identity_code FROM monthly_facts GROUP BY 1
                    HAVING SUM(stem_ten_god IS NULL)>0 AND SUM(stem_ten_god IS NOT NULL)>0)"""
                ).fetchone()[0]
                self.assertEqual(mixed, 0, ds)
            finally:
                con.close()

    def test_510050_groups(self):
        con = api.connect("etf")
        try:
            d = api.ten_god_stats(con, "510050.SH", "ALL")
            self.assertEqual(d["status"], "OK")
            self.assertEqual(sum(g["n"] for g in d["groups"]), d["accounting"]["total_rows"])
            self.assertEqual(d["accounting"]["unclassified"], 0)
            self.assertEqual([g["n"] for g in d["groups"]], [26] * 10)
            for g in d["groups"]:
                self.assertEqual(g["n"], g["up"] + g["down"] + g["flat"])
        finally:
            con.close()

    def test_510050_exclude_single_identity(self):
        con = api.connect("etf")
        try:
            a = api.ten_god_stats(con, "510050.SH", "ALL")
            e = api.ten_god_stats(con, "510050.SH", "EXCLUDE_SINGLE")
            self.assertEqual(a["accounting"]["total_rows"] - e["accounting"]["total_rows"],
                             sum(g["single_day_n"] for g in a["groups"]))
        finally:
            con.close()

    def test_single_day_difference_tracked(self):
        con = api.connect("ashare")
        try:
            a = api.ten_god_stats(con, "000536.SZ", "ALL")
            e = api.ten_god_stats(con, "000536.SZ", "EXCLUDE_SINGLE")
            self.assertEqual(a["accounting"]["total_rows"] - e["accounting"]["total_rows"], 3)
            self.assertEqual(sum(g["single_day_n"] for g in a["groups"]), 3)
            self.assertEqual(sum(g["single_day_n"] for g in e["groups"]), 0)
        finally:
            con.close()

    def test_relation_filter_no_duplication(self):
        con = api.connect("etf")
        try:
            d = api.ten_god_stats(con, "510050.SH", "ALL", relation_type="天干克")
            self.assertEqual(sum(g["n"] for g in d["groups"]), d["accounting"]["total_rows"])
            self.assertEqual(d["accounting"]["unclassified"], 0)
        finally:
            con.close()

    def test_missing_profile_not_classified(self):
        con = api.connect("ashare")
        try:
            d = api.ten_god_stats(con, "000003.SZ", "ALL")
            self.assertEqual(d["status"], "UNAVAILABLE_PROFILE")
            self.assertEqual(sum(g["n"] for g in d["groups"]), 0)
            self.assertEqual(d["unavailable"]["missing_profile"],
                             d["accounting"]["total_rows"])
        finally:
            con.close()

    def test_group_metrics_match_frozen_facts(self):
        """每组 n/涨/跌/平/均值/指数比较必须与 monthly_facts 直查一致。"""
        con = api.connect("etf")
        try:
            d = api.ten_god_stats(con, "510050.SH", "ALL")
            for g in d["groups"]:
                row = con.execute("""
                  SELECT COUNT(*) n, SUM(direction='UP') up, SUM(direction='DOWN') down,
                         SUM(direction='FLAT') flat, AVG(body_return) mean,
                         AVG(excess_return) excess,
                         SUM(comparison_status='MATCHED') bn
                  FROM monthly_facts WHERE identity_code='510050.SH' AND stem_ten_god=?""",
                                  (g["ten_god"],)).fetchone()
                self.assertEqual(g["n"], row["n"])
                self.assertEqual(g["up"], row["up"])
                self.assertEqual(g["down"], row["down"])
                self.assertEqual(g["flat"], row["flat"])
                self.assertAlmostEqual(g["mean_return"], row["mean"], places=9)
                if row["bn"]:
                    self.assertAlmostEqual(g["index_comparison"]["excess_mean"],
                                           row["excess"], places=9)
            # 逐月下钻的行数必须等于该组 n（防重复/漏计）
            for g in d["groups"]:
                m = api.ten_god_months(con, "510050.SH", g["ten_god"], "ALL")
                self.assertEqual(m["n"], g["n"], g["ten_god"])
        finally:
            con.close()


class Golden510050Test(unittest.TestCase):
    def test_four_occurrences(self):
        con = api.connect("etf")
        try:
            rows = api.pillar_detail(con, "510050.SH", "己亥")
            self.assertEqual([r["month_id"] for r in rows],
                             ["2006-M10", "2011-M10", "2016-M10", "2021-M10"])
            self.assertEqual([r["direction"] for r in rows], ["UP", "DOWN", "UP", "UP"])
            for r in rows:
                self.assertEqual(r["stem_ten_god"], "劫财")
                self.assertTrue(r["stem_ten_god_consistent"])
                self.assertEqual([(h["stem"], h["ten_god"]) for h in
                                  r["flow_hidden_stem_ten_gods"]],
                                 [("壬", "偏财"), ("甲", "七杀")])
                self.assertEqual(r["ten_god"]["day_master"], "戊")
            self.assertAlmostEqual(rows[0]["body_return"], 0.185224, places=5)
            self.assertAlmostEqual(rows[0]["excess_return"], 0.041578, places=5)
        finally:
            con.close()

    def test_future_calendar_no_fake_market(self):
        con = api.connect("etf")
        try:
            cal = api.calendar_now(con, "510050.SH", "etf")
            self.assertEqual(cal["day_master"], "戊")
            self.assertEqual(cal["ten_god_status"], "OK")
            cur = cal["current"]
            self.assertEqual(cur["month_pillar"], "戊戌")
            self.assertEqual(cur["ten_god"]["stem"]["ten_god"], "比肩")
            nxt = [m for m in cal["months"] if m["offset"] == 1][0]
            self.assertEqual(nxt["month_pillar"], "己亥")
            self.assertEqual(nxt["ten_god"]["stem"]["ten_god"], "劫财")
            self.assertEqual([(h["stem"], h["ten_god"]) for h in
                              nxt["ten_god"]["hidden_stems"]],
                             [("壬", "偏财"), ("甲", "七杀")])
            for m in cal["months"]:
                self.assertNotIn("body_return", m)
                self.assertNotIn("direction", m)
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
