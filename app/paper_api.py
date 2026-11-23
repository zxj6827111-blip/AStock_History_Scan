"""模拟盘只读 API：读 data/paper_public/ 下的脱敏快照。

快照由 paper_trade 引擎生成（daily_live_job 挂点或 CLI 手动）；本模块只读产物，
不连接雪球、不打开任何业务库、不写任何文件（与 survivor_api 同一模式）。
环境变量 BAZI_PAPER_PUBLIC_DIR 可覆盖快照目录（沙箱验收用）。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from paper_trade import config as paper_config


class PaperUnavailable(Exception):
    """快照缺失/损坏（503）。"""


class PaperError(Exception):
    """参数错误（400）。"""


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _public_dir() -> Path:
    d = Path(os.environ.get("BAZI_PAPER_PUBLIC_DIR",
                            str(paper_config.public_dir())))
    if not d.is_dir():
        raise PaperUnavailable("模拟盘快照目录不存在（引擎未启用或未生成）")
    return d


def _read_json(name: str) -> dict:
    p = _public_dir() / name
    if not p.is_file():
        raise PaperUnavailable(f"缺少快照：{name}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PaperUnavailable(f"快照无法读取：{name}") from exc


def latest() -> dict:
    return _read_json("latest.json")


def summary() -> dict:
    d = latest()
    return {"schema": "paper_summary_v1",
            "generated_at_bjt": d.get("generated_at_bjt"),
            "synced_at": d.get("synced_at"),
            "sync_ok": d.get("sync_ok"),
            "stale": d.get("stale"),
            "summary": d.get("summary"),
            "warnings": d.get("warnings")}


def positions() -> dict:
    d = latest()
    return {"generated_at_bjt": d.get("generated_at_bjt"),
            "synced_at": d.get("synced_at"),
            "stale": d.get("stale"),
            "positions": d.get("positions") or []}


def plans(q: dict) -> dict:
    d = latest()
    detail = None
    date = (q.get("date") or [""])[0]
    if date:
        if not _DATE_RE.match(date):
            raise PaperError("date 参数需 YYYY-MM-DD")
        p = _public_dir() / "plans" / f"{date}.json"
        if p.is_file():
            try:
                detail = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                detail = None
        if detail is None:
            raise PaperUnavailable(f"缺少该日计划快照：{date}.json")
    else:
        try:
            detail = _read_json("plans/latest.json")
        except PaperUnavailable:
            detail = None
    return {"generated_at_bjt": d.get("generated_at_bjt"),
            "plans": d.get("plans_recent") or [],
            "detail": detail}


def trades() -> dict:
    d = latest()
    return {"generated_at_bjt": d.get("generated_at_bjt"),
            "trades": d.get("trades_recent") or []}


def equity() -> dict:
    p = _public_dir() / "nav.jsonl"
    if not p.is_file():
        raise PaperUnavailable("缺少快照：nav.jsonl")
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return {"equity": rows}


def dispatch(path: str, q: dict) -> dict:
    if path == "/api/paper/latest":
        return latest()
    if path == "/api/paper/summary":
        return summary()
    if path == "/api/paper/positions":
        return positions()
    if path == "/api/paper/plans":
        return plans(q)
    if path == "/api/paper/trades":
        return trades()
    if path == "/api/paper/equity":
        return equity()
    raise PaperError("未知的模拟盘接口")
