"""十神服务层：把纯函数包装成 API 可直接返回的受控对象。

状态语义（PLAN P2.2）：
  OK                    有合法日主与合法流月柱
  UNAVAILABLE_PROFILE   没有本命档案 / 日主非法 —— 禁止猜测十神
  INVALID_PILLAR        流月柱不是合法六十甲子

任何情况下都不返回 weight / good_or_bad / 行情预测字段。
"""
from __future__ import annotations

from ten_god_rules import (
    BRANCH_ELEMENT, HIDDEN_RULE_VERSION, RULE_VERSION, STEM_ELEMENT, STEM_IS_YANG,
    TenGodRuleError, hidden_role, hidden_stems, ten_god, validate_pillar,
)

PREDICTION = False
COMPUTED_FROM = "day_master_and_flow_pillar"

# 事实层状态（与十神状态分开）：对应 PLAN P2.2 的 NO_HISTORICAL_FACT / NO_VALID_PRICE_SAMPLE
FACT_HISTORICAL = "HISTORICAL_FACT"
FACT_NO_HISTORY = "NO_HISTORICAL_FACT"
FACT_NO_PRICE = "NO_VALID_PRICE_SAMPLE"


def fact_status(has_fact: bool, direction) -> str:
    """区分：有历史事实且有方向 / 无历史事实 / 有事实但无有效行情。"""
    if not has_fact:
        return FACT_NO_HISTORY
    if direction is None:
        return FACT_NO_PRICE
    return FACT_HISTORICAL


def _utc_none(status: str, reason: str, day_master=None, pillar=None) -> dict:
    return {
        "status": status,
        "reason": reason,
        "rule_version": RULE_VERSION,
        "hidden_rule_version": HIDDEN_RULE_VERSION,
        "day_master": day_master,
        "flow_pillar": pillar,
        "stem": None,
        "hidden_stems": [],
        "hidden_summary": "",
        "computed_from": COMPUTED_FROM,
        "prediction": PREDICTION,
    }


def _valid_day_master(day_master) -> bool:
    return isinstance(day_master, str) and len(day_master) == 1 and day_master in STEM_ELEMENT


def get_flow_stem_ten_god(day_master: str, flow_stem: str) -> dict:
    """流月天干十神。仅以本命日干为基准。"""
    if not _valid_day_master(day_master):
        return {"status": "UNAVAILABLE_PROFILE", "reason": "缺少合法本命日主",
                "day_master": day_master, "flow_stem": flow_stem,
                "ten_god": None, "rule_version": RULE_VERSION, "prediction": PREDICTION}
    try:
        god = ten_god(day_master, flow_stem)
    except TenGodRuleError as exc:
        return {"status": "INVALID_PILLAR", "reason": str(exc), "day_master": day_master,
                "flow_stem": flow_stem, "ten_god": None, "rule_version": RULE_VERSION,
                "prediction": PREDICTION}
    return {"status": "OK", "reason": "", "day_master": day_master, "flow_stem": flow_stem,
            "ten_god": god, "rule_version": RULE_VERSION, "prediction": PREDICTION}


def get_hidden_stem_ten_gods(day_master: str, flow_branch: str) -> dict:
    """流月地支藏干十神（多标签，顺序即源顺序，不带权重）。"""
    if not _valid_day_master(day_master):
        return {"status": "UNAVAILABLE_PROFILE", "reason": "缺少合法本命日主",
                "day_master": day_master, "flow_branch": flow_branch, "items": [],
                "rule_version": RULE_VERSION, "hidden_rule_version": HIDDEN_RULE_VERSION,
                "prediction": PREDICTION}
    try:
        stems = hidden_stems(flow_branch)
        total = len(stems)
        items = [{
            "order": i + 1,
            "role": hidden_role(i, total),
            "stem": stem,
            "element": STEM_ELEMENT[stem],
            "yinyang": "阳" if STEM_IS_YANG[stem] else "阴",
            "ten_god": ten_god(day_master, stem),
        } for i, stem in enumerate(stems)]
    except TenGodRuleError as exc:
        return {"status": "INVALID_PILLAR", "reason": str(exc), "day_master": day_master,
                "flow_branch": flow_branch, "items": [], "rule_version": RULE_VERSION,
                "hidden_rule_version": HIDDEN_RULE_VERSION, "prediction": PREDICTION}
    return {"status": "OK", "reason": "", "day_master": day_master,
            "flow_branch": flow_branch, "branch_element": BRANCH_ELEMENT[flow_branch],
            "items": items, "rule_version": RULE_VERSION,
            "hidden_rule_version": HIDDEN_RULE_VERSION, "prediction": PREDICTION}


def hidden_summary(items: list[dict]) -> str:
    """表格用的紧凑文本：壬·偏财 / 甲·七杀。"""
    return " / ".join(f"{it['stem']}·{it['ten_god']}" for it in items)


def get_pillar_ten_gods(day_master, pillar) -> dict:
    """整根流月柱的十神对象（P1.2 契约）。"""
    if not _valid_day_master(day_master):
        return _utc_none("UNAVAILABLE_PROFILE", "缺少合法本命日主", day_master, pillar)
    if not isinstance(pillar, str) or len(pillar) != 2:
        return _utc_none("INVALID_PILLAR", f"流月柱必须是两个字：{pillar!r}",
                         day_master, pillar)
    try:
        stem, branch = validate_pillar(pillar)
    except TenGodRuleError as exc:
        return _utc_none("INVALID_PILLAR", str(exc), day_master, pillar)

    hidden = get_hidden_stem_ten_gods(day_master, branch)
    return {
        "status": "OK",
        "reason": "",
        "rule_version": RULE_VERSION,
        "hidden_rule_version": HIDDEN_RULE_VERSION,
        "day_master": day_master,
        "flow_pillar": pillar,
        "stem": {"stem": stem, "element": STEM_ELEMENT[stem],
                 "yinyang": "阳" if STEM_IS_YANG[stem] else "阴",
                 "ten_god": ten_god(day_master, stem)},
        "hidden_stems": hidden["items"],
        "hidden_summary": hidden_summary(hidden["items"]),
        "computed_from": COMPUTED_FROM,
        "prediction": PREDICTION,
    }


def attach_pillar_ten_gods(row: dict, day_master, pillar, prefix: str = "") -> dict:
    """给已有历史行追加十神对象，不覆盖旧字段。"""
    obj = get_pillar_ten_gods(day_master, pillar)
    row[prefix + "ten_god"] = obj
    row[prefix + "flow_hidden_stem_ten_gods"] = obj["hidden_stems"]
    row[prefix + "hidden_stem_summary"] = obj["hidden_summary"]
    return row
