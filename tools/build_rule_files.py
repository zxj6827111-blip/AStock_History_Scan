"""一次性生成十神规则数据文件（只读上游，不 import 上游代码）。

产物：
  app/rules/ten_god_reference_v1.json      100 组「日主 × 流月天干」冻结映射（校验用）
  app/rules/ten_god_hidden_stems_v1.json   12 地支藏干序列（本地独立副本，带 provenance）

本脚本不写任何 SQLite、不修改运行工程。上游只按文本读取。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE.parent / "app"
RULES = APP / "rules"
RULES.mkdir(parents=True, exist_ok=True)

ROOT = Path("/Volumes/workSSD/software development/AStock_History_Scan")
REF_JSON = ROOT / "v13_shadow_ready_20261018/release_candidate/rules/V121_verified_reference.json"
RESEARCH = Path("/Volumes/workSSD/software development/stock-metaphysics-platform")
RESEARCH_CONST = RESEARCH / "src/core/constants.py"

REFERENCE_RULE_VERSION = "tg-local-v1.0.0"
HIDDEN_RULE_VERSION = "tg-hidden-v1.0.0"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_reference() -> dict:
    raw = json.loads(REF_JSON.read_text(encoding="utf-8"))
    mapping = raw["ten_god_day_master_to_flow"]
    assert len(mapping) == 100, f"冻结映射应为 100 组，实际 {len(mapping)}"
    entries = {}
    for key, god in mapping.items():
        dm, other = key.split(">")
        entries[f"{dm}>{other}"] = god
    return {
        "rule_version": REFERENCE_RULE_VERSION,
        "purpose": "十神纯函数交叉校验（不是运行时唯一来源；运行时由五行+阴阳推导）",
        "source": {
            "project": "AStock_History_Scan/v13_shadow_ready_20261018",
            "file": str(REF_JSON),
            "key": "ten_god_day_master_to_flow",
            "sha256": sha256_file(REF_JSON),
            "note": "逐项复制为本地只读数据副本，不 import 上游引擎。",
        },
        "entries": entries,
    }


# 与研究平台 BRANCH_HIDDEN_STEMS 逐项对照的候选基线。
# 这里显式写死而不是运行时读取，是为了让本项目的规则不随上游项目变化。
CANDIDATE_HIDDEN = {
    "子": ["癸"],
    "丑": ["己", "癸", "辛"],
    "寅": ["甲", "丙", "戊"],
    "卯": ["乙"],
    "辰": ["戊", "乙", "癸"],
    "巳": ["丙", "庚", "戊"],
    "午": ["丁", "己"],
    "未": ["己", "丁", "乙"],
    "申": ["庚", "壬", "戊"],
    "酉": ["辛"],
    "戌": ["戊", "辛", "丁"],
    "亥": ["壬", "甲"],
}


def verify_against_research() -> None:
    """解析上游源码里的字面量表，逐支比对候选表，防止抄错顺序。"""
    import ast

    text = RESEARCH_CONST.read_text(encoding="utf-8")
    start = text.index("BRANCH_HIDDEN_STEMS")
    block = text[text.index("{", start):text.index("}", start) + 1]
    upstream = ast.literal_eval(block)
    assert list(upstream) == list(CANDIDATE_HIDDEN), "地支顺序与研究平台不一致"
    for branch, stems in CANDIDATE_HIDDEN.items():
        assert tuple(stems) == tuple(upstream[branch]), (
            f"研究平台藏干表与候选不一致：{branch} -> {stems} vs {upstream[branch]}")


def build_hidden() -> dict:
    verify_against_research()
    return {
        "rule_version": HIDDEN_RULE_VERSION,
        "scope": "流月地支藏干十神展示；不含权重、不含吉凶评分、不做未来预测",
        "order_policy": ("数组下标即源顺序：index 0 = 本气；两支时 index 1 = 其他藏干；"
                         "三支时 index 1/2 = 其他藏干1/2。本版不把数组位置解释为中气/余气的"
                         "传统定论，也不设任何数值权重。"),
        "source": {
            "project": "stock-metaphysics-platform",
            "file": str(RESEARCH_CONST),
            "symbol": "BRANCH_HIDDEN_STEMS",
            "git_head": "decb789948b785cb2baf9a3d0f9edc23f9ed6e89",
            "sha256": sha256_file(RESEARCH_CONST),
            "note": ("按 PLAN R-04 逐项核对后复制为本地独立纯数据表；不 import 上游、"
                     "不复制其 0.6/0.3/0.1 权重函数。"),
        },
        "branches": [{"branch": b, "stems": list(s)} for b, s in CANDIDATE_HIDDEN.items()],
    }


def main() -> None:
    ref = build_reference()
    hid = build_hidden()
    (RULES / "ten_god_reference_v1.json").write_text(
        json.dumps(ref, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (RULES / "ten_god_hidden_stems_v1.json").write_text(
        json.dumps(hid, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("wrote", RULES / "ten_god_reference_v1.json", len(ref["entries"]), "entries")
    print("wrote", RULES / "ten_god_hidden_stems_v1.json", len(hid["branches"]), "branches")
    for f in (RULES / "ten_god_reference_v1.json", RULES / "ten_god_hidden_stems_v1.json"):
        print("sha256", f.name, sha256_file(f))


if __name__ == "__main__":
    main()
