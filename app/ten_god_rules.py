"""十神规则纯函数层 —— 规则版本 tg-local-v1.0.0。

设计约束（对应 PLAN R-01 / R-04 / P1）：
  * 唯一基准是本命日干 day_master_stem，不接受年干/月干/流月干冒充；
  * 十神由五行生克 + 阴阳推导，不依赖数据库、不做 IO 写入；
  * 12 支藏干是本地独立纯数据表（app/rules/ten_god_hidden_stems_v1.json），
    保留源顺序，不设权重、不判吉凶；
  * 非法输入抛 TenGodRuleError，由上层转换成受控状态，不猜测结果。
"""
from __future__ import annotations

import json
from pathlib import Path

RULE_VERSION = "tg-local-v1.0.0"
HIDDEN_RULE_VERSION = "tg-hidden-v1.0.0"

RULES_DIR = Path(__file__).resolve().parent / "rules"
HIDDEN_PATH = RULES_DIR / "ten_god_hidden_stems_v1.json"
REFERENCE_PATH = RULES_DIR / "ten_god_reference_v1.json"

STEMS: tuple[str, ...] = tuple("甲乙丙丁戊己庚辛壬癸")
BRANCHES: tuple[str, ...] = tuple("子丑寅卯辰巳午未申酉戌亥")
TEN_GODS: tuple[str, ...] = (
    "比肩", "劫财", "食神", "伤官", "偏财", "正财", "七杀", "正官", "偏印", "正印")

STEM_ELEMENT: dict[str, str] = {
    "甲": "木", "乙": "木", "丙": "火", "丁": "火", "戊": "土",
    "己": "土", "庚": "金", "辛": "金", "壬": "水", "癸": "水",
}
# True = 阳干（甲丙戊庚壬），False = 阴干（乙丁己辛癸）
STEM_IS_YANG: dict[str, bool] = {
    "甲": True, "乙": False, "丙": True, "丁": False, "戊": True,
    "己": False, "庚": True, "辛": False, "壬": True, "癸": False,
}
BRANCH_ELEMENT: dict[str, str] = {
    "子": "水", "丑": "土", "寅": "木", "卯": "木", "辰": "土", "巳": "火",
    "午": "火", "未": "土", "申": "金", "酉": "金", "戌": "土", "亥": "水",
}

# 五行相生：木→火→土→金→水→木
ELEMENT_GENERATES: dict[str, str] = {
    "木": "火", "火": "土", "土": "金", "金": "水", "水": "木"}
# 五行相克：木克土、土克水、水克火、火克金、金克木
ELEMENT_CONTROLS: dict[str, str] = {
    "木": "土", "土": "水", "水": "火", "火": "金", "金": "木"}


class TenGodRuleError(ValueError):
    """非法天干 / 地支 / 干支组合。"""


def _require_stem(stem: str, label: str) -> str:
    if not isinstance(stem, str) or stem not in STEM_ELEMENT:
        raise TenGodRuleError(f"{label}不是合法天干：{stem!r}")
    return stem


def ten_god(day_master: str, other_stem: str) -> str:
    """返回 other_stem 相对日主 day_master 的十神（子平通行规则）。"""
    _require_stem(day_master, "日主")
    _require_stem(other_stem, "对象天干")

    dm_el, other_el = STEM_ELEMENT[day_master], STEM_ELEMENT[other_stem]
    same_polarity = STEM_IS_YANG[day_master] == STEM_IS_YANG[other_stem]

    if dm_el == other_el:
        return "比肩" if same_polarity else "劫财"
    if ELEMENT_GENERATES[dm_el] == other_el:          # 我生
        return "食神" if same_polarity else "伤官"
    if ELEMENT_CONTROLS[dm_el] == other_el:           # 我克
        return "偏财" if same_polarity else "正财"
    if ELEMENT_CONTROLS[other_el] == dm_el:           # 克我
        return "七杀" if same_polarity else "正官"
    if ELEMENT_GENERATES[other_el] == dm_el:          # 生我
        return "偏印" if same_polarity else "正印"
    raise TenGodRuleError(f"无法判定十神：日主 {day_master} / 对象 {other_stem}")


def validate_pillar(pillar: str) -> tuple[str, str]:
    """校验流月柱：两字、合法干支、且阴阳同序（60 甲子内）。"""
    if not isinstance(pillar, str) or len(pillar) != 2:
        raise TenGodRuleError(f"流月柱必须是两个字：{pillar!r}")
    stem, branch = pillar[0], pillar[1]
    _require_stem(stem, "流月天干")
    if branch not in BRANCHES:
        raise TenGodRuleError(f"流月地支非法：{branch!r}")
    stem_index, branch_index = STEMS.index(stem), BRANCHES.index(branch)
    if stem_index % 2 != branch_index % 2:
        raise TenGodRuleError(f"干支阴阳不匹配，不属于六十甲子：{pillar!r}")
    return stem, branch


def _load_hidden_table() -> tuple[dict[str, tuple[str, ...]], dict]:
    raw = json.loads(HIDDEN_PATH.read_text(encoding="utf-8"))
    table: dict[str, tuple[str, ...]] = {}
    for item in raw["branches"]:
        branch = item["branch"]
        stems = tuple(item["stems"])
        if branch not in BRANCHES:
            raise TenGodRuleError(f"藏干表出现非法地支：{branch!r}")
        if branch in table:
            raise TenGodRuleError(f"藏干表地支重复：{branch}")
        if not stems:
            raise TenGodRuleError(f"藏干表缺藏干：{branch}")
        if len(set(stems)) != len(stems):
            raise TenGodRuleError(f"藏干表同一支藏干重复：{branch}")
        for stem in stems:
            _require_stem(stem, f"{branch}支藏干")
        table[branch] = stems
    missing = [b for b in BRANCHES if b not in table]
    if missing:
        raise TenGodRuleError(f"藏干表缺支：{missing}")
    return table, raw


HIDDEN_STEMS, HIDDEN_META = _load_hidden_table()


def hidden_stems(branch: str) -> tuple[str, ...]:
    if branch not in HIDDEN_STEMS:
        raise TenGodRuleError(f"地支非法：{branch!r}")
    return HIDDEN_STEMS[branch]


def hidden_role(index: int, total: int) -> str:
    """按源顺序命名；不把数组位置包装成中气/余气的传统定论。"""
    if total == 1:
        return "本气"
    if total == 2:
        return "本气" if index == 0 else "其他藏干"
    if index == 0:
        return "本气"
    return f"其他藏干{index}"


def reference_mismatches() -> list[str]:
    """把冻结 100 格映射当校验夹具，返回与推导不一致的条目（应为空）。"""
    raw = json.loads(REFERENCE_PATH.read_text(encoding="utf-8"))
    bad = []
    for key, expected in raw["entries"].items():
        dm, other = key.split(">")
        actual = ten_god(dm, other)
        if actual != expected:
            bad.append(f"{key}: 推导={actual} 冻结={expected}")
    return bad


def rule_manifest() -> dict:
    """规则版本与来源摘要，供 API / 报告引用。"""
    return {
        "rule_version": RULE_VERSION,
        "hidden_rule_version": HIDDEN_RULE_VERSION,
        "hidden_source": HIDDEN_META.get("source", {}),
        "hidden_order_policy": HIDDEN_META.get("order_policy", ""),
        "reference_entries": 100,
        "reference_mismatches": len(reference_mismatches()),
    }
