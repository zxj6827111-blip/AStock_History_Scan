"""生存者报告只读 API：读 survivor_report_v1/reports/ 下的 JSON/Markdown。

报告由 daily_live_job 挂点在切片重建成功后自动生成（盘中 30m 档=半小时版、
午盘档=午盘版、晚间档=日终版）；本模块只读产物，不碰任何业务库。
环境变量 BAZI_SURVIVOR_DIR 可覆盖模块目录（沙箱验收用）。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SURVIVOR_DIR = Path(os.environ.get("BAZI_SURVIVOR_DIR",
                                   str(ROOT / "survivor_report_v1")))
SLOT_RE = re.compile(r"^survivor_(\d{4}-\d{2}-\d{2})_(\d{4})_([A-Z]+)\.json$")


class SurvivorUnavailable(Exception):
    """报告产物缺失（503）。"""


class SurvivorError(Exception):
    """参数错误（400）。"""


def _report_dir() -> Path:
    d = Path(SURVIVOR_DIR) / "reports"
    if not d.is_dir():
        raise SurvivorUnavailable(f"报告目录不存在：{d}")
    return d


def _read_json(path: Path) -> dict:
    if not path.is_file():
        raise SurvivorUnavailable(f"缺少数据文件：{path.name}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SurvivorUnavailable(f"数据文件无法读取：{path.name}") from exc


def latest() -> dict:
    rep = _read_json(_report_dir() / "latest.json")
    rep["slots_today"] = _slots_of(rep.get("asof_date") or "")
    return rep


def _slots_of(date_str: str) -> list[dict]:
    """某日的全部档位（按时间升序），供网页切换查看。"""
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str or ""):
        return []
    out = []
    for f in _report_dir().glob(f"survivor_{date_str}_*.json"):
        m = SLOT_RE.match(f.name)
        if m:
            out.append({"date": m.group(1), "hhmm": m.group(2),
                        "session": m.group(3), "file": f.name})
    out.sort(key=lambda x: (x["hhmm"], x["session"]))
    return out


def slots(q: dict) -> dict:
    date_str = (q.get("date") or [""])[0]
    if not date_str:
        raise SurvivorError("缺少 date 参数（YYYY-MM-DD）")
    return {"date": date_str, "slots": _slots_of(date_str)}


def dates(_q: dict) -> dict:
    """所有留有报告的交易日（降序）及档位数，供网页直接选历史日期。"""
    counts: dict[str, int] = {}
    for f in _report_dir().glob("survivor_*.json"):
        m = SLOT_RE.match(f.name)
        if m:
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    return {"dates": [{"date": d, "slots": n}
                      for d, n in sorted(counts.items(), reverse=True)]}


def slot(q: dict) -> dict:
    date_str = (q.get("date") or [""])[0]
    hhmm = (q.get("hhmm") or [""])[0]
    session = (q.get("session") or [""])[0]
    if not (re.match(r"^\d{4}-\d{2}-\d{2}$", date_str)
            and re.match(r"^\d{4}$", hhmm) and re.match(r"^[A-Z]+$", session)):
        raise SurvivorError("参数需 date=YYYY-MM-DD&hhmm=HHMM&session=SESSION")
    return _read_json(_report_dir() / f"survivor_{date_str}_{hhmm}_{session}.json")


def export_md(q: dict) -> tuple[bytes, str]:
    name = (q.get("name") or ["latest.md"])[0]
    if name != "latest.md" and not SLOT_RE.match(name.replace(".md", ".json")):
        raise SurvivorError("非法文件名")
    p = _report_dir() / name
    if not p.is_file():
        raise SurvivorUnavailable(f"缺少文件：{name}")
    try:
        data = p.read_bytes()
    except OSError as exc:
        raise SurvivorUnavailable(f"文件无法读取：{name}") from exc
    return data, name


def dispatch(path: str, q: dict) -> dict:
    if path == "/api/survivor/latest":
        return latest()
    if path == "/api/survivor/dates":
        return dates(q)
    if path == "/api/survivor/slots":
        return slots(q)
    if path == "/api/survivor/slot":
        return slot(q)
    raise SurvivorError("未知的生存者报告接口")
