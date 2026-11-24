"""模拟盘人工建仓/清仓：预览、确认、幂等、T+1与S4覆盖（隔离目录）。"""
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

import sys
APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from paper_trade import config, manual, store  # noqa: E402

TODAY = config.now_bjt().strftime("%F")
PREV = (config.now_bjt() - timedelta(days=1)).strftime("%F")


def make_slice(path: Path, close=10.5, volume=1e6, replace=False):
    con = sqlite3.connect(path)
    if not replace:
        con.execute("CREATE TABLE bars (code TEXT, date TEXT, open REAL, high REAL, "
                    "low REAL, close REAL, volume REAL)")
        con.executemany("INSERT INTO bars VALUES (?,?,?,?,?,?,?)", [
            ("000001", PREV, 9.8, 10.1, 9.7, 10.0, 1e6),
            ("000001", TODAY, 10.0, 10.8, 9.9, close, volume),
        ])
    else:
        con.execute("UPDATE bars SET close=? WHERE code='000001' AND date=?",
                    (close, TODAY))
    con.commit()
    con.close()


def report_fixture():
    return {
        "schema": "survivor_report_v1", "month_id": "2026-M10",
        "asof_date": TODAY, "session": "INTRADAY",
        "current_segment": {"id": "S4", "start": TODAY, "end": TODAY,
                            "action": "只持有/减仓，不新买"},
        "index_filter": {"regime": "Bear"},
        "rows": [{"code": "000001.SZ", "name": "平安银行", "group": "D",
                  "d_v2_signal": True, "close": 10.5}],
        "_source_file": "fixture.json",
    }


class ManualTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.slice = root / "slice.sqlite"
        make_slice(self.slice)
        self.private = root / "private"
        self.public = root / "public"
        self.private.mkdir()
        (self.private / "settings.local.json").write_text(json.dumps({
            "mode": "live", "execution_backend": "local", "manual_enabled": True,
        }), encoding="utf-8")
        self._env = {
            "BAZI_PAPER_PRIVATE_DIR": str(self.private),
            "BAZI_PAPER_PUBLIC_DIR": str(self.public),
            "BAZI_PAPER_SLICE_DB": str(self.slice),
            "BAZI_PAPER_MODE": "live",
            "BAZI_PAPER_BACKEND": "local",
        }
        self._saved = {k: os.environ.get(k) for k in self._env}
        os.environ.update(self._env)
        self.rep_patch = mock.patch("paper_trade.manual.latest_report",
                                    side_effect=report_fixture)
        self.rep_patch.start()
        con = store.connect()
        try:
            with con:
                store.create_account(con, 1_000_000.0)
                store.save_sync(con, kind="test", ok=1, cash=1_000_000.0,
                                total=1_000_000.0, market_value=0.0,
                                positions=[], market_as_of=f"{TODAY} 15:00")
        finally:
            con.close()

    def tearDown(self):
        self.rep_patch.stop()
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def _buy_payload(self, **over):
        d = {"side": "buy", "code": "000001.SZ", "amount": 33300,
             "reason": "人工建仓", "stop_pct": -5, "target_pct": 3,
             "override": {"ack": True, "note": "S4人工例外"}}
        d.update(over)
        return d

    def _execute(self, base):
        prev = manual.preview(base)
        body = {**base, "client_token": prev["client_token"],
                "preview_token": prev["preview_token"], "confirm": True}
        return prev, manual.execute(body)


