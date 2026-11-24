"""模拟盘信号层单测：四类策略意图、语义哈希、退出与段规则（纯函数，不触网不落盘）。

跑法（tengod 根目录）：python3 -m unittest -v tests.test_paper_signals
"""
import json
import tempfile
import unittest
from pathlib import Path

import sys
APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from paper_trade import signals  # noqa: E402


def make_report(rows, asof="2026-11-20", session="PM", seg_id="S4",
                regime="Bear", month="2026-M10"):
    return {
        "schema": "survivor_report_v1", "month_id": month, "pillar": "己亥",
        "title": "t", "asof_date": asof, "asof_time": "15:00",
        "session": session, "generated_at_bjt": "2026-11-20 15:05:00",
        "current_segment": ({"id": seg_id, "start": "2026-11-23",
                             "end": "2026-11-27", "action": "段动作"} if seg_id
                            else None),
        "index_filter": {"regime": regime, "text": "x"},
        "rows": rows,
    }


def row(code, name=None, group="D", b_tier=None, action=None, no_chase=False,
        close=10.0, month_open=10.0, mtd=0.0, mtm=0.0, dif=0.2, dea=0.1,
        d_v1_state="none", d_v2_signal=False, **kw):
    d = {
        "code": code, "name": name or f"股{code[:6]}", "group": group,
        "b_tier": b_tier, "b_tier_action": action, "no_chase": no_chase,
        "close": close, "month_open": month_open,
        "break_level": kw.pop("break_level", month_open),
        "mtd_pct": mtd, "mtm_pct": mtm, "dif": dif, "dea": dea,
        "cross": "金叉" if dif > dea else "死叉",
        "d_v1_state": d_v1_state, "d_v1_trigger_date": None,
        "d_v1_buy_ref": None, "d_v2_signal": d_v2_signal,
        "advice": "", "stop_loss": round(close * 0.95, 2),
        "target_3": round(close * 1.03, 2), "target_5": round(close * 1.05, 2),
    }
    d.update(kw)
    return d


def make_rules(**over):
    rules = {
        "schema": "paper_month_rules_v1", "month_id": "2026-M10",
        "regime_total_cap": {"Bear": 50.0, "Neutral": 80.0, "Bull": 100.0},
        "no_new_buy_segments": ["S4", "S5", "S6"],
        "strategies": {
            "b8": {"enabled": True, "per_name_weight": 10.0,
                   "buy_segments": ["S2", "S3"], "stop_pct": -5.0},
            "s1c": {"enabled": True, "per_name_weight": 10.0,
                    "buy_dates": ["2026-11-12"], "stop_pct": -5.0},
            "d_v1": {"enabled": True, "per_name_weight": 5.0, "st_weight": 2.5,
                     "buy_segments": ["S1", "S2", "S3"],
                     "target_pct": 3.0, "stop_pct": -5.0},
            "d_v2": {"enabled": True, "execution_mode": "signal_only",
                     "buy_segments": [], "target_pct": 3.0, "stop_pct": -5.0},
        },
        "s5_max_total_weight": 50.0,
        "final_liquidate_date": "2026-12-07",
    }
    rules.update(over)
    return rules


def held(code, strategy="b8", weight=10.0, stop=9.5, target=None,
         month_id="2026-M10", entry_ref=10.0, is_probe=0, entry_date="2026-11-20"):
    return {"code": code, "strategy": strategy, "weight": weight,
            "stop": stop, "target": target, "entry_date": entry_date,
            "entry_ref": entry_ref, "approx_entry": 1, "month_id": month_id,
            "is_probe": is_probe}


