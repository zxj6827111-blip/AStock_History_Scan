"""客户机本机模拟盘：独立账本 + 人工单 + 自动计划。

边界（2026-11-25）：
- 只在客户机启用（无 exports/update_pkgs/latest.json，或 BAZI_ROLE=client）；
- 首次启用从发布端最新公开快照接续现金/持仓/净值；没有快照就从初始资金空账本开始；
- 启用日不追单：execute_trade_date <= seed_date 的计划统一标记 missed；
- 盘中试探默认关闭（client_intraday_probe=false），避免客户机晚 20 分钟的信号价成交；
- 本机快照写 app/data/paper_trade/public，页面优先读它，发布端下发的快照只作回退。
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import config, engine, report, store

_SEED_FILE = "client_seed.json"
_REPORTS_FILE = "client_reports.json"
_REPORT_KEEP_DAYS = 7
_MAX_REPORT_FAILURES = 3
_START_LOCK = threading.Lock()
_STARTED = False


def _today() -> str:
    return config.now_bjt().strftime("%F")


def _reports_dir() -> Path:
    return config.OUTER_ROOT / "survivor_report_v1" / "reports"


def _read_json(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(path: Path, payload: dict) -> None:
    try:
        config.write_json_atomic(path, payload)
    except OSError as exc:
        config.log(f"客户机模拟盘状态写失败：{exc}")


def _num(value, default=0.0):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out


def _latest_month_id() -> str:
    """最新报告里的节气月编号；没有报告时用 seed 占位。"""
    try:
        from . import manual
        rep = manual.latest_report()
        if rep and rep.get("month_id"):
            return str(rep["month_id"])
    except Exception:  # noqa: BLE001
        pass
    return "seed"


def _ensure_account(con) -> dict:
    """空账本时建 100 万初始账户，并落一条空回读，避免页面显示“尚无回读”。"""
    acct = store.get_account(con)
    if acct is not None:
        return {"created": False, "cash": acct.get("cash")}
    initial = float(config.settings()["initial_assets"])
    with con:
        store.create_account(con, initial)
        store.save_sync(con, kind="init", ok=1, positions=[], cash=initial,
                        total=initial, market_value=0.0,
                        market_as_of=config.iso_bjt())
    return {"created": True, "cash": initial}


def _seed_position(con, p: dict, month_id: str) -> None:
    code = str(p.get("code") or "")
    if not code:
        return
    qty = int(p.get("qty") or 0)
    if qty <= 0:
        return
    name = str(p.get("name") or "")
    avg_cost = _num(p.get("avg_cost"), _num(p.get("price"), 0.0))
    sellable = p.get("sellable_qty")
    available = int(qty if sellable is None else sellable)
    strategy = str(p.get("strategy") or "seed")
    entry_date = str(p.get("entry_date") or _today())
    entry_ref = p.get("entry_ref")
    entry_ref = avg_cost if entry_ref is None else _num(entry_ref, avg_cost)
    approx = 1 if p.get("approx_entry") else 0
    now = config.iso_bjt()
    con.execute(
        "INSERT OR REPLACE INTO holdings"
        " (code, name, qty, available_qty, avg_cost, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        (code, name, qty, available, avg_cost, now))
    con.execute(
        "INSERT OR REPLACE INTO position_meta"
        " (code, strategy, month_id, entry_date, entry_ref, approx_entry,"
        "  is_probe, stop, target, stop_pct, target_pct, status, opened_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (code, strategy, month_id, entry_date, entry_ref, approx, 0,
         p.get("stop"), p.get("target"), None, None, "open", now))


def _copy_trades(con, rows: list) -> None:
    """接续发布端成交明细：让费用/已实现盈亏与页面“调仓记录”连续。

    只写展示与费用口径用的 trades 历史；不重算现金/持仓（那两项由 summary/positions 落）。
    """
    for t in rows or []:
        code = str(t.get("code") or "")
        qty = int(t.get("qty") or 0)
        if not code or qty <= 0:
            continue
        side = str(t.get("kind") or t.get("side") or "").lower()
        if side not in ("buy", "sell"):
            continue
        con.execute(
            "INSERT INTO trades (trade_date, slot, code, name, side, qty, price,"
            " fee, tax, transfer_fee, plan_id, reason, created_at, source,"
            " manual_order_id, override_segment, price_source, price_time)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(t.get("trade_date") or ""), t.get("slot"), code, t.get("name"),
             side, qty, _num(t.get("price")), _num(t.get("fee")),
             _num(t.get("tax")), _num(t.get("transfer_fee")),
             t.get("plan_id"), t.get("reason"),
             t.get("created_at") or config.iso_bjt(),
             t.get("source") or "auto", t.get("manual_order_id"),
             t.get("override_segment"), t.get("price_source"),
             t.get("price_time")))


def _copy_equity(con, rows: list, skip_date: str | None) -> None:
    """把发布端历史净值曲线接到本机（只用于展示，不参与撮合）。"""
    for row in rows or []:
        date = str(row.get("date") or "")
        if not date or (skip_date and date == skip_date):
            continue
        try:
            con.execute(
                "INSERT OR REPLACE INTO nav"
                " (trade_date, total, cash, market_value, nav, day_pnl, cum_pnl, synced_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (date, row.get("total"), None, None, row.get("nav"),
                 row.get("day_pnl"), row.get("cum_pnl"), config.iso_bjt()))
        except Exception:  # noqa: BLE001  历史展示缺列不影响账本
            continue


def seed_from_public() -> dict:
    """首次启用：从发布端最新公开快照接续现金/持仓/净值。

    只执行一次（client_seed.json 存在即跳过）；发布端快照超过 30 天视为过期，
    不再复制旧持仓，改为空账本，避免客户机从几个月前的组合开始。
    """
    if not config.client_paper_enabled():
        return {"status": "disabled"}
    state_path = config.private_dir() / _SEED_FILE
    if state_path.is_file():
        state = _read_json(state_path)
        con = store.connect()
        try:
            account = store.get_account(con)
        finally:
            con.close()
        if account is not None:
            return {"status": "exists", **state}
        # 账本 DB 被删但种子状态还在：允许重建，避免客户机永久空账本。

    pub_path = config.public_dir() / "latest.json"
    pub = _read_json(pub_path) if pub_path.is_file() else {}
    summary = pub.get("summary") or {}
    positions = pub.get("positions") or []
    generated = str(pub.get("generated_at_bjt") or "")
    stale = False
    if generated:
        try:
            age = config.now_bjt() - datetime.fromisoformat(generated)
            stale = age > timedelta(days=30)
        except ValueError:
            stale = False
    if stale:
        pub, summary, positions = {}, {}, []

    initial = float(config.settings()["initial_assets"])
    total = _num(summary.get("total"), 0.0)
    if total <= 0:
        total = initial
    cash = summary.get("cash")
    cash = total if cash is None else _num(cash, total)
    market_value = _num(summary.get("market_value"), max(total - cash, 0.0))
    month_id = _latest_month_id()
    seed_date = _today()

    con = store.connect()
    try:
        if store.get_account(con) is not None:
            state = {"seed_date": seed_date, "status": "account_exists"}
            _write_state(state_path, state)
            return {"status": "account_exists", **state}
        with con:
            store.create_account(con, initial)
            store.update_account_cash(con, cash)
            for p in positions:
                _seed_position(con, p, month_id)
            store.save_sync(con, kind="seed", ok=1, positions=positions,
                            cash=cash, total=total, market_value=market_value,
                            market_as_of=pub.get("market_as_of")
                            or (f"{pub.get('market_date')} 15:00"
                                if pub.get("market_date") else None))
            _copy_trades(con, pub.get("trades_recent") or [])
            _copy_equity(con, pub.get("equity") or [], pub.get("market_date"))
            if pub.get("market_date"):
                nav = _num(summary.get("nav"), total / initial if initial else 1.0)
                con.execute(
                    "INSERT OR REPLACE INTO nav"
                    " (trade_date, total, cash, market_value, nav, day_pnl,"
                    "  cum_pnl, synced_at) VALUES (?,?,?,?,?,?,?,?)",
                    (str(pub["market_date"]), total, cash, market_value, nav,
                     summary.get("day_pnl"), summary.get("cum_pnl"),
                     generated or config.iso_bjt()))
    finally:
        con.close()

    state = {
        "seed_date": seed_date,
        "source": str(pub_path) if pub else None,
        "snapshot_generated_at": generated or None,
        "initial_cash": initial,
        "total": total,
        "positions": len(positions),
    }
    _write_state(state_path, state)
    try:
        report.refresh_public_snapshot()
    except Exception as exc:  # noqa: BLE001
        config.log(f"客户机种子快照刷新失败（不影响账本）：{exc}")
    config.log(f"客户机本机账本初始化：{json.dumps(state, ensure_ascii=False)}")
    return {"status": "seeded" if positions or pub else "empty", **state}


def _skip_seed_day_plan(plan_id: str) -> None:
    """启用日不追单：把执行日 <= seed_date 的未执行计划标记 missed。"""
    if not plan_id:
        return
    seed = _read_json(config.private_dir() / _SEED_FILE)
    seed_date = str(seed.get("seed_date") or _today())
    con = store.connect()
    try:
        plan = store.get_plan(con, plan_id)
        if not plan:
            return
        if str(plan.get("execute_trade_date") or "") > seed_date:
            return
        if plan.get("status") not in ("planned", "dry_run"):
            return
        with con:
            store.mark_plan(con, plan_id, "missed", {
                "reason": f"客户机启用日 {seed_date} 不追单"})
        try:
            report.write_plan_snapshot(store.get_plan(con, plan_id))
            report.refresh_public_snapshot()
        except Exception as exc:  # noqa: BLE001
            config.log(f"启用日计划快照刷新失败：{exc}")
        config.log(f"客户机启用日跳过计划 {plan_id}（执行日 {plan.get('execute_trade_date')}）")
    finally:
        con.close()


def _run_report(path: Path, rep: dict, state_path: Path, processed: dict) -> None:
    """处理一份报告；busy 不标记，error 最多重试 _MAX_REPORT_FAILURES 次。"""
    name = path.name
    asof = str(rep.get("asof_date") or "")
    session = str(rep.get("session") or "")
    try:
        if session in ("PM", "EOD"):
            res = engine.finalize_from_report({"json": str(path)})
        elif session == "INTRADAY":
            res = engine.finalize_intraday({"json": str(path)})
        else:
            res = {"status": "skip_session"}
    except Exception as exc:  # noqa: BLE001  单份报告异常不拖垮扫描
        res = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    status = str(res.get("status") or "")
    if status == "busy":
        # 引擎另一实例正在执行；下一轮再试，不标记已处理
        return
    if status == "error":
        failures = int((processed.get(name) or {}).get("failures") or 0) + 1
        if failures < _MAX_REPORT_FAILURES:
            processed[name] = {"status": "error", "failures": failures,
                               "session": session, "asof": asof,
                               "at": config.iso_bjt()}
            _write_state(state_path, {"processed": processed,
                                      "last_scan": config.iso_bjt()})
            return
    processed[name] = {"status": status, "session": session, "asof": asof,
                       "plan_id": res.get("plan_id"), "at": config.iso_bjt()}
    if res.get("plan_id"):
        try:
            _skip_seed_day_plan(str(res["plan_id"]))
        except Exception as exc:  # noqa: BLE001
            config.log(f"启用日计划跳过失败 {res['plan_id']}：{exc}")


def finalize_new_reports() -> dict:
    """扫描新归档报告：只取最新 PM/EOD 生成次日计划；INTRADAY 默认跳过。

    客户机可能一次补到多天报告；旧报告全部标记 skip_superseded，避免在启用时
    一次生成一串过期计划。盘中试探由 client_intraday_probe 控制，默认关闭。
    """
    if not config.client_paper_enabled():
        return {"status": "disabled"}
    if not bool(config.settings().get("client_auto_plan", True)):
        return {"status": "auto_plan_off"}
    rdir = _reports_dir()
    if not rdir.is_dir():
        return {"status": "no_reports"}
    state_path = config.private_dir() / _REPORTS_FILE
    state = _read_json(state_path) or {}
    processed = state.get("processed") if isinstance(state.get("processed"), dict) else {}
    cutoff = (config.now_bjt() - timedelta(days=_REPORT_KEEP_DAYS)).strftime("%F")
    daily, intraday = [], []
    changed = False
    for p in sorted(rdir.glob("survivor_*.json"), key=lambda x: x.stat().st_mtime):
        name = p.name
        if name in processed:
            continue
        rep = _read_json(p)
        if rep.get("schema") != "survivor_report_v1":
            processed[name] = {"status": "skip_schema", "at": config.iso_bjt()}
            changed = True
            continue
        asof = str(rep.get("asof_date") or "")
        session = str(rep.get("session") or "")
        if asof < cutoff:
            processed[name] = {"status": "skip_old", "asof": asof,
                               "at": config.iso_bjt()}
            changed = True
            continue
        if session in ("PM", "EOD"):
            daily.append((p, rep))
        elif session == "INTRADAY":
            intraday.append((p, rep))
        else:
            processed[name] = {"status": "skip_session", "session": session,
                               "asof": asof, "at": config.iso_bjt()}
            changed = True

    if daily:
        daily.sort(key=lambda x: (str(x[1].get("asof_date") or ""),
                                  x[0].stat().st_mtime, x[0].name))
        latest = daily[-1]
        for p, rep in daily[:-1]:
            processed[p.name] = {"status": "skip_superseded",
                                 "session": rep.get("session"),
                                 "asof": rep.get("asof_date"),
                                 "at": config.iso_bjt()}
            changed = True
        _run_report(latest[0], latest[1], state_path, processed)
        changed = True
    if intraday:
        if bool(config.settings().get("client_intraday_probe")):
            intraday.sort(key=lambda x: (str(x[1].get("asof_date") or ""),
                                         x[0].stat().st_mtime, x[0].name))
            _run_report(*intraday[-1], state_path, processed)
            for p, rep in intraday[:-1]:
                processed[p.name] = {"status": "skip_superseded",
                                     "session": rep.get("session"),
                                     "asof": rep.get("asof_date"),
                                     "at": config.iso_bjt()}
        else:
            for p, rep in intraday:
                processed[p.name] = {"status": "skip_session",
                                     "session": rep.get("session"),
                                     "asof": rep.get("asof_date"),
                                     "at": config.iso_bjt()}
        changed = True
    if changed:
        _write_state(state_path, {"processed": processed,
                                  "last_scan": config.iso_bjt()})
    return {"status": "ok", "processed": len(processed)}


def ensure_client_paper() -> dict:
    """确保客户机本机模拟盘可用：配置 → 账户 → 种子快照。"""
    settings_info = config.ensure_client_settings()
    if not config.client_paper_enabled():
        return {"enabled": False, "settings": settings_info}
    # 先尝试从发布端快照接续；只有没有快照/没有账户时才落空账本。
    try:
        seed = seed_from_public()
    except Exception as exc:  # noqa: BLE001  种子失败也要让空账本可用
        config.log(f"客户机种子快照失败（改用空账本）：{type(exc).__name__}: {exc}")
        seed = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    con = store.connect()
    try:
        account = _ensure_account(con)
    finally:
        con.close()
    try:
        report.refresh_public_snapshot()
    except Exception as exc:  # noqa: BLE001
        config.log(f"客户机启动快照刷新失败（不影响账本）：{exc}")
    return {"enabled": True, "settings": settings_info,
            "account": account, "seed": seed}


def start_client() -> dict:
    """启动客户机本机模拟盘线程（幂等）。发布端直接返回 disabled。"""
    global _STARTED
    if not config.client_paper_enabled() and config.is_publisher():
        return {"started": False, "role": "publisher"}
    info = ensure_client_paper()
    if not info.get("enabled"):
        return {"started": False, "role": "client", **info}
    with _START_LOCK:
        if _STARTED:
            return {"started": True, "already": True, **info}
        _STARTED = True

    def loop():
        while True:
            try:
                engine.tick(config.now_bjt())
            except Exception as exc:  # noqa: BLE001
                config.log(f"客户机模拟盘 tick 异常（不影响页面）：{type(exc).__name__}: {exc}")
            try:
                finalize_new_reports()
            except Exception as exc:  # noqa: BLE001
                config.log(f"客户机报告跟进异常（不影响页面）：{type(exc).__name__}: {exc}")
            time.sleep(20)

    threading.Thread(target=loop, name="paper-client", daemon=True).start()
    config.log("客户机本机模拟盘线程启动")
    return {"started": True, "role": "client", **info}
