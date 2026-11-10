"""流日关系影子库（N2）只读 API。

只读打开 daily_relation_shadow_fullmarket_v1.sqlite（mode=ro + PRAGMA query_only=ON），
不打开任何可写连接，不写任何文件。股票名称按需从生产主库 securities 表只读附加获取，
拿不到就只显示代码，不影响查询。

统计看板读 N2-5 导出的 CSV（n2_evidence/stats/），与报告逐数一致，不在请求路径里重算中位数。
"""
from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import threading
from pathlib import Path

try:  # 与 config.ROOT 同源：一个 BAZI_ROOT 就能把主库与影子库一起指到项目根
    import config as _config
    ROOT = Path(_config.ROOT)
except Exception:  # noqa: BLE001
    ROOT = Path(__file__).resolve().parents[2]
N2_ROOT = Path(os.environ.get(
    "BAZI_N2_ROOT", str(ROOT / "daily_relation_shadow_fullmarket_v1")))
N2_DB = Path(os.environ.get(
    "BAZI_N2_DB", str(N2_ROOT / "database/daily_relation_shadow_fullmarket_v1.sqlite")))
N2_STATS_DIR = Path(os.environ.get("BAZI_N2_STATS_DIR", str(N2_ROOT / "n2_evidence/stats")))
N2_ELIGIBLE_CSV = Path(os.environ.get(
    "BAZI_N2_ELIGIBLE_CSV", str(N2_ROOT / "n2_evidence/n2_02_eligible_identities.csv")))

CALIBERS = [
    ("MAIN", "主口径", "return_valid=1，5,131 只全部"),
    ("EXCL_DISPUTED", "敏感性A", "主口径再剔除 73 只边界争议身份"),
    ("MAIN_EXCL_ANOM", "主口径(排异常)", "主口径本身已把异常收益排除在分子分母外，数值与主口径相同"),
    ("INCL_SUSPECT", "敏感性C", "把 return_valid=0 的可疑收益计入分子与分母"),
]
STATS_FILES = {
    "overall": "n2_stats_overall.csv",
    "pillar": "n2_stats_day_pillar.csv",
    "stem": "n2_stats_day_stem.csv",
    "branch": "n2_stats_day_branch.csv",
    "relation_type": "n2_stats_relation_type.csv",
    "benchmark": "n2_stats_benchmark_pair.csv",
    "index": "n2_stats_index_experiment.csv",
}

FACT_SELECT = """
SELECT f.identity_code, f.trade_date, f.month_id, f.day_no, f.flow_day_pillar,
       f.flow_day_stem, f.flow_day_branch, f.qfq_open, f.qfq_close,
       f.open_close_return, f.day_body_direction, f.body_flat,
       f.data_quality_status, f.return_valid,
       t.official_ten_god, t.relation_types_json, t.confirmed_event_count,
       t.distinct_relation_type_count, t.candidate_structure_count
FROM daily_facts f JOIN relation_templates t USING(template_key)
"""


class N2Unavailable(Exception):
    """影子库缺失或不可读（503）：外置盘掉线时页面给明确提示，不伪造数据。"""


class N2Error(Exception):
    """参数错误（400）。"""


def _q(q: dict, key: str, default: str = "") -> str:
    values = q.get(key)
    return values[0] if values else default


def _int(raw, default: int, lo: int, hi: int) -> int:
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return min(max(n, lo), hi)


