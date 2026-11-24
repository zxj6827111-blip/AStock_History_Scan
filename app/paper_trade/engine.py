"""模拟盘引擎：盘后定稿 → 次日执行 → 周期同步 → 收盘快照。

安全口径：
- 真实调仓三重开关：mode=live + BAZI_PAPER_TRADE_ENABLE=1 + 已确认基线；缺一即 blocked；
- 提交采用「最多一次 + 主动对账」：网络异常绝不自动重发，先回读组合核对，
  全部落地才 confirmed，部分落地/无法确认 → needs_review/unknown 等人工处理；
- 所有入口都抢 paper.lock（非阻塞）：抢不到说明另一实例在跑，直接跳过；
- 状态写失败会抛异常终止（不像通知类任务那样吞掉）——交易状态必须真实；
- 任何入口的异常只进日志/告警卡，由 daily_live_job 侧兜底，不影响行情主链。
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from pathlib import Path

from . import calendar as pcal
from . import config, feishu, report, signals, store
from .xueqiu import XueqiuClient

# execute_due 返回这些状态后，本日不再重试（busy/out_of_window 除外：前者让位，
# 后者等进窗）
_TERMINAL_EXECUTE = frozenset((
    "dry_run", "confirmed", "unknown", "needs_review", "no_due_plan",
    "blocked_mode", "blocked_arming", "blocked_baseline", "error",
    "disabled", "not_trade_day", "no_report", "no_next_trade_date",
))


def _make_client(initial_assets=None):
    """雪球客户端工厂（测试用 FakeXueqiuClient 替换这里，绝不触网）。"""
    return XueqiuClient(initial_assets=initial_assets)


def _portfolio_label() -> str:
    try:
        d = json.loads(config.xueqiu_config_path().read_text(encoding="utf-8"))
        return str(d.get("portfolio_code") or "xq")
    except (OSError, ValueError):
        return "xq"


def _parse_hhmm(now: datetime, hhmm) -> datetime:
    h, m = (int(x) for x in str(hhmm).split(":")[:2])
    return now.replace(hour=h, minute=m, second=0, microsecond=0)


def _in_window(now: datetime, start, end) -> bool:
    return _parse_hhmm(now, start) <= now <= _parse_hhmm(now, end)


def _in_trading_session(now: datetime) -> bool:
    """连续竞价时段（BJT）：09:30–11:30 / 13:00–15:00。盘中试探只在这两段执行。"""
    t = now.strftime("%H:%M")
    return ("09:30" <= t <= "11:30") or ("13:00" <= t <= "15:00")


def _err(exc) -> str:
    return f"{type(exc).__name__}: {exc}"


def _load_state() -> dict:
    try:
        d = json.loads(config.state_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        config.write_json_atomic(config.state_path(), state)
    except OSError as exc:  # 状态写失败必须可见，不能静默吞掉后照常交易
        config.log(f"状态写失败（停止后续动作）：{exc}")
        raise


# ---------------- 盘后定稿（daily_live_job 挂点） ----------------

def finalize_from_report(info: dict) -> dict:
    """PM 报告生成成功/幂等跳过后调用：读归档（不读 latest.json）固化次日计划。"""
    if config.disabled():
        return {"status": "disabled"}
    report_path = Path(str((info or {}).get("json") or ""))
    if not report_path.is_file():
        config.log("定稿跳过：报告路径无效（不定稿、不调仓）")
        return {"status": "no_report"}
    try:
        rep = signals.load_report(report_path)
        rules = signals.load_month_rules(str(rep["month_id"]),
                                         config.month_rules_dir())
        dates = pcal.load_dates(config.market_sessions_path())
        execute_date, cal_warn = pcal.next_trade_date_fallback(
            str(rep["asof_date"]), dates,
            str(rules.get("final_liquidate_date") or ""))
        if not execute_date:
            return {"status": "no_next_trade_date"}
    except (signals.SignalError, OSError) as exc:
        config.log(f"定稿失败（不调仓）：{exc}")
        return {"status": "error", "error": str(exc)}
    lock = store.acquire_lock()
    if lock is None:
        return {"status": "busy"}
    try:
        con = store.connect()
        try:
            with con:
                held = store.active_held(con)
                month_positions = store.month_positions(con, str(rep["month_id"]))
                plan = signals.build_plan(
                    rep, rules, held, month_positions, execute_date,
                    _portfolio_label(), report_path.name)
                plan["created_at_bjt"] = config.iso_bjt()
                if cal_warn:   # 日历滞后：工作日推定要进计划告警（页面/卡片可见）
                    plan["warnings"] = list(plan.get("warnings") or []) + [cal_warn]
                res = store.upsert_plan(con, plan)
                for h in plan["holds"]:        # S4+ 移动止损回写（signals 保证只上移）
                    if h.get("trailing_updated"):
                        store.position_meta_update_stop(
                            con, h["code"], h["strategy"],
                            h.get("month_id"), h["stop"])
        finally:
            con.close()
        if res == "conflict":
            config.log(f"计划冲突 {plan['plan_id']}：同执行日已有已执行计划，"
                       f"需人工处理（新计划不自动执行）")
        report.write_plan_snapshot(plan)
        report.refresh_public_snapshot()
        try:
            feishu.dry_run("plan", plan)
        except Exception as exc:  # noqa: BLE001  卡片失败不影响定稿
            config.log(f"计划卡片 dry-run 失败（不影响定稿）：{_err(exc)}")
        config.log(f"定稿 {res} {plan['plan_id']}：买 {len(plan['buys'])} / "
                   f"卖 {len(plan['exits'])} / 排除 {len(plan['excluded'])}"
                   f"（{plan['execute_trade_date']} 执行）")
        return {"status": res, "plan_id": plan["plan_id"], "plan": plan}
    except Exception as exc:  # noqa: BLE001
        config.log(f"定稿异常：{_err(exc)}")
        return {"status": "error", "error": _err(exc)}
    finally:
        store.release_lock(lock)


# ---------------- 盘中试探（INTRADAY 档，报告 S3 打法） ----------------

def finalize_intraday(info: dict, now=None) -> dict:
    """盘中档出现 B8 新买信号 → 1/3 试探（交易时段内立即执行）。

    - 只在 09:30–11:30 / 13:00–15:00 执行（午休/收盘后的档只跳过）；
    - 只买不卖；已持仓/本月开过仓的票不重复试探（台账去重）；
    - dry-run 只落计划快照；live 与 EOD 走同一套三重开关 + 对账。
    """
    if config.disabled():
        return {"status": "disabled"}
    report_path = Path(str((info or {}).get("json") or ""))
    if not report_path.is_file():
        return {"status": "no_report"}
    try:
        rep = signals.load_report(report_path, allow_intraday=True)
    except (signals.SignalError, OSError) as exc:
        config.log(f"盘中试探跳过：{exc}")
        return {"status": "error", "error": str(exc)}
    if str(rep.get("session")) != "INTRADAY":
        return {"status": "skip_session"}
    now = now or config.now_bjt()
    today = now.strftime("%F")
    if str(rep.get("asof_date")) != today:
        return {"status": "skip_stale"}   # 隔日归档/回填不试探
    if not _in_trading_session(now):
        return {"status": "outside_market_hours"}
    try:
        rules = signals.load_month_rules(str(rep["month_id"]),
                                         config.month_rules_dir())
    except (signals.SignalError, OSError) as exc:
        config.log(f"盘中试探跳过：{exc}")
        return {"status": "error", "error": str(exc)}
    lock = store.acquire_lock()
    if lock is None:
        return {"status": "busy"}
    con = store.connect()
    try:
        with con:
            held = store.active_held(con)
            month_positions = store.month_positions(con, str(rep["month_id"]))
            intents = signals.build_intraday_intents(rep, rules, held,
                                                     month_positions)
            if not intents["buys"]:
                return {"status": "no_probe",
                        "excluded": intents["excluded"]}
            hhmm = str(rep.get("asof_time") or "").replace(":", "") or "0000"
            sh = signals.semantic_hash(rep)
            plan = {
                "plan_schema": "paper_plan_v1",
                "kind": "intraday_probe",
                "plan_id": f"{_portfolio_label()}|{today}|probe|{hhmm}|{sh[:12]}",
                "month_id": rep.get("month_id"),
                "signal_date": today,
                "execute_trade_date": today,
                "source_report": report_path.name,
                "source_built_at": rep.get("generated_at_bjt"),
                "config_hash": signals.rules_hash(rules),
                "signal_hash": sh,
                "regime": intents["regime"],
                "segment_id": intents["segment_id"],
                "total_weight_cap": intents["total_weight_cap"],
                "buys": intents["buys"], "exits": [], "holds": [],
                "excluded": intents["excluded"],
                "warnings": intents["warnings"],
                "target_weights": {},
                "status": "planned",
                "created_at_bjt": config.iso_bjt(),
            }
            res = store.upsert_plan(con, plan)
        if res == "exists":
            return {"status": "exists", "plan_id": plan["plan_id"]}
        if res == "conflict":
            config.log(f"盘中试探计划冲突 {plan['plan_id']}（需人工处理）")
            return {"status": "conflict", "plan_id": plan["plan_id"]}
        plan_row = store.get_plan(con, plan["plan_id"])
        report.write_plan_snapshot(plan_row)
        if config.mode() == "dry_run":
            with con:
                store.mark_plan(con, plan["plan_id"], "dry_run", {
                    "executed_at": config.iso_bjt(), "mode": "dry_run",
                    "note": "盘中试探 dry-run：不发请求、不动台账"})
            report.write_plan_snapshot(store.get_plan(con, plan["plan_id"]))
            report.refresh_public_snapshot()
            config.log(f"盘中试探 dry-run {plan['plan_id']}："
                       f"买 {len(intents['buys'])}")
            return {"status": "dry_run", "plan_id": plan["plan_id"]}
        if config.mode() != "live":
            return {"status": "blocked_mode", "mode": config.mode()}
        if _backend() == "local":
            if store.get_account(con) is None:
                return {"status": "blocked_local_account"}
        else:
            if not config.live_arming_ready():
                return {"status": "blocked_arming"}
            if store.baseline(con) is None:
                return {"status": "blocked_baseline"}
        result = _execute_live(con, plan_row, today, config.settings())
        return result
    except Exception as exc:  # noqa: BLE001
        config.log(f"盘中试探异常：{_err(exc)}")
        return {"status": "error", "error": _err(exc)}
    finally:
        con.close()
        store.release_lock(lock)


# ---------------- 次日执行 ----------------

def execute_due(now=None) -> dict:
    """09:35（含迟到补窗）执行到期计划。dry-run 只落结果，绝不发请求。"""
    if config.disabled():
        return {"status": "disabled"}
    now = now or config.now_bjt()
    s = config.settings()
    dates = pcal.load_dates(config.market_sessions_path())
    today = now.strftime("%F")
    is_td, td_warn = pcal.is_trade_date_safe(today, dates)
    if not is_td:
        return {"status": "not_trade_day"}
    if td_warn:
        config.log(td_warn)
    if not _in_window(now, s["execute_time"], s["catchup_until"]):
        return {"status": "out_of_window"}
    lock = store.acquire_lock()
    if lock is None:
        return {"status": "busy"}
    try:
        con = store.connect()
        try:
            plan = store.due_plan(con, today)
            if plan is None:
                return {"status": "no_due_plan"}
            if config.mode() == "dry_run":
                with con:
                    store.mark_plan(con, plan["plan_id"], "dry_run", {
                        "executed_at": config.iso_bjt(), "mode": "dry_run",
                        "note": "dry-run 只验证计划：不发请求、不动持仓台账"})
                updated = store.get_plan(con, plan["plan_id"])
                report.write_plan_snapshot(updated)
                report.refresh_public_snapshot()
                config.log(f"dry-run 执行 {plan['plan_id']}（未向雪球发请求）")
                return {"status": "dry_run", "plan_id": plan["plan_id"]}
            if config.mode() != "live":
                return {"status": "blocked_mode", "mode": config.mode()}
            if _backend() == "local":
                # 本地账本无外部写入：不需要雪球总闸/雪球基线，只需账户已初始化
                if store.get_account(con) is None:
                    return {"status": "blocked_local_account"}
            else:
                if not config.live_arming_ready():
                    return {"status": "blocked_arming"}
                if store.baseline(con) is None:
                    return {"status": "blocked_baseline"}
            return _execute_live(con, plan, today, s)
        finally:
            con.close()
    finally:
        store.release_lock(lock)


def snapshot_now(kind: str = "slot") -> dict:
    """本地后端：立即按最新切片重估并刷新公开快照（每个盘中档报告后调用）。

    雪球后端不强制回读（会触网），仍按 tick 的 30 分钟节流。
    """
    if config.disabled() or _backend() != "local":
        return {"status": "skip"}
    lock = store.acquire_lock()
    if lock is None:
        return {"status": "busy"}
    try:
        con = store.connect()
        try:
            res = _do_sync(con, kind=kind)
        finally:
            con.close()
        report.refresh_public_snapshot()
        return {"status": "ok", "sync": res}
    except Exception as exc:  # noqa: BLE001
        config.log(f"快照刷新失败（{kind}）：{_err(exc)}")
        return {"status": "error", "error": _err(exc)}
    finally:
        store.release_lock(lock)


def _basket_from_sync(sync) -> dict:
    """回读持仓 → {code: weight}；待成交调仓权重报 0 但有市值的按市值占比折算。"""
    total = float((sync or {}).get("total") or 0.0)
    out: dict[str, float] = {}
    for p in (sync or {}).get("positions") or []:
        code = str(p.get("code") or "")
        w = float(p.get("weight") or 0.0)
        if w <= 0 and total > 0 and p.get("market_value"):
            w = round(float(p["market_value"]) / total * 100.0, 2)
        if code and w > 0:
            out[code] = round(w, 2)
    return out


def _build_basket(con, plan) -> tuple[dict, list]:
    """目标篮子。
    - EOD 计划：计划目标权重 + 未纳管持仓原权重保留（不静默清仓）；
    - 盘中试探：当前回读全量权重 + 试探加权（只加不减，绝不因篮子缺失误卖）。
    """
    intents = plan.get("intents") or {}
    kind = str(plan.get("kind") or "eod")
    if kind == "intraday_probe":
        sync = store.latest_sync_ok(con)   # 调用前已做 pre-sync 刷新
        basket = _basket_from_sync(sync)
        for b in intents.get("buys") or []:
            code = str(b.get("code"))
            basket[code] = round(basket.get(code, 0.0)
                                 + float(b.get("weight") or 0.0), 2)
        return basket, []
    target = plan.get("target_weights") or {}
    managed = {m["code"] for m in store.active_position_meta(con)}
    managed |= {str(b.get("code")) for b in intents.get("buys") or []}
    basket = {str(c): round(float(w), 2) for c, w in target.items()}
    unmanaged = []
    sync = store.latest_sync_ok(con)
    for p in (sync or {}).get("positions") or []:
        code = str(p.get("code") or "")
        if not code or code in managed:
            continue
        w = _basket_from_sync({"positions": [p],
                               "total": (sync or {}).get("total")}).get(code, 0.0)
        if w > 0:
            basket[code] = w
            unmanaged.append(code)
    return basket, unmanaged


def _execute_live(con, plan, today, s) -> dict:
    if str(s.get("execution_backend") or "xueqiu") == "local":
        return _execute_local(con, plan, today, s)
    plan_id = plan["plan_id"]
    # 提交前必须先回读当前组合：未纳管持仓（用户手仓/占位股）要保留原权重，
    # 不能按上次快照猜。回读失败绝不盲发（宁可 missed 不误卖）。
    pre = _do_sync(con, kind="pre_execute")
    if not pre.get("ok"):
        with con:
            store.mark_plan(con, plan_id, "needs_review", {
                "reason": "提交前回读失败，不盲发", "pre_sync": pre})
        updated = store.get_plan(con, plan_id)
        report.write_plan_snapshot(updated)
        report.refresh_public_snapshot()
        config.log(f"执行中止（提交前回读失败）{plan_id}")
        return {"status": "needs_review", "plan_id": plan_id}
    basket, unmanaged = _build_basket(con, plan)
    req = {"plan_id": plan_id, "weights": basket,
           "ignore_minor": float(s["ignore_minor"])}
    request_hash = hashlib.sha256(
        json.dumps(req, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    attempt = store.next_attempt(con, plan_id)
    with con:
        store.record_submission(con, plan_id, attempt, request_hash, req,
                                "submitting")
        store.mark_plan(con, plan_id, "submitting", {"attempt": attempt})
    submitted, err = False, None
    try:
        client = _make_client(float(s["initial_assets"]))
        client.adjust_weights(basket, ignore_minor=float(s["ignore_minor"]))
        submitted = True   # easytrader 网络失败也返回 None：返回值不可信，仅=未抛异常
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
        config.log(f"调仓提交异常（结果未知，不自动重发）：{err}")
    # 不论提交是否报错：先回读对账，绝不凭返回值/异常直接重发
    sync_res = _do_sync(con, kind="after_execute")
    status = _reconcile(con, plan, basket, unmanaged, submitted, err,
                        attempt, sync_res)
    with con:
        store.record_submission(con, plan_id, attempt, request_hash, req,
                                status, summary=f"sync_ok={sync_res.get('ok')}",
                                error=err)
    updated = store.get_plan(con, plan_id)
    report.write_plan_snapshot(updated)
    report.refresh_public_snapshot()
    try:
        feishu.dry_run("result", {"plan_id": plan_id, "status": status,
                                  "result": updated.get("result") or {}})
    except Exception as exc:  # noqa: BLE001
        config.log(f"结果卡片 dry-run 失败（不影响执行）：{_err(exc)}")
    config.log(f"执行 {status} {plan_id}（attempt {attempt}）")
    return {"status": status, "plan_id": plan_id}


def _backend() -> str:
    return str(config.settings().get("execution_backend") or "xueqiu")


def _do_sync(con, kind="read", client=None) -> dict:
    """回读组合快照（调用方必须已持 paper.lock）。

    backend=local：直接读本地账本 + 切片价（不触网）；
    backend=xueqiu：读雪球组合（原逻辑）。
    """
    if _backend() == "local":
        try:
            from . import local_match
            snap = local_match.snapshot(con, local_match.prepare_prices(con))
            with con:
                store.save_sync(con, kind=kind, ok=1,
                                positions=snap["positions"],
                                cash=snap["cash"], total=snap["total"],
                                market_value=snap["market_value"])
            return {"ok": 1, "total": snap["total"], "drift": None}
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)[:300]
            with con:
                store.save_sync(con, kind=kind, ok=0, positions=[], error=msg)
            config.log(f"本地账本快照失败（{kind}）：{msg}")
            return {"ok": 0, "error": msg}
    if client is None and not config.xueqiu_config_path().is_file():
        # 观察期（dry-run）没配凭证：明确跳过而不是每 30 分钟制造一次回读失败
        msg = "雪球未配置（app/data/paper_trade/xueqiu.local.json 缺失）：跳过回读"
        with con:
            store.save_sync(con, kind=kind, ok=0, positions=[], error=msg)
        return {"ok": 0, "error": msg}
    try:
        if client is None:
            client = _make_client()
        bal = client.balance()
        positions = client.position()
        drift = _drift_check(con, positions)
        with con:
            store.save_sync(con, kind=kind, ok=1, positions=positions,
                            cash=bal.get("cash"), total=bal.get("total"),
                            market_value=bal.get("market_value"), drift=drift)
        return {"ok": 1, "total": bal.get("total"), "drift": drift}
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)[:300]
        with con:
            store.save_sync(con, kind=kind, ok=0, positions=[], error=msg)
        config.log(f"组合回读失败（{kind}）：{msg}")
        return {"ok": 0, "error": msg}


def _drift_check(con, positions) -> str | None:
    managed = {m["code"] for m in store.active_position_meta(con)}
    pos_codes = {str(p.get("code")) for p in positions or []}
    parts = []
    unmanaged = sorted(pos_codes - managed)
    missing = sorted(managed - pos_codes)
    if unmanaged:
        parts.append(f"未纳管持仓（人工改动？）：{'、'.join(unmanaged)}")
    if missing:
        parts.append(f"台账在场、组合已无：{'、'.join(missing)}")
    return "；".join(parts) if parts else None


def _execute_local(con, plan, today, s) -> dict:
    """本地撮合执行（同步、确定性；无网络、无 unknown 态）。"""
    plan_id = plan["plan_id"]
    intents = plan.get("intents") or {}
    codes = ([str(b.get("code")) for b in intents.get("buys") or []]
             + [str(e.get("code")) for e in intents.get("exits") or []]
             + [str(h.get("code")) for h in intents.get("holds") or []])
    try:
        from . import local_match
        prices = local_match.prepare_prices(con, codes)
        # 切片还没更新到执行日（切片是原子替换，不会读到半截）：等下一分钟重试，
        # 绝不能把"还没数据"误判成停牌拒单。窗口结束后未等到则由 tick 标 missed。
        if not any(prices.bar_on(local_match.code6(c), today) for c in codes if c):
            return {"status": "no_price_yet", "plan_id": plan_id}
        result = local_match.execute_plan(con, plan, today, prices, s)
    except Exception as exc:  # noqa: BLE001
        with con:
            store.mark_plan(con, plan_id, "needs_review",
                            {"reason": f"本地撮合失败：{type(exc).__name__}: {exc}"})
        updated = store.get_plan(con, plan_id)
        report.write_plan_snapshot(updated)
        report.refresh_public_snapshot()
        config.log(f"本地撮合失败 {plan_id}：{_err(exc)}")
        return {"status": "needs_review", "plan_id": plan_id}
    # 立即落一次快照：让 active_held 拿到最新权重（否则下一份计划按 0 权重重复加仓）
    _do_sync(con, kind="after_execute")
    with con:
        _apply_local_positions(con, plan, result, today)
        store.mark_plan(con, plan_id, "confirmed", result)
    updated = store.get_plan(con, plan_id)
    report.write_plan_snapshot(updated)
    report.refresh_public_snapshot()
    try:
        feishu.dry_run("result", {"plan_id": plan_id, "status": "confirmed",
                                  "result": updated.get("result") or {}})
    except Exception as exc:  # noqa: BLE001
        config.log(f"结果卡片 dry-run 失败（不影响执行）：{_err(exc)}")
    config.log(f"本地撮合 {plan_id}：成交 {len(result['fills'])} 笔 / "
               f"拒绝 {len(result['rejected'])} 笔")
    return {"status": "confirmed", "plan_id": plan_id, "result": result}


def _apply_local_positions(con, plan, result, today) -> None:
    """本地成交后落策略台账：买入开仓（含 probe 标记）、卖出平仓、补仓确认。"""
    intents = plan.get("intents") or {}
    month_id = str(plan.get("month_id") or "")
    buy_by_code = {str(b.get("code")): b for b in intents.get("buys") or []}
    exit_by_code = {str(e.get("code")): e for e in intents.get("exits") or []}
    for f in result.get("fills") or []:
        code = str(f.get("code"))
        if f.get("side") == "buy":
            b = buy_by_code.get(code) or {}
            store.position_meta_open(
                con, code, str(b.get("strategy") or "b8"), month_id, today,
                entry_ref=float(f.get("price") or 0) or None, approx=0,
                stop=b.get("stop"), target=b.get("target"),
                is_probe=1 if b.get("probe") else 0)
        elif f.get("side") == "sell":
            e = exit_by_code.get(code) or {}
            store.close_position(con, code, str(e.get("strategy") or ""),
                                 str(e.get("month_id") or month_id),
                                 str(e.get("reason") or ""), today)
    for h in intents.get("holds") or []:
        if h.get("weight_target") is not None:
            store.position_meta_confirm_probe(
                con, str(h.get("code")), str(h.get("strategy")),
                str(h.get("month_id") or month_id))


def _reconcile(con, plan, basket, unmanaged, submitted, err, attempt,
              sync_res) -> str:
    """对账：全部落地→confirmed（并落台账）；否则 unknown/needs_review 等人工。"""
    plan_id = plan["plan_id"]
    tol = float(config.settings()["weight_tolerance"])
    today = str(plan["execute_trade_date"])
    payload = {"attempt": attempt, "submitted": submitted, "submit_error": err,
               "unmanaged_preserved": unmanaged,
               "sync_ok": bool(sync_res.get("ok"))}
    if not sync_res.get("ok"):
        status = "unknown" if submitted else "needs_review"
        with con:
            store.mark_plan(con, plan_id, status, payload)
        return status
    sync = store.latest_sync(con)
    actual = {p.get("code"): float(p.get("weight") or 0.0)
              for p in sync.get("positions") or []}
    unapplied = [c for c, w in basket.items()
                 if abs(actual.get(c, 0.0) - float(w)) > tol]
    payload["unapplied"] = unapplied
    if not unapplied:
        with con:
            _apply_positions(con, plan, sync, today)
            store.mark_plan(con, plan_id, "confirmed", payload)
        return "confirmed"
    partial = any(actual.get(c, 0.0) > 0 for c in unapplied)
    # 提交抛错且目标全未落地 → unknown（人工对账后可手动重试）；其余 → needs_review
    status = "unknown" if (submitted is False and not partial) else "needs_review"
    with con:
        store.mark_plan(con, plan_id, status, payload)
    return status


def _apply_positions(con, plan, sync, today) -> None:
    """confirmed 后落台账：买入开仓（用回读价优先），退出平仓（幂等）。"""
    intents = plan.get("intents") or {}
    pos_by_code = {p.get("code"): p for p in sync.get("positions") or []}
    month_id = str(plan.get("month_id") or "")
    for b in intents.get("buys") or []:
        code = str(b.get("code"))
        p = pos_by_code.get(code) or {}
        price = p.get("price")
        entry_ref = (float(price) if price not in (None, "", 0) else None) \
            or float(b.get("entry_ref") or 0) or None
        approx = 0 if (price not in (None, "", 0)) else 1
        store.position_meta_open(
            con, code, str(b.get("strategy")), month_id, today,
            entry_ref=entry_ref, approx=approx,
            stop=b.get("stop"), target=b.get("target"),
            is_probe=1 if b.get("probe") else 0)
    for h in intents.get("holds") or []:
        # 收盘确认的试探仓补满后转为正式持仓（不再参与撤回判断）
        if h.get("weight_target") is not None:
            store.position_meta_confirm_probe(
                con, str(h.get("code")), str(h.get("strategy")),
                str(h.get("month_id") or month_id))
    for e in intents.get("exits") or []:
        store.close_position(con, str(e.get("code")), str(e.get("strategy")),
                             str(e.get("month_id") or month_id),
                             str(e.get("reason") or ""), today)


def reconcile_plan(plan_id: str) -> dict:
    """人工对账入口：按当前组合核对计划（CLI 用，不自动补发请求）。"""
    lock = store.acquire_lock()
    if lock is None:
        return {"status": "busy"}
    try:
        con = store.connect()
        try:
            plan = store.get_plan(con, plan_id)
            if plan is None:
                return {"status": "no_plan"}
            basket, unmanaged = _build_basket(con, plan)
            sync_res = _do_sync(con, kind="reconcile")
            status = _reconcile(con, plan, basket, unmanaged, None, None,
                                None, sync_res)
            updated = store.get_plan(con, plan_id)
            report.write_plan_snapshot(updated)
            report.refresh_public_snapshot()
            return {"status": status, "plan_id": plan_id}
        finally:
            con.close()
    finally:
        store.release_lock(lock)


# ---------------- 周期同步 / 收盘快照 ----------------

def sync_portfolio(kind="read") -> dict:
    if config.disabled():
        return {"status": "disabled"}
    lock = store.acquire_lock()
    if lock is None:
        return {"status": "busy"}
    try:
        con = store.connect()
        try:
            res = _do_sync(con, kind=kind)
        finally:
            con.close()
        report.refresh_public_snapshot()
        if not res.get("ok"):
            try:
                feishu.dry_run("failure", {"kind": kind,
                                           "error": res.get("error")})
            except Exception:  # noqa: BLE001
                pass
        return {"status": "ok" if res.get("ok") else "fail", **res}
    finally:
        store.release_lock(lock)


def eod_snapshot(now=None) -> dict:
    """收盘后最终回读 + NAV 落档 + 日终卡片（dry-run）。"""
    if config.disabled():
        return {"status": "disabled"}
    now = now or config.now_bjt()
    lock = store.acquire_lock()
    if lock is None:
        return {"status": "busy"}
    try:
        s = config.settings()
        today = now.strftime("%F")
        nav = None
        con = store.connect()
        try:
            res = _do_sync(con, kind="eod")
            if res.get("ok"):
                sync = store.latest_sync(con)
                total = float(sync.get("total") or 0.0)
                with con:
                    nav = store.upsert_nav(
                        con, today, total, sync.get("cash"),
                        sync.get("market_value"),
                        round(total / float(s["initial_assets"]), 4),
                        sync.get("synced_at") or config.iso_bjt())
        finally:
            con.close()
        report.refresh_public_snapshot()
        try:
            feishu.dry_run("daily", {"nav": nav, "sync_ok": res.get("ok")})
        except Exception as exc:  # noqa: BLE001
            config.log(f"日终卡片 dry-run 失败（不影响快照）：{_err(exc)}")
        config.log(f"收盘快照：sync_ok={res.get('ok')} "
                   f"nav={nav.get('nav') if nav else '—'}")
        return {"status": "ok" if res.get("ok") else "fail",
                "nav": nav, "sync": res}
    finally:
        store.release_lock(lock)


# ---------------- 调度 tick（daily_live_job 主循环挂点） ----------------

def tick(now=None) -> dict:
    """每分钟至多跑一次：执行 → 周期回读 → 收盘快照（自带防重状态）。"""
    if config.disabled():
        return {"status": "disabled"}
    now = now or config.now_bjt()
    today = now.strftime("%F")
    state = _load_state()
    if state.get("date") != today:
        state = {"date": today, "executed": False, "eod_done": False,
                 "last_sync_ts": 0.0, "last_tick_min": ""}
    minute = now.strftime("%F %H:%M")
    if state.get("last_tick_min") == minute:
        return {"status": "idle"}
    state["last_tick_min"] = minute
    res: dict = {}
    s = config.settings()
    try:
        dates = pcal.load_dates(config.market_sessions_path())
        is_td, td_warn = pcal.is_trade_date_safe(today, dates)
        if td_warn and state.get("cal_warn_date") != today:
            state["cal_warn_date"] = today   # 日历滞后告警每天只记一次，不逐分钟刷日志
            config.log(td_warn)
        if is_td:
            # 1) 执行（迟到过窗 → 标记 missed 不追单）
            if not state.get("executed"):
                r = execute_due(now=now)
                res["execute"] = r.get("status")
                if r.get("status") in _TERMINAL_EXECUTE:
                    state["executed"] = True
                elif now > _parse_hhmm(now, s["catchup_until"]):
                    _mark_missed(today)
                    res["execute"] = "missed"
                    state["executed"] = True
            # 2) 交易时段周期回读（与行情 30m 档同节奏，但不依赖它）
            if _in_window(now, "09:30", "15:10"):
                gap = float(s["sync_interval_minutes"]) * 60.0
                if time.time() - float(state.get("last_sync_ts") or 0) >= gap:
                    r = sync_portfolio(kind="read")
                    res["sync"] = r.get("status")
                    if r.get("status") != "busy":
                        state["last_sync_ts"] = time.time()
            # 3) 收盘最终快照 + NAV
            if (not state.get("eod_done")
                    and now >= _parse_hhmm(now, s["eod_time"])):
                r = eod_snapshot(now=now)
                res["eod"] = r.get("status")
                state["eod_done"] = True
        _save_state(state)
    except OSError as exc:
        config.log(f"tick 状态写入失败（本轮中止）：{exc}")
        raise
    return res or {"status": "idle"}


def _mark_missed(today: str) -> None:
    lock = store.acquire_lock()
    if lock is None:
        return
    try:
        con = store.connect()
        try:
            plan = store.due_plan(con, today)
            if plan is not None:
                with con:
                    store.mark_plan(con, plan["plan_id"], "missed",
                                    {"reason": "迟到补执行窗口外（不追单）"})
                report.write_plan_snapshot(store.get_plan(con, plan["plan_id"]))
        finally:
            con.close()
    finally:
        store.release_lock(lock)


# ---------------- 基线 ----------------

def adopt_baseline() -> dict:
    """把当前雪球组合确认为基线（占位股/已有持仓纳管，一次性、必须显式执行）。

    同时落一次回读快照：否则活跃持仓权重为 0，下一份计划会把它们当 0 权重卖出。
    """
    lock = store.acquire_lock()
    if lock is None:
        return {"status": "busy"}
    try:
        con = store.connect()
        try:
            if _backend() == "local":
                from . import local_match
                with con:
                    res = local_match.init_account(
                        con, float(config.settings()["initial_assets"]))
                report.refresh_public_snapshot()
                return res
            client = _make_client()
            positions = client.position()
            try:
                bal = client.balance()
            except Exception as exc:  # noqa: BLE001  余额失败不挡基线（持仓才是主体）
                config.log(f"基线回读余额失败（不影响采纳）：{_err(exc)}")
                bal = {}
            with con:
                store.save_sync(con, kind="baseline", ok=1, positions=positions,
                                cash=bal.get("cash"), total=bal.get("total"),
                                market_value=bal.get("market_value"))
                store.set_baseline(con, positions)
                for p in positions:
                    code = str(p.get("code") or "")
                    # 待成交调仓的持仓权重可能为 0，但有市值 = 真实在册，必须纳管
                    if code and (float(p.get("weight") or 0) > 0
                                 or float(p.get("market_value") or 0) > 0):
                        store.position_meta_open(
                            con, code, "adopted", "adopted",
                            config.now_bjt().strftime("%F"),
                            entry_ref=None, approx=0, stop=None, target=None)
            report.refresh_public_snapshot()
            return {"status": "ok", "positions": len(positions)}
        finally:
            con.close()
    finally:
        store.release_lock(lock)
