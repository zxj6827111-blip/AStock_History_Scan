"""公开只读快照（data/paper_public/）：脱敏，随更新包下发客户机。

- 只读账本（store）写 JSON/JSONL，不连雪球、不含 Cookie/账号/请求头；
- latest.json 是页面唯一数据源（summary/positions/plans/trades/equity/warnings）；
- nav.jsonl 每日净值（行数小，全量重写）；
- plans/<执行日>.json + plans/latest.json 是计划快照（含被拒原因，雪球页面看不到的）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from . import config, store


def _public_err(msg) -> str:
    """公开快照里的错误文本：抹本机绝对路径再限长。"""
    s = str(msg or "")
    s = re.sub(r"/Users/[^/\s'\"]+", "~", s)
    s = re.sub(r"/Volumes/[^/\s'\"]+", "~/vol", s)
    return s[:300]


def _normalize_plan(plan: dict) -> dict:
    """兼容两种输入：signals 生成的计划（buys 在顶层）与 store 行（intents_json 解析后）。"""
    p = dict(plan)
    if "buys" not in p:
        intents = p.get("intents") or {}
        for k in ("buys", "exits", "holds", "excluded", "warnings"):
            p.setdefault(k, intents.get(k) or [])
        p.setdefault("target_weights", p.get("target_weights") or {})
    return p


def _plan_public(plan: dict) -> dict:
    p = _normalize_plan(plan)
    return {k: p.get(k) for k in (
        "plan_schema", "plan_id", "month_id", "signal_date",
        "execute_trade_date", "source_report", "source_built_at",
        "signal_hash", "regime", "segment_id", "total_weight_cap",
        "buys", "exits", "holds", "excluded", "warnings",
        "target_weights", "status", "created_at_bjt", "result")}


def write_plan_snapshot(plan: dict) -> None:
    p = _normalize_plan(plan)
    snap = _plan_public(p)
    d = config.public_dir() / "plans"
    config.write_json_atomic(d / f"{p.get('execute_trade_date')}.json", snap)
    config.write_json_atomic(d / "latest.json", snap)


def refresh_public_snapshot() -> dict:
    s = config.settings()
    con = store.connect()
    try:
        sync = store.latest_sync(con)
        metas = {m["code"]: m for m in store.active_position_meta(con)}
        navs = store.nav_rows(con)
        fee_totals = store.trade_fee_totals(con)
        rows = [dict(r) for r in con.execute(
            "SELECT plan_id, kind, signal_date, execute_trade_date, status, regime,"
            " segment_id, total_weight_cap, intents_json, result_json,"
            " created_at, executed_at FROM plans "
            "ORDER BY created_at DESC, plan_id DESC LIMIT 14")]
        fills = store.trades_recent(con, 80)   # 真实成交（本地账本）
        manual_orders = store.manual_orders_recent(con, 30)
    finally:
        con.close()

    from . import manual as manual_mod
    latest_rep = manual_mod.latest_report()
    manual_universe = manual_mod.report_universe(latest_rep)
    manual_caps = manual_mod.capabilities()
    today_str = config.now_bjt().strftime("%F")
    # 今日人工单按“提交时间”计；按收盘价回填的单 trade_date 可能是上一交易日
    manual_today = [o for o in manual_orders
                    if str(o.get("created_at") or "").startswith(today_str)]
    manual_summary = {
        "enabled": bool(manual_caps.get("enabled")),
        "reason": manual_caps.get("reason"),
        "n_orders_today": len(manual_today),
        "n_filled": sum(o.get("status") == "filled" for o in manual_today),
        "n_rejected": sum(o.get("status") == "rejected" for o in manual_today),
        "n_override": sum(bool(o.get("override")) for o in manual_today),
        "last_order_at": manual_today[0].get("created_at") if manual_today else None,
        "segment_id": ((latest_rep or {}).get("current_segment") or {}).get("id"),
        "report": (latest_rep or {}).get("_source_file"),
    }
    manual_public = []
    for o in manual_orders:
        ov = o.get("override") or None
        manual_public.append({
            "order_id": o.get("order_id"), "trade_date": o.get("trade_date"),
            "created_at": o.get("created_at"), "side": o.get("side"),
            "code": o.get("code"), "name": o.get("name"),
            "amount_requested": o.get("amount_requested"),
            "qty_requested": o.get("qty_requested"),
            "fill_qty": o.get("fill_qty"), "price": o.get("price"),
            "amount": o.get("amount"), "fee": o.get("fee"),
            "tax": o.get("tax"), "transfer_fee": o.get("transfer_fee"),
            "price_time": o.get("price_time"), "stop": o.get("stop"),
            "price_source": o.get("price_source"),
            "target": o.get("target"), "status": o.get("status"),
            "reject_reason": o.get("reject_reason"),
            "override": ({"ack": bool(ov.get("ack")), "note": ov.get("note")}
                         if isinstance(ov, dict) else None),
        })

    # 最新计划保留可读明细；历史计划仍只展示摘要。
    plans_brief: list[dict] = []
    signal_refs: dict[tuple[str, str], float] = {}
    repair_marks: dict[tuple[str, str], str] = {}
    for r in rows:
        try:
            intents = json.loads(r.pop("intents_json") or "{}")
        except ValueError:
            intents = {}
        try:
            r["result"] = json.loads(r.pop("result_json") or "null")
        except ValueError:
            r["result"] = None
        d = str(r.get("execute_trade_date"))
        excluded_items = intents.get("excluded") or []
        observations = [x for x in excluded_items if x.get("signal_only")]
        blocked_candidates = [x for x in excluded_items
                              if x.get("blocked_reason") and not x.get("signal_only")]
        result = r.get("result") or {}
        brief = {
            "plan_id": r["plan_id"], "kind": r.get("kind") or "eod",
            "signal_date": r["signal_date"], "execute_trade_date": d,
            "status": r["status"], "regime": r.get("regime"),
            "segment_id": r.get("segment_id"),
            "total_weight_cap": r.get("total_weight_cap"),
            "created_at": r.get("created_at"), "executed_at": r.get("executed_at"),
            "n_buys": len(intents.get("buys") or []),
            "n_exits": len(intents.get("exits") or []),
            "n_excluded": len(excluded_items),
            "n_observations": len(observations),
            "n_blocked": len(blocked_candidates),
            "methodology_review": result.get("methodology_review"),
            "policy_filter": result.get("policy_filter")}
        if not plans_brief:
            brief.update({k: intents.get(k) or []
                          for k in ("buys", "exits", "holds", "excluded", "warnings")})
            brief["observations"] = observations
            brief["blocked_candidates"] = blocked_candidates
        plans_brief.append(brief)
        for group in ("buys", "exits", "holds"):
            for item in intents.get(group) or []:
                ref = item.get("entry_ref") if group == "buys" else item.get("close")
                if ref is not None:
                    signal_refs[(r["plan_id"], str(item.get("code") or ""))] = float(ref)
        repair = (r["result"] or {}).get("repair") or {}
        for code in repair.get("filled") or []:
            repair_marks[(r["plan_id"], str(code))] = str(repair.get("at") or "")

    # 调仓记录 = 真实成交 + 被拒订单（不是计划里的买卖意图——那是计划历史）。
    # 成交费用、档期与入账时间直接取账本，不由前端反推。
    trades: list[dict] = []
    for t in fills:
        qty, price = t.get("qty"), t.get("price")
        key = (str(t.get("plan_id") or ""), str(t.get("code") or ""))
        ref = signal_refs.get(key)
        reason = t.get("reason") or ""
        repair_at = repair_marks.get(key)
        fee = round(float(t.get("fee") or 0), 2)
        tax = round(float(t.get("tax") or 0), 2)
        transfer = round(float(t.get("transfer_fee") or 0), 2)
        trades.append({
            "trade_date": t["trade_date"], "kind": t["side"], "code": t["code"],
            "name": t.get("name") or "", "qty": qty, "price": price,
            "amount": round(float(qty) * float(price), 2)
                      if qty is not None and price is not None else None,
            "fee": fee, "tax": tax, "transfer_fee": transfer,
            "total_fee": round(fee + tax + transfer, 2),
            "slot": t.get("slot") or "", "created_at": t.get("created_at"),
            "ref_price": ref,
            "ref_deviation_pct": (round((float(price) / ref - 1.0) * 100.0, 2)
                                  if ref and price is not None else None),
            "is_repair": bool(repair_at is not None or "补执行" in reason),
            "repair_at": repair_at,
            "source": t.get("source") or "auto",
            "manual_order_id": t.get("manual_order_id"),
            "override_segment": t.get("override_segment"),
            "price_source": t.get("price_source"),
            "price_time": t.get("price_time"),
            "reason": reason, "status": "filled", "plan_id": t["plan_id"]})
    for r in rows:
        result = r.get("result") or {}
        for j in result.get("rejected") or []:
            key = (r["plan_id"], str(j.get("code") or ""))
            ref = signal_refs.get(key)
            qty, price = j.get("qty"), j.get("price")
            trades.append({
                "trade_date": str(r.get("execute_trade_date")), "plan_id": r["plan_id"],
                "kind": j.get("side") or "", "code": j.get("code"),
                "name": j.get("name") or "", "qty": qty, "price": price,
                "amount": round(float(qty) * float(price), 2)
                          if qty is not None and price is not None else None,
                "fee": None, "tax": None, "transfer_fee": None, "total_fee": None,
                "slot": "", "created_at": r.get("executed_at"),
                "ref_price": ref,
                "ref_deviation_pct": (round((float(price) / ref - 1.0) * 100.0, 2)
                                      if ref and price is not None else None),
                "is_repair": False, "repair_at": None, "source": "auto",
                "manual_order_id": None, "override_segment": None,
                "price_source": None, "price_time": None,
                "reason": j.get("reason") or "", "status": "rejected"})

    positions = []
    for p in (sync or {}).get("positions") or []:
        code = str(p.get("code") or "")
        m = metas.get(code) or {}
        positions.append({
            "code": code, "name": p.get("name"), "strategy": m.get("strategy"),
            "weight": float(p.get("weight") or 0.0),
            "market_value": p.get("market_value"),
            "qty": p.get("qty"), "today_buy_qty": p.get("today_buy_qty"),
            "sellable_qty": p.get("sellable_qty"), "price_date": p.get("price_date"),
            "price": p.get("price"),
            "prev_close": p.get("prev_close"), "day_pct": p.get("day_pct"),
            "day_pnl": p.get("day_pnl"), "avg_cost": p.get("avg_cost"),
            "pnl": p.get("pnl"), "pnl_pct": p.get("pnl_pct"),
            "entry_date": m.get("entry_date"), "entry_ref": m.get("entry_ref"),
            "approx_entry": bool(m.get("approx_entry")),
            "stop": m.get("stop"), "target": m.get("target")})
    positions.sort(key=lambda x: -x["weight"])

    ok_sync = bool(sync and sync.get("ok"))
    total = float(sync.get("total") or 0.0) if ok_sync else None
    last_nav = navs[-1] if navs else None
    initial = float((sync or {}).get("initial") or s["initial_assets"])
    # 盘中也要能看到"现在赚多少"：有实时快照就按实时价算，不被昨日 NAV 顶住
    day_live = pnl_live = None
    if ok_sync:
        today_str = config.now_bjt().strftime("%F")
        if last_nav and str(last_nav.get("trade_date")) == today_str \
                and last_nav.get("day_pnl") is not None:
            day_live = float(last_nav["day_pnl"])      # 已收盘落档，直接用当日 NAV
        else:
            # 账户当日盈亏 = 当前总资产 - 上一交易日收盘总资产（首日按初始资金）。
            # 不能用持仓"日涨跌"求和：今日买入的票没赚到开盘前的跳空。
            prev_total = float(last_nav["total"]) if last_nav else initial
            day_live = round(total - prev_total, 2) if total is not None else None
        pnl_vals = [p["pnl"] for p in positions if p.get("pnl") is not None]
        if pnl_vals:
            pnl_live = round(sum(pnl_vals), 2)
        elif not positions:
            pnl_live = 0.0
    cum_live = (round(total - initial, 2) if total and initial else
                (last_nav["cum_pnl"] if last_nav else None))
    is_local = str(s.get("execution_backend") or "xueqiu") == "local"
    total_fees = fee_totals["total"] if is_local else None
    # 毛盈亏与费用分开：累计净盈亏 = 已实现毛盈亏 + 浮动毛盈亏 - 累计费用。
    # 已实现毛盈亏由账户恒等式反推，天然包含全部已平仓交易且无需估算历史成本。
    realized_pnl = (round(cum_live - pnl_live + total_fees, 2)
                    if cum_live is not None and pnl_live is not None
                    and total_fees is not None else None)
    summary = {
        "mode": config.mode(),
        "execution_backend": s.get("execution_backend") or "xueqiu",
        "execute_time": s.get("execute_time"), "catchup_until": s.get("catchup_until"),
        "total": total,
        "cash": float(sync.get("cash")) if ok_sync and sync.get("cash") is not None else None,
        "market_value": (float(sync.get("market_value"))
                         if ok_sync and sync.get("market_value") is not None else None),
        "total_weight": round(sum(p["weight"] for p in positions), 2),
        "nav": (round(total / initial, 4) if total and initial else
                (last_nav["nav"] if last_nav else None)),
        "day_pnl": (day_live if day_live is not None else
                    (last_nav["day_pnl"] if last_nav else None)),
        "cum_pnl": cum_live,
        "unrealized_pnl": pnl_live,
        "realized_pnl": realized_pnl,
        "total_fees": total_fees,
        "fee_breakdown": fee_totals if is_local else None,
    }

    stale = True
    if ok_sync:
        try:
            age = (config.now_bjt()
                   - datetime.fromisoformat(str(sync["synced_at"]))).total_seconds()
            stale = age > 12 * 3600
        except ValueError:
            stale = False
    warnings: list[str] = []
    if not sync:
        warnings.append("尚无组合回读（雪球未连接或模拟盘未启用）")
    elif not ok_sync:
        warnings.append(f"最近回读失败：{_public_err(sync.get('error'))}")
    if sync and sync.get("drift"):
        warnings.append(str(sync["drift"]))

    market_as_of = (sync or {}).get("market_as_of")
    if not market_as_of and ok_sync and not is_local:
        market_as_of = sync.get("synced_at")  # 雪球实时回读时间
    market_date = max((str(p.get("price_date") or "") for p in positions), default="")
    if not market_date and market_as_of:
        market_date = str(market_as_of)[:10]

    latest = {
        "schema": "paper_public_v1",
        "schema_capabilities": ["manual_v1"],
        "generated_at_bjt": config.iso_bjt(),
        "synced_at": sync.get("synced_at") if sync else None,
        "sync_kind": sync.get("kind") if sync else None,
        "market_as_of": market_as_of, "market_date": market_date or None,
        "sync_ok": ok_sync,
        "stale": stale,
        "summary": summary,
        "positions": positions,
        "plans_recent": plans_brief,
        "trades_recent": trades,
        "manual_summary": manual_summary,
        "manual_orders_recent": manual_public,
        "manual_universe": manual_universe,
        "equity": [{"date": n["trade_date"], "nav": n["nav"],
                    "total": n["total"], "day_pnl": n["day_pnl"],
                    "cum_pnl": n["cum_pnl"]} for n in navs],
        "warnings": warnings,
        "disclaimer": ("模拟盘：按真实行情切片驱动的前向模拟，非实盘；"
                       "不构成投资建议。"),
    }
    config.public_dir().mkdir(parents=True, exist_ok=True)
    config.write_json_atomic(config.public_dir() / "latest.json", latest)
    lines = [json.dumps({k: n.get(k) for k in
                         ("trade_date", "total", "nav", "day_pnl", "cum_pnl")},
                        ensure_ascii=False) for n in navs]
    (config.public_dir() / "nav.jsonl").write_text(
        "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return latest
