"""模拟盘公开快照的信息完整性与费用对账测试（不触网、不写生产目录）。

跑法（tengod 根目录）：python3 -m unittest -v tests.test_paper_report
"""
import os
import tempfile
import unittest
from pathlib import Path

import sys
APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from paper_trade import config, report, store  # noqa: E402


class TestPublicReport(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._env = {
            "BAZI_PAPER_PRIVATE_DIR": str(root / "private"),
            "BAZI_PAPER_PUBLIC_DIR": str(root / "public"),
            "BAZI_PAPER_BACKEND": "local",
            "BAZI_PAPER_MODE": "live",
        }
        self._saved = {k: os.environ.get(k) for k in self._env}
        os.environ.update(self._env)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def test_snapshot_contains_accounting_plan_and_trade_details(self):
        con = store.connect()
        try:
            with con:
                store.create_account(con, 1_000_000.0)
                store.update_account_cash(con, 989_994.9)
                store.holding_set(con, "000001.SZ", "平安银行", 1000, 10.0)
                store.position_meta_open(
                    con, "000001.SZ", "b8", "2026-M10", "2026-11-24",
                    entry_ref=10.0, stop=9.5, target=None, is_probe=1)
                plan = {
                    "plan_id": "p1", "kind": "eod", "month_id": "2026-M10",
                    "signal_date": "2026-11-23", "execute_trade_date": "2026-11-24",
                    "source_report": "test.json", "source_built_at": "2026-11-23 15:05:00",
                    "config_hash": "cfg", "signal_hash": "sig", "regime": "Bear",
                    "segment_id": "S3", "total_weight_cap": 50.0,
                    "buys": [
                        {"code": "000001.SZ", "name": "平安银行", "strategy": "b8",
                         "weight": 1.0, "entry_ref": 9.8, "reason": "测试买入"},
                        {"code": "000002.SZ", "name": "测试拒单", "strategy": "b8",
                         "weight": 1.0, "entry_ref": 20.0, "reason": "测试拒单"},
                    ],
                    "exits": [], "holds": [],
                    "excluded": [
                        {"code": "000003.SZ", "strategy": "d_v1", "reason": "总仓上限"},
                        {"code": "000045.SZ", "name": "深纺织A", "strategy": "d_v2",
                         "signal_only": True, "blocked_reason": "d_v2_signal_only",
                         "reason": "D_V2 MTM 上拐；全段位仅观察，不自动下单"},
                    ],
                    "warnings": ["测试日历告警"],
                    "target_weights": {"000001.SZ": 1.0},
                }
                store.upsert_plan(con, plan)
                store.trade_insert(
                    con, "2026-11-24", "1005", "000001.SZ", "buy", 1000, 10.0,
                    5.0, 0.0, 0.1, "p1", "测试买入", name="平安银行")
                store.mark_plan(con, "p1", "confirmed", {
                    "status": "confirmed",
                    "fills": [{"code": "000001.SZ", "side": "buy",
                               "qty": 1000, "price": 10.0}],
                    "rejected": [{"code": "000002.SZ", "name": "测试拒单",
                                  "side": "buy", "qty": 400, "price": 20.0,
                                  "reason": "涨停买不进"}],
                    "methodology_review": {
                        "issue": "D_V2观察信号在S4被误执行为自动买入",
                        "resolution": "positions_retained_exit_only"},
                    "repair": {"at": "2026-11-24T10:45:00+08:00",
                               "filled": ["000001.SZ"]},
                })
                store.save_sync(
                    con, kind="slot", market_as_of="2026-11-24 11:00", ok=1,
                    cash=989_994.9, total=1_000_494.9, market_value=10_500.0,
                    positions=[{
                        "code": "000001.SZ", "name": "平安银行", "qty": 1000,
                        "today_buy_qty": 1000, "sellable_qty": 0,
                        "price_date": "2026-11-24", "price": 10.5,
                        "prev_close": 10.0, "day_pct": 0.0, "day_pnl": 0.0,
                        "weight": 1.05, "market_value": 10_500.0,
                        "avg_cost": 10.0, "pnl": 500.0, "pnl_pct": 5.0,
                    }])
        finally:
            con.close()

        out = report.refresh_public_snapshot()
        s = out["summary"]
        self.assertEqual(s["execution_backend"], "local")
        self.assertEqual(s["total_fees"], 5.1)
        self.assertEqual(s["fee_breakdown"],
                         {"commission": 5.0, "tax": 0.0, "transfer_fee": 0.1, "total": 5.1})
        self.assertEqual(s["unrealized_pnl"], 500.0)
        self.assertEqual(s["cum_pnl"], 494.9)
        self.assertEqual(s["realized_pnl"], 0.0)
        self.assertAlmostEqual(
            s["cum_pnl"],
            s["realized_pnl"] + s["unrealized_pnl"] - s["total_fees"], places=2)

        p = out["positions"][0]
        self.assertEqual((p["qty"], p["today_buy_qty"], p["sellable_qty"]),
                         (1000, 1000, 0))
        self.assertEqual(p["price_date"], "2026-11-24")
        self.assertEqual(out["market_as_of"], "2026-11-24 11:00")
        self.assertEqual(out["market_date"], "2026-11-24")

        plan = out["plans_recent"][0]
        self.assertEqual(plan["total_weight_cap"], 50.0)
        self.assertEqual(len(plan["buys"]), 2)
        self.assertEqual(plan["n_excluded"], 2)       # 旧计数口径不变
        self.assertEqual(plan["n_observations"], 1)
        self.assertEqual(plan["observations"][0]["code"], "000045.SZ")
        self.assertEqual(plan["excluded"][0]["reason"], "总仓上限")
        self.assertEqual(plan["methodology_review"]["resolution"],
                         "positions_retained_exit_only")
        self.assertEqual(plan["warnings"], ["测试日历告警"])

        filled = next(t for t in out["trades_recent"] if t["status"] == "filled")
        self.assertEqual(filled["amount"], 10_000.0)
        self.assertEqual((filled["fee"], filled["tax"], filled["transfer_fee"]),
                         (5.0, 0.0, 0.1))
        self.assertEqual(filled["total_fee"], 5.1)
        self.assertEqual(filled["slot"], "1005")
        self.assertEqual(filled["ref_price"], 9.8)
        self.assertEqual(filled["ref_deviation_pct"], 2.04)
        self.assertTrue(filled["is_repair"])
        self.assertEqual(filled["repair_at"], "2026-11-24T10:45:00+08:00")

        rejected = next(t for t in out["trades_recent"] if t["status"] == "rejected")
        self.assertEqual(rejected["reason"], "涨停买不进")
        self.assertIsNone(rejected["total_fee"])
        self.assertEqual(rejected["ref_price"], 20.0)
        self.assertEqual(filled["source"], "auto")

    def test_manual_order_audit_visible_without_changing_auto_counts(self):
        con = store.connect()
        try:
            today = config.now_bjt().strftime("%F")
            with con:
                store.create_account(con, 1_000_000.0)
                store.trade_insert(
                    con, today, "1430", "000001.SZ", "buy", 100, 10.5,
                    5.0, 0.0, 0.1, "manual|mo|1", "人工建仓", name="平安银行",
                    source="manual", manual_order_id="mo|1",
                    override_segment="S4", price_source="slice_30m_close",
                    price_time=f"{today} 14:30")
                store.manual_order_insert(con, {
                    "order_id": "mo|1", "client_token": "manual-token-1",
                    "created_at": f"{today}T14:30:00+08:00",
                    "trade_date": today, "side": "buy", "code": "000001.SZ",
                    "name": "平安银行", "amount_requested": 1100.0,
                    "fill_qty": 100, "price": 10.5, "amount": 1050.0,
                    "fee": 5.0, "tax": 0.0, "transfer_fee": 0.1,
                    "price_source": "manual_price",
                    "price_time": f"{today} 14:30", "stop_pct": -5.0,
                    "target_pct": 3.0, "stop": 9.98, "target": 10.82,
                    "segment_id": "S4", "override": {"ack": True,
                    "note": "人工覆盖S4"}, "status": "filled",
                    "request_hash": "rh", "trade_date": today,
                }, {"ok": True, "status": "filled"})
        finally:
            con.close()
        out = report.refresh_public_snapshot()
        self.assertIn("manual_v1", out["schema_capabilities"])
        ms = out["manual_summary"]
        self.assertEqual((ms["n_orders_today"], ms["n_filled"], ms["n_override"]),
                         (1, 1, 1))
        self.assertEqual(out["manual_orders_recent"][0]["order_id"], "mo|1")
        self.assertEqual(out["manual_orders_recent"][0]["price_source"], "manual_price")
        self.assertTrue(out["manual_orders_recent"][0]["override"]["ack"])
        tr = out["trades_recent"][0]
        self.assertEqual((tr["source"], tr["manual_order_id"], tr["override_segment"]),
                         ("manual", "mo|1", "S4"))
        # 人工单不进入自动计划计数
        self.assertTrue(all("n_buys" in p for p in out["plans_recent"]))


class TestPaperStaticContract(unittest.TestCase):
    def test_page_exposes_new_information(self):
        static = APP_DIR / "static"
        html = (static / "paper.html").read_text(encoding="utf-8")
        js = (static / "paper.js").read_text(encoding="utf-8")
        for text in ("pp-market", "可卖", "数据时间", "成本（未含费）",
                     "止损（盘后）", "费用明细", "计划动作", "D_V2 全段位只观察",
                     "人工干预", "人工建仓", "减仓 / 清仓", "人工订单二次确认",
                     "我确认覆盖当前段位", "买入方式", "pp-buy-price", "pp-buy-qty"):
            self.assertIn(text, html)
        for text in ("本地模拟", "realized_pnl", "total_fees", "sellable_qty",
                     "ref_deviation_pct", "pp-plan-details", "收盘确认补仓",
                     "positionQuoteTime", "D_V2观察（不下单）", "本段禁买 / 禁补仓",
                     "演练（未下单）", "methodology_review", "manual_orders_recent",
                     "preview_token", "client_token", "X-Paper-Request", "人工覆盖",
                     "syncBuyMode", "NO_CURRENT_QUOTE", "price_label"):
            self.assertIn(text, js)
        self.assertIn('paper.js?v=1.5.1', html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