class TestManualBuy(ManualTestBase):
    def test_s4_requires_override(self):
        with self.assertRaises(manual.ManualError) as ctx:
            manual.preview(self._buy_payload(override=None))
        self.assertEqual(ctx.exception.code, "OVERRIDE_REQUIRED")

    def test_buy_preview_and_execute(self):
        prev, out = self._execute(self._buy_payload())
        self.assertEqual(prev["qty"], 3100)
        self.assertEqual(prev["price"], 10.5)
        self.assertEqual(prev["stop"], 9.97)
        self.assertEqual(prev["target"], 10.81)
        self.assertTrue(prev["override_required"])
        self.assertTrue(out["ok"])
        self.assertEqual(out["order_id"].startswith("mo|"), True)
        con = store.connect()
        try:
            h = store.holding_get(con, "000001.SZ")
            self.assertEqual(h["qty"], 3100)
            tr = con.execute("SELECT * FROM trades WHERE source='manual'").fetchone()
            self.assertEqual((tr["qty"], tr["price"], tr["source"]), (3100, 10.5, "manual"))
            self.assertEqual(tr["override_segment"], "S4")
            mo = store.manual_order_by_token(con, out["client_token"])
            self.assertEqual(mo["status"], "filled")
            meta = con.execute("SELECT * FROM position_meta WHERE code='000001.SZ'").fetchone()
            self.assertEqual((meta["strategy"], meta["entry_date"], meta["stop"]),
                             ("manual", TODAY, 9.97))
        finally:
            con.close()

    def test_client_token_idempotent(self):
        base = self._buy_payload()
        prev, out = self._execute(base)
        body = {**base, "client_token": prev["client_token"],
                "preview_token": prev["preview_token"], "confirm": True}
        again = manual.execute(body)
        self.assertTrue(again["duplicate"])
        con = store.connect()
        try:
            self.assertEqual(store.holding_get(con, "000001.SZ")["qty"], 3100)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 1)
        finally:
            con.close()

    def test_stale_preview_rejected(self):
        base = self._buy_payload()
        prev = manual.preview(base)
        make_slice(self.slice, close=10.6, replace=True)
        with self.assertRaises(manual.ManualError) as ctx:
            manual.execute({**base, "client_token": prev["client_token"],
                            "preview_token": prev["preview_token"], "confirm": True})
        self.assertEqual(ctx.exception.code, "PREVIEW_STALE")
        con = store.connect()
        try:
            self.assertIsNone(store.holding_get(con, "000001.SZ"))
        finally:
            con.close()

    def test_already_held_rejected(self):
        con = store.connect()
        try:
            with con:
                store.holding_set(con, "000001.SZ", "平安银行", 100, 10.0)
        finally:
            con.close()
        with self.assertRaises(manual.ManualError) as ctx:
            manual.preview(self._buy_payload())
        self.assertEqual(ctx.exception.code, "ALREADY_HELD")

    def test_reason_and_override_note_optional(self):
        base = self._buy_payload(reason="", override={"ack": True, "note": ""})
        prev, out = self._execute(base)
        self.assertEqual(prev["trade_date"], TODAY)
        con = store.connect()
        try:
            mo = store.manual_order_by_token(con, out["client_token"])
            self.assertEqual(mo["override"]["note"], "人工覆盖S4")
            tr = con.execute("SELECT * FROM trades WHERE source='manual'").fetchone()
            self.assertEqual(tr["reason"], "人工建仓")
        finally:
            con.close()

    def test_buy_by_share_count(self):
        base = self._buy_payload()
        base.pop("amount")
        base["qty"] = 500
        prev, out = self._execute(base)
        self.assertEqual((prev["qty"], prev["qty_requested"]), (500, 500))
        self.assertIsNone(prev["amount_requested"])
        self.assertEqual(prev["amount"], 5250.0)
        self.assertEqual((prev["fees"]["fee"], prev["fees"]["transfer_fee"]),
                         (5.0, 0.05))
        con = store.connect()
        try:
            mo = store.manual_order_by_token(con, out["client_token"])
            self.assertEqual((mo["qty_requested"], mo["amount_requested"]), (500, None))
        finally:
            con.close()

    def test_buy_qty_must_be_lot_and_exclusive_with_amount(self):
        with self.assertRaises(manual.ManualError) as ctx:
            manual.preview(self._buy_payload(qty=100))
        self.assertEqual(ctx.exception.code, "INVALID_PARAM")
        base = self._buy_payload()
        base.pop("amount")
        base["qty"] = 150
        with self.assertRaises(manual.ManualError) as ctx:
            manual.preview(base)
        self.assertEqual(ctx.exception.code, "LOT_SIZE")

    def test_buy_custom_price_when_no_current_day_bar(self):
        # 只有上一交易日K线：默认价拒绝，自定义价可成交并标注参考收盘
        alt = Path(self._tmp.name) / "prev_only.sqlite"
        con = sqlite3.connect(alt)
        con.execute("CREATE TABLE bars (code TEXT, date TEXT, open REAL, high REAL, "
                    "low REAL, close REAL, volume REAL)")
        con.executemany("INSERT INTO bars VALUES (?,?,?,?,?,?,?)", [
            ("000001", PREV, 9.8, 10.1, 9.7, 10.0, 1e6)])
        con.commit()
        con.close()
        old = os.environ["BAZI_PAPER_SLICE_DB"]
        os.environ["BAZI_PAPER_SLICE_DB"] = str(alt)
        try:
            with self.assertRaises(manual.ManualError) as ctx:
                manual.preview(self._buy_payload())
            self.assertEqual(ctx.exception.code, "NO_CURRENT_QUOTE")
            self.assertEqual(ctx.exception.extra["reference_close"], 10.0)
            self.assertTrue(ctx.exception.extra["can_use_custom_price"])
            base = self._buy_payload(price=10.0)
            prev, out = self._execute(base)
            self.assertEqual((prev["price"], prev["price_source"]),
                             (10.0, "manual_price"))
            self.assertEqual(prev["reference_date"], PREV)
            self.assertEqual((prev["trade_date"], prev["backdated"]), (PREV, True))
            self.assertIn("参考", prev["price_label"])
            con = store.connect()
            try:
                tr = con.execute("SELECT * FROM trades WHERE source='manual'").fetchone()
                self.assertEqual((tr["price"], tr["price_source"], tr["trade_date"]),
                                 (10.0, "manual_price", PREV))
                h = store.holding_get(con, "000001.SZ")
                self.assertEqual(h["qty"], 3300)
                meta = con.execute(
                    "SELECT * FROM position_meta WHERE code='000001.SZ'").fetchone()
                self.assertEqual(meta["entry_date"], PREV)
                # 成交记在上一交易日 → 今日不受T+1锁定
                self.assertEqual(store.bought_today(con, "000001.SZ", TODAY), 0)
            finally:
                con.close()
        finally:
            os.environ["BAZI_PAPER_SLICE_DB"] = old

    def test_custom_price_limit_up_rejected(self):
        # 前收10.0 → 涨停11.0
        with self.assertRaises(manual.ManualError) as ctx:
            manual.preview(self._buy_payload(price=11.0))
        self.assertEqual(ctx.exception.code, "LIMIT_UP")

    def test_buy_qty_exceeding_cash_rejected(self):
        base = self._buy_payload()
        base.pop("amount")
        base["qty"] = 200000
        with self.assertRaises(manual.ManualError) as ctx:
            manual.preview(base)
        self.assertEqual(ctx.exception.code, "INSUFFICIENT_CASH")


