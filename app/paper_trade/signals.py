"""盘后信号定稿：指定归档报告 → 四类策略意图 → 调仓计划（含语义哈希）。

口径（2026-11-23 实施计划）：
- 只读生成器 stdout 指向的归档 JSON；不读 latest.json；
- B8：group==B 且 b_tier==B8 且 b_tier_action=='执行' 且 no_chase=false，且段允许新买；
- S1/C：仅在月度执行配置的 buy_dates（S1 末交易日）从 C 组生成，过期不追；
- D_V1：d_v1_state=='first_break_today'（PM 档收盘确认）次日执行；buy_today/expired 不买；
- D_V2：d_v2_signal=true 仅记录观察信号，全段位不自动生成买单；
- 已持仓（任意策略）不重复开仓；退出只看止损/破月初价/目标/段规则，不机械追杀分组变化；
- 报告缺行/缺月度执行配置 → 阻止相关计划并告警，绝不解释成卖出；
- 语义哈希只覆盖交易字段（排除 generated_at 等展示字段）：同语义幂等、修订生新计划。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

PLAN_SCHEMA = "paper_plan_v1"
RULES_SCHEMA = "paper_month_rules_v1"

MAIN_STRATEGIES = ("b8", "s1c")     # ABCD 主策略（B8 执行档 / S1 早买）
D_STRATEGIES = ("d_v1", "d_v2")     # D 反转独立策略（D_V2 仅观察）
DEFAULT_NO_NEW_BUY_SEGMENTS = frozenset(("S4", "S5", "S6"))


def new_buy_allowed(rules: dict, segment_id: str) -> bool:
    """方法论全局段位闸门：禁新买也禁加仓，不能被单策略配置覆盖。"""
    blocked = rules.get("no_new_buy_segments", DEFAULT_NO_NEW_BUY_SEGMENTS)
    return str(segment_id or "") not in {str(s) for s in blocked}


# 语义哈希覆盖的行字段：只取会改变交易决策的
_HASH_ROW_KEYS = (
    "code", "name", "group", "b_tier", "b_tier_action", "no_chase", "advice",
    "close", "month_open", "stop_loss", "break_level", "target_3", "target_5",
    "mtd_pct", "mtm_pct", "dif", "dea", "cross",
    "d_v1_state", "d_v1_trigger_date", "d_v1_buy_ref", "d_v2_signal",
)


class SignalError(Exception):
    """报告/配置不可用或校验失败（调用方跳过并告警，不下单）。"""


def load_report(path, allow_intraday: bool = False) -> dict:
    """读归档报告。盘中试探允许 INTRADAY 档（allow_intraday=True）；
    EOD 定稿仍只认 PM/EOD（盘中档拒收，防订单抖动）。"""
    p = Path(path)
    try:
        rep = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SignalError(f"报告无法读取：{p.name}") from exc
    if rep.get("schema") != "survivor_report_v1":
        raise SignalError(f"schema 不是 survivor_report_v1：{rep.get('schema')}")
    if not rep.get("rows"):
        raise SignalError("报告 rows 为空（数据不完整，不定稿）")
    sessions = ("PM", "EOD", "INTRADAY") if allow_intraday else ("PM", "EOD")
    if rep.get("session") not in sessions:
        raise SignalError(f"报告档位不符：{rep.get('session')}")
    return rep


def semantic_hash(rep: dict) -> str:
    """交易语义哈希：同语义同哈希（幂等）；展示字段变化不算修订。"""
    rows = sorted(rep.get("rows") or [], key=lambda r: r.get("code") or "")
    payload = {
        "month_id": rep.get("month_id"),
        "asof_date": rep.get("asof_date"),
        "regime": (rep.get("index_filter") or {}).get("regime"),
        "segment_id": (rep.get("current_segment") or {}).get("id"),
        "rows": [{k: r.get(k) for k in _HASH_ROW_KEYS} for r in rows],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def rules_hash(rules: dict) -> str:
    blob = json.dumps(rules, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def load_month_rules(month_id: str, mdir) -> dict:
    p = Path(mdir) / f"month_{month_id}.json"
    if not p.is_file():
        raise SignalError(f"缺月度执行配置：month_{month_id}.json（新月不自动新买）")
    try:
        rules = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SignalError(f"月度执行配置无法读取：{p.name}") from exc
    if rules.get("schema") != RULES_SCHEMA or rules.get("month_id") != month_id:
        raise SignalError(f"月度执行配置不匹配：期望 {month_id}")
    for s in ("b8", "s1c", "d_v1", "d_v2"):
        if s not in (rules.get("strategies") or {}):
            raise SignalError(f"月度执行配置缺策略 {s}")
    return rules


def _hold(h: dict) -> dict:
    return {k: h.get(k) for k in
            ("code", "strategy", "weight", "stop", "target",
             "stop_pct", "target_pct", "entry_date",
             "entry_ref", "approx_entry", "month_id", "trailing_updated",
             "is_probe", "weight_target")}


def build_intents(rep: dict, rules: dict, held: list, month_positions: list) -> dict:
    """纯函数：报告 + 月度规则 + 当前持仓 → {buys, exits, holds, excluded, warnings, ...}。

    held: 活跃持仓（含 adopted 基线仓；weight 取自最近一次成功回读）；
    month_positions: 本月全部开仓记录（含已平仓，供同月去重）；
    holds 可能带 trailing_updated=True（S4+ 移动止损只上移，引擎负责回写）。
    """
    rows = {str(r.get("code")): r for r in rep.get("rows") or [] if r.get("code")}
    regime = (rep.get("index_filter") or {}).get("regime") or "Bear"
    cap = float((rules.get("regime_total_cap") or {}).get(regime, 50.0))
    seg_id = str((rep.get("current_segment") or {}).get("id") or "")
    signal_date = str(rep.get("asof_date") or "")
    strategies = rules.get("strategies") or {}

    # B8 收盘可执行代码集（供盘中试探的收盘确认/撤回判断，也供新买）
    b8_st = strategies.get("b8") or {}
    b8_allowed = set(b8_st.get("buy_segments") or [])
    b8_exec_codes = {
        str(r.get("code")) for r in (rep.get("rows") or [])
        if r.get("group") == "B" and r.get("b_tier") == "B8"
        and r.get("b_tier_action") == "执行" and not r.get("no_chase")
        and seg_id in b8_allowed
    }

    buys: list[dict] = []
    exits: list[dict] = []
    excluded: list[dict] = []
    warnings: list[str] = []
    holds: list[dict] = []
    held_codes: set[str] = set()

    # ---- 1) 持仓退出（报告缺行情不卖，只告警） ----
    for h in sorted(held, key=lambda x: x.get("code") or ""):
        code = str(h.get("code") or "")
        strategy = str(h.get("strategy") or "")
        row = rows.get(code)
        close = (row or {}).get("close")
        if row is None or close is None:
            warnings.append(f"{code}: 报告缺行情，退出检查跳过（不卖）")
            holds.append(_hold(h))
            held_codes.add(code)
            continue
        reason = None
        if strategy in MAIN_STRATEGIES or strategy == "adopted":
            bl = row.get("break_level")
            if bl is not None and close < float(bl):
                reason = f"收盘 {close} < 破月初价 {bl}"
        if reason is None:
            stop = h.get("stop")
            if stop is not None and close < float(stop):
                reason = f"收盘 {close} < 止损 {stop}"
        if reason is None and (strategy in D_STRATEGIES or strategy == "manual"):
            tgt = h.get("target")
            if tgt is not None and close >= float(tgt):
                reason = f"收盘 {close} ≥ 目标 {tgt}"
        if reason is not None:
            exits.append({"code": code, "strategy": strategy,
                          "month_id": h.get("month_id"),
                          "weight": float(h.get("weight") or 0.0),
                          "reason": reason, "close": close})
            continue
        # S4+ 移动止损：close×0.95，只上移不下移
        if strategy in MAIN_STRATEGIES and seg_id in ("S4", "S5", "S6"):
            new_stop = round(float(close) * 0.95, 2)
            old_stop = h.get("stop")
            if old_stop is None or new_stop > float(old_stop):
                h = {**h, "stop": new_stop, "trailing_updated": True}
        holds.append(_hold(h))
        held_codes.add(code)

    # ---- 1.5) 今日盘中试探票的收盘处置：确认→补满；未确认→撤回 ----
    # （按报告 S3 打法：盘中 1/3 试探，收盘三条件仍成立→补仓，不成立→撤）
    probe_ratio = float(b8_st.get("probe_ratio", 1.0 / 3.0))
    full_w = float(b8_st.get("per_name_weight", 10.0))
    for h in list(holds):
        if not int(h.get("is_probe") or 0):
            continue
        if str(h.get("entry_date") or "") != signal_date:
            continue   # 只处理"今天试探、今天收盘"的票
        code = str(h["code"])
        if code in b8_exec_codes:
            actual = float(h.get("weight") or 0.0)
            delta = round(max(0.0, full_w - actual), 2)
            total_now = sum(float(x.get("weight") or 0.0) for x in holds)
            if delta > 0 and total_now + delta <= cap + 1e-9:
                h["weight_target"] = full_w   # 补满目标（引擎写回 is_probe=0）
                warnings.append(f"{code}: 收盘确认，试探加仓至 {full_w}%")
            elif delta > 0:
                warnings.append(f"{code}: 收盘确认但总仓上限不足，维持试探仓位")
        else:
            exits.append({"code": code, "strategy": str(h.get("strategy") or ""),
                          "month_id": h.get("month_id"),
                          "weight": float(h.get("weight") or 0.0),
                          "reason": "试探未确认（收盘不再可执行），撤回",
                          "close": (rows.get(code) or {}).get("close")})
            holds = [x for x in holds if x["code"] != code]
            held_codes.discard(code)

    # ---- 2) S6 末强制清仓（月窗最后开市日及以后不持有） ----
    final_liq = str(rules.get("final_liquidate_date") or "")
    if final_liq and signal_date >= final_liq:
        for h in list(holds):
            exits.append({"code": h["code"], "strategy": str(h.get("strategy") or ""),
                          "month_id": h.get("month_id"),
                          "weight": float(h.get("weight") or 0.0),
                          "reason": f"S6 清仓（{final_liq} 后不持有）",
                          "close": (rows.get(h["code"]) or {}).get("close")})
        holds = []

    # ---- 3) S5 减仓：主策略总仓压到配置上限（弱势先卖：当日 MTD 升序） ----
    s5_cap = rules.get("s5_max_total_weight")
    if seg_id == "S5" and s5_cap is not None:
        main = [h for h in holds
                if h.get("strategy") in ("b8", "s1c", "adopted")]
        total_main = sum(float(h.get("weight") or 0.0) for h in main)
        if total_main > float(s5_cap) + 1e-9:
            def _weak(h):
                return float(((rows.get(h["code"]) or {}).get("mtd_pct")) or 0.0)
            for h in sorted(main, key=_weak):
                if total_main <= float(s5_cap) + 1e-9:
                    break
                exits.append({"code": h["code"], "strategy": str(h.get("strategy") or ""),
                              "month_id": h.get("month_id"),
                              "weight": float(h.get("weight") or 0.0),
                              "reason": (f"S5 减仓（主策略 {total_main:.1f}% "
                                         f"> 上限 {s5_cap}%）"),
                              "close": (rows.get(h["code"]) or {}).get("close")})
                holds = [x for x in holds if x["code"] != h["code"]]
                total_main -= float(h.get("weight") or 0.0)
                warnings.append(f"S5 减仓：卖出 {h['code']}（弱势优先）")

    # 禁买段同样禁止已有仓补仓；原持仓及原权重保留，不做机械卖出。
    if not new_buy_allowed(rules, seg_id):
        adjusted_holds = []
        for h in holds:
            target = h.get("weight_target")
            if (target is not None
                    and float(target) > float(h.get("weight") or 0.0) + 1e-9):
                h = dict(h)
                h.pop("weight_target", None)
                excluded.append({"code": h["code"], "strategy": h.get("strategy"),
                                 "blocked_reason": "segment_no_new_buy",
                                 "reason": f"段 {seg_id} 禁止已有仓补仓，仅管理退出"})
            adjusted_holds.append(h)
        holds = adjusted_holds

    # ---- 4) 新买（策略优先级；同月去重；总仓上限） ----
    total = round(sum(float(h.get("weight") or 0.0) for h in holds), 4)
    opened = {(str(m.get("strategy")), str(m.get("code"))) for m in month_positions}

    def _try_buy(strategy: str, code: str, row: dict, weight: float, why: str) -> None:
        nonlocal total
        if strategy == "d_v2":
            excluded.append({"code": code, "name": row.get("name"),
                             "strategy": strategy, "signal_only": True,
                             "blocked_reason": "d_v2_signal_only",
                             "reason": "D_V2 全段位仅观察，不自动下单"})
            return
        if not new_buy_allowed(rules, seg_id):
            excluded.append({"code": code, "name": row.get("name"),
                             "strategy": strategy,
                             "blocked_reason": "segment_no_new_buy",
                             "reason": f"段 {seg_id or '—'} 全策略禁止新开仓/加仓"})
            return
        if code in held_codes:
            excluded.append({"code": code, "strategy": strategy,
                             "reason": "已持仓（任意策略不重复开仓）"})
            return
        if (strategy, code) in opened:
            excluded.append({"code": code, "strategy": strategy,
                             "reason": "本月已开过仓（不重复）"})
            return
        if total + weight > cap + 1e-9:
            excluded.append({"code": code, "strategy": strategy,
                             "reason": f"总仓 {total:.1f}%+{weight:.1f}% 超 {cap:.0f}% 上限"})
            return
        st = strategies[strategy]
        stop_pct = float(st.get("stop_pct", -5.0))
        tgt_pct = st.get("target_pct")
        entry_ref = float(row.get("close"))
        stop = round(entry_ref * (1 + stop_pct / 100.0), 2)
        target = (round(entry_ref * (1 + float(tgt_pct) / 100.0), 2)
                  if tgt_pct is not None else None)
        buys.append({
            "code": code, "name": row.get("name"), "strategy": strategy,
            "weight": weight, "reason": why,
            # entry_ref/stop/target 是信号日口径（计划展示用）；真正落账的
            # 止损/目标由引擎按成交价 × stop_pct/target_pct 重算。
            "entry_ref": entry_ref, "stop": stop, "target": target,
            "stop_pct": stop_pct, "target_pct": tgt_pct,
            "break_level": row.get("break_level"), "approx_entry": True,
        })
        total = round(total + weight, 4)

    # B8 执行档（MTD 降序，与报告排序一致；no_chase/段外只记排除原因）
    st = strategies.get("b8") or {}
    if st.get("enabled"):
        allowed = set(st.get("buy_segments") or [])
        cands = [r for r in (rep.get("rows") or [])
                 if r.get("group") == "B" and r.get("b_tier") == "B8"
                 and r.get("b_tier_action") == "执行"]
        for r in sorted(cands, key=lambda x: -float(x.get("mtd_pct") or 0)):
            code = str(r.get("code"))
            if r.get("no_chase"):
                excluded.append({"code": code, "strategy": "b8",
                                 "reason": "MTM>15 不追（no_chase）"})
                continue
            if seg_id not in allowed:
                excluded.append({"code": code, "strategy": "b8",
                                 "reason": f"段 {seg_id or '—'} 不允许 B8 新买"})
                continue
            _try_buy("b8", code, r, float(st.get("per_name_weight", 10.0)),
                     f"B8 执行档（MTD {r.get('mtd_pct')}%）")

    # S1/C 早买（只在 buy_dates 当天定稿的 C 组；过期不追）
    st = strategies.get("s1c") or {}
    if st.get("enabled"):
        buy_dates = {str(d) for d in st.get("buy_dates") or []}
        if signal_date in buy_dates:
            for r in sorted((r for r in rep.get("rows") or [] if r.get("group") == "C"),
                            key=lambda x: -float(x.get("mtd_pct") or 0)):
                _try_buy("s1c", str(r.get("code")), r,
                         float(st.get("per_name_weight", 10.0)), "S1 末 C 组早买")

    # D_V1（仅 first_break_today；ST 再半）
    st = strategies.get("d_v1") or {}
    if st.get("enabled"):
        w = float(st.get("per_name_weight", 5.0))
        st_w = st.get("st_weight")
        for r in sorted(rep.get("rows") or [], key=lambda x: str(x.get("code"))):
            if r.get("d_v1_state") != "first_break_today":
                continue
            code, name = str(r.get("code")), str(r.get("name") or "")
            is_st = "ST" in name.upper()
            weight = float(st_w) if (is_st and st_w is not None) else w
            _try_buy("d_v1", code, r, weight,
                     "D_V1 首次收盘跌破月初价，次日开盘买"
                     + ("（ST 再半）" if is_st and st_w is not None else ""))

    # D_V2：方法论 §4.5 只提供观察信号；仓位/排序/退出未经补充论证，不自动下单。
    st = strategies.get("d_v2") or {}
    if st.get("enabled"):
        for r in sorted(rep.get("rows") or [], key=lambda x: str(x.get("code"))):
            if r.get("d_v2_signal"):
                excluded.append({
                    "code": str(r.get("code")), "name": r.get("name"),
                    "strategy": "d_v2", "signal_only": True,
                    "blocked_reason": "d_v2_signal_only",
                    "entry_ref": r.get("close"), "mtm_pct": r.get("mtm_pct"),
                    "mtm_prev": r.get("mtm_prev"),
                    "reason": "D_V2 MTM 上拐；全段位仅观察，不自动下单"})

    return {"buys": buys, "exits": exits, "holds": holds,
            "excluded": excluded, "warnings": warnings,
            "regime": regime, "segment_id": seg_id,
            "total_weight_cap": cap, "total_after": round(total, 2)}


def build_intraday_intents(rep: dict, rules: dict, held: list,
                           month_positions: list) -> dict:
    """盘中试探意图（INTRADAY 档，交易时段内调用）：只买不卖，B8 新信号买 1/3。

    - 报告自己的 S3 打法：盘中先 1/3 试探，收盘确认再补满（EOD build_intents 处理）；
    - 已持仓/本月已开过仓的票不重复试探；段规则（S4 不新买等）与 EOD 一致；
    - D 策略的触发语义基于收盘（D_V1 次日开盘买），不参与盘中试探。
    """
    rows = {str(r.get("code")): r for r in rep.get("rows") or [] if r.get("code")}
    regime = (rep.get("index_filter") or {}).get("regime") or "Bear"
    cap = float((rules.get("regime_total_cap") or {}).get(regime, 50.0))
    seg_id = str((rep.get("current_segment") or {}).get("id") or "")
    strategies = rules.get("strategies") or {}
    st = strategies.get("b8") or {}
    buys: list[dict] = []
    excluded: list[dict] = []
    warnings: list[str] = []
    if not new_buy_allowed(rules, seg_id):
        return {"buys": [], "excluded": [],
                "warnings": [f"段 {seg_id or '—'} 全策略禁止新开仓/加仓"],
                "regime": regime, "segment_id": seg_id,
                "total_weight_cap": cap,
                "total_after": round(sum(float(h.get("weight") or 0.0) for h in held), 2)}
    if not st.get("enabled"):
        return {"buys": [], "excluded": [], "warnings": ["B8 未启用"]}
    allowed = set(st.get("buy_segments") or [])
    if seg_id not in allowed:
        return {"buys": [], "excluded": [], "warnings": [f"段 {seg_id or '—'} 不允许 B8 新买"]}
    ratio = float(st.get("probe_ratio", 1.0 / 3.0))
    probe_w = round(float(st.get("per_name_weight", 10.0)) * ratio, 2)
    total = round(sum(float(h.get("weight") or 0.0) for h in held), 2)
    held_codes = {str(h.get("code")) for h in held}
    opened = {(str(m.get("strategy")), str(m.get("code"))) for m in month_positions}
    cands = [r for r in (rep.get("rows") or [])
             if r.get("group") == "B" and r.get("b_tier") == "B8"
             and r.get("b_tier_action") == "执行" and not r.get("no_chase")]
    for r in sorted(cands, key=lambda x: -float(x.get("mtd_pct") or 0)):
        code = str(r.get("code"))
        if code in held_codes:
            excluded.append({"code": code, "strategy": "b8",
                             "reason": "已持仓（含今日已试探）"})
            continue
        if ("b8", code) in opened:
            excluded.append({"code": code, "strategy": "b8",
                             "reason": "本月已开过仓（不重复）"})
            continue
        if total + probe_w > cap + 1e-9:
            excluded.append({"code": code, "strategy": "b8",
                             "reason": f"总仓 {total:.1f}%+{probe_w:.1f}% 超 {cap:.0f}% 上限"})
            continue
        entry_ref = float(r.get("close"))
        buys.append({
            "code": code, "name": r.get("name"), "strategy": "b8",
            "weight": probe_w, "probe": True,
            "reason": f"B8 盘中试探（{ratio:.0%}，MTD {r.get('mtd_pct')}%）",
            "entry_ref": entry_ref,
            "stop": round(entry_ref * (1 + float(st.get("stop_pct", -5.0)) / 100.0), 2),
            "target": None,
            "stop_pct": float(st.get("stop_pct", -5.0)),
            "target_pct": None,
            "break_level": r.get("break_level"),
            "approx_entry": True,
        })
        total = round(total + probe_w, 2)
        held_codes.add(code)   # 同一档多票不冲突；跨档由台账去重
    return {"buys": buys, "excluded": excluded, "warnings": warnings,
            "regime": regime, "segment_id": seg_id,
            "total_weight_cap": cap, "total_after": total}


def build_plan(rep: dict, rules: dict, held: list, month_positions: list,
               execute_trade_date: str, portfolio_label: str,
               source_report: str) -> dict:
    """组装次日调仓计划（纯函数；created_at_bjt 由引擎填）。"""
    intents = build_intents(rep, rules, held, month_positions)
    sh = semantic_hash(rep)
    plan_id = f"{portfolio_label}|{execute_trade_date}|{sh[:16]}"
    target: dict[str, float] = {}
    for h in intents["holds"]:
        target[h["code"]] = round(float(h.get("weight_target") or h.get("weight") or 0.0), 2)
    for b in intents["buys"]:
        target[b["code"]] = round(float(b["weight"]), 2)
    return {
        "plan_schema": PLAN_SCHEMA,
        "plan_id": plan_id,
        "month_id": rep.get("month_id"),
        "signal_date": rep.get("asof_date"),
        "execute_trade_date": execute_trade_date,
        "source_report": source_report,
        "source_built_at": rep.get("generated_at_bjt"),
        "config_hash": rules_hash(rules),
        "signal_hash": sh,
        "regime": intents["regime"],
        "segment_id": intents["segment_id"],
        "total_weight_cap": intents["total_weight_cap"],
        "buys": intents["buys"],
        "exits": intents["exits"],
        "holds": intents["holds"],
        "excluded": intents["excluded"],
        "warnings": intents["warnings"],
        "target_weights": target,
        "status": "planned",
        "created_at_bjt": None,
    }