class TestHash(unittest.TestCase):
    def test_semantic_hash_ignores_display_fields(self):
        r1 = make_report([row("000001.SZ")])
        r2 = make_report([row("000001.SZ")])
        r2["generated_at_bjt"] = "2026-11-21 19:05:00"
        r2["title"] = "别的标题"
        self.assertEqual(signals.semantic_hash(r1), signals.semantic_hash(r2))

    def test_semantic_hash_changes_on_close(self):
        r1 = make_report([row("000001.SZ", close=10.0)])
        r2 = make_report([row("000001.SZ", close=10.5)])
        self.assertNotEqual(signals.semantic_hash(r1), signals.semantic_hash(r2))

    def test_semantic_hash_changes_on_d_v2_signal(self):
        r1 = make_report([row("000001.SZ", d_v2_signal=False)])
        r2 = make_report([row("000001.SZ", d_v2_signal=True)])
        self.assertNotEqual(signals.semantic_hash(r1), signals.semantic_hash(r2))

    def test_load_report_rejects_intraday(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "survivor_2026-11-20_1030_INTRADAY.json"
            p.write_text(json.dumps(make_report([], session="INTRADAY")),
                         encoding="utf-8")
            with self.assertRaises(signals.SignalError):
                signals.load_report(p)

    def test_load_month_rules_missing(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(signals.SignalError):
                signals.load_month_rules("2026-M11", Path(td))


class TestBuys(unittest.TestCase):
    def _intents(self, rep, rules=None, held_list=None, month_positions=None):
        return signals.build_intents(rep, rules or make_rules(),
                                      held_list or [], month_positions or [])

    def test_b8_buys_in_s3(self):
        rep = make_report([row("000001.SZ", group="B", b_tier="B8",
                              action="执行", mtd=5.0, mtm=9.0)], seg_id="S3")
        out = self._intents(rep)
        self.assertEqual(len(out["buys"]), 1)
        b = out["buys"][0]
        self.assertEqual((b["code"], b["strategy"], b["weight"]),
                         ("000001.SZ", "b8", 10.0))
        self.assertEqual(b["stop"], 9.5)   # 10 × 0.95（信号日口径，计划展示用）
        self.assertEqual(b["stop_pct"], -5.0)
        self.assertIsNone(b["target_pct"])

    def test_b8_blocked_in_s4_with_reason(self):
        rep = make_report([row("000001.SZ", group="B", b_tier="B8",
                              action="执行", mtd=5.0, mtm=9.0)], seg_id="S4")
        out = self._intents(rep)
        self.assertEqual(out["buys"], [])
        self.assertTrue(any(x["code"] == "000001.SZ"
                            and "不允许" in x["reason"] for x in out["excluded"]))

    def test_b8_no_chase_excluded_with_reason(self):
        rep = make_report([row("000001.SZ", group="B", b_tier="B8",
                              action="执行", no_chase=True, mtm=16.0)],
                          seg_id="S3")
        out = self._intents(rep)
        self.assertEqual([b for b in out["buys"] if b["code"] == "000001.SZ"], [])
        self.assertTrue(any(x["code"] == "000001.SZ" and "不追" in x["reason"]
                            for x in out["excluded"]))

    def test_b8_watch_tier_not_bought(self):
        rep = make_report([row("000001.SZ", group="A-other", b_tier="B7",
                              action="观察", mtd=5.0, mtm=7.5)], seg_id="S3")
        out = self._intents(rep)
        self.assertEqual(out["buys"], [])

    def test_cap_limits_new_buys(self):
        rows = [row(f"00000{i}.SZ", group="B", b_tier="B8", action="执行",
                   mtd=5.0, mtm=9.0) for i in range(1, 7)]   # 6 只 × 10% > 50%
        rep = make_report(rows, seg_id="S3")
        out = self._intents(rep)
        self.assertEqual(len(out["buys"]), 5)   # Bear cap 50%
        self.assertEqual(len(out["excluded"]), 1)
        self.assertIn("上限", out["excluded"][0]["reason"])

    def test_s1c_only_on_buy_dates(self):
        r_c = row("000002.SZ", group="C", mtd=3.0, mtm=-2.0)
        out = self._intents(make_report([r_c], asof="2026-11-12", seg_id="S1"))
        self.assertEqual([(b["code"], b["strategy"], b["weight"]) for b in out["buys"]],
                         [("000002.SZ", "s1c", 10.0)])
        out2 = self._intents(make_report([r_c], asof="2026-11-13", seg_id="S2"))
        self.assertEqual(out2["buys"], [])   # 过期不追

    def test_d_v1_first_break_buys_and_st_halves(self):
        rows = [row("000003.SZ", d_v1_state="first_break_today", close=8.0),
                row("000004.SZ", name="ST测试", d_v1_state="first_break_today",
                    close=4.0),
                row("000005.SZ", d_v1_state="buy_today", close=8.0),
                row("000006.SZ", d_v1_state="expired", close=8.0)]
        out = self._intents(make_report(rows, seg_id="S3"))
        buys = {b["code"]: b for b in out["buys"]}
        self.assertEqual(set(buys), {"000003.SZ", "000004.SZ"})
        self.assertEqual(buys["000003.SZ"]["weight"], 5.0)
        self.assertEqual(buys["000004.SZ"]["weight"], 2.5)   # ST 再半
        self.assertEqual(buys["000003.SZ"]["target"], 8.24)  # +3%（信号日口径）
        self.assertEqual(buys["000003.SZ"]["stop"], 7.6)     # −5%
        self.assertEqual(buys["000003.SZ"]["stop_pct"], -5.0)   # 引擎按成交价重算
        self.assertEqual(buys["000003.SZ"]["target_pct"], 3.0)

    def test_d_v2_signal_only_all_segments(self):
        r = row("000007.SZ", group="D", d_v2_signal=True,
                close=9.0, mtm=-2.0, mtm_prev=-4.0)
        for seg in ("S1", "S2", "S3", "S4", "S5", "S6"):
            with self.subTest(segment=seg):
                rep = make_report([r], seg_id=seg)
                out = self._intents(rep)
                self.assertEqual(out["buys"], [])
                obs = [x for x in out["excluded"] if x.get("signal_only")]
                self.assertEqual(len(obs), 1)
                self.assertEqual(obs[0]["blocked_reason"], "d_v2_signal_only")
                self.assertIn("不自动下单", obs[0]["reason"])
                plan = signals.build_plan(rep, make_rules(), [], [],
                                          "2026-11-25", "ZH", "r.json")
                self.assertNotIn("000007.SZ", plan["target_weights"])

    def test_no_new_buy_segments_block_d_v1(self):
        r = row("000003.SZ", d_v1_state="first_break_today", close=8.0)
        for seg in ("S4", "S5", "S6"):
            with self.subTest(segment=seg):
                out = self._intents(make_report([r], seg_id=seg))
                self.assertEqual(out["buys"], [])
                self.assertTrue(any(x.get("blocked_reason") == "segment_no_new_buy"
                                    for x in out["excluded"]))

    def test_no_new_buy_segments_clear_probe_topup(self):
        r = row("000001.SZ", group="B", b_tier="B8", action="执行",
                mtd=5.0, mtm=9.0)
        rep = make_report([r], asof="2026-11-20", seg_id="S4")
        rules = make_rules()
        rules["strategies"]["b8"]["buy_segments"].append("S4")  # 错配也不能绕过总闸门
        held_list = [held("000001.SZ", weight=3.33, is_probe=1,
                          entry_date="2026-11-20")]
        out = signals.build_intents(rep, rules, held_list, [])
        self.assertEqual(out["buys"], [])
        self.assertNotIn("weight_target", out["holds"][0])
        self.assertTrue(any("补仓" in x["reason"] for x in out["excluded"]))
        plan = signals.build_plan(rep, rules, held_list, [],
                                  "2026-11-23", "ZH", "r.json")
        self.assertEqual(plan["target_weights"]["000001.SZ"], 3.33)

    def test_already_held_not_rebought(self):
        r = row("000008.SZ", group="B", b_tier="B8", action="执行",
                mtd=5.0, mtm=9.0)
        out = self._intents(make_report([r], seg_id="S3"),
                           held_list=[held("000008.SZ", strategy="d_v1")])
        self.assertEqual(out["buys"], [])
        self.assertTrue(any("已持仓" in x["reason"] for x in out["excluded"]))


class TestExits(unittest.TestCase):
    def _intents(self, rep, held_list, rules=None, month_positions=None):
        return signals.build_intents(rep, rules or make_rules(), held_list,
                                     month_positions or [])

    def test_stop_loss_exit(self):
        # 破位价压低：只触发止损（signals 里破月初价优先于止损检查）
        rep = make_report([row("000001.SZ", close=9.0, month_open=8.0)])
        out = self._intents(rep, [held("000001.SZ", stop=9.5)])
        self.assertEqual(len(out["exits"]), 1)
        self.assertIn("止损", out["exits"][0]["reason"])
        self.assertEqual(out["holds"], [])

    def test_break_level_exit(self):
        rep = make_report([row("000001.SZ", close=9.9, month_open=10.0)])
        out = self._intents(rep, [held("000001.SZ", stop=8.0)])
        self.assertEqual(len(out["exits"]), 1)
        self.assertIn("破月初价", out["exits"][0]["reason"])

    def test_d_target_exit(self):
        rep = make_report([row("000001.SZ", close=10.5)])
        out = self._intents(rep, [held("000001.SZ", strategy="d_v1",
                                       stop=9.0, target=10.3)])
        self.assertEqual(len(out["exits"]), 1)
        self.assertIn("目标", out["exits"][0]["reason"])

    def test_d_v2_existing_position_target_exit_still_works(self):
        rep = make_report([row("000001.SZ", close=10.5)], seg_id="S4")
        out = self._intents(rep, [held("000001.SZ", strategy="d_v2",
                                       stop=9.0, target=10.3)])
        self.assertEqual(out["buys"], [])
        self.assertEqual(len(out["exits"]), 1)
        self.assertIn("目标", out["exits"][0]["reason"])

    def test_manual_position_stop_and_target_exit(self):
        rep = make_report([row("000001.SZ", close=10.5)], seg_id="S4")
        out = self._intents(rep, [held("000001.SZ", strategy="manual",
                                       stop=9.0, target=10.3)])
        self.assertEqual(len(out["exits"]), 1)
        self.assertIn("目标", out["exits"][0]["reason"])
        out2 = self._intents(make_report([row("000001.SZ", close=8.9)], seg_id="S4"),
                             [held("000001.SZ", strategy="manual", stop=9.0,
                                   target=10.3)])
        self.assertEqual(len(out2["exits"]), 1)
        self.assertIn("止损", out2["exits"][0]["reason"])

    def test_manual_position_blocks_auto_rebuy(self):
        r = row("000001.SZ", group="B", b_tier="B8", action="执行",
                mtd=5.0, mtm=9.0)
        out = self._intents(make_report([r], seg_id="S3"),
                            [held("000001.SZ", strategy="manual", stop=9.0)])
        self.assertEqual(out["buys"], [])
        self.assertTrue(any("已持仓" in x["reason"] for x in out["excluded"]))

    def test_manual_position_participates_s6_liquidation(self):
        rep = make_report([row("000001.SZ", close=10.0)], asof="2026-12-07")
        out = self._intents(rep, [held("000001.SZ", strategy="manual", stop=9.0)])
        self.assertEqual(len(out["exits"]), 1)
        self.assertIn("S6 清仓", out["exits"][0]["reason"])

    def test_missing_row_keeps_position_with_warning(self):
        rep = make_report([row("000009.SZ")])
        out = self._intents(rep, [held("000001.SZ")])
        self.assertEqual(out["exits"], [])
        self.assertEqual(len(out["holds"]), 1)
        self.assertTrue(any("缺行情" in w for w in out["warnings"]))

    def test_trailing_stop_only_moves_up_in_s4(self):
        rep = make_report([row("000001.SZ", close=11.0)])
        out = self._intents(rep, [held("000001.SZ", stop=9.0)])
        h = out["holds"][0]
        self.assertEqual(h["stop"], 10.45)   # 11 × 0.95，只上移
        self.assertTrue(h.get("trailing_updated"))
        # 已高于新移动止损时不动
        rep2 = make_report([row("000001.SZ", close=10.5)])
        out2 = self._intents(rep2, [held("000001.SZ", stop=10.45)])
        self.assertFalse(out2["holds"][0].get("trailing_updated"))

    def test_group_change_without_exit_not_sold(self):
        # 跌出 B8 到 C（未触发止损/破位）不机械追杀
        rep = make_report([row("000001.SZ", group="C", close=10.5)])
        out = self._intents(rep, [held("000001.SZ", stop=8.0)])
        self.assertEqual(out["exits"], [])

    def test_s6_liquidates_all(self):
        rep = make_report([row("000001.SZ", close=10.0)], asof="2026-12-07")
        out = self._intents(rep, [held("000001.SZ"), held("000002.SZ")])
        self.assertEqual(out["holds"], [])
        self.assertEqual(len(out["exits"]), 2)
        self.assertTrue(all("S6 清仓" in e["reason"] for e in out["exits"]))

    def test_s5_reduces_main_to_cap(self):
        rows = [row("000001.SZ", close=10.0, mtd=2.0),
                row("000002.SZ", close=10.0, mtd=6.0)]
        rep = make_report(rows, seg_id="S5")
        helds = [held("000001.SZ", weight=30.0), held("000002.SZ", weight=30.0)]
        out = self._intents(rep, helds)
        # 主策略 60% > 50%：弱势（MTD 低者）先卖，卖 1 只后剩 30%
        self.assertEqual(len(out["exits"]), 1)
        self.assertEqual(out["exits"][0]["code"], "000001.SZ")
        self.assertIn("S5 减仓", out["exits"][0]["reason"])

    def test_d_positions_excluded_from_s5_main_reduce(self):
        rows = [row("000001.SZ", close=10.0, mtd=6.0)]
        rep = make_report(rows, seg_id="S5")
        helds = [held("000001.SZ", weight=30.0),
                 held("000005.SZ", strategy="d_v1", weight=30.0, target=10.3)]
        out = self._intents(rep, helds)
        # 30+30=60% 但主策略只有 30% ≤ 50%：D 独立策略不触发 S5 减仓
        self.assertEqual(out["exits"], [])
        self.assertEqual(len(out["holds"]), 2)


class TestIntradayProbe(unittest.TestCase):
    """盘中试探（INTRADAY 档）+ 收盘补满/撤回（报告 S3 打法）。"""

    def _intra(self, rep, held_list=None, month_positions=None, rules=None):
        return signals.build_intraday_intents(rep, rules or make_rules(),
                                              held_list or [],
                                              month_positions or [])

    def _b8row(self, code="000001.SZ", close=10.0):
        return row(code, group="B", b_tier="B8", action="执行",
                   mtd=5.0, mtm=9.0, close=close)

    def test_probe_buys_one_third(self):
        rep = make_report([self._b8row()], session="INTRADAY", seg_id="S3")
        out = self._intra(rep)
        self.assertEqual(len(out["buys"]), 1)
        b = out["buys"][0]
        self.assertEqual((b["code"], b["strategy"], b["probe"]), ("000001.SZ", "b8", True))
        self.assertEqual(b["weight"], 3.33)   # 10 × 1/3
        self.assertEqual(b["stop"], 9.5)      # close × 0.95

    def test_probe_dedup_held_and_opened(self):
        rep = make_report([self._b8row()], session="INTRADAY", seg_id="S3")
        out = self._intra(rep, held_list=[held("000001.SZ", weight=3.33,
                                               is_probe=1, entry_date="2026-11-20")])
        self.assertEqual(out["buys"], [])
        self.assertTrue(any("已持仓" in x["reason"] for x in out["excluded"]))
        out2 = self._intra(rep, month_positions=[{"strategy": "b8",
                                                  "code": "000001.SZ"}])
        self.assertEqual(out2["buys"], [])
        self.assertTrue(any("本月已开过仓" in x["reason"] for x in out2["excluded"]))

    def test_probe_blocked_in_no_new_buy_segments(self):
        for seg in ("S4", "S5", "S6"):
            with self.subTest(segment=seg):
                rep = make_report([self._b8row()], session="INTRADAY", seg_id=seg)
                out = self._intra(rep)
                self.assertEqual(out["buys"], [])
                self.assertTrue(any("禁止" in w for w in out["warnings"]))

    def test_eod_top_up_confirmed_probe(self):
        # 今日试探仓（3.33%）+ 收盘仍 B8 → 目标补满 10%
        rep = make_report([self._b8row()], asof="2026-11-24", seg_id="S3")
        rules = make_rules()
        rules["strategies"]["b8"]["probe_ratio"] = 0.3333
        out = signals.build_intents(
            rep, rules,
            [held("000001.SZ", weight=3.33, is_probe=1,
                  entry_date="2026-11-24")], [])
        self.assertEqual(out["exits"], [])
        h = out["holds"][0]
        self.assertEqual(h["weight_target"], 10.0)
        plan = signals.build_plan(rep, rules,
                                  [held("000001.SZ", weight=3.33, is_probe=1,
                                        entry_date="2026-11-24")],
                                  [], "2026-11-25", "ZH", "r.json")
        self.assertEqual(plan["target_weights"]["000001.SZ"], 10.0)

    def test_eod_withdraw_unconfirmed_probe(self):
        # 今日试探仓 + 收盘不再可执行（分组变了）→ 撤回
        rep = make_report([row("000001.SZ", group="C", close=10.0)],
                          asof="2026-11-24", seg_id="S3")
        out = signals.build_intents(
            rep, make_rules(),
            [held("000001.SZ", weight=3.33, is_probe=1,
                  entry_date="2026-11-24")], [])
        self.assertEqual(len(out["exits"]), 1)
        self.assertIn("试探未确认", out["exits"][0]["reason"])
        self.assertEqual(out["holds"], [])


class TestBuildPlan(unittest.TestCase):
    def test_plan_id_and_target_weights(self):
        rep = make_report([row("000001.SZ", group="B", b_tier="B8",
                              action="执行", mtd=5.0, mtm=9.0)], seg_id="S3")
        plan = signals.build_plan(rep, make_rules(), [], [],
                                  "2026-11-23", "ZH123", "r.json")
        self.assertEqual(plan["plan_id"],
                         f"ZH123|2026-11-23|{signals.semantic_hash(rep)[:16]}")
        self.assertEqual(plan["target_weights"], {"000001.SZ": 10.0})
        self.assertEqual(plan["status"], "planned")
        self.assertEqual(plan["segment_id"], "S3")


if __name__ == "__main__":
    unittest.main(verbosity=2)