class TestManualSell(ManualTestBase):
    def _seed(self, qty=1000, bought_today=0):
        con = store.connect()
        try:
            with con:
                store.holding_set(con, "000001.SZ", "平安银行", qty, 10.0)
                store.position_meta_open(con, "000001.SZ", "manual", "2026-M10",
                                         PREV, entry_ref=10.0, stop=9.5, target=10.8,
                                         stop_pct=-5, target_pct=8)
                if bought_today:
                    store.trade_insert(con, TODAY, "0900", "000001.SZ", "buy",
                                       bought_today, 10.0, 5.0, 0.0, 0.1,
                                       "seed", "测试当日买入", name="平安银行")
        finally:
            con.close()

    def _sell_payload(self, **over):
        d = {"side": "sell", "code": "000001.SZ", "qty": 100,
             "reason": "人工减仓"}
        d.update(over)
        return d

    def test_partial_sell_keeps_meta_open(self):
        self._seed(1000)
        _, out = self._execute(self._sell_payload())
        self.assertTrue(out["ok"])
        con = store.connect()
        try:
            self.assertEqual(store.holding_get(con, "000001.SZ")["qty"], 900)
            meta = con.execute("SELECT status FROM position_meta WHERE code='000001.SZ'").fetchone()
            self.assertEqual(meta["status"], "open")
        finally:
            con.close()

    def test_full_sell_closes_meta(self):
        self._seed(1000)
        _, out = self._execute(self._sell_payload(qty=1000))
        self.assertTrue(out["ok"])
        con = store.connect()
        try:
            self.assertIsNone(store.holding_get(con, "000001.SZ"))
            meta = con.execute("SELECT status,exit_reason FROM position_meta WHERE code='000001.SZ'").fetchone()
            self.assertEqual((meta["status"], meta["exit_reason"]), ("closed", "人工清仓"))
        finally:
            con.close()

    def test_t1_rejected(self):
        self._seed(1000, bought_today=1000)
        with self.assertRaises(manual.ManualError) as ctx:
            manual.preview(self._sell_payload(qty=100))
        self.assertEqual(ctx.exception.code, "T1_LOCKED")

    def test_partial_sell_must_be_lot(self):
        self._seed(1000, bought_today=600)  # 可卖400
        with self.assertRaises(manual.ManualError) as ctx:
            manual.preview(self._sell_payload(qty=50))
        self.assertEqual(ctx.exception.code, "LOT_SIZE")

    def test_sell_custom_price_not_supported(self):
        self._seed(1000)
        with self.assertRaises(manual.ManualError) as ctx:
            manual.preview(self._sell_payload(price=10.2))
        self.assertEqual(ctx.exception.code, "INVALID_PARAM")


if __name__ == "__main__":
    unittest.main(verbosity=2)
