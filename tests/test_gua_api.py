"""流月卦 API：09:30 归属正反例、月窗=6段并集、600519 金标三方对照、四态与降级。"""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKTREE / "app"))

import api  # noqa: E402
import gua_api as G  # noqa: E402

TZ = timezone(timedelta(hours=8))


def _month_row(con, month_id: str) -> dict:
    return api._rows(con, "SELECT month_id, start_at, end_at FROM calendar_months"
                          " WHERE month_id=?", (month_id,))[0]


def _offset_of(month_id: str) -> int:
    con = api.connect()
    try:
        for m in G._timeline(con):
            if m["month_id"] == month_id:
                return m["offset"]
    finally:
        con.close()
    raise AssertionError(f"{month_id} 不在时间线内")


class TestBoundary0930(unittest.TestCase):
    """交节时刻跨 09:30 的正反例（首界/尾界各两条，全部来自冻结历法真实行）。"""

    def test_start_after_0930(self):
        # 2026-M09 寒露 2026-10-08T14:29 → 首个交易日 10-09
        con = api.connect()
        try:
            r = _month_row(con, "2026-M09")
        finally:
            con.close()
        s, _ = api._win_bounds(r["start_at"], r["end_at"], None, None)
        self.assertEqual(s, date(2026, 10, 9))

    def test_start_before_0930(self):
        # 2024-M09 寒露 2024-10-08T02:59 → 交节当日即 d0
        con = api.connect()
        try:
            r = _month_row(con, "2024-M09")
        finally:
            con.close()
        s, _ = api._win_bounds(r["start_at"], r["end_at"], None, None)
        self.assertEqual(s, date(2024, 10, 8))

    def test_end_at_or_after_0930(self):
        # 2026-M09 立冬 2026-11-07T17:52 → 尾界=交节当日 11-07（周六，实际末根 11-06）
        con = api.connect()
        try:
            r = _month_row(con, "2026-M09")
        finally:
            con.close()
        _, e = api._win_bounds(r["start_at"], r["end_at"], None, None)
        self.assertEqual(e, date(2026, 11, 7))

    def test_end_before_0930(self):
        # 2024-M09 立冬 2024-11-07T06:20 → 尾界=前一日 11-06
        con = api.connect()
        try:
            r = _month_row(con, "2024-M09")
        finally:
            con.close()
        _, e = api._win_bounds(r["start_at"], r["end_at"], None, None)
        self.assertEqual(e, date(2024, 11, 6))


class TestMonthEqualsWeekUnion(unittest.TestCase):
    """整月窗必须恰好等于 6 段窗的并集（日期首尾相接、K线数守恒）。"""

    def test_tiling_and_bar_conservation(self):
        for mid in ("2026-M08", "2026-M09", "2024-M09"):
            off = _offset_of(mid)
            out = G.cast({"code": ["600519.SH"], "layer": ["week"],
                          "offset": [str(off)]})
            wins = out["windows"]
            self.assertEqual(len(wins), 6, mid)
            con = api.connect()
            try:
                r = _month_row(con, mid)
            finally:
                con.close()
            ms, me = api._win_bounds(r["start_at"], r["end_at"], None, None)
            self.assertEqual(wins[0]["start"], ms.isoformat(), mid)
            self.assertEqual(wins[-1]["end"], me.isoformat(), mid)
            for a, b in zip(wins, wins[1:]):
                nxt = date.fromisoformat(a["end"]) + timedelta(days=1)
                self.assertEqual(b["start"], nxt.isoformat(), f"{mid} 段间缝隙")
            month_bars = G._daily_bars("600519.SH", False,
                                       ms.isoformat(), me.isoformat())
            self.assertEqual(sum(w["n_bars"] for w in wins), len(month_bars), mid)
            # 已走完的月：6 段全部 fixed，且各段 OHLC 聚合与整月一致
            if me < datetime.now(TZ).date():
                self.assertTrue(all(w["status"] == "fixed" for w in wins), mid)
                bars = month_bars
                self.assertEqual(wins[0]["ohlc"]["open"], f"{bars[0][1]:.2f}", mid)
                self.assertEqual(wins[-1]["ohlc"]["close"], f"{bars[-1][4]:.2f}", mid)
                o = bars[0][1]
                h = max(b[2] for b in bars)
                l = min(b[3] for b in bars)
                c = bars[-1][4]
                r = G.cast_ohlc(o, h, l, c)
                mo = G.cast({"code": ["600519.SH"], "layer": ["month"],
                             "offset": [str(off + 1)]})  # 下一月的月卦锚=本月
                self.assertEqual(mo["anchor_month"]["month_id"], mid)
                w0 = mo["windows"][0]
                self.assertEqual(w0["ohlc"],
                                 {"open": r.open_price, "high": r.high_price,
                                  "low": r.low_price, "close": r.close_price}, mid)
                self.assertEqual(w0["gua"]["gua_name"], r.gua_name, mid)


