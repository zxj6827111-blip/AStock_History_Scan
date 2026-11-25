"""客户机本机模拟盘：配置、种子账本、快照隔离、报告跟进（隔离目录）。"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from paper_trade import client, config, store  # noqa: E402


class ClientTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.private = root / "private"
        self.public = root / "public"
        self.local_public = self.private / "public"
        self.private.mkdir()
        self.public.mkdir()
        self._env = {
            "BAZI_ROLE": "client",
            "BAZI_PAPER_PRIVATE_DIR": str(self.private),
            "BAZI_PAPER_PUBLIC_DIR": str(self.public),
            "BAZI_PAPER_LOCAL_PUBLIC_DIR": str(self.local_public),
            "BAZI_PAPER_SLICE_DB": str(root / "none.sqlite"),
        }
        self._saved = {k: os.environ.get(k) for k in self._env}
        self._saved.update({
            k: os.environ.pop(k, None) for k in (
                "BAZI_PAPER_MODE", "BAZI_PAPER_BACKEND", "BAZI_PAPER_DISABLE",
                "BAZI_PAPER_CLIENT_AUTO")})
        os.environ.update(self._env)
        self.rep_patch = mock.patch("paper_trade.manual.latest_report",
                                    return_value=None)
        self.rep_patch.start()

    def tearDown(self):
        self.rep_patch.stop()
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def write_public(self, **overrides):
        payload = {
            "schema": "paper_public_v1",
            "schema_capabilities": ["manual_v1"],
            "generated_at_bjt": "2026-11-25T15:05:00+08:00",
            "market_date": "2026-11-25",
            "summary": {
                "mode": "live", "execution_backend": "local",
                "total": 999743.5, "cash": 974993.5, "market_value": 24750.0,
                "nav": 0.9997, "day_pnl": -256.5, "cum_pnl": -256.5,
            },
            "positions": [{
                "code": "000656.SZ", "name": "金科股份", "strategy": "manual",
                "qty": 25000, "sellable_qty": 25000, "avg_cost": 1.0,
                "price": 0.99, "entry_date": "2026-11-24", "entry_ref": 1.0,
                "approx_entry": False, "stop": 0.95, "target": 1.03,
                "weight": 2.48, "market_value": 24750.0,
            }],
            "equity": [{"date": "2026-11-24", "nav": 0.9997,
                        "total": 999743.5, "day_pnl": -256.5,
                        "cum_pnl": -256.5}],
            "trades_recent": [{
                "trade_date": "2026-11-24", "kind": "buy",
                "code": "000656.SZ", "name": "金科股份", "qty": 25000,
                "price": 1.0, "amount": 25000.0, "fee": 6.25, "tax": 0.0,
                "transfer_fee": 0.25, "slot": "1500",
                "created_at": "2026-11-25T00:46:02+08:00",
                "source": "manual", "manual_order_id": "mo|seed",
                "override_segment": "S4", "price_source": "manual_price",
                "price_time": "2026-11-24 15:00", "reason": "种子",
                "status": "filled", "plan_id": "manual|mo|seed",
            }],
        }
        payload.update(overrides)
        (self.public / "latest.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return payload


class TestClientConfig(ClientTestBase):
    def test_client_settings_created(self):
        info = config.ensure_client_settings()
        self.assertTrue(info["created"])
        s = config.settings()
        self.assertEqual(s["mode"], "live")
        self.assertEqual(s["execution_backend"], "local")
        self.assertTrue(s["manual_enabled"])
        self.assertEqual(s["execute_time"], "10:35")
        self.assertTrue(config.client_paper_enabled())

    def test_local_public_dir_is_used_on_client(self):
        config.ensure_client_settings()
        self.assertEqual(config.effective_public_dir(),
                         config.local_public_dir())
        self.assertNotEqual(config.effective_public_dir(), config.public_dir())

    def test_publisher_is_not_auto_enabled(self):
        os.environ["BAZI_ROLE"] = "publisher"
        before = config.private_dir() / "settings.local.json"
        info = config.ensure_client_settings()
        self.assertFalse(info["created"])
        self.assertFalse(before.exists())
        self.assertFalse(config.client_paper_enabled())
        self.assertEqual(config.effective_public_dir(), config.public_dir())


class TestClientSeed(ClientTestBase):
    def test_seed_from_public_snapshot(self):
        self.write_public()
        info = client.ensure_client_paper()
        self.assertTrue(info["enabled"])
        self.assertEqual(info["seed"]["status"], "seeded")
        con = store.connect()
        try:
            acct = store.get_account(con)
            self.assertAlmostEqual(acct["cash"], 974993.5)
            self.assertAlmostEqual(acct["initial_cash"], 1000000.0)
            holds = store.holdings_list(con)
            self.assertEqual(len(holds), 1)
            self.assertEqual(holds[0]["code"], "000656.SZ")
            self.assertEqual(holds[0]["qty"], 25000)
            metas = store.active_position_meta(con)
            self.assertEqual(len(metas), 1)
            self.assertEqual(metas[0]["strategy"], "manual")
            sync = store.latest_sync(con)
            self.assertEqual(sync["kind"], "seed")
            self.assertEqual(len(sync["positions"]), 1)
            nav = store.nav_rows(con)
            self.assertTrue(any(r["trade_date"] == "2026-11-25" for r in nav))
            trades = store.trades_recent(con)
            self.assertEqual(len(trades), 1)
            self.assertEqual(trades[0]["source"], "manual")
            fees = store.trade_fee_totals(con)
            self.assertAlmostEqual(fees["total"], 6.5)
        finally:
            con.close()
        local = config.local_public_dir() / "latest.json"
        self.assertTrue(local.is_file())
        snap = json.loads(local.read_text(encoding="utf-8"))
        self.assertEqual(snap["ledger_scope"], "client")
        self.assertEqual(len(snap["positions"]), 1)
        # 第二次启动幂等，不重复建账
        again = client.ensure_client_paper()
        self.assertEqual(again["seed"]["status"], "exists")
        con = store.connect()
        try:
            self.assertAlmostEqual(store.get_account(con)["cash"], 974993.5)
            self.assertEqual(len(store.holdings_list(con)), 1)
        finally:
            con.close()

    def test_no_snapshot_starts_empty_account(self):
        info = client.ensure_client_paper()
        self.assertTrue(info["enabled"])
        self.assertEqual(info["seed"]["status"], "empty")
        con = store.connect()
        try:
            acct = store.get_account(con)
            self.assertAlmostEqual(acct["cash"], 1000000.0)
            self.assertEqual(store.holdings_list(con), [])
            self.assertIn(store.latest_sync(con)["kind"], ("init", "seed"))
        finally:
            con.close()

    def test_stale_snapshot_is_not_seeded(self):
        self.write_public(generated_at_bjt="2020-01-01T00:00:00+08:00")
        info = client.ensure_client_paper()
        self.assertEqual(info["seed"]["status"], "empty")
        con = store.connect()
        try:
            self.assertEqual(store.holdings_list(con), [])
            self.assertAlmostEqual(store.get_account(con)["cash"], 1000000.0)
        finally:
            con.close()


class TestClientPlanReport(ClientTestBase):
    def setUp(self):
        super().setUp()
        config.ensure_client_settings()
        con = store.connect()
        try:
            with con:
                store.create_account(con, 1_000_000.0)
        finally:
            con.close()

    def test_same_day_plan_is_missed_on_seed_day(self):
        seed = self.private / "client_seed.json"
        seed.write_text(json.dumps({"seed_date": "2026-11-25"}),
                        encoding="utf-8")
        con = store.connect()
        try:
            with con:
                store.upsert_plan(con, {
                    "plan_id": "xq|2026-11-25|eod|seed",
                    "kind": "eod", "month_id": "2026-M10",
                    "signal_date": "2026-11-24",
                    "execute_trade_date": "2026-11-25",
                    "source_report": "x.json",
                    "config_hash": "c", "signal_hash": "s",
                    "regime": "Bear", "segment_id": "S4",
                    "total_weight_cap": 50.0,
                    "buys": [], "exits": [], "holds": [],
                    "excluded": [], "warnings": [],
                    "target_weights": {},
                    "status": "planned",
                    "created_at_bjt": config.iso_bjt(),
                })
        finally:
            con.close()
        client._skip_seed_day_plan("xq|2026-11-25|eod|seed")
        con = store.connect()
        try:
            plan = store.get_plan(con, "xq|2026-11-25|eod|seed")
            self.assertEqual(plan["status"], "missed")
        finally:
            con.close()

    def test_only_latest_daily_report_is_finalized(self):
        rdir = Path(self._tmp.name) / "reports"
        rdir.mkdir()
        for date, session in (("2026-11-21", "PM"), ("2026-11-24", "PM")):
            (rdir / f"survivor_{date}_1500_{session}.json").write_text(
                json.dumps({"schema": "survivor_report_v1", "asof_date": date,
                            "session": session, "month_id": "2026-M10"}),
                encoding="utf-8")
        calls = []

        def fake_finalize(info):
            calls.append(info["json"])
            return {"status": "planned", "plan_id": None}

        with mock.patch.object(client, "_reports_dir", return_value=rdir), \
                mock.patch.object(client.engine, "finalize_from_report",
                                  side_effect=fake_finalize):
            out = client.finalize_new_reports()
        self.assertEqual(out["status"], "ok")
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].endswith("survivor_2026-11-24_1500_PM.json"))
        state = json.loads((self.private / "client_reports.json").read_text(
            encoding="utf-8"))
        self.assertEqual(state["processed"]["survivor_2026-11-21_1500_PM.json"]
                         ["status"], "skip_superseded")

    def test_busy_report_is_retried_not_marked(self):
        rdir = Path(self._tmp.name) / "reports"
        rdir.mkdir()
        (rdir / "survivor_2026-11-24_1500_PM.json").write_text(
            json.dumps({"schema": "survivor_report_v1", "asof_date": "2026-11-24",
                        "session": "PM", "month_id": "2026-M10"}),
            encoding="utf-8")
        with mock.patch.object(client, "_reports_dir", return_value=rdir), \
                mock.patch.object(client.engine, "finalize_from_report",
                                  return_value={"status": "busy"}):
            client.finalize_new_reports()
        state_path = self.private / "client_reports.json"
        if state_path.is_file():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertNotIn("survivor_2026-11-24_1500_PM.json",
                             state.get("processed") or {})


class TestMarketAsOfCarryForward(ClientTestBase):
    def test_read_sync_keeps_last_slot_market_time(self):
        config.ensure_client_settings()
        con = store.connect()
        try:
            with con:
                store.create_account(con, 1_000_000.0)
                store.save_sync(con, kind="slot", ok=1, positions=[],
                                cash=1_000_000.0, total=1_000_000.0,
                                market_value=0.0,
                                market_as_of="2026-11-25 11:30")
                store.save_sync(con, kind="read", ok=1, positions=[],
                                cash=1_000_000.0, total=1_000_000.0,
                                market_value=0.0)
            self.assertIsNone(store.latest_sync(con)["market_as_of"])
            self.assertEqual(store.latest_market_as_of(con), "2026-11-25 11:30")
        finally:
            con.close()
        from paper_trade import report
        snap = report.refresh_public_snapshot()
        self.assertEqual(snap["market_as_of"], "2026-11-25 11:30")


class TestPaperApiLocalSnapshot(ClientTestBase):
    def test_client_reads_local_snapshot_not_delivered_one(self):
        config.ensure_client_settings()
        local = config.local_public_dir()
        local.mkdir(parents=True, exist_ok=True)
        (local / "latest.json").write_text(
            json.dumps({"schema": "paper_public_v1",
                        "summary": {"total": 1}, "ledger_scope": "client"}),
            encoding="utf-8")
        # BAZI_PAPER_PUBLIC_DIR 是显式覆盖；生产客户机不设它，这里临时去掉以测优先级
        saved = os.environ.pop("BAZI_PAPER_PUBLIC_DIR", None)
        try:
            import paper_api
            snap = paper_api.latest()
        finally:
            if saved is not None:
                os.environ["BAZI_PAPER_PUBLIC_DIR"] = saved
        self.assertEqual(snap["summary"]["total"], 1)
        self.assertEqual(snap["ledger_scope"], "client")


class TestCrossPlatformLock(ClientTestBase):
    def test_lock_acquire_release(self):
        from paper_trade import lock
        p = Path(self._tmp.name) / "x.lock"
        fh = p.open("a+")
        self.assertTrue(lock.acquire(fh))
        lock.release(fh)
        # 释放后可再次抢到
        fh2 = p.open("a+")
        self.assertTrue(lock.acquire(fh2))
        lock.release(fh2)


if __name__ == "__main__":
    unittest.main()
