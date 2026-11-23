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
    con = store.connect()
    try:
        sync = store.latest_sync(con)
        metas = {m["code"]: m for m in store.active_position_meta(con)}
        navs = store.nav_rows(con)
        rows = [dict(r) for r in con.execute(
            "SELECT plan_id, signal_date, execute_trade_date, status, regime,"
            " segment_id, intents_json, result_json FROM plans "
            "ORDER BY created_at DESC, plan_id DESC LIMIT 14")]
        fills = store.trades_recent(con, 80)   # 真实成交（本地账本）
    finally:
        con.close()

    # 调仓记录 = 真实成交 + 被拒订单（不是计划里的买卖意图——那是计划历史）
    trades: list[dict] = [
        {"trade_date": t["trade_date"], "kind": t["side"], "code": t["code"],
         "name": t.get("name") or "", "qty": t["qty"], "price": t["price"],
         "reason": t["reason"] or "", "status": "filled",
         "plan_id": t["plan_id"]} for t in fills]
    plans_brief: list[dict] = []
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
        plans_brief.append({
            "plan_id": r["plan_id"], "signal_date": r["signal_date"],
            "execute_trade_date": d, "status": r["status"],
            "regime": r.get("regime"), "segment_id": r.get("segment_id"),
            "n_buys": len(intents.get("buys") or []),
            "n_exits": len(intents.get("exits") or []),
            "n_excluded": len(intents.get("excluded") or [])})
        for j in (r["result"] or {}).get("rejected") or []:
            trades.append({"trade_date": d, "plan_id": r["plan_id"],
                           "kind": j.get("side") or "", "code": j.get("code"),
                           "name": j.get("name") or "", "qty": j.get("qty"),
                           "price": j.get("price"),
                           "reason": j.get("reason") or "",
                           "status": "rejected"})

    positions = []
    for p in (sync or {}).get("positions") or []:
        code = str(p.get("code") or "")
        m = metas.get(code) or {}
        positions.append({
            "code": code, "name": p.get("name"), "strategy": m.get("strategy"),
            "weight": float(p.get("weight") or 0.0),
            "market_value": p.get("market_value"),
            "qty": p.get("qty"), "price": p.get("price"),
            "entry_date": m.get("entry_date"), "entry_ref": m.get("entry_ref"),
            "approx_entry": bool(m.get("approx_entry")),
            "stop": m.get("stop"), "target": m.get("target")})
    positions.sort(key=lambda x: -x["weight"])

    s = config.settings()
    ok_sync = bool(sync and sync.get("ok"))
    total = float(sync.get("total") or 0.0) if ok_sync else None
    last_nav = navs[-1] if navs else None
    summary = {
        "mode": config.mode(),
        "total": total,
        "cash": float(sync.get("cash")) if ok_sync and sync.get("cash") is not None else None,
        "market_value": (float(sync.get("market_value"))
                         if ok_sync and sync.get("market_value") is not None else None),
        "total_weight": round(sum(p["weight"] for p in positions), 2),
        "nav": last_nav["nav"] if last_nav else
               (round(total / float(s["initial_assets"]), 4) if total else None),
        "day_pnl": last_nav["day_pnl"] if last_nav else None,
        "cum_pnl": last_nav["cum_pnl"] if last_nav else None,
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

    latest = {
        "schema": "paper_public_v1",
        "generated_at_bjt": config.iso_bjt(),
        "synced_at": sync.get("synced_at") if sync else None,
        "sync_ok": ok_sync,
        "stale": stale,
        "summary": summary,
        "positions": positions,
        "plans_recent": plans_brief,
        "trades_recent": trades,
        "equity": [{"date": n["trade_date"], "nav": n["nav"],
                    "total": n["total"], "day_pnl": n["day_pnl"],
                    "cum_pnl": n["cum_pnl"]} for n in navs],
        "warnings": warnings,
        "disclaimer": ("模拟盘：雪球组合真实行情驱动的前向模拟，非实盘；"
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
