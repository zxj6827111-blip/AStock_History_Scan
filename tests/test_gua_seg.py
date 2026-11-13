"""100%段抽屉卦：历次同段三方独立对照、本段四态、实时库 code6 合并回归、正对照。

段卦口径：段所跨交易日的日线 raw OHLC（60分钟线无高/低）。历次同段日期区间取
seg_api.segment_path 的 hist[].start/end；本段取本月 axis。全部只读。
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKTREE / "app"))

import api  # noqa: E402
import gua_api as G  # noqa: E402
import seg_api as S  # noqa: E402
from bagua import cast_ohlc  # noqa: E402

TZ = timezone(timedelta(hours=8))


def _pick_segment(s_start_max=87):
    """从段库取一个真实 100% 段（动态，随当前柱变化也稳）。"""
    b = S.board({"dir": ["up"], "seg_n": ["5"], "cap": ["60"]})
    for r in b.get("rows", []):
        sg = r.get("_seg") or {}
        if "s" in sg and int(sg["s"]) <= s_start_max:
            return r["identity_code"], r["flow_pillar"], int(sg["s"]), int(sg["e"])
    raise AssertionError("段库取不到可用 100% 段")


def _independent_ohlc(code, d0, d1):
    """不经 gua_api 取数：直接 nas 日线 + 实时库（6位码）合并，重算区间 OHLC。"""
    import duckdb
    import sqlite3
    merged = {}
    con = duckdb.connect(str(G.NAS_SOURCE_DB), read_only=True)
    try:
        for dt_, o, h, l, c in con.execute(
                "SELECT date,open,high,low,close FROM bars WHERE code=? AND date>=? AND date<=?",
                [code, d0, d1]).fetchall():
            if None not in (o, h, l, c):
                merged[str(dt_)[:10]] = (float(o), float(h), float(l), float(c))
    finally:
        con.close()
    if G.LIVE_DB.is_file():
        lc = sqlite3.connect(G.LIVE_DB.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            for dt_, o, h, l, c in lc.execute(
                    "SELECT date,open,high,low,close FROM bars WHERE code=? AND date>=? AND date<=?",
                    (code.split(".")[0], d0, d1)).fetchall():
                if None not in (o, h, l, c):
                    merged[str(dt_)[:10]] = (float(o), float(h), float(l), float(c))
        finally:
            lc.close()
    days = [merged[k] for k in sorted(merged)]
    if not days:
        return None
    return (days[0][0], max(d[1] for d in days), min(d[2] for d in days), days[-1][3], len(days))


class TestSegHistThreeWay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.code, cls.pillar, cls.s, cls.e = _pick_segment()
        cls.out = G.seg_cast({"code": [cls.code], "pillar": [cls.pillar],
                              "s": [str(cls.s)], "e": [str(cls.e)], "offset": ["0"]})

    def test_hist_nonempty_and_matches_path(self):
        path = S.segment_path({"code": [self.code], "pillar": [self.pillar],
                               "s": [str(self.s)], "e": [str(self.e)], "offset": ["0"]})
        self.assertEqual(self.out["n_hist"], len(path["hist"]))
        self.assertGreater(self.out["n_hist"], 0)
        self.assertEqual([h["month_id"] for h in self.out["hist_guas"]],
                         [h["month_id"] for h in path["hist"]])

    def test_hist_three_way_independent(self):
        for h in self.out["hist_guas"]:
            ind = _independent_ohlc(self.code, h["start"], h["end"])
            self.assertIsNotNone(ind, f"{h['month_id']} 独立重算无日线")
            o, hh, ll, c, n = ind
            ref = cast_ohlc(o, hh, ll, c)
            self.assertEqual(h["ohlc"], {"open": ref.open_price, "high": ref.high_price,
                                         "low": ref.low_price, "close": ref.close_price},
                             f"{h['month_id']} OHLC 不一致")
            self.assertEqual(h["digit_sums"], {"open": ref.open_digit_sum, "high": ref.high_digit_sum,
                                               "low": ref.low_digit_sum, "close": ref.close_digit_sum},
                             f"{h['month_id']} 数字和不一致")
            self.assertEqual(h["gua"]["gua_name"], ref.gua_name, f"{h['month_id']} 卦名不一致")
            self.assertEqual(h["gua"]["yao_name"], ref.yao_name, f"{h['month_id']} 动爻不一致")
            self.assertEqual(h["n_bars"], n, f"{h['month_id']} K线数不一致")

    def test_hist_past_all_fixed(self):
        today = datetime.now(TZ).date()
        for h in self.out["hist_guas"]:
            if date.fromisoformat(h["end"]) < today:
                self.assertEqual(h["status"], "fixed", f"{h['month_id']} 已走完应定卦")
                self.assertIsNotNone(h["gua"])

    def test_positive_control_detects_tamper(self):
        h = self.out["hist_guas"][0]
        ind = _independent_ohlc(self.code, h["start"], h["end"])
        ref = cast_ohlc(*ind[:4])
        bad = dict(h["gua"])
        bad["gua_name"] = "篡改卦"
        self.assertNotEqual(bad["gua_name"], ref.gua_name)
        self.assertEqual(h["gua"]["gua_name"], ref.gua_name)

    def test_gua_has_gaodao_and_consensus(self):
        # 每个卦都带高岛断语 + 双好/双差共识字段（供抽屉/机会表统一渲染）
        for h in self.out["hist_guas"]:
            g = h["gua"]
            self.assertIn("gaodao_text", g)
            self.assertIn("gaodao_side", g)
            self.assertIn(g["consensus"], {"▲双好", "▼双差", "分歧", ""})


class TestSegLive(unittest.TestCase):
    def test_live_code6_merge_regression(self):
        """本段必须合并实时库（6位码）当月日线——code6 修复回归。"""
        code, pillar, s, e = _pick_segment(s_start_max=3)  # 第1交易日起的段
        out = G.seg_cast({"code": [code], "pillar": [pillar], "s": [str(s)], "e": [str(e)],
                          "offset": ["0"]})
        lg = out["live_gua"]
        self.assertIsNotNone(lg, "本月第1日起的段应有本段卦")
        ind = _independent_ohlc(code, lg["start"], lg["end"])
        self.assertIsNotNone(ind)
        self.assertEqual(lg["n_bars"], ind[4], "本段K线数应等于 nas+实时库合并后的天数")
        self.assertGreaterEqual(lg["n_bars"], 1)

    def test_live_four_state(self):
        code, pillar, s, e = _pick_segment()
        out = G.seg_cast({"code": [code], "pillar": [pillar], "s": [str(s)], "e": [str(e)],
                          "offset": ["0"]})
        lg = out["live_gua"]
        if lg is not None:
            self.assertIn(lg["status"], {"fixed", "dynamic", "none", "insufficient"})
            if lg["status"] == "dynamic":
                self.assertIn("未定", lg["note"])

    def test_future_offset_no_live(self):
        code, pillar, s, e = _pick_segment()
        out = G.seg_cast({"code": [code], "pillar": [pillar], "s": [str(s)], "e": [str(e)],
                          "offset": ["200"]})
        self.assertIsNone(out["live_gua"], "未来月非本月，无本段卦")
        self.assertGreater(out["n_hist"], 0, "历次同段是该柱历史，与 offset 无关")


class TestSegBadParams(unittest.TestCase):
    def test_bad_pillar(self):
        with self.assertRaises(G.GuaError):
            G.seg_cast({"code": ["600519.SH"], "pillar": ["不存在柱"], "s": ["0"], "e": ["10"]})

    def test_bad_code(self):
        with self.assertRaises(G.GuaError):
            G.seg_cast({"code": ["999999"], "pillar": ["己亥"], "s": ["0"], "e": ["10"]})

    def test_unknown_endpoint(self):
        with self.assertRaises(G.GuaError):
            G.dispatch("/api/gua/nope", {})


if __name__ == "__main__":
    unittest.main()