def connect() -> sqlite3.Connection:
    if not N2_DB.is_file():
        raise N2Unavailable(f"影子库不存在：{N2_DB.name}（先跑 N2-3 构建）")
    try:
        con = sqlite3.connect(N2_DB.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise N2Unavailable(f"影子库无法打开（外置盘可能已断开）：{exc}") from exc
    con.row_factory = sqlite3.Row
    try:
        import config
        main_db = Path(config.ASHARE_DB)
        if main_db.is_file() and main_db.resolve() != N2_DB.resolve():
            con.execute("ATTACH DATABASE ? AS base",
                        (main_db.resolve().as_uri() + "?mode=ro",))
    except Exception:  # noqa: BLE001
        # 名称只是锦上添花：生产主库不在场时照样能查，只是不显示名称
        pass
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA cache_size=-32768")
    con.execute("PRAGMA temp_store=FILE")
    return con


# ---------- 静态映射（争议身份 / 股票名称） ----------
_lock = threading.Lock()
_disputed_cache: dict = {"mtime": None, "set": frozenset()}


def disputed_set() -> frozenset[str]:
    try:
        mtime = N2_ELIGIBLE_CSV.stat().st_mtime
    except OSError:
        return frozenset()
    with _lock:
        if _disputed_cache["mtime"] == mtime:
            return _disputed_cache["set"]
        out = set()
        try:
            with N2_ELIGIBLE_CSV.open(encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    if row.get("boundary_bucket") == "BOUNDARY_DISPUTED":
                        out.add(row["identity_code"])
        except OSError:
            out = set()
        _disputed_cache["mtime"] = mtime
        _disputed_cache["set"] = frozenset(out)
        return _disputed_cache["set"]


def _names(con, codes: list[str]) -> dict[str, str]:
    if not codes:
        return {}
    try:
        marks = ",".join("?" * len(codes))
        rows = con.execute(f"SELECT identity_code, name FROM base.securities "
                           f"WHERE identity_code IN ({marks})", codes).fetchall()
        return {r[0]: r[1] for r in rows if r[1]}
    except sqlite3.Error:
        return {}


def _row_out(r, disputed: frozenset, names: dict[str, str]) -> dict:
    try:
        rels = json.loads(r["relation_types_json"])
    except (TypeError, ValueError):
        rels = []
    return {
        "code": r["identity_code"],
        "name": names.get(r["identity_code"], ""),
        "disputed": r["identity_code"] in disputed,
        "date": r["trade_date"],
        "month_id": r["month_id"] if "month_id" in r.keys() else None,
        "day_no": r["day_no"] if "day_no" in r.keys() else None,
        "pillar": r["flow_day_pillar"],
        "stem": r["flow_day_stem"] if "flow_day_stem" in r.keys() else None,
        "branch": r["flow_day_branch"] if "flow_day_branch" in r.keys() else None,
        "open": r["qfq_open"],
        "close": r["qfq_close"],
        "ret_pct": r["open_close_return"] * 100.0,
        "direction": r["day_body_direction"],
        "quality": r["data_quality_status"],
        "valid": int(r["return_valid"]),
        "ten_god": r["official_ten_god"],
        "relations": rels,
        "n_events": r["confirmed_event_count"],
        "n_types": r["distinct_relation_type_count"],
        "n_candidates": r["candidate_structure_count"],
    }


# ---------- 统计 CSV（mtime 缓存） ----------
_csv_cache: dict = {}


def _read_stats(name: str) -> list[dict]:
    rel = STATS_FILES.get(name)
    if not rel:
        raise N2Error(f"未知统计维度：{name}")
    path = N2_STATS_DIR / rel
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        raise N2Unavailable(f"统计文件缺失：{rel}（先跑 N2-5 导出）") from exc
    key = (name, mtime)
    with _lock:
        if key in _csv_cache:
            return _csv_cache[key]
        with path.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        for row in rows:
            for k, v in row.items():
                if k in ("caliber", "asset_class", "key", "relation_type", "exchange",
                         "benchmark_identity"):
                    continue
                row[k] = float(v) if v not in ("", None) else None
        _csv_cache[key] = rows
        return rows


def _num(row: dict, key: str):
    v = row.get(key)
    return None if v in (None, "") else float(v)


# ---------- 接口 ----------
def meta(con: sqlite3.Connection) -> dict:
    meta_rows = dict(con.execute("SELECT key, value FROM meta").fetchall())
    first, last = con.execute(
        "SELECT min(trade_date), max(trade_date) FROM daily_facts").fetchone()
    last_a = con.execute(
        "SELECT max(trade_date) FROM daily_facts WHERE asset_class='A_SHARE'").fetchone()[0]
    try:
        # 只报本机文件的修改时间，不做时区换算：本机时钟与北京时间不一致时，
        # 强行加 +08:00 会造出一个不存在的时刻（2026-10-31 实测过一次）。
        stats_mtime = max((N2_STATS_DIR / f).stat().st_mtime
                          for f in STATS_FILES.values())
        from datetime import datetime
        stats_at = datetime.fromtimestamp(stats_mtime).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError):
        stats_at = None
    rel_rows = _read_stats("relation_type")
    types = sorted({r["relation_type"] for r in rel_rows if r.get("relation_type")})
    return {
        "db": N2_DB.name,
        "db_bytes": int(meta_rows.get("db_bytes") or 0),
        "build_status": meta_rows.get("build_status"),
        "build_tool": meta_rows.get("build_tool"),
        "built_at_bjt": meta_rows.get("built_at_bjt"),
        "published_at_bjt": meta_rows.get("published_at_bjt"),
        "rule_version": meta_rows.get("relation_rule_version"),
        "calculation_mode": meta_rows.get("calculation_mode"),
        "scope": meta_rows.get("scope"),
        "disclaimer": meta_rows.get("disclaimer"),
        "mean_definition": meta_rows.get("mean_definition"),
        "exchange_benchmarks": json.loads(meta_rows.get("exchange_benchmarks") or "{}"),
        "coverage": {"first": first, "last": last, "last_a_share": last_a},
        "counts": {
            "a_share": int(meta_rows.get("n_a_share") or 0),
            "relation_subjects": int(meta_rows.get("n_relation_subjects") or 0),
            "daily_facts": int(meta_rows.get("n_daily_facts") or 0),
            "a_share_facts": int(meta_rows.get("n_a_share_facts") or 0),
            "index_facts": int(meta_rows.get("n_index_facts") or 0),
            "benchmark_prices": int(meta_rows.get("n_benchmark_prices") or 0),
            "templates": int(meta_rows.get("n_relation_templates") or 0),
            "template_events": int(meta_rows.get("n_template_events") or 0),
            "profiles": int(meta_rows.get("n_natal_profiles_distinct") or 0),
        },
        "disputed_identities": len(disputed_set()),
        "relation_types": types,
        "ten_gods": [g for g in ("比肩", "劫财", "食神", "伤官", "偏财", "正财",
                                 "七杀", "正官", "偏印", "正印")
                     if g in {r["official_ten_god"] for r in
                              con.execute("SELECT DISTINCT official_ten_god "
                                          "FROM relation_templates")}],
        "calibers": [{"key": k, "label": lb, "note": nt} for k, lb, nt in CALIBERS],
        "stats_file_time": stats_at,
        "index_benchmark_cutoff": meta_rows.get("index_benchmark_cutoff") or None,
        "refreshed_at_bjt": meta_rows.get("refreshed_at_bjt") or None,
    }


def search(con: sqlite3.Connection, text: str, limit: int) -> dict:
    text = (text or "").strip()
    if not text:
        return {"rows": []}
    like = f"%{text}%"
    rows = con.execute(
        "SELECT identity_code FROM research_identities "
        "WHERE asset_class='A_SHARE' AND identity_code LIKE ? "
        "ORDER BY length(identity_code), identity_code LIMIT ?", (like, limit)).fetchall()
    codes = [r[0] for r in rows]
    names = _names(con, codes)
    return {"rows": [{"code": c, "name": names.get(c, "")} for c in codes]}


def _by_name(con: sqlite3.Connection, text: str) -> list[dict]:
    try:
        rows = con.execute(
            "SELECT identity_code, name FROM base.securities "
            "WHERE name = ? OR name LIKE ? ORDER BY (name = ?) DESC, length(identity_code) LIMIT 6",
            (text, f"%{text}%", text)).fetchall()
    except sqlite3.Error:
        return []
    return [{"code": r[0], "name": r[1]} for r in rows]


def _resolve(con: sqlite3.Connection, text: str) -> tuple[str | None, list[dict]]:
    """把「002436」「002436.SZ」「兴森科技」都归一到一个身份代码。

    6 位纯数字在主库里同一个号码可能同时存在 A 股与指数（000001），此时优先 A 股。
    """
    raw = (text or "").strip()
    if not raw:
        return None, []
    up = raw.upper()
    row = con.execute("SELECT identity_code FROM research_identities WHERE identity_code = ?",
                      (up,)).fetchone()
    if row:
        return row[0], []
    if raw.isdigit() and len(raw) == 6:
        rows = con.execute(
            "SELECT r.identity_code FROM research_identities r "
            "WHERE substr(r.identity_code, 1, 6) = ? "
            "ORDER BY (r.asset_class = 'A_SHARE') DESC, r.identity_code", (raw,)).fetchall()
        if len(rows) == 1:
            return rows[0][0], []
        if rows:
            return rows[0][0], []
    cands = _by_name(con, raw)
    if len(cands) == 1:
        return cands[0]["code"], []
    return None, cands


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _fact_filters(flt: dict) -> tuple[str, list]:
    """把页面上的篩選条件变成 SQL 片段；只接受白名单取值。"""
    clauses: list[str] = []
    params: list = []
    date_from = (flt.get("date_from") or "").strip()
    date_to = (flt.get("date_to") or "").strip()
    if date_from:
        if not _DATE_RE.match(date_from):
            raise N2Error("起始日期格式应为 YYYY-MM-DD")
        clauses.append("f.trade_date >= ?")
        params.append(date_from)
    if date_to:
        if not _DATE_RE.match(date_to):
            raise N2Error("结束日期格式应为 YYYY-MM-DD")
        clauses.append("f.trade_date <= ?")
        params.append(date_to)
    if flt.get("direction") in ("UP", "DOWN", "FLAT"):
        clauses.append("f.day_body_direction = ?")
        params.append(flt["direction"])
    if flt.get("pillar"):
        clauses.append("f.flow_day_pillar = ?")
        params.append(flt["pillar"])
    if flt.get("ten_god"):
        clauses.append("t.official_ten_god = ?")
        params.append(flt["ten_god"])
    if flt.get("rel"):
        clauses.append("EXISTS (SELECT 1 FROM json_each(t.relation_types_json) WHERE value = ?)")
        params.append(flt["rel"])
    return " AND ".join(clauses), params


def stock(con: sqlite3.Connection, identity: str, limit: int, offset: int,
          order: str, flt: dict | None = None) -> dict:
    raw = (identity or "").strip()
    if not raw:
        raise N2Error("缺少股票代码")
    identity, cands = _resolve(con, raw)
    if identity is None:
        hint = "；".join(f"{c['code']} {c['name']}" for c in cands[:5])
        raise N2Error(
            f"没有找到「{raw}」" + (f"，你是不是要找：{hint}" if hint
                                   else "。可以写 6 位代码（如 002436）、完整代码（如 600519.SH）或股票名称"))
    head = con.execute(
        "SELECT identity_code, asset_class, natal_profile_key, natal_pillars, profile_status "
        "FROM natal_template_map WHERE identity_code=?", (identity,)).fetchone()
    if head is None:
        raise N2Error(f"{identity} 不在本轮全市场研究范围内（5,131 只 A 股 + 1 个指数）")
    n_total = con.execute("SELECT count(*) FROM daily_facts WHERE identity_code=?",
                          (identity,)).fetchone()[0]
    extra, params = _fact_filters(flt or {})
    where = "f.identity_code = ?" + (" AND " + extra if extra else "")
    args = [identity] + params
    n_rows = con.execute(
        "SELECT count(*) FROM daily_facts f JOIN relation_templates t USING(template_key) "
        f"WHERE {where}", args).fetchone()[0]
    direction = "DESC" if order != "asc" else "ASC"
    rows = con.execute(
        FACT_SELECT + f" WHERE {where} ORDER BY f.trade_date {direction} "
        "LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
    disputed = disputed_set()
    names = _names(con, [identity])
    bidir = con.execute(
        "SELECT count(*), sum(f.day_body_direction='UP'), sum(f.day_body_direction='DOWN'), "
        "sum(f.day_body_direction='FLAT'), "
        "sum(case when f.return_valid=1 then f.open_close_return else 0 end), sum(f.return_valid) "
        "FROM daily_facts f JOIN relation_templates t USING(template_key) "
        f"WHERE {where}", args).fetchone()
    nvalid = bidir[5] or 0
    return {
        "identity": {
            "code": head["identity_code"],
            "name": names.get(identity, ""),
            "asset_class": head["asset_class"],
            "pillars": head["natal_pillars"],
            "profile_status": head["profile_status"],
            "profile_key": head["natal_profile_key"],
            "profile_scope": "RESEARCH_CORE_3P",
            "disputed": identity in disputed,
            "n_rows": n_rows,
        },
        "summary": {
            "n_rows": bidir[0] or 0,
            "n_up": bidir[1] or 0,
            "n_down": bidir[2] or 0,
            "n_flat": bidir[3] or 0,
            "n_valid": nvalid,
            "mean_pct": (bidir[4] / nvalid * 100.0) if nvalid else None,
        },
        "n_rows": n_rows,
        "n_total": n_total,
        "rows": [_row_out(r, disputed, names) for r in rows],
    }


def _day_filters(date: str, flt: dict):
    if not date:
        raise N2Error("缺少交易日")
    if not _DATE_RE.match(date):
        raise N2Error("交易日格式应为 YYYY-MM-DD")
    extra, params = _fact_filters(flt)
    where = "f.asset_class='A_SHARE' AND f.trade_date=?" + (" AND " + extra if extra else "")
    return where, [date] + params


def day(con: sqlite3.Connection, date: str, flt: dict, limit: int, offset: int) -> dict:
    where, params = _day_filters(date, flt)
    total = con.execute(
        f"SELECT count(*) FROM daily_facts f JOIN relation_templates t USING(template_key) "
        f"WHERE {where}", params).fetchone()[0]
    agg = con.execute(
        "SELECT sum(f.day_body_direction='UP'), sum(f.day_body_direction='DOWN'), "
        "sum(f.day_body_direction='FLAT'), "
        "sum(case when f.return_valid=1 then f.open_close_return else 0 end), sum(f.return_valid) "
        f"FROM daily_facts f JOIN relation_templates t USING(template_key) WHERE {where}",
        params).fetchone()
    nvalid = agg[4] or 0
    rows = con.execute(
        FACT_SELECT + f" WHERE {where} ORDER BY f.identity_code LIMIT ? OFFSET ?",
        params + [limit, offset]).fetchall()
    disputed = disputed_set()
    names = _names(con, [r["identity_code"] for r in rows])
    return {
        "date": date,
        "direction": flt.get("direction") or "ALL",
        "relation": flt.get("rel") or "",
        "pillar": flt.get("pillar") or "",
        "ten_god": flt.get("ten_god") or "",
        "n_rows": total,
        "summary": {
            "n_up": agg[0] or 0,
            "n_down": agg[1] or 0,
            "n_flat": agg[2] or 0,
            "n_valid": nvalid,
            "mean_pct": (agg[3] / nvalid * 100.0) if nvalid else None,
        },
        "rows": [_row_out(r, disputed, names) for r in rows],
    }


def stats(dim: str, caliber: str) -> dict:
    if caliber not in {c[0] for c in CALIBERS}:
        raise N2Error(f"未知口径：{caliber}")
    rows = [r for r in _read_stats(dim) if r.get("caliber") == caliber]
    overall = [r for r in _read_stats("overall") if r.get("caliber") == caliber]
    base = overall[0] if overall else None
    out = []
    for r in rows:
        item = dict(r)
        for k in ("n_identities", "n_stock_days", "n_up", "n_down", "n_flat",
                  "n_return_valid", "n_dates"):
            if item.get(k) is not None:
                item[k] = int(item[k])
        if item.get("mean_pct") is not None and base and base.get("mean_pct") is not None:
            item["delta_pp"] = item["mean_pct"] - base["mean_pct"]
        out.append(item)
    out.sort(key=lambda r: (r.get("mean_pct") is None, -(r.get("mean_pct") or 0)))
    return {
        "dim": dim,
        "caliber": caliber,
        "baseline": base,
        "n": len(out),
        "rows": out,
    }


def benchmark(caliber_note: str = "") -> dict:
    return {"rows": _read_stats("benchmark"), "index": _read_stats("index")}


# ---------- 流月聚合（由日级库离线聚合，见 scripts/export_n2_month_agg.py） ----------
N2_MONTH_CSV = Path(os.environ.get(
    "BAZI_N2_MONTH_CSV", str(Path(__file__).resolve().parent / "data" / "n2_month_agg.csv")))
N2_MONTH_BASE = N2_MONTH_CSV.with_name("n2_month_agg_baseline.json")
_month_cache: dict = {}

_MONTH_INT = ("n_dates", "n_identities", "n_stock_days", "n_up", "n_down", "n_flat",
              "n_return_valid", "n_month_valid")
_MONTH_FLOAT = ("mean_day_pct", "mean_month_pct", "median_month_pct", "up_share_month")


def _read_months() -> tuple[list[dict], dict]:
    try:
        mtime = N2_MONTH_CSV.stat().st_mtime
    except OSError as exc:
        raise N2Unavailable("流月聚合文件缺失（先跑 scripts/export_n2_month_agg.py）") from exc
    with _lock:
        if _month_cache.get("mtime") == mtime:
            return _month_cache["rows"], _month_cache["baseline"]
        with N2_MONTH_CSV.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        for r in rows:
            for k in _MONTH_INT:
                r[k] = int(float(r[k])) if r.get(k) not in ("", None) else None
            for k in _MONTH_FLOAT:
                r[k] = float(r[k]) if r.get(k) not in ("", None) else None
        try:
            baseline = json.loads(N2_MONTH_BASE.read_text("utf-8"))
        except (OSError, ValueError):
            baseline = {}
        _month_cache.update({"mtime": mtime, "rows": rows, "baseline": baseline})
        return rows, baseline


def months(limit: int) -> dict:
    rows, baseline = _read_months()
    keep = rows[-limit:] if limit > 0 else rows
    return {"baseline": baseline, "n_total": len(rows),
            "rows": list(reversed(keep))}


def _flt_of(q: dict) -> dict:
    return {
        "date_from": _q(q, "from"),
        "date_to": _q(q, "to"),
        "direction": _q(q, "dir"),
        "pillar": _q(q, "pillar"),
        "ten_god": _q(q, "ten_god"),
        "rel": _q(q, "rel"),
    }


def dispatch(path: str, q: dict) -> dict:
    con = connect()
    try:
        if path == "/api/n2/meta":
            return meta(con)
        if path == "/api/n2/search":
            return search(con, _q(q, "q"), _int(_q(q, "limit"), 20, 1, 50))
        if path == "/api/n2/stock":
            return stock(con, _q(q, "id"), _int(_q(q, "limit"), 100, 1, 500),
                         _int(_q(q, "offset"), 0, 0, 10 ** 7),
                         _q(q, "order", "desc"), _flt_of(q))
        if path == "/api/n2/day":
            return day(con, _q(q, "date"), _flt_of(q),
                       _int(_q(q, "limit"), 100, 1, 500),
                       _int(_q(q, "offset"), 0, 0, 10 ** 7))
        if path == "/api/n2/day_relation":
            flt = _flt_of(q)
            if not flt.get("rel"):
                raise N2Error("缺少关系类型")
            return day(con, _q(q, "date"), flt,
                       _int(_q(q, "limit"), 100, 1, 500),
                       _int(_q(q, "offset"), 0, 0, 10 ** 7))
        if path == "/api/n2/stats":
            return stats(_q(q, "dim", "pillar"), _q(q, "caliber", "MAIN"))
        if path == "/api/n2/benchmark":
            return benchmark()
        if path == "/api/n2/months":
            return months(_int(_q(q, "limit"), 120, 0, 1000))
        raise N2Error("未知的流日关系接口")
    except sqlite3.Error as exc:
        raise N2Unavailable(f"影子库查询失败（外置盘可能已断开）：{exc}") from exc
    finally:
        con.close()
