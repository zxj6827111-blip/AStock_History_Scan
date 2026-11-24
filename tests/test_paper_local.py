"""本地撮合账本单测：涨跌幅/费用/T+1/涨跌停/停牌/整手/补仓（不触网不碰雪球）。

跑法（tengod 根目录）：python3 -m unittest -v tests.test_paper_local
"""
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sys
APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from paper_trade import local_match, store  # noqa: E402

BJT = timezone(timedelta(hours=8))


def make_slice(path: Path, bars: list[tuple]) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE bars (code TEXT, date TEXT, open REAL, high REAL, "
                "low REAL, close REAL, volume REAL)")
    con.executemany("INSERT INTO bars VALUES (?,?,?,?,?,?,?)", bars)
    con.commit()
    con.close()


class LocalTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        t = Path(self._tmp.name)
        self.private = t / "private"
        self.public = t / "public"
        self.slice = t / "slice.sqlite"
        self._env = {
            "BAZI_PAPER_PRIVATE_DIR": str(self.private),
            # 公开快照也必须隔离：否则测试的 refresh_public_snapshot 会写进生产页面数据
            "BAZI_PAPER_PUBLIC_DIR": str(self.public),
            "BAZI_PAPER_SLICE_DB": str(self.slice),
        }
        self._saved = {k: os.environ.get(k) for k in self._env}
        os.environ.update(self._env)
        # 2026-11-23 收 10.0；11-24 开 10.0 / 收 10.5（涨停 11.0 / 跌停 9.0）
        make_slice(self.slice, [
            ("000001", "2026-11-23", 9.8, 10.1, 9.7, 10.0, 1e6),
            ("000001", "2026-11-24", 10.0, 10.6, 9.9, 10.5, 1e6),
            ("000003", "2026-11-23", 9.8, 10.1, 9.7, 10.0, 1e6),
            ("000003", "2026-11-24", 11.0, 11.0, 11.0, 11.0, 5e5),   # 一字涨停
            ("000004", "2026-11-23", 9.8, 10.1, 9.7, 10.0, 1e6),
            ("000004", "2026-11-24", 10.0, 10.0, 10.0, 10.0, 0.0),    # 停牌
            ("300001", "2026-11-23", 19.8, 20.1, 19.7, 20.0, 1e6),
            ("300001", "2026-11-24", 20.0, 24.0, 19.9, 23.0, 1e6),     # 创业板 20%
        ])

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def db(self):
        con = store.connect()
        con.row_factory = sqlite3.Row
        return con


class TestRules(unittest.TestCase):
    def test_limit_pct_by_board_and_st(self):
        self.assertEqual(local_match._limit_pct("300001.SZ", "普通"), 20.0)
        self.assertEqual(local_match._limit_pct("688001.SH", "普通"), 20.0)
        self.assertEqual(local_match._limit_pct("600000.SH", "ST海龙"), 5.0)
        self.assertEqual(local_match._limit_pct("000001.SZ", "平安银行"), 10.0)

    def test_fees(self):
        comm, stamp, transfer = local_match.calc_fees("buy", 10000.0, {})
        self.assertEqual((comm, stamp), (5.0, 0.0))       # 佣金最低 5 元
        self.assertEqual(transfer, 0.1)
        comm2, stamp2, _ = local_match.calc_fees("sell", 10000.0, {})
        self.assertEqual(stamp2, 5.0)                     # 印花税 0.05%
        comm3, _, _ = local_match.calc_fees("buy", 1_000_000.0, {})
        self.assertEqual(comm3, 250.0)                    # 万 2.5


