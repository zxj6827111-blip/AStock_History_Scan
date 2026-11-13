# -*- coding: utf-8 -*-
"""《高岛易断》营商断语 sidecar 读取（本地冻结副本，无外部依赖）。

数据文件 bagua_gaodao.json 与 Analysis-of-A-shares 逐字节一致（见 manifest.json），
按 state_id（如 "04-3"）取断语。fail-open：文件缺失/损坏一律返回空，绝不影响卦象主流程
（缺失时前端仍以 market_judgement 兜底）。

与 consensus.py 分工：本模块只负责「取断语文本」，好/差语气与双好/双差共识由 consensus.py 判。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
_DATA = _HERE / "bagua_gaodao.json"
_FALLBACK_FMT = "（{category}）"

_INDEX: Optional[dict] = None


def _index() -> dict:
    global _INDEX
    if _INDEX is None:
        try:
            data = json.loads(_DATA.read_text(encoding="utf-8"))
            _INDEX = data if (isinstance(data, dict)
                              and isinstance(data.get("by_state_id"), dict)) else {}
        except Exception:
            _INDEX = {}
    return _INDEX


def _by_state() -> dict:
    return _index().get("by_state_id") or {}


def _primary() -> frozenset:
    pol = _index().get("policy") or {}
    prim = pol.get("primary")
    return frozenset(str(x) for x in prim) if isinstance(prim, list) else frozenset()


def gaodao_item(state_id: Optional[str]) -> Optional[dict]:
    """按 state_id 取断语条目 {text, category, gua_name, yao_name}；未命中返回 None。"""
    if not state_id:
        return None
    it = _by_state().get(str(state_id))
    if not isinstance(it, dict) or not it.get("text"):
        return None
    return it


def gaodao_category(state_id: Optional[str]) -> str:
    it = gaodao_item(state_id)
    return str((it or {}).get("category") or "")


def gaodao_is_fallback(state_id: Optional[str]) -> bool:
    """该爻断语是否来自兜底类别（时运/功名等非营商类）。无断语返回 False。"""
    it = gaodao_item(state_id)
    if not it:
        return False
    cat = str(it.get("category") or "")
    prim = _primary()
    if not cat or not prim:
        return False
    return cat not in prim


def gaodao_text(state_id: Optional[str]) -> str:
    """展示串：非营商类别追加类别后缀，避免误读成营商断语；无断语返回空串。"""
    it = gaodao_item(state_id)
    if not it:
        return ""
    text = str(it.get("text") or "").strip()
    if not text:
        return ""
    cat = str(it.get("category") or "").strip()
    if not cat or cat in _primary():
        return text
    return f"{text}{_FALLBACK_FMT.format(category=cat)}"


def gaodao_coverage() -> Optional[dict]:
    """覆盖度摘要（供页面脚注）；sidecar 不可用返回 None。"""
    d = _index()
    if not d:
        return None
    counts = d.get("counts") or {}
    return {"total": counts.get("total"), "state_total": counts.get("state_total"),
            "primary": counts.get("primary"), "fallback": counts.get("fallback"),
            "missing": counts.get("missing")}