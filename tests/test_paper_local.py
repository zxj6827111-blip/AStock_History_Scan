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
        self.slice = t / "slice.sqlite"
        self._env = {
            "BAZI_PAPER_PRIVATE_DIR": str(self.private),
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
            res = local_match.execute_plan(
                con, self._plan(exits=[{"code": "000001.SZ", "reason": "退出"}],
                                kind="eod"),
                "2026-11-25", prices2, self._settings())
            self.assertEqual(len(res["fills"]), 1)
            self.assertEqual(res["fills"][0]["price"], 10.2)   # EOD 按开盘价
            self.assertIsNone(store.holding_get(con, "000001.SZ"))
        finally:
            con.close()

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