class TestExecute(LocalTestBase):
    def setUp(self):
        super().setUp()
        con = self.db()
        with con:
            store.create_account(con, 1_000_000.0)
        con.close()

    def _plan(self, buys=None, exits=None, holds=None, kind="intraday_probe"):
        return {"plan_id": "p1", "kind": kind, "month_id": "2026-M10",
                "intents": {"buys": buys or [], "exits": exits or [],
                            "holds": holds or []}}

    def _settings(self):
        return {"fees": {"commission_rate": 0.00025, "commission_min": 5.0,
                         "stamp_rate": 0.0005, "transfer_rate": 0.00001}}

    def _prices(self):
        return local_match.prepare_prices(self.db(), ["000001.SZ"])

    def test_buy_at_probe_close(self):
        con = self.db()
        try:
            prices = local_match.prepare_prices(con, ["000001.SZ"])
            res = local_match.execute_plan(
                con, self._plan(buys=[{"code": "000001.SZ", "name": "平安银行",
                                       "weight": 3.33, "reason": "试探"}]),
                "2026-11-24", prices, self._settings(), slot="1000")
            self.assertEqual(res["status"], "confirmed")
            self.assertEqual(len(res["fills"]), 1)
            f = res["fills"][0]
            self.assertEqual(f["price"], 10.5)                # 试探按收盘价成交
            self.assertEqual(f["qty"], 3100)                  # floor(33300/(10.5*100))=31 手
            h = store.holding_get(con, "000001.SZ")
            self.assertEqual(h["qty"], 3100)
            acct = store.get_account(con)
            self.assertLess(acct["cash"], 1_000_000.0)
        finally:
            con.close()

    def test_t_plus_1_no_sell_same_day(self):
        con = self.db()
        try:
            prices = local_match.prepare_prices(con, ["000001.SZ"])
            local_match.execute_plan(
                con, self._plan(buys=[{"code": "000001.SZ", "weight": 3.33}]),
                "2026-11-24", prices, self._settings())
            res2 = local_match.execute_plan(
                con, self._plan(exits=[{"code": "000001.SZ", "reason": "试卖"}]),
                "2026-11-24", prices, self._settings())
            self.assertEqual(res2["fills"], [])
            self.assertTrue(any("T+1" in r["reason"] for r in res2["rejected"]))
        finally:
            con.close()

    def test_limit_up_buy_rejected(self):
        con = self.db()
        try:
            prices = local_match.prepare_prices(con, ["000003.SZ"])
            res = local_match.execute_plan(
                con, self._plan(buys=[{"code": "000003.SZ", "weight": 3.33}]),
                "2026-11-24", prices, self._settings())
            self.assertEqual(res["fills"], [])
            self.assertTrue(any("涨停" in r["reason"] for r in res["rejected"]))
        finally:
            con.close()

    def test_suspended_rejected(self):
        con = self.db()
        try:
            prices = local_match.prepare_prices(con, ["000004.SZ"])
            res = local_match.execute_plan(
                con, self._plan(buys=[{"code": "000004.SZ", "weight": 3.33}]),
                "2026-11-24", prices, self._settings())
            self.assertEqual(res["fills"], [])
            self.assertTrue(any("停牌" in r["reason"] for r in res["rejected"]))
        finally:
            con.close()

    def test_eod_exit_at_next_open(self):
        # 昨日买入 3100 股；今日 EOD 计划卖出 → 按今日开盘价 10.0 成交
        con = self.db()
        try:
            prices = local_match.prepare_prices(con, ["000001.SZ"])
            local_match.execute_plan(
                con, self._plan(buys=[{"code": "000001.SZ", "weight": 3.33}]),
                "2026-11-24", prices, self._settings())
            # 模拟次日：需要一条 11-25 的 bar
            con2 = sqlite3.connect(self.slice)
            con2.execute("INSERT INTO bars VALUES ('000001','2026-11-25',10.2,10.3,10.1,10.2,1e6)")
            con2.commit()
            con2.close()
            prices2 = local_match.prepare_prices(con, ["000001.SZ"])
            snap2 = local_match.snapshot(con, prices2, "2026-11-25")
            self.assertEqual(snap2["positions"][0]["today_buy_qty"], 0)
            self.assertEqual(snap2["positions"][0]["sellable_qty"], 3100)
            res = local_match.execute_plan(
                con, self._plan(exits=[{"code": "000001.SZ", "reason": "退出"}],
                                kind="eod"),
                "2026-11-25", prices2, self._settings())
            self.assertEqual(len(res["fills"]), 1)
            self.assertEqual(res["fills"][0]["price"], 10.2)   # EOD 按开盘价
            self.assertIsNone(store.holding_get(con, "000001.SZ"))
        finally:
            con.close()

    def test_snapshot_pnl_fields(self):
        con = self.db()
        try:
            prices = local_match.prepare_prices(con, ["000001.SZ"])
            local_match.execute_plan(
                con, self._plan(buys=[{"code": "000001.SZ", "name": "平安银行",
                                       "weight": 30.0}]),
                "2026-11-24", prices, self._settings(), slot="1005")
            # 今日买入：今日盈亏从买入价起算，不把开盘前的跳空算成自己的收益
            with con:
                store.position_meta_open(con, "000001.SZ", "b8", "2026-M10",
                                         "2026-11-24", entry_ref=10.5, stop=9.98)
            snap = local_match.snapshot(con, prices, "2026-11-24")
            p = snap["positions"][0]
            self.assertEqual(p["price"], 10.5)          # 最新切片收盘价
            self.assertEqual(p["prev_close"], 10.0)     # 前一交易日收盘
            self.assertEqual(p["day_pct"], 0.0)         # 10.5 vs 买入价 10.5
            self.assertEqual(p["day_pnl"], 0.0)
            self.assertEqual(p["today_buy_qty"], p["qty"])
            self.assertEqual(p["sellable_qty"], 0)       # 当日买入，T+1 全量不可卖
            self.assertEqual(p["price_date"], "2026-11-24")
            self.assertIsNotNone(p["pnl"])
            self.assertIsNotNone(p["pnl_pct"])
            self.assertEqual(snap["day_pnl"], 0.0)
            self.assertEqual(snap["unrealized_pnl"], round(p["pnl"], 2))
            self.assertEqual(snap["initial"], 1_000_000.0)
            # 昨日买入：今日盈亏从昨收起算（10.0 → 10.5 = +5%）
            with con:
                con.execute("UPDATE position_meta SET entry_date='2026-11-23' "
                            "WHERE code='000001.SZ'")
            p2 = local_match.snapshot(con, prices, "2026-11-24")["positions"][0]
            self.assertEqual(p2["day_pct"], 5.0)
            self.assertAlmostEqual(p2["day_pnl"], p2["qty"] * 0.5, places=2)
        finally:
            con.close()

    def test_nav_first_day_uses_initial_cash(self):
        con = self.db()
        try:
            with con:
                nav = store.upsert_nav(con, "2026-11-24", 990_000.0, 500_000.0,
                                       490_000.0, 0.99,
                                       "2026-11-24T15:12:00+08:00")
            self.assertEqual(nav["day_pnl"], -10_000.0)   # 首日：vs 初始 100 万
            self.assertEqual(nav["cum_pnl"], -10_000.0)
            with con:
                nav2 = store.upsert_nav(con, "2026-11-25", 1_005_000.0, 500_000.0,
                                        505_000.0, 1.005,
                                        "2026-11-25T15:12:00+08:00")
            self.assertEqual(nav2["day_pnl"], 15_000.0)   # vs 11-24 的 99 万
            self.assertEqual(nav2["cum_pnl"], 5_000.0)    # vs 初始 100 万
        finally:
            con.close()

    def test_legacy_mixed_plan_executes_exit_but_not_d_v2_buy(self):
        from paper_trade import engine
        con = self.db()
        old_backend = os.environ.get("BAZI_PAPER_BACKEND")
        os.environ["BAZI_PAPER_BACKEND"] = "local"
        try:
            prices = local_match.prepare_prices(con, ["000001.SZ"])
            local_match.execute_plan(
                con, self._plan(buys=[{"code": "000001.SZ", "name": "平安银行",
                                       "weight": 10.0}]),
                "2026-11-24", prices, self._settings())
            with con:
                store.position_meta_open(con, "000001.SZ", "d_v2", "2026-M10",
                                         "2026-11-24", entry_ref=10.5,
                                         stop=9.5, target=11.0, stop_pct=-5.0,
                                         target_pct=3.0)
                plan = {
                    "plan_id": "legacy-local-s4", "kind": "eod",
                    "month_id": "2026-M10", "signal_date": "2026-11-24",
                    "execute_trade_date": "2026-11-25", "source_report": "legacy.json",
                    "source_built_at": "2026-11-24 15:05:00", "config_hash": "old",
                    "signal_hash": "old", "regime": "Bear", "segment_id": "S4",
                    "total_weight_cap": 50.0,
                    "buys": [{"code": "000003.SZ", "name": "错误新买",
                              "strategy": "d_v2", "weight": 2.5}],
                    "exits": [{"code": "000001.SZ", "strategy": "d_v2",
                               "month_id": "2026-M10", "reason": "既有持仓止损"}],
                    "holds": [], "excluded": [], "warnings": [],
                    "target_weights": {"000003.SZ": 2.5}, "status": "planned"}
                store.upsert_plan(con, plan)
            con2 = sqlite3.connect(self.slice)
            con2.execute("INSERT INTO bars VALUES ('000001','2026-11-25',10.2,10.3,10.1,10.2,1e6)")
            con2.commit()
            con2.close()
            res = engine._execute_live(con, store.get_plan(con, plan["plan_id"]),
                                       "2026-11-25", engine.config.settings())
            self.assertEqual(res["status"], "needs_review")
            self.assertIsNone(store.holding_get(con, "000001.SZ"))  # 合法退出已执行
            sides = [tuple(r) for r in con.execute(
                "SELECT code, side FROM trades ORDER BY id").fetchall()]
            self.assertIn(("000001.SZ", "sell"), sides)
            self.assertFalse(any(code == "000003.SZ" for code, _ in sides))
            saved = store.get_plan(con, plan["plan_id"])
            self.assertEqual(saved["status"], "needs_review")
            self.assertIn("policy_filter", saved["result"])
        finally:
            con.close()
            if old_backend is None:
                os.environ.pop("BAZI_PAPER_BACKEND", None)
            else:
                os.environ["BAZI_PAPER_BACKEND"] = old_backend

    def test_top_up_to_target_weight(self):
        con = self.db()
        try:
            prices = local_match.prepare_prices(con, ["000001.SZ"])
            local_match.execute_plan(
                con, self._plan(buys=[{"code": "000001.SZ", "weight": 3.33}]),
                "2026-11-24", prices, self._settings())
            res = local_match.execute_plan(
                con, self._plan(holds=[{"code": "000001.SZ", "weight_target": 10.0,
                                        "strategy": "b8", "month_id": "2026-M10"}],
                                kind="eod"),
                "2026-11-24", prices, self._settings())
            self.assertEqual(res["status"], "confirmed")
            h = store.holding_get(con, "000001.SZ")
            self.assertGreater(h["qty"], 3100)   # 已加仓
            self.assertIn("补仓", (res["fills"][0]["reason"] if res["fills"] else ""))
        finally:
            con.close()


