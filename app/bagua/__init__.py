# -*- coding: utf-8 -*-
"""冻结起卦器（OHLC 数字起卦）只读副本，公式与 Analysis-of-A-shares 逐字节一致。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .calculator import BaguaCalculator, BaguaKnowledge, BaguaResult, TRIGRAMS
from .consensus import gua_side, gaodao_side, consensus
from .gaodao import (gaodao_item, gaodao_text, gaodao_category,
                     gaodao_is_fallback, gaodao_coverage)

_HERE = Path(__file__).resolve().parent
_MANIFEST = json.loads((_HERE / "manifest.json").read_text(encoding="utf-8"))

# 启动即校验：副本被改动时拒绝加载，避免卦象口径悄悄漂移
for _name, _meta in _MANIFEST["files"].items():
    _digest = hashlib.sha256((_HERE / _name).read_bytes()).hexdigest()
    if _digest != _meta["sha256"]:
        raise RuntimeError(f"bagua 冻结副本校验失败：{_name} sha256 不符")

_KNOWLEDGE = BaguaKnowledge(_HERE / "bagua_384.json")
_CALC = BaguaCalculator(_KNOWLEDGE)


def cast_ohlc(open_price, high_price, low_price, close_price) -> BaguaResult:
    return _CALC.calculate(open_price=open_price, high_price=high_price,
                          low_price=low_price, close_price=close_price)


__all__ = ["BaguaCalculator", "BaguaKnowledge", "BaguaResult", "TRIGRAMS",
           "gua_side", "gaodao_side", "consensus", "cast_ohlc",
           "gaodao_item", "gaodao_text", "gaodao_category",
           "gaodao_is_fallback", "gaodao_coverage"]