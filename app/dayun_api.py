"""V1.1 大运扩展 API 适配层（十神版 8737 服务内使用）。

只读：
  * main 连接由 server.dispatch 以 mode=ro 打开；
  * 扩展库由 dayun_query.connect_ext 以 mode=ro 打开。
缺扩展库/损坏时返回结构化状态，绝不让主页面 500。
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import config

EXT_ROOT = config.ROOT / "bazi_month_relation_local_v1" / "extensions" / "gender_dayun_v1"
ETF_ROOT = config.ROOT / "bazi_month_relation_etf_v1" / "extensions" / "gender_dayun_etf_v1"
EXT_DBS = {
    "ashare": Path(os.environ.get(
        "BAZI_DAYUN_ASHARE_DB",
        str(EXT_ROOT / "database" / "gender_dayun_extension_v1_proxy.sqlite"))),
    "etf": Path(os.environ.get(
        "BAZI_DAYUN_ETF_DB",
        str(ETF_ROOT / "database" / "gender_dayun_etf_extension_v1_proxy.sqlite"))),
}
_SRC = str(EXT_ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import dayun_query as Q  # noqa: E402


def _guard(ext_db: Path, fn):
    try:
        ext = Q.connect_ext(ext_db)
        try:
            return fn(ext)
        finally:
            ext.close()
    except Q.ExtensionUnavailable as exc:
        return {"extension_status": "UNAVAILABLE",
                "message": f"扩展暂不可用：{exc}",
                "hint": "主 V1 查询不受影响；构建扩展库后可恢复。"}
    except Q.ExtensionCorrupt as exc:
        return {"extension_status": "CORRUPT",
                "message": f"扩展库损坏：{exc}",
                "hint": "请用 RUNBOOK 回滚或重建扩展库。"}
    except (sqlite3.DatabaseError, ValueError, KeyError, TypeError) as exc:
        return {"extension_status": "ERROR",
                "message": f"{type(exc).__name__}: {exc}"}


def _steps(ext: sqlite3.Connection, identity: str):
    p = ext.execute(
        "SELECT direction_status, timeline_status FROM gender_dayun_profiles"
        " WHERE identity_code=?", (identity,)).fetchone()
    if p is None:
        return {"extension_status": "NO_PROFILE_ROW", "steps": [], "candidates": {}}
    steps = [dict(r) for r in ext.execute("""
      SELECT step_no, method_id, ganzhi, stem, branch, stem_element, branch_element,
             dayun_stem_tengod, tengod_status, start_at, end_at, start_precision,
             age_years_at_start, start_age_lunar, end_age_lunar,
             start_calendar_year, end_calendar_year, status
      FROM dayun_steps WHERE identity_code=? ORDER BY step_no""", (identity,))]
    candidates = [dict(r) for r in ext.execute("""
      SELECT candidate_direction, step_no, month_pillar, ganzhi, stem, branch,
             dayun_stem_tengod, is_effective, sequence_status
      FROM dayun_candidate_steps WHERE identity_code=?
      ORDER BY candidate_direction, step_no""", (identity,))]
    out: dict[str, list] = {"FORWARD": [], "REVERSE": []}
    for c in candidates:
        out.setdefault(c["candidate_direction"], []).append(c)
    return {"extension_status": "AVAILABLE", "steps": steps, "candidates": out,
            "direction_status": p["direction_status"],
            "timeline_status": p["timeline_status"]}


def _with_dataset(out, dataset: str):
    if isinstance(out, dict):
        out.setdefault("dataset", dataset)
    return out


def dispatch(main: sqlite3.Connection, path: str, g, dataset: str) -> dict:
    ext_db = EXT_DBS.get(dataset)
    if ext_db is None:
        return {"extension_status": "NOT_APPLICABLE",
                "message": f"未知数据集 {dataset!r}；可用：{', '.join(EXT_DBS)}"}
    if path == "/api/dayun/meta":
        return _with_dataset(_guard(ext_db, lambda ext: Q.meta(ext, ext_db)), dataset)
    identity = (g("id") or "").strip()
    if not identity:
        return {"extension_status": "BAD_REQUEST", "message": "缺少 id 参数"}
    if path == "/api/dayun/profile":
        return _with_dataset(
            _guard(ext_db, lambda ext: Q.profile(ext, main, identity)), dataset)
    if path == "/api/dayun/steps":
        return _with_dataset(
            _guard(ext_db, lambda ext: _steps(ext, identity)), dataset)
    if path == "/api/dayun/months":
        return _with_dataset(_guard(ext_db, lambda ext: Q.months(
            ext, main, identity,
            g("step", "ALL") or "ALL", g("policy", "ALL") or "ALL",
            g("status", "ALL") or "ALL")), dataset)
    if path == "/api/dayun/stats":
        return _with_dataset(_guard(ext_db, lambda ext: Q.stats(
            ext, main, identity, g("step", "ALL") or "ALL",
            g("policy", "ALL") or "ALL")), dataset)
    return {"extension_status": "UNKNOWN_ROUTE", "message": path}