class TestFillAnchoredLevels(LocalTestBase):
    """止损/目标必须按实际成交价（补仓按新均价）重算，而不是信号日收盘价。"""

    def test_stop_and_target_use_fill_price(self):
        from paper_trade import engine
        con = self.db()
        try:
            with con:
                store.create_account(con, 1_000_000.0)
                engine._apply_local_positions(con, {
                    "month_id": "2026-M10", "kind": "eod",
                    "intents": {"buys": [{
                        "code": "002281.SZ", "name": "光迅科技",
                        "strategy": "d_v2", "weight": 2.5,
                        "stop": 78.47, "target": 85.08,      # 信号日口径（应被覆盖）
                        "stop_pct": -5.0, "target_pct": 3.0}],
                        "exits": [], "holds": []},
                }, {"fills": [{"code": "002281.SZ", "name": "光迅科技",
                               "side": "buy", "qty": 200, "price": 89.0}]},
                    "2026-11-24")
            m = con.execute("select * from position_meta where code='002281.SZ'").fetchone()
            self.assertEqual(m["entry_ref"], 89.0)
            self.assertEqual(m["stop"], 84.55)      # 89 × 0.95，不是 78.47
            self.assertEqual(m["target"], 91.67)    # 89 × 1.03，不是 85.08
            self.assertEqual((m["stop_pct"], m["target_pct"]), (-5.0, 3.0))
        finally:
            con.close()

    def test_probe_top_up_recomputes_from_avg_cost(self):
        from paper_trade import engine
        con = self.db()
        try:
            with con:
                store.create_account(con, 1_000_000.0)
                store.holding_set(con, "000001.SZ", "平安银行", 3300, 10.35)
                store.position_meta_open(con, "000001.SZ", "b8", "2026-M10",
                                         "2026-11-23", entry_ref=10.5,
                                         stop=9.98, is_probe=1, stop_pct=-5.0)
                engine._apply_local_positions(con, {
                    "month_id": "2026-M10", "kind": "eod",
                    "intents": {"buys": [], "exits": [], "holds": [{
                        "code": "000001.SZ", "strategy": "b8",
                        "month_id": "2026-M10", "weight_target": 10.0,
                        "stop_pct": -5.0, "target_pct": None}]},
                }, {"fills": [{"code": "000001.SZ", "side": "buy",
                               "qty": 200, "price": 10.2}]}, "2026-11-24")
            m = con.execute("select * from position_meta where code='000001.SZ'").fetchone()
            self.assertEqual(m["is_probe"], 0)
            self.assertEqual(m["stop"], round(10.35 * 0.95, 2))   # 按新均价
        finally:
            con.close()


