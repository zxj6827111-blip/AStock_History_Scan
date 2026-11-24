"""模拟盘引擎单测：定稿/执行/对账/调度 tick（FakeXueqiuClient，绝不触网）。

跑法（tengod 根目录）：python3 -m unittest -v tests.test_paper_engine
关键断言（对应实施计划的安全口径）：
- 最多提交一次：网络失败后绝不自动重发，先对账；
- 同执行日新修订顶掉未执行旧计划；已执行后的修订标 conflict 不自动执行；
- 迟到过窗标记 missed 不追单；未纳管持仓保留原权重不清掉。
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sys
APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from paper_trade import engine, store  # noqa: E402
from paper_trade.xueqiu import FakeXueqiuClient  # noqa: E402

BJT = timezone(timedelta(hours=8))
DATES = ["2026-11-12", "2026-11-13", "2026-11-19", "2026-11-20",
         "2026-11-23", "2026-11-24", "2026-12-07"]


def make_report(rows, asof="2026-11-20", seg_id="S3", session="PM"):
    return {
        "schema": "survivor_report_v1", "month_id": "2026-M10",
        "asof_date": asof, "session": session,
        "generated_at_bjt": asof + " 15:05:00",
        "current_segment": {"id": seg_id, "start": "2026-11-18",
                            "end": "2026-11-20", "action": "a"},
        "index_filter": {"regime": "Bear", "text": "x"},
        "rows": rows,
    }


def row_b8(code, close=10.0, mtd=5.0):
    return {"code": code, "name": f"股{code[:6]}", "group": "B",
            "b_tier": "B8", "b_tier_action": "执行", "no_chase": False,
            "close": close, "month_open": close / 1.05,
            "break_level": close / 1.05, "mtd_pct": mtd, "mtm_pct": 9.0,
            "dif": 0.2, "dea": 0.1, "d_v1_state": "none", "d_v2_signal": False,
            "stop_loss": round(close * 0.95, 2), "target_3": None,
            "target_5": None}


class EngineTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        t = Path(self._tmp.name)
        self.private = t / "private"
        self.public = t / "public"
        self.reports = t / "reports"
        self.reports.mkdir(parents=True)
        sess = t / "sessions.csv"
        sess.write_text("date,month_id,mn,mkt\n"
                        + "".join(f"{d},2026-M10,1,1\n" for d in DATES),
                        encoding="utf-8")
        self._env = {
            "BAZI_PAPER_PRIVATE_DIR": str(self.private),
            "BAZI_PAPER_PUBLIC_DIR": str(self.public),
            "BAZI_PAPER_MARKET_SESSIONS": str(sess),
            "BAZI_PAPER_MODE": "dry_run",
        }
        self._saved = {k: os.environ.get(k) for k in self._env}
        os.environ.update(self._env)
        os.environ.pop("BAZI_PAPER_DISABLE", None)
        os.environ.pop("BAZI_PAPER_TRADE_ENABLE", None)
        # 凭证文件存在（走真实校验分支；客户端已换成 Fake，不触网）——
        # 否则 _do_sync 的"无凭证跳过"护栏会拦截测试
        self.private.mkdir(parents=True, exist_ok=True)
        (self.private / "xueqiu.local.json").write_text(json.dumps({
            "portfolio_code": "ZH000000", "cookies": "a=b"}), encoding="utf-8")
        self.fake = FakeXueqiuClient()
        self._orig_make = engine._make_client
        engine._make_client = (lambda initial_assets=None: self.fake)

    def tearDown(self):
        engine._make_client = self._orig_make
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    # ---- 工具 ----
    def write_report(self, rep) -> Path:
        p = self.reports / f"survivor_{rep['asof_date']}_1500_PM.json"
        p.write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")
        return p

    def finalize(self, rep) -> dict:
        p = self.write_report(rep)
        return engine.finalize_from_report({"json": str(p)})

    def db(self):
        return store.connect()


class TestFinalize(EngineTestBase):
    def test_finalize_creates_plan_and_public_snapshot(self):
        res = self.finalize(make_report([row_b8("000001.SZ")]))
        self.assertEqual(res["status"], "created")
        con = self.db()
        try:
            plan = store.due_plan(con, "2026-11-23")   # 周五 → 下周一
            self.assertIsNotNone(plan)
            self.assertEqual(plan["status"], "planned")
            self.assertEqual(plan["target_weights"], {"000001.SZ": 10.0})
        finally:
            con.close()
        self.assertTrue((self.public / "plans" / "2026-11-23.json").is_file())
        self.assertTrue((self.public / "latest.json").is_file())

    def test_revision_supersedes_pending(self):
        self.assertEqual(self.finalize(make_report(
            [row_b8("000001.SZ", close=10.0)]))["status"], "created")
        res2 = self.finalize(make_report(
            [row_b8("000001.SZ", close=11.0)]))["status"]  # 收盘价变 → 新语义哈希
        self.assertEqual(res2, "created")
        con = self.db()
        try:
            rows = [dict(r) for r in con.execute(
                "SELECT plan_id, status FROM plans ORDER BY created_at")]
            self.assertEqual(rows[0]["status"], "superseded")
            self.assertEqual(rows[1]["status"], "planned")
            self.assertEqual(store.due_plan(con, "2026-11-23")["plan_id"],
                             rows[1]["plan_id"])
        finally:
            con.close()

    def test_conflict_after_execution(self):
        self.finalize(make_report([row_b8("000001.SZ")]))
        con = self.db()
        pid = store.due_plan(con, "2026-11-23")["plan_id"]
        with con:
            store.mark_plan(con, pid, "confirmed", {"attempt": 1})
        con.close()
        res = self.finalize(make_report([row_b8("000001.SZ", close=11.0)]))
        self.assertEqual(res["status"], "conflict")
        con = self.db()
        try:
            self.assertIsNone(store.due_plan(con, "2026-11-23"))  # conflict 不自动执行
        finally:
            con.close()

    def test_am_session_not_finalized(self):
        rep = make_report([row_b8("000001.SZ")])
        rep["session"] = "AM"
        rep["asof_date"] = "2026-11-20"
        res = self.finalize(rep)
        # load_report 拒绝非 PM/EOD → 定稿 error，不产生计划
        self.assertEqual(res["status"], "error")


class TestPolicyGuard(EngineTestBase):
    def _legacy_plan(self, seg="S4", strategy="d_v2"):
        buy = {"code": "000045.SZ", "name": "深纺织A", "strategy": strategy,
               "weight": 2.5, "entry_ref": 8.26, "reason": "旧版错误买意"}
        return {
            "plan_id": f"legacy|{seg}|{strategy}", "kind": "eod",
            "month_id": "2026-M10", "signal_date": "2026-11-20",
            "execute_trade_date": "2026-11-23", "source_report": "legacy.json",
            "source_built_at": "2026-11-20 15:05:00", "config_hash": "old",
            "signal_hash": "old", "regime": "Bear", "segment_id": seg,
            "total_weight_cap": 50.0,
            "buys": [buy], "exits": [], "holds": [], "excluded": [], "warnings": [],
            "target_weights": {"000045.SZ": 2.5}, "status": "planned",
        }

    def test_filter_mixed_plan_preserves_exit_and_blocks_all_buys(self):
        plan = self._legacy_plan()
        plan["intents"] = {
            "buys": [{"code": "000001.SZ", "strategy": "b8", "weight": 10.0},
                     {"code": "000045.SZ", "strategy": "d_v2", "weight": 2.5}],
            "exits": [{"code": "002281.SZ", "strategy": "d_v2", "reason": "止损"}],
            "holds": [{"code": "000009.SZ", "strategy": "b8", "weight": 3.33,
                       "weight_target": 10.0}],
            "excluded": [], "warnings": []}
        filtered, audit = engine._policy_filter_plan(plan)
        self.assertEqual(filtered["intents"]["buys"], [])
        self.assertNotIn("weight_target", filtered["intents"]["holds"][0])
        self.assertEqual(len(filtered["intents"]["exits"]), 1)
        self.assertEqual(filtered["target_weights"], {"000009.SZ": 3.33})
        self.assertEqual(len(audit["blocked_buys"]), 2)
        self.assertEqual(len(audit["blocked_topups"]), 1)

    def test_filter_d_v2_even_in_allowed_segment(self):
        plan = self._legacy_plan(seg="S3", strategy="d_v2")
        plan["intents"] = {
            "buys": [{"code": "000001.SZ", "strategy": "b8", "weight": 10.0},
                     {"code": "000045.SZ", "strategy": "d_v2", "weight": 2.5}],
            "exits": [], "holds": [], "excluded": [], "warnings": []}
        filtered, audit = engine._policy_filter_plan(plan)
        self.assertEqual([b["code"] for b in filtered["intents"]["buys"]], ["000001.SZ"])
        self.assertEqual(len(audit["blocked_buys"]), 1)

    def test_live_legacy_plan_without_legal_action_never_submits(self):
        plan = self._legacy_plan()
        con = self.db()
        try:
            with con:
                store.upsert_plan(con, plan)
            res = engine._execute_live(con, store.get_plan(con, plan["plan_id"]),
                                       "2026-11-23", engine.config.settings())
            self.assertEqual(res["status"], "needs_review")
            self.assertEqual(self.fake.calls["adjust"], [])
            saved = store.get_plan(con, plan["plan_id"])
            self.assertEqual(saved["status"], "needs_review")
            self.assertIn("policy_filter", saved["result"])
        finally:
            con.close()

    def test_dry_run_legacy_plan_also_needs_review(self):
        plan = self._legacy_plan(seg="S4", strategy="b8")
        con = self.db()
        try:
            with con:
                store.upsert_plan(con, plan)
        finally:
            con.close()
        res = engine.execute_due(now=datetime(2026, 11, 23, 9, 40, tzinfo=BJT))
        self.assertEqual(res["status"], "needs_review")
        self.assertEqual(self.fake.calls["adjust"], [])
        con = self.db()
        try:
            self.assertEqual(store.active_position_meta(con), [])
        finally:
            con.close()


class TestExecuteDryRun(EngineTestBase):
    def test_dry_run_no_client_calls_no_ledger_change(self):
        self.finalize(make_report([row_b8("000001.SZ")]))
        res = engine.execute_due(now=datetime(2026, 11, 23, 9, 40, tzinfo=BJT))
        self.assertEqual(res["status"], "dry_run")
        self.assertEqual(self.fake.calls["adjust"], [])   # 绝不发请求
        con = self.db()
        try:
            self.assertEqual(store.active_position_meta(con), [])  # 不动台账
        finally:
            con.close()


class TestExecuteLive(EngineTestBase):
    def setUp(self):
        super().setUp()
        os.environ["BAZI_PAPER_MODE"] = "live"
        os.environ["BAZI_PAPER_TRADE_ENABLE"] = "1"

    def _baseline(self):
        self.assertEqual(engine.adopt_baseline()["status"], "ok")

    def test_execute_confirm_once(self):
        self._baseline()
        self.finalize(make_report([row_b8("000001.SZ")]))
        res = engine.execute_due(now=datetime(2026, 11, 23, 9, 40, tzinfo=BJT))
        self.assertEqual(res["status"], "confirmed")
        self.assertEqual(len(self.fake.calls["adjust"]), 1)
        self.assertEqual(self.fake.basket, {"000001.SZ": 10.0})
        con = self.db()
        try:
            metas = store.active_position_meta(con)
            self.assertEqual([(m["code"], m["strategy"]) for m in metas],
                             [("000001.SZ", "b8")])
        finally:
            con.close()
        # 同日重复执行：不再有新提交（最多一次）
        res2 = engine.execute_due(now=datetime(2026, 11, 23, 10, 0, tzinfo=BJT))
        self.assertEqual(res2["status"], "no_due_plan")
        self.assertEqual(len(self.fake.calls["adjust"]), 1)

    def test_blocked_without_arming_or_baseline(self):
        os.environ.pop("BAZI_PAPER_TRADE_ENABLE")
        self.finalize(make_report([row_b8("000001.SZ")]))
        res = engine.execute_due(now=datetime(2026, 11, 23, 9, 40, tzinfo=BJT))
        self.assertEqual(res["status"], "blocked_arming")
        os.environ["BAZI_PAPER_TRADE_ENABLE"] = "1"
        res = engine.execute_due(now=datetime(2026, 11, 23, 9, 41, tzinfo=BJT))
        self.assertEqual(res["status"], "blocked_baseline")
        self.assertEqual(self.fake.calls["adjust"], [])

    def test_network_fail_unknown_then_manual_reconcile(self):
        self._baseline()
        self.finalize(make_report([row_b8("000001.SZ")]))
        self.fake.fail_adjust = True
        res = engine.execute_due(now=datetime(2026, 11, 23, 9, 40, tzinfo=BJT))
        self.assertEqual(res["status"], "unknown")
        self.assertEqual(len(self.fake.calls["adjust"]), 1)
        # 绝不自动重发：同日再执行没有新提交
        res2 = engine.execute_due(now=datetime(2026, 11, 23, 9, 50, tzinfo=BJT))
        self.assertEqual(res2["status"], "no_due_plan")
        self.assertEqual(len(self.fake.calls["adjust"]), 1)
        # 人工对账发现请求其实已落地（权重已生效）→ confirmed + 台账落档
        con = self.db()
        try:
            pid = store.plans_recent(con, 1)[0]["plan_id"]   # unknown 后 due_plan 为空
        finally:
            con.close()
        self.fake.fail_adjust = False
        self.fake.basket = {"000001.SZ": 10.0}
        res3 = engine.reconcile_plan(pid)
        self.assertEqual(res3["status"], "confirmed")
        con = self.db()
        try:
            self.assertEqual(len(store.active_position_meta(con)), 1)
        finally:
            con.close()
        self.assertEqual(len(self.fake.calls["adjust"]), 1)  # 对账只读不重发

    def test_unmanaged_preserved_in_basket(self):
        self.fake.basket = {"000099.SZ": 3.0}
        self._baseline()
        # 用户在雪球手动加了 000088.SZ（未采纳基线）→ 未纳管
        self.fake.basket["000088.SZ"] = 4.0
        self.finalize(make_report([row_b8("000001.SZ")]))
        res = engine.execute_due(now=datetime(2026, 11, 23, 9, 40, tzinfo=BJT))
        self.assertEqual(res["status"], "confirmed")
        sent = self.fake.calls["adjust"][0]
        self.assertEqual(sent["000099.SZ"], 3.0)   # adopted 持仓（holds 带入）
        self.assertEqual(sent["000088.SZ"], 4.0)   # 未纳管：原权重保留不清掉
        self.assertEqual(sent["000001.SZ"], 10.0)  # 新买
        con = self.db()
        try:
            sync = store.latest_sync(con)
            self.assertIn("未纳管", sync["drift"] or "")
        finally:
            con.close()


class TestIntradayProbe(EngineTestBase):
    """盘中试探：INTRADAY 档在交易时段 → 1/3 试探；收盘 PM 补满。"""

    def _write_intraday(self, rep) -> Path:
        p = self.reports / f"survivor_{rep['asof_date']}_1030_INTRADAY.json"
        p.write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")
        return p

    def test_dry_run_probe(self):
        rep = make_report([row_b8("000001.SZ")], asof="2026-11-24",
                          seg_id="S3", session="INTRADAY")
        p = self._write_intraday(rep)
        res = engine.finalize_intraday({"json": str(p)},
                                       now=datetime(2026, 11, 24, 10, 6, tzinfo=BJT))
        self.assertEqual(res["status"], "dry_run")
        self.assertEqual(self.fake.calls["adjust"], [])   # 绝不发请求
        con = self.db()
        try:
            plan = store.get_plan(con, res["plan_id"])
            self.assertEqual(plan["kind"], "intraday_probe")
            self.assertEqual(plan["intents"]["buys"][0]["weight"], 3.33)
            self.assertEqual(store.active_position_meta(con), [])  # dry-run 不动台账
        finally:
            con.close()

    def test_outside_market_hours(self):
        rep = make_report([row_b8("000001.SZ")], asof="2026-11-24",
                          seg_id="S3", session="INTRADAY")
        p = self._write_intraday(rep)
        res = engine.finalize_intraday({"json": str(p)},
                                       now=datetime(2026, 11, 24, 12, 10, tzinfo=BJT))
        self.assertEqual(res["status"], "outside_market_hours")
        con = self.db()
        try:
            self.assertEqual(store.plans_recent(con), [])
        finally:
            con.close()

    def test_stale_date_skipped(self):
        rep = make_report([row_b8("000001.SZ")], asof="2026-11-23",
                          seg_id="S3", session="INTRADAY")
        p = self._write_intraday(rep)
        res = engine.finalize_intraday({"json": str(p)},
                                       now=datetime(2026, 11, 24, 10, 6, tzinfo=BJT))
        self.assertEqual(res["status"], "skip_stale")

    def test_live_probe_then_eod_top_up(self):
        os.environ["BAZI_PAPER_MODE"] = "live"
        os.environ["BAZI_PAPER_TRADE_ENABLE"] = "1"
        # 基线：组合里已有 600000.SH 5%
        self.fake.basket = {"600000.SH": 5.0}
        self.assertEqual(engine.adopt_baseline()["status"], "ok")
        # 盘中 10:06 出 B8 试探
        rep = make_report([row_b8("000001.SZ")], asof="2026-11-24",
                          seg_id="S3", session="INTRADAY")
        p = self._write_intraday(rep)
        res = engine.finalize_intraday({"json": str(p)},
                                       now=datetime(2026, 11, 24, 10, 6, tzinfo=BJT))
        self.assertEqual(res["status"], "confirmed")
        sent = self.fake.calls["adjust"][0]
        self.assertEqual(sent["600000.SH"], 5.0)    # 已有持仓权重保留
        self.assertEqual(sent["000001.SZ"], 3.33)   # 1/3 试探
        con = self.db()
        try:
            metas = {m["code"]: m for m in store.active_position_meta(con)}
            self.assertEqual(int(metas["000001.SZ"]["is_probe"]), 1)
        finally:
            con.close()
        # 当晚 PM 报告仍确认 B8 → 次日计划补满 10%
        pm = make_report([row_b8("000001.SZ")], asof="2026-11-24", seg_id="S3")
        res2 = engine.finalize_from_report({"json": str(self.write_report(pm))})
        self.assertEqual(res2["status"], "created")
        con = self.db()
        try:
            plan = store.due_plan(con, "2026-12-07")   # 测试日历的下一个交易日
            self.assertEqual(plan["target_weights"]["000001.SZ"], 10.0)
        finally:
            con.close()
        # 执行补仓 → 正式持仓（is_probe 清零）
        res3 = engine.execute_due(now=datetime(2026, 12, 7, 9, 40, tzinfo=BJT))
        self.assertEqual(res3["status"], "confirmed")
        con = self.db()
        try:
            metas = {m["code"]: m for m in store.active_position_meta(con)}
            self.assertEqual(int(metas["000001.SZ"]["is_probe"]), 0)
        finally:
            con.close()


class TestTick(EngineTestBase):
    def test_tick_executes_once_then_eod(self):
        self.finalize(make_report([row_b8("000001.SZ")]))
        # 09:30 未到执行窗：out_of_window（非终态）；但交易时段回读已经开跑
        r = engine.tick(now=datetime(2026, 11, 23, 9, 30, tzinfo=BJT))
        self.assertEqual(r.get("execute"), "out_of_window")
        self.assertIn(r.get("sync"), ("ok", "fail"))
        self.assertTrue((self.public / "latest.json").is_file())
        # 09:36 进窗：执行 dry-run
        r = engine.tick(now=datetime(2026, 11, 23, 9, 36, tzinfo=BJT))
        self.assertEqual(r.get("execute"), "dry_run")
        # 同分钟再 tick：不重复
        r = engine.tick(now=datetime(2026, 11, 23, 9, 36, 30, tzinfo=BJT))
        self.assertEqual(r, {"status": "idle"})
        # 10:05：同步间隔（真实 30 分钟）未到，不重复回读
        r = engine.tick(now=datetime(2026, 11, 23, 10, 5, tzinfo=BJT))
        self.assertNotIn("sync", r)
        # 收盘后：NAV 落档
        r = engine.tick(now=datetime(2026, 11, 23, 15, 13, tzinfo=BJT))
        self.assertEqual(r.get("eod"), "ok")
        self.assertTrue((self.public / "nav.jsonl").is_file())
        line = json.loads((self.public / "nav.jsonl").read_text(
            encoding="utf-8").strip().splitlines()[0])
        self.assertEqual(line["trade_date"], "2026-11-23")

    def test_missed_after_catchup_window(self):
        self.finalize(make_report([row_b8("000001.SZ")]))
        r = engine.tick(now=datetime(2026, 11, 23, 10, 30, tzinfo=BJT))
        self.assertEqual(r.get("execute"), "missed")
        con = self.db()
        try:
            plan = store.due_plan(con, "2026-11-23")
            self.assertIsNone(plan)   # missed 不再被取
            rows = [dict(x) for x in con.execute(
                "SELECT status FROM plans")]
            self.assertEqual(rows[0]["status"], "missed")
        finally:
            con.close()

    def test_not_trade_day_no_execute(self):
        self.finalize(make_report([row_b8("000001.SZ")], asof="2026-11-23"))
        # 11-23 的计划执行日是 11-24；11-25 不在日历 → 不执行
        r = engine.tick(now=datetime(2026, 11, 25, 9, 40, tzinfo=BJT))
        self.assertNotIn("execute", r)


if __name__ == "__main__":
    unittest.main(verbosity=2)