class TestGolden600519(unittest.TestCase):
    """600519.SH 2026-M09 整月：独立 duckdb 重算 + 手算数字和 + API 三方一致。"""

    def test_golden(self):
        off = _offset_of("2026-M10")  # 选 M10，月卦锚=M09
        out = G.cast({"code": ["600519.SH"], "layer": ["month"],
                      "offset": [str(off)]})
        self.assertEqual(out["anchor_month"]["month_id"], "2026-M09")
        w = out["windows"][0]
        self.assertEqual(w["status"], "fixed")
        self.assertEqual(w["start"], "2026-10-09")
        self.assertEqual(w["end"], "2026-11-07")
        self.assertEqual(w["n_bars"], 21)
        self.assertEqual(w["ohlc"], {"open": "1255.42", "high": "1480.60",
                                     "low": "1255.42", "close": "1453.00"})
        self.assertEqual(w["digit_sums"], {"open": 19, "high": 19,
                                           "low": 19, "close": 13})
        self.assertEqual(w["gua"]["gua_name"], "火风鼎")
        self.assertEqual(w["gua"]["yao_name"], "九二")
        self.assertEqual(w["gua"]["upper_name"], "离")
        self.assertEqual(w["gua"]["lower_name"], "巽")
        # 独立重算：直接读 nas duckdb 原始日线，不经 gua_api 任何取数函数
        import duckdb
        con = duckdb.connect(str(G.NAS_SOURCE_DB), read_only=True)
        try:
            rows = con.execute(
                "SELECT date, open, high, low, close FROM bars"
                " WHERE code='600519.SH' AND date>='2026-10-09'"
                " AND date<='2026-11-07' ORDER BY date").fetchall()
        finally:
            con.close()
        self.assertEqual(len(rows), 21)
        o, h, l, c = (float(rows[0][1]), max(float(r[2]) for r in rows),
                      min(float(r[3]) for r in rows), float(rows[-1][4]))
        self.assertEqual((f"{o:.2f}", f"{h:.2f}", f"{l:.2f}", f"{c:.2f}"),
                         ("1255.42", "1480.60", "1255.42", "1453.00"))


class TestStatesAndDegrade(unittest.TestCase):
    def test_future_windows_are_none(self):
        out = G.cast({"code": ["600519.SH"], "layer": ["xun"], "offset": ["200"]})
        self.assertTrue(all(w["status"] == "none" for w in out["windows"]))
        self.assertTrue(all(w["gua"] is None for w in out["windows"]))

    def test_current_month_has_dynamic_or_none(self):
        out = G.cast({"code": ["600519.SH"], "layer": ["week"], "offset": ["0"]})
        sts = {w["status"] for w in out["windows"]}
        self.assertTrue(sts <= {"fixed", "dynamic", "none", "insufficient"})
        self.assertTrue(sts & {"dynamic", "none"}, "当前月不应全是定卦")
        for w in out["windows"]:
            if w["status"] == "dynamic":
                self.assertIn("未定", w["note"])

    @unittest.skipIf(G.INDEX_EXT.is_file(), "指数补拉已存在，降级路径不再适用")
    def test_index_insufficient_before_ext(self):
        out = G.cast({"code": ["000001.SH"], "layer": ["month"], "offset": ["0"]})
        w = out["windows"][0]
        self.assertEqual(w["status"], "insufficient")
        self.assertIsNone(w["gua"])
        self.assertTrue(w["missing_days"])
        self.assertIn("数据不足", w["note"])

    def test_bad_params(self):
        with self.assertRaises(G.GuaError):
            G.cast({"code": ["600519.SH"], "layer": ["year"], "offset": ["0"]})
        with self.assertRaises(G.GuaError):
            G.cast({"code": ["999999"], "layer": ["month"], "offset": ["0"]})
        with self.assertRaises(G.GuaError):
            G.cast({"code": ["600519.SH"], "layer": ["month"], "offset": ["abc"]})


if __name__ == "__main__":
    unittest.main()