class TestEngineLocal(LocalTestBase):
    """引擎端到端：本地后端 probe→收盘补满（BAZI_PAPER_BACKEND=local）。"""

    def setUp(self):
        super().setUp()
        os.environ["BAZI_PAPER_MODE"] = "live"
        os.environ["BAZI_PAPER_BACKEND"] = "local"
        # 12-07 的 bar（EOD 计划执行日）
        con = sqlite3.connect(self.slice)
        con.execute("INSERT INTO bars VALUES ('000001','2026-12-07',10.2,10.4,10.1,10.4,1e6)")
        con.commit()
        con.close()
        self._rep_dir = Path(self._tmp.name) / "reports"
        self._rep_dir.mkdir()
        sess = Path(self._tmp.name) / "sessions.csv"
        sess.write_text("date,month_id,mn,mkt\n"
                        "2026-11-23,2026-M10,1,1\n2026-11-24,2026-M10,2,2\n"
                        "2026-12-07,2026-M10,3,3\n", encoding="utf-8")
        os.environ["BAZI_PAPER_MARKET_SESSIONS"] = str(sess)

    def tearDown(self):
        os.environ.pop("BAZI_PAPER_BACKEND", None)
        os.environ.pop("BAZI_PAPER_MARKET_SESSIONS", None)
        super().tearDown()

    def _report(self, session="INTRADAY", asof="2026-11-24"):
        return {
            "schema": "survivor_report_v1", "month_id": "2026-M10",
            "asof_date": asof, "session": session,
            "generated_at_bjt": asof + " 10:05:00",
            "current_segment": {"id": "S3", "start": "2026-11-18",
                                "end": "2026-11-20", "action": "a"},
            "index_filter": {"regime": "Bear", "text": "x"},
            "rows": [{"code": "000001.SZ", "name": "平安银行", "group": "B",
                      "b_tier": "B8", "b_tier_action": "执行", "no_chase": False,
                      "close": 10.5, "month_open": 9.9, "break_level": 9.9,
                      "mtd_pct": 6.0, "mtm_pct": 9.0, "dif": 0.2, "dea": 0.1,
                      "d_v1_state": "none", "d_v2_signal": False}],
        }

    def _write(self, rep) -> str:
        name = (f"survivor_{rep['asof_date']}_"
                f"{'1030' if rep['session']=='INTRADAY' else '1500'}_{rep['session']}.json")
        p = self._rep_dir / name
        p.write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")
        return str(p)

    def test_probe_then_top_up_local_fills(self):
        from paper_trade import engine
        # 初始化本地账户（不需要雪球凭证/基线）
        res = engine.adopt_baseline()
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["cash"], 1_000_000.0)
        # 盘中 10:06 → 试探按 11-24 收盘价 10.5 成交
        res = engine.finalize_intraday(
            {"json": self._write(self._report())},
            now=datetime(2026, 11, 24, 10, 6, tzinfo=BJT))
        self.assertEqual(res["status"], "confirmed")
        con = self.db()
        try:
            h = store.holding_get(con, "000001.SZ")
            self.assertEqual(h["qty"], 3100)
            self.assertEqual(store.get_account(con)["initial_cash"], 1_000_000.0)
        finally:
            con.close()
        # 当晚 PM 确认 → 次日(12-07) 10:10 按开盘价 10.2 补满到 ~10%
        res2 = engine.finalize_from_report({"json": self._write(
            self._report(session="PM", asof="2026-11-24"))})
        self.assertEqual(res2["status"], "created")
        res3 = engine.execute_due(now=datetime(2026, 12, 7, 10, 10, tzinfo=BJT))
        self.assertEqual(res3["status"], "confirmed")
        con = self.db()
        try:
            h = store.holding_get(con, "000001.SZ")
            self.assertGreater(h["qty"], 9000)   # 约 10% 仓位
            # 快照可用（页面数据源）
            sync = store.latest_sync(con)
            self.assertEqual(sync["ok"], 1)
            self.assertGreater(sync["total"], 0)
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
