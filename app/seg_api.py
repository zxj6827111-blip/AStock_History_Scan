"""100% 段页面的只读查询后端。

数据来自 slot88_full_market_v1 的 Parquet 分片（2.2GB，列式），用 DuckDB 只读扫描；
不打开 16.4GB 的 SQLite，也不写任何库。名称从主库 securities 只读补齐。
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import duckdb
except ImportError:  # 运行环境没装 duckdb 时，不影响主系统启动；本页给出明确提示
    duckdb = None

import config

ROOT = config.ROOT
PARQUET = Path(os.environ.get(
    "BAZI_SEG_PARQUET",
    str(ROOT / "slot88_full_market_v1/data/parquet_n3/*.parquet")))
SEG_DB = Path(os.environ.get(
    "BAZI_SEG_DB",
    str(ROOT / "slot88_full_market_v1/data/full_market_segments_n3.sqlite")))
LIVE_DB = Path(os.environ.get(
    "BAZI_LIVE_DB", str(ROOT / "source_input/live/current_month.sqlite")))
FACTS60 = Path(os.environ.get(
    "BAZI_FACTS60", str(ROOT / "slot88_month_v1/work/facts60.duckdb")))
LIVE60 = Path(os.environ.get(
    "BAZI_LIVE60", str(ROOT / "slot88_full_market_v1/data/live_60m.parquet")))
LIVE60_META = Path(os.environ.get(
    "BAZI_LIVE60_META", str(ROOT / "slot88_full_market_v1/data/live_60m_meta.json")))
SESSIONS = Path(os.environ.get(
    "BAZI_SESSIONS",
    str(ROOT / "slot88_full_market_v1/data/market_sessions_ext.csv")))
SESSIONS_FROZEN = Path(os.environ.get(
    "BAZI_SESSIONS_FROZEN",
    str(ROOT / "month_path_stage_v1/data/market_sessions.csv")))
NAS_SRC = Path(os.environ.get(
    "BAZI_NAS_SRC", str(ROOT / "source_input/nas_source.duckdb")))
FROZEN_LAST_COMPLETE = "2026-M05"   # facts60.cm 里最后一个完整月
LAST_IN_LIBRARY = "2026-M09"        # 段库已经补到的最新月份（M06..M09 已入库）
LIBRARY_EXTRA_MONTHS = {"2026-M06", "2026-M07", "2026-M08", "2026-M09"}  # 事后补进的新月份
GAPS_JSON = Path(os.environ.get(
    "BAZI_SEG_GAPS", str(ROOT / "slot88_full_market_v1/data/data_gaps.json")))
_GAPS_CACHE: dict = {"mtime": 0.0, "rows": []}


def _gaps() -> list[dict]:
    """哪些流月因为源头没有 60 分钟数据而没能入库（按 pillar 给页面提示）。"""
    try:
        st = GAPS_JSON.stat()
    except OSError:
        return []
    if st.st_mtime != _GAPS_CACHE["mtime"]:
        try:
            data = json.loads(GAPS_JSON.read_text(encoding="utf-8"))
            _GAPS_CACHE["rows"] = data.get("gaps", [])
            _GAPS_CACHE["mtime"] = st.st_mtime
        except (OSError, ValueError):
            return _GAPS_CACHE["rows"]
    return _GAPS_CACHE["rows"]
TZ = timezone(timedelta(hours=8))

PILLARS = ("癸未,甲申,乙酉,丙戌,丁亥,戊子,己丑,庚寅,辛卯,壬辰,癸巳,甲午,乙未,丙申,"
           "丁酉,戊戌,己亥,庚子,辛丑,壬寅,癸卯,甲辰,乙巳,丙午,丁未,戊申,己酉,庚戌,"
           "辛亥,壬子,癸丑,甲寅,乙卯,丙辰,丁巳,戊午,己未,庚申,辛酉,壬戌,癸亥,甲子,"
           "乙丑,丙寅,丁卯,戊辰,己巳,庚午,辛未,壬申,癸酉,甲戌,乙亥,丙子,丁丑,戊寅,"
           "己卯,庚辰,辛巳,壬午").split(",")
SORTS = {"score", "mean", "median", "min", "length", "year"}
CODE_RE = re.compile(r"^[0-9]{6}\.(SH|SZ)$")

_LOCK = threading.Lock()
_CON = None
_COUNT_CACHE: dict[tuple, int] = {}


class SegError(Exception):
    pass


class SegUnavailable(SegError):
    pass


def _parquet_glob() -> str:
    if PARQUET.parent.is_dir() and any(PARQUET.parent.glob(PARQUET.name)):
        return str(PARQUET)
    raise SegUnavailable("100%段数据文件不存在，请检查 slot88_full_market_v1/data/parquet_n3")


def _parquet_stamp() -> float:
    """分片目录最新 mtime：补月换目录后，计数与抽屉缓存自动失效。"""
    try:
        return max(p.stat().st_mtime for p in PARQUET.parent.glob(PARQUET.name))
    except (OSError, ValueError):
        return 0.0


_LIB_CACHE: dict = {"stamp": None, "last": LAST_IN_LIBRARY,
                    "extra": frozenset(LIBRARY_EXTRA_MONTHS)}


def _library_frontier() -> tuple[str, frozenset[str]]:
    """段库补到哪个月：优先读 SQLite meta，缺键时回退模块常量。"""
    global _LIB_CACHE
    try:
        st = SEG_DB.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        return _LIB_CACHE["last"], _LIB_CACHE["extra"]
    if _LIB_CACHE["stamp"] == stamp:
        return _LIB_CACHE["last"], _LIB_CACHE["extra"]
    last, extra = LAST_IN_LIBRARY, frozenset(LIBRARY_EXTRA_MONTHS)
    try:
        con = sqlite3.connect(SEG_DB.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            m = dict(con.execute(
                "SELECT key,value FROM meta WHERE key IN "
                "('library_last_month','library_extra_months')").fetchall())
        finally:
            con.close()
        if m.get("library_last_month"):
            last = str(m["library_last_month"])
        if m.get("library_extra_months"):
            extra = frozenset(x.strip() for x in str(m["library_extra_months"]).split(",")
                              if x.strip())
    except sqlite3.Error:
        pass
    _LIB_CACHE = {"stamp": stamp, "last": last, "extra": extra}
    return last, extra


def _con():
    global _CON
    if duckdb is None:
        raise SegUnavailable("运行环境缺少 duckdb，100%段页面暂不可用")
    if _CON is None:
        _CON = duckdb.connect()
        tmp = Path(tempfile.gettempdir()) / "seg_duckdb"
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            _CON.execute("SET memory_limit='2GB'")
            _CON.execute("SET threads=2")
            _CON.execute(f"SET temp_directory='{tmp.as_posix()}'")
        except Exception:
            pass
    return _CON


def _first(q: dict, key: str, default: str = "") -> str:
    v = q.get(key)
    return (v[0] if isinstance(v, list) and v else v) or default


def _int(q: dict, key: str, default: int, lo: int, hi: int) -> int:
    raw = _first(q, key)
    if raw == "":
        return default
    try:
        n = int(float(raw))
    except (TypeError, ValueError):
        raise SegError(f"参数 {key} 不是整数")
    return min(max(n, lo), hi)


def _float(q: dict, key: str, default: float | None = None) -> float | None:
    raw = _first(q, key)
    if raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise SegError(f"参数 {key} 不是数字")


def _params(q: dict) -> dict:
    direction = _first(q, "direction", "up").lower()
    if direction not in ("up", "down"):
        raise SegError("direction 只能是 up 或 down")
    n_min = _int(q, "n_min", 5, 1, 6)
    n_max = _int(q, "n_max", 0, 0, 6)
    len_min = _float(q, "len_min", 1.0) or 1.0
    len_max = _float(q, "len_max", 5.0) or 5.0
    len_min = min(max(len_min, 1.0), 22.0)
    len_max = min(max(len_max, 1.0), 22.0)
    if len_min > len_max:
        len_min, len_max = len_max, len_min
    if n_max and n_max < n_min:
        n_min, n_max = n_max, n_min
    pillar = _first(q, "pillar")
    if pillar and pillar not in PILLARS:
        raise SegError("未知流月柱")
    code = _first(q, "code").strip().upper()
    if code and not CODE_RE.match(code):
        raise SegError("代码格式应为 000001.SZ")
    sort = _first(q, "sort", "score")
    if sort not in SORTS:
        sort = "score"
    order = _first(q, "order", "desc").lower()
    return {
        "direction": direction, "n_min": n_min, "n_max": n_max,
        "len_min": len_min, "len_max": len_max,
        "mean_min": _float(q, "mean_min"),
        "mean_max": _float(q, "mean_max"),
        "min_min": _float(q, "min_min"),
        "max_max": _float(q, "max_max"),
        "all_sign": 1 if _first(q, "all_sign", "1") not in ("0", "false") else 0,
        "pillar": pillar, "code": code, "sort": sort, "order": order,
        "limit": _int(q, "limit", 100, 1, 500),
        "offset": _int(q, "offset", 0, 0, 5000),
    }


def _where(p: dict) -> tuple[str, list]:
    w = ["direction = ?"]
    a: list = [p["direction"]]
    w.append("n_valid >= ?"); a.append(p["n_min"])
    if p["n_max"]:
        w.append("n_valid <= ?"); a.append(p["n_max"])
    w.append("length_days >= ?"); a.append(p["len_min"])
    w.append("length_days <= ?"); a.append(p["len_max"])
    if p["mean_min"] is not None:
        w.append("mean_pct >= ?"); a.append(p["mean_min"])
    if p["mean_max"] is not None:
        w.append("mean_pct <= ?"); a.append(p["mean_max"])
    if p["min_min"] is not None:
        w.append("min_pct >= ?"); a.append(p["min_min"])
    if p["max_max"] is not None:
        w.append("max_pct <= ?"); a.append(p["max_max"])
    if p["all_sign"]:
        w.append("min_pct > 0" if p["direction"] == "up" else "max_pct < 0")
    if p["pillar"]:
        w.append("pillar = ?"); a.append(p["pillar"])
    if p["code"]:
        w.append("code = ?"); a.append(p["code"])
    return " AND ".join(w), a


def _order(p: dict) -> str:
    d = "ASC" if p["order"] == "asc" else "DESC"
    if p["sort"] == "score":
        # 次数优先：永远先看 5/5、6/6；同次数再按方向取最有利的一端
        return f"n_valid DESC, mean_pct {'ASC' if p['direction'] == 'down' else 'DESC'}"
    col = {"mean": "mean_pct", "median": "median_pct", "min": "min_pct",
           "length": "length_days", "year": "year_max"}[p["sort"]]
    return f"{col} {d}"


_COLS = ("code,pillar,direction,n_valid,n_seen,n_months,n_avail,length_days,length_bars,"
         "start_day,start_time,end_day,end_time,mean_pct,median_pct,min_pct,max_pct,"
         "year_min,year_max,month_ids,month_rets")


def _fetch(p: dict, limit: int, offset: int) -> list[dict]:
    where, args = _where(p)
    sql = (f"SELECT {_COLS} FROM read_parquet(?) WHERE {where} "
           f"ORDER BY {_order(p)} LIMIT ? OFFSET ?")
    with _LOCK:
        cur = _con().execute(sql, [ _parquet_glob(), *args, limit + 1, offset])
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    rows = rows[:limit]
    _attach_names(rows)
    return rows


def _attach_names(rows: list[dict]) -> None:
    codes = sorted({r["code"] for r in rows})
    if not codes:
        return
    try:
        con = sqlite3.connect(config.ASHARE_DB.resolve().as_uri() + "?mode=ro", uri=True)
        q = "SELECT identity_code,name FROM securities WHERE identity_code IN (%s)" % (
            ",".join("?" * len(codes)))
        names = dict(con.execute(q, codes).fetchall())
        con.close()
    except Exception:
        names = {}
    for r in rows:
        r["name"] = names.get(r["code"], "")


def meta() -> dict:
    if not SEG_DB.is_file():
        raise SegUnavailable("100%段数据库不存在")
    con = sqlite3.connect(SEG_DB.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        m = dict(con.execute("SELECT key,value FROM meta").fetchall())
    finally:
        con.close()
    return {
        "n_segments": int(m.get("n_segments", 0)),
        "n_up": int(m.get("n_up", 0)),
        "n_down": int(m.get("n_down", 0)),
        "n_stocks": int(m.get("n_stocks_with_segments", 0)),
        "n_identities": int(m.get("n_identities", 0)),
        "months": f"2000-M06 至 {m.get('library_last_month') or '2026-M05'}",
        "pillars": PILLARS,
        "defaults": {"direction": "up", "n_min": 5, "len_max": 5,
                     "mean_min": 5, "all_sign": 1, "sort": "score"},
    }


def query(q: dict) -> dict:
    p = _params(q)
    t0 = time.time()
    rows = _fetch(p, p["limit"], p["offset"])
    return {
        "rows": rows,
        "limit": p["limit"], "offset": p["offset"],
        "has_more": len(rows) == p["limit"],
        "elapsed_ms": round((time.time() - t0) * 1000),
    }


def count(q: dict) -> dict:
    p = _params(q)
    key = (p["direction"], p["n_min"], p["n_max"], p["len_min"], p["len_max"],
           p["mean_min"], p["mean_max"], p["min_min"], p["max_max"],
           p["all_sign"], p["pillar"], p["code"])
    key = (*key, _parquet_stamp())
    if key in _COUNT_CACHE:
        return {"count": _COUNT_CACHE[key], "cached": True}
    where, args = _where(p)
    t0 = time.time()
    with _LOCK:
        n = _con().execute(
            f"SELECT count(*) FROM read_parquet(?) WHERE {where}",
            [_parquet_glob(), *args]).fetchone()[0]
    _COUNT_CACHE[key] = int(n)
    if len(_COUNT_CACHE) > 96:
        _COUNT_CACHE.pop(next(iter(_COUNT_CACHE)))
    return {"count": int(n), "cached": False,
            "elapsed_ms": round((time.time() - t0) * 1000)}


def export_csv(q: dict) -> tuple[bytes, str]:
    p = _params(q)
    rows = _fetch(p, 5000, 0)
    buf = io.StringIO()
    buf.write("\ufeff")
    w = csv.writer(buf)
    w.writerow(["代码", "名称", "流月柱", "方向", "命中次数", "有行情次数", "全柱次数",
                "长度天数", "起点日", "起点时间", "终点日", "终点时间",
                "平均%", "中位%", "最差%", "最好%", "最早年", "最晚年",
                "命中月份", "各次收益%"])
    for r in rows:
        w.writerow([r["code"], r.get("name", ""), r["pillar"],
                    "涨" if r["direction"] == "up" else "跌",
                    r["n_valid"], r["n_seen"], r["n_months"], r["length_days"],
                    r["start_day"], r["start_time"], r["end_day"], r["end_time"],
                    round(r["mean_pct"], 4), round(r["median_pct"], 4),
                    round(r["min_pct"], 4), round(r["max_pct"], 4),
                    r["year_min"], r["year_max"], r["month_ids"],
                    ";".join(f"{float(x) * 100:.4f}" for x in str(r["month_rets"]).split(";") if x)])
    name = time.strftime("slot88_segments_%Y%m%d_%H%M%S.csv")
    return buf.getvalue().encode("utf-8"), name


def _live_now(offset: int) -> dict:
    now = datetime.now(TZ)
    if offset != 0:
        return {"status": "not_current", "now_pos": None,
                "now_bjt": now.isoformat(timespec="seconds"),
                "now_text": "所选不是本月，无法判断本月位置"}
    if not LIVE_DB.is_file():
        return {"status": "live_not_ready", "now_pos": None,
                "now_bjt": now.isoformat(timespec="seconds"),
                "now_text": "当月层尚未生成"}
    con = sqlite3.connect(LIVE_DB.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        meta = dict(con.execute("SELECT k,v FROM meta").fetchall())
    finally:
        con.close()
    latest = str(meta.get("latest_trade_date") or "")
    days = int(float(meta.get("days") or 0))
    if not latest or days <= 0:
        return {"status": "live_not_ready", "now_pos": None,
                "now_bjt": now.isoformat(timespec="seconds"),
                "now_text": "当月层没有最新交易日"}
    asof = datetime.fromisoformat(latest).date()
    if now.date() > asof:
        pos = days * 4 - 1e-6
    elif now.date() < asof:
        pos = -1e-6
    else:
        mins = now.hour * 60 + now.minute
        if mins < 570:
            pos = (days - 1) * 4 - 1e-6
        elif mins < 630:
            pos = (days - 1) * 4 + 0.5
        elif mins < 690:
            pos = (days - 1) * 4 + 1.5
        elif mins < 780:
            pos = (days - 1) * 4 + 1.99
        elif mins < 840:
            pos = (days - 1) * 4 + 2.5
        elif mins < 900:
            pos = (days - 1) * 4 + 3.5
        else:
            pos = days * 4 - 1e-6
    session = "收盘后" if now.hour * 60 + now.minute >= 900 else (
        "开盘前" if now.hour * 60 + now.minute < 570 else "盘中")
    return {"status": "ok", "now_pos": pos, "now_bjt": now.isoformat(timespec="seconds"),
            "month_id": meta.get("month_id"), "pillar": meta.get("pillar"),
            "latest_trade_date": latest, "days": days,
            "now_text": f"本月 {meta.get('month_id')} {meta.get('pillar')} · "
                        f"第{days}个交易日（{latest}）{session}"}


def _segments(pillar: str, direction: str, n_min: int, len_max: float,
              mean_min: float, all_sign: int, now_pos: float | None) -> list[dict]:
    up = direction == "up"
    where = ["pillar = ?", "direction = ?", "n_valid >= ?", "length_days <= ?"]
    args: list = [pillar, direction, n_min, len_max]
    if mean_min and mean_min > 0:
        if up:
            where.append("mean_pct >= ?"); args.append(mean_min)
        else:
            where.append("mean_pct <= ?"); args.append(-abs(mean_min))
    if all_sign:
        where.append("min_pct > 0" if up else "max_pct < 0")
    md = "DESC" if up else "ASC"
    pref = "-mean_pct" if up else "mean_pct"
    sql = f"""
      WITH base AS (
        SELECT code,pillar,direction,n_valid,n_seen,n_months,n_avail,length_days,
               start_day,start_time,end_day,end_time,mean_pct,median_pct,min_pct,max_pct,
               year_min,year_max,month_ids,month_rets,s,e
        FROM read_parquet(?) WHERE {' AND '.join(where)}
      ),
      ranked AS (
        SELECT *, CASE WHEN ? IS NULL THEN 9
                       WHEN s <= ? AND e >= ? THEN 0
                       WHEN s > ? THEN 1 ELSE 2 END AS pr
        FROM base
      ),
      picked AS (
        SELECT *, ROW_NUMBER() OVER (
          PARTITION BY code
          ORDER BY pr,
            CASE WHEN ? IS NULL THEN 0 WHEN s <= ? AND e >= ? THEN {pref} ELSE 0 END,
            CASE WHEN s > ? THEN s ELSE -e END,
            mean_pct {md}, s, e) AS rn
        FROM ranked
      )
      SELECT * FROM picked WHERE rn=1 ORDER BY pr, mean_pct {md}, s
    """
    params = [_parquet_glob(), *args,
              now_pos, now_pos, now_pos, now_pos,
              now_pos, now_pos, now_pos, now_pos]
    with _LOCK:
        cur = _con().execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return rows


def _attach_status(rows: list[dict], now_pos: float | None, ctx: dict,
                   dates: list[str] | None = None,
                   actuals: dict[str, dict] | None = None,
                   month_exact: bool = True,
                   month_actuals: dict[str, dict] | None = None) -> None:
    for r in rows:
        s, e = int(r["s"]), int(r["e"])
        if now_pos is None:
            status, text, progress, delta = "not_current", "非本月", None, None
        elif s <= now_pos <= e:
            status = "ongoing"
            progress = max(0.0, min(1.0, (now_pos - s) / (e - s + 1)))
            text = f"进行中 {progress * 100:.0f}%"
            delta = None
        elif s > now_pos:
            status, progress = "upcoming", 0.0
            delta = (s - now_pos) / 4
            text = f"未到（还差 {delta:.2f} 个交易日）"
        else:
            status, progress = "passed", 1.0
            delta = (now_pos - e) / 4
            text = f"已过去 {delta:.2f} 个交易日"
        remain_days = None
        remain_bars = None
        if status == "ongoing" and now_pos is not None:
            remain_bars = max(0, int(round(e + 1 - now_pos)))
            remain_days = round(max(0.0, (e + 1 - now_pos) / 4.0), 2)
        r["_seg"] = {
            "status": status, "status_text": text, "progress": progress,
            "remain_days": remain_days, "remain_bars": remain_bars,
            "delta_days": None if delta is None else round(delta, 2),
            "n_valid": r["n_valid"], "n_avail": r["n_avail"], "n_seen": r["n_seen"],
            "n_months": r["n_months"],
            "length_days": r["length_days"], "mean_pct": r["mean_pct"],
            "median_pct": r["median_pct"], "min_pct": r["min_pct"], "max_pct": r["max_pct"],
            "start_day": r["start_day"], "start_time": r["start_time"],
            "end_day": r["end_day"], "end_time": r["end_time"], "s": r["s"], "e": r["e"],
            "year_min": r["year_min"], "year_max": r["year_max"],
            "month_ids": r["month_ids"], "month_rets": r["month_rets"],
            "now_text": ctx.get("now_text"),
        }
        if dates:
            d0, d1 = s // 4 + 1, e // 4 + 1
            sd, sx = _date_for_day(dates, d0, month_exact)
            ed, ex = _date_for_day(dates, d1, month_exact)
            r["_seg"]["start_date"] = sd
            r["_seg"]["end_date"] = ed
            r["_seg"]["start_exact"] = bool(sx)
            r["_seg"]["end_exact"] = bool(ex)
        st = _seg_stats(r, r.get("direction") or "up")
        r["_seg"].update(st)
        if actuals is not None:
            r["_seg"]["latest"] = actuals.get(r["code"])
        if month_actuals is not None:
            r["_seg"]["month_actual"] = month_actuals.get(r["code"])


BOARD_KEYS = ("offset", "min_n", "grade", "dir", "rate_min", "rate_eq", "stab", "cons",
              "both_months", "next_rate", "policy", "sort", "msort", "cap", "cap_tier",
              "q", "rel", "clash", "harmony", "both_clash_harmony", "punish", "harm",
              "broken", "include_delisted", "include_index")


def board(q: dict) -> dict:
    """机会表第三模式：本页股票由段库选出，股票行仍走机会表同一条 SQL。"""
    import api  # 延迟导入：只有这一页需要，避免主查询模块反向依赖
    offset = _int(q, "offset", 0, -260, 260)
    direction = _first(q, "dir", "up").lower()
    if direction not in ("up", "down"):
        direction = "up"
    n_min = _int(q, "seg_n", 5, 3, 6)
    len_max = float(_float(q, "seg_len", 5.0) or 5.0)
    mean_min = float(_float(q, "seg_mean", 5.0) or 0.0)
    all_sign = 0 if _first(q, "seg_all", "1") in ("0", "false") else 1
    cap = _int(q, "cap", 500, 1, 500)
    flt2 = {k: _first(q, k) for k in BOARD_KEYS}
    flt2.update({"months": "1", "layer": "month", "offset": str(offset),
                 "dir": direction, "cap": str(cap)})
    con = api.connect("ashare")
    try:
        cal = api.calendar_now(con)
        entry = next((m for m in cal["months"] if m["offset"] == offset), None)
        if entry is None:
            raise SegError("所选月份不在可选时间线内")
        pillar = entry["month_pillar"]
        ctx = _live_now(offset)
        now_pos = ctx.get("now_pos") if ctx.get("status") == "ok" else None
        segs = _segments(pillar, direction, n_min, len_max, mean_min, all_sign, now_pos)
        total = len(segs)
        picked = segs[:cap]
        codes = [r["code"] for r in picked]
        dates: list[str] = []
        month_exact = True
        actuals: dict[str, dict] = {}
        if ctx.get("status") == "ok" and ctx.get("pillar") == pillar:
            dates, qfq_by_code = _live_month_ctx([c.split(".")[0] for c in codes])
            if dates and LIVE60.is_file():
                rows60 = _live60_rows([c.split(".")[0] for c in codes], dates)
                for r in picked:
                    c6 = r["code"].split(".")[0]
                    bar = _bars_map(rows60.get(c6, []), dates, qfq_by_code.get(c6, {}))
                    w = _walk_seg(bar, int(r["s"]), int(r["e"]))
                    if w:
                        actuals[r["code"]] = {k: v for k, v in w.items() if k != "pts"}
        if not dates:
            # 往月用真实交易日；未来月按工作日外推（exact=False，前端加 ≈）
            dates, month_exact = _month_dates(entry["month_id"], entry["start_at"],
                                              entry["end_at"])
        lib_last, lib_extra = _library_frontier()
        month_actuals: dict[str, dict] = {}
        if offset != 0 and entry["month_id"] <= lib_last:
            # 该月份已经在段库里：这一次的收益直接从本行的 month_ids/month_rets 取，
            # 不再去 60 分钟源重算（那会得出「未计入历史」的错误口径）。
            mid = entry["month_id"]
            for r in picked:
                mids = (r.get("month_ids") or "").split(";")
                if mid in mids:
                    rets = (r.get("month_rets") or "").split(";")
                    i = mids.index(mid)
                    if i < len(rets):
                        month_actuals[r["code"]] = {
                            "e_bp": int(round(float(rets[i]) * 10000)),
                            "complete": True, "source": "library"}
        _attach_status(picked, now_pos, ctx, dates, actuals, month_exact, month_actuals)
        flt2["codes"] = ",".join(codes) if codes else "__NONE__"
        out = api.opportunities(con, flt2, "ashare")
    finally:
        con.close()
    by_code = {r["code"]: r["_seg"] for r in picked}
    order = {c: i for i, c in enumerate(codes)}
    rows = [{**r, "_seg": by_code[r["identity_code"]]}
            for r in out.get("rows", []) if r.get("identity_code") in by_code]
    rows.sort(key=lambda r: order.get(r["identity_code"], 10 ** 9))
    out["rows"] = rows
    month_remain = None
    if ctx.get("status") == "ok" and ctx.get("latest_trade_date"):
        try:
            d = datetime.fromisoformat(str(ctx["latest_trade_date"])).date()
            d1 = datetime.fromisoformat(str(entry["end_at"])[:10]).date()
            n = 0
            d = d + timedelta(days=1)
            while d <= d1:
                if d.weekday() < 5:
                    n += 1
                d = d + timedelta(days=1)
            month_remain = n
        except ValueError:
            month_remain = None
    out["seg_ctx"] = {**ctx, "month_id": entry["month_id"], "pillar": pillar,
                      "offset": offset, "total": total,
                      "month_remain_days": month_remain,
                      "month_in_history": (entry["month_id"] <= FROZEN_LAST_COMPLETE
                                           or entry["month_id"] in lib_extra),
                      "gaps": [g for g in _gaps() if g.get("pillar") == pillar]}
    out["seg_params"] = {"n_min": n_min, "len_max": len_max,
                         "mean_min": mean_min, "all_sign": all_sign,
                         "direction": direction, "cap": cap}
    return out


SLOT_TIMES = ("09:30", "10:30", "13:00", "14:00")
SLOT_ENDS = ("10:30", "11:30", "14:00", "15:00")
_PATH_CACHE: dict[tuple, dict] = {}


def _bars_map(rows, dates: list[str], qfq_close: dict[str, float]) -> dict[int, tuple]:
    """把某只票的 60 分钟行映射成 88 格 slot -> (时间标签, 前复权开, 前复权收)。

    原始 60 分钟价未复权：按「当日日线前复权收盘 / 当日最后一根 60 分钟收盘」逐日缩放。
    """
    day_map = {d: i + 1 for i, d in enumerate(dates)}
    by_date: dict[str, list] = {}
    for d, h, o, c in rows:
        by_date.setdefault(str(d)[:10], []).append((int(h), float(o), float(c)))
    bar: dict[int, tuple] = {}
    for d, lst in by_date.items():
        lst.sort()
        last_c = lst[-1][2]
        qc = qfq_close.get(d)
        mn = day_map.get(d)
        if not last_c or qc is None or mn is None:
            continue
        scale = qc / last_c
        for h, o, c in lst:
            bar[(mn - 1) * 4 + (h - 1)] = (f"{d} {SLOT_ENDS[h - 1]}", o * scale, c * scale)
    return bar


def _walk_seg(bar: dict, s: int, e: int) -> dict | None:
    """段内累计：口径与历史 r_chain 一致（qc/prev_qc），段首基准 = 前一根收盘。"""
    if not bar:
        return None
    if s == 0 and s in bar:
        base = bar[s][1]
    elif (s - 1) in bar:
        base = bar[s - 1][2]
    elif s in bar:
        base = bar[s][1]
    else:
        return None
    if not base:
        return None
    pts, ys, labels = [], [], []
    for k in range(s, e + 1):
        if k not in bar:
            break
        cum = bar[k][2] / base - 1.0
        ys.append(cum)
        labels.append(bar[k][0])
        pts.append([round((k - s + 1) / (e - s + 1), 4), int(round(cum * 10000))])
    if not pts:
        return None
    lk = min(range(len(ys)), key=lambda i: ys[i]) + 1
    hk = max(range(len(ys)), key=lambda i: ys[i]) + 1
    return {
        "pts": pts, "e_bp": pts[-1][1], "n_bars": len(pts),
        "covered": len(pts), "total": e - s + 1,
        "asof": labels[-1][:10], "asof_dt": labels[-1],
        "lk": lk, "hk": hk, "complete": len(pts) == (e - s + 1),
        "base_dt": (bar[s - 1][0] if (s - 1) in bar and s != 0 else None),
        "note": "本月线 = NAS 日更 60 分钟线（前复权），段首前收盘 = 0%",
    }


def _live60_rows(codes: list[str], dates: list[str]) -> dict[str, list]:
    """一次读完一批股票的 60 分钟行，避免每只票一条 SQL。"""
    if not LIVE60.is_file() or not dates or not codes:
        return {}
    out: dict[str, list] = {}
    con = _con()
    for i in range(0, len(codes), 400):
        part = codes[i:i + 400]
        ph = ",".join("?" for _ in part)
        with _LOCK:
            rows = con.execute(
                f'SELECT code, date, "h", "open", "close" FROM read_parquet(?) '
                f'WHERE code IN ({ph}) AND date>=? AND date<=? ORDER BY code, date, "h"',
                [str(LIVE60), *part, dates[0], dates[-1]]).fetchall()
        for code6, d, h, o, c in rows:
            out.setdefault(str(code6), []).append((str(d)[:10], int(h), float(o), float(c)))
    return out


def _live60_path(code: str, s: int, e: int, dates: list[str],
                 qfq_close: dict[str, float]) -> dict | None:
    """本月这条段的真实 60 分钟线（NAS 日更）。"""
    if not LIVE60.is_file() or not dates:
        return None
    code6 = code.split(".")[0]
    rows = _live60_rows([code6], dates).get(code6, [])
    return _walk_seg(_bars_map(rows, dates, qfq_close), s, e)


def _live_month_ctx(codes: list[str]) -> tuple[list[str], dict[str, dict[str, float]]]:
    """当月层的交易日列表 + 每只票的日线前复权收盘（只读）。"""
    if not LIVE_DB.is_file():
        return [], {}
    dates: list[str] = []
    qfq: dict[str, dict[str, float]] = {}
    con = sqlite3.connect(LIVE_DB.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        meta = dict(con.execute("SELECT k,v FROM meta").fetchall())
        start_at = str(meta.get("start_at") or "")[:10]
        latest = str(meta.get("latest_trade_date") or "")
        dates = [r[0] for r in con.execute(
            "SELECT DISTINCT date FROM bars WHERE date>=? AND date<=? ORDER BY date",
            (start_at, latest)).fetchall()]
        for i in range(0, len(codes), 400):
            part = [c for c in codes[i:i + 400] if c]
            if not part:
                continue
            ph = ",".join("?" for _ in part)
            for c6, d, cl in con.execute(
                    f"SELECT code, date, close FROM bars WHERE code IN ({ph}) "
                    f"AND date>=? AND date<=?", [*part, start_at, latest]).fetchall():
                qfq.setdefault(str(c6), {})[d] = float(cl)
    finally:
        con.close()
    return dates, qfq


def _adj_rows(codes6: list[str], d0: str, d1: str) -> dict[str, dict[str, float]]:
    """NAS 复权因子（按日）。只用来把同一个月内不同日期的 60 分钟价拉成同一复权口径。"""
    if not NAS_SRC.is_file() or not codes6:
        return {}
    con = _con()
    out: dict[str, dict[str, float]] = {}
    with _LOCK:
        try:
            con.execute(f"ATTACH '{NAS_SRC.as_posix()}' AS nas (READ_ONLY)")
        except Exception:
            pass
        for i in range(0, len(codes6), 400):
            part = codes6[i:i + 400]
            ph = ",".join("?" for _ in part)
            try:
                rows = con.execute(
                    f"SELECT code, date, try_cast(adj_factor AS DOUBLE) FROM nas.factors "
                    f"WHERE code IN ({ph}) AND date>=? AND date<=?",
                    [*part, d0, d1]).fetchall()
            except Exception:
                return out
            for code, d, adj in rows:
                if adj is None:
                    continue
                out.setdefault(str(code), {})[str(d)[:10]] = float(adj)
    return out


def _bars_map_adj(rows, dates: list[str], adj: dict[str, float]) -> dict[int, tuple]:
    """60 分钟行 → slot 价，用逐日复权因子缩放（前值填充，除权日不会被当成暴跌）。"""
    day_map = {d: i + 1 for i, d in enumerate(dates)}
    by_date: dict[str, list] = {}
    for d, h, o, c in rows:
        by_date.setdefault(str(d)[:10], []).append((int(h), float(o), float(c)))
    bar: dict[int, tuple] = {}
    last = None
    for d in sorted(by_date):
        a = adj.get(d)
        if a is None:
            a = last
        else:
            last = a
        mn = day_map.get(d)
        if a is None or mn is None:
            continue
        lst = sorted(by_date[d])
        for h, o, c in lst:
            bar[(mn - 1) * 4 + (h - 1)] = (f"{d} {SLOT_ENDS[h - 1]}", o * a, c * a)
    return bar


def _store_month_actuals(picked: list[dict], dates: list[str]) -> dict[str, dict]:
    """已结束的月份用 NAS 日更 60 分钟线算「该月实际」。"""
    if not dates or not LIVE60.is_file():
        return {}
    codes6 = [r["code"].split(".")[0] for r in picked]
    rows60 = _live60_rows(codes6, dates)
    # nas.factors 用带后缀的代码（000030.SZ），60 分钟文件用 000030
    adj = _adj_rows([r["code"] for r in picked], dates[0], dates[-1])
    out: dict[str, dict] = {}
    for r in picked:
        c6 = r["code"].split(".")[0]
        bar = _bars_map_adj(rows60.get(c6, []), dates, adj.get(r["code"], {}))
        w = _walk_seg(bar, int(r["s"]), int(r["e"]))
        if w:
            w = {k: v for k, v in w.items() if k != "pts"}
            w["source"] = "nas60"
            out[r["code"]] = w
    return out


def _facts_month_actuals(picked: list[dict], month_id: str) -> dict[str, dict]:
    """冻结 panel 里已有的完整月份：直接链 r_chain。"""
    if not FACTS60.is_file():
        return {}
    con = duckdb.connect(str(FACTS60), read_only=True)
    out: dict[str, dict] = {}
    try:
        codes = [r["code"] for r in picked]
        slots: dict[str, dict[int, float]] = {}
        for i in range(0, len(codes), 400):
            part = codes[i:i + 400]
            ph = ",".join("?" for _ in part)
            rows = con.execute(
                f"SELECT p.identity, p.mn, p.h, p.r_chain FROM panel p "
                f"JOIN month_flags f ON f.identity=p.identity AND f.month_id=p.month_id "
                f"WHERE p.month_id=? AND p.identity IN ({ph}) "
                f"AND f.full_month_int=1 AND NOT f.any_gap_bad AND NOT f.any_extreme",
                [month_id, *part]).fetchall()
            for ident, mn, h, rc in rows:
                if rc is None:
                    continue
                slots.setdefault(str(ident), {})[(int(mn) - 1) * 4 + (int(h) - 1)] = float(rc)
        for r in picked:
            rc = slots.get(r["code"], {})
            s_, e_ = int(r["s"]), int(r["e"])
            if not rc or any(k not in rc for k in range(s_, e_ + 1)):
                continue
            cum, ys, labels = 1.0, [], []
            for k in range(s_, e_ + 1):
                cum *= (1.0 + rc[k])
                ys.append(cum - 1.0)
                labels.append(k)
            pts = [[round((i + 1) / (e_ - s_ + 1), 4), int(round(y * 10000))]
                   for i, y in enumerate(ys)]
            out[r["code"]] = {
                "pts": pts, "e_bp": pts[-1][1], "n_bars": len(pts),
                "covered": len(pts), "total": e_ - s_ + 1,
                "complete": len(pts) == (e_ - s_ + 1),
                "lk": min(range(len(ys)), key=lambda i: ys[i]) + 1,
                "hk": max(range(len(ys)), key=lambda i: ys[i]) + 1,
                "source": "frozen",
            }
    finally:
        con.close()
    return out


_SESS: dict[str, list[str]] | None = None
_SESS_STAMP: tuple | None = None
_MONTH_ENTRY_CACHE: dict = {"at": 0.0, "map": {}}


def _sessions_by_month() -> dict[str, list[str]]:
    """真实交易日历（date, month_id, mn）→ {month_id: [date...]}，按 mn 排序。"""
    global _SESS, _SESS_STAMP
    path = SESSIONS if SESSIONS.is_file() else SESSIONS_FROZEN
    try:
        stamp = (str(path), path.stat().st_mtime_ns)
    except OSError:
        stamp = (str(path), None)
    if _SESS is not None and _SESS_STAMP == stamp:
        return _SESS
    out: dict[str, list[tuple[int, str]]] = {}
    try:
        with path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    out.setdefault(row["month_id"], []).append(
                        (int(row["mn"]), row["date"]))
                except (KeyError, ValueError):
                    continue
    except OSError:
        out = {}
    _SESS = {k: [d for _, d in sorted(v)] for k, v in out.items()}
    _SESS_STAMP = stamp
    return _SESS


def _month_dates(month_id: str, start_at: str, end_at: str) -> tuple[list[str], bool]:
    """某个流月的交易日与「是否真实」。有真实交易日就用真实的；未来月份按工作日外推。"""
    real = _sessions_by_month().get(month_id)
    if real:
        return real, True
    try:
        d = datetime.fromisoformat(str(start_at)[:10]).date()
        d1 = datetime.fromisoformat(str(end_at)[:10]).date()
    except ValueError:
        return [], False
    out = []
    while d <= d1:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d = d + timedelta(days=1)
    return out, False


def _month_entry(offset: int) -> dict | None:
    """按 offset 取流月（月 id / 起止）。缓存 5 分钟，跨月自动过期。"""
    now = time.time()
    if now - _MONTH_ENTRY_CACHE["at"] > 300 or not _MONTH_ENTRY_CACHE["map"]:
        import api
        con = api.connect("ashare")
        try:
            cal = api.calendar_now(con)
        finally:
            con.close()
        _MONTH_ENTRY_CACHE["map"] = {m["offset"]: m for m in cal["months"]}
        _MONTH_ENTRY_CACHE["at"] = now
    return _MONTH_ENTRY_CACHE["map"].get(offset)


def _date_for_day(dates: list[str], day: int,
                  month_exact: bool = True) -> tuple[str | None, bool]:
    """流月内第 day 个交易日 → 日期；超出已知日历就按工作日外推（不是官方休市日历）。"""
    if not dates:
        return None, False
    if 1 <= day <= len(dates):
        return dates[day - 1], month_exact
    d = datetime.fromisoformat(dates[-1]).date()
    extra = day - len(dates)
    while extra > 0:
        d = d + timedelta(days=1)
        if d.weekday() < 5:
            extra -= 1
    return d.isoformat(), False


def _seg_stats(r: dict, direction: str) -> dict:
    """段自己的稳健度/一致性：阈值与流月机会表同一套常量（api.stability_level）。"""
    try:
        import api
    except Exception:
        return {}
    mids = [x.strip() for x in (r.get("month_ids") or "").split(";") if x.strip()]
    try:
        rets = [float(x) for x in (r.get("month_rets") or "").split(";") if x.strip()]
    except ValueError:
        return {}
    n = len(rets)
    if n < 2 or len(mids) != n:
        return {}
    up = direction != "down"
    vals = rets if up else [-r for r in rets]
    total = sum(vals)
    mean = total / n
    std = (sum((v - mean) ** 2 for v in vals) / (n - 1)) ** 0.5
    mn, mx = min(rets), max(rets)
    margin = mn if up else -mx
    sum_pos = sum(v for v in vals if v > 0)
    share = (max(vals) / sum_pos) if sum_pos > 0 else None
    rng = mx - mn
    level, level_cn, reason = api.stability_level(direction, n, margin, share, rng)
    best_i = max(range(n), key=lambda i: rets[i])
    worst_i = min(range(n), key=lambda i: rets[i])
    cv = (std / mean) if mean > 0 else None
    cons_level = "—" if cv is None else (
        "一致" if cv <= api.CONS_GOOD_CV else "差异大" if cv > api.CONS_BAD_CV else "一般")
    floor = 1 if mn >= api.CONS_FLOOR else 0
    # 展示值统一用「百分数」（与 min_pct/max_pct 同单位）；stab/cons 内部仍用小数
    best_pct = float(r.get("max_pct") if r.get("max_pct") is not None else mx * 100.0)
    worst_pct = float(r.get("min_pct") if r.get("min_pct") is not None else mn * 100.0)
    return {
        "best_pct": best_pct, "best_month": mids[best_i], "best_year": int(mids[best_i][:4]),
        "worst_pct": worst_pct, "worst_month": mids[worst_i], "worst_year": int(mids[worst_i][:4]),
        "stab": {"level": level, "cn": level_cn, "reason": reason, "n": n,
                 "margin": margin, "share": share, "range": rng},
        "cons": {"level": cons_level, "cv": cv, "min": margin, "n": n, "total": n,
                 "reverse": 0, "floor": floor},
    }


def _row_has_month(code: str, pillar: str, s: int, e: int, month_id: str) -> bool:
    """库里的这条段是否把某个月计入了（用于决定图上要不要补那条线）。"""
    try:
        with _LOCK:
            row = _con().execute(
                "SELECT month_ids FROM read_parquet(?) WHERE code=? AND pillar=? AND s=? AND e=? LIMIT 1",
                [_parquet_glob(), code, pillar, s, e]).fetchone()
    except Exception:
        return False
    return bool(row and month_id in (row[0] or ""))


def _month_hist_from_store(code: str, month_id: str, s: int, e: int) -> dict | None:
    """用 NAS 日更 60 分钟线还原某个已入库新月份的同段路径。"""
    dates = _sessions_by_month().get(month_id)
    if not dates or not LIVE60.is_file():
        return None
    c6 = code.split(".")[0]
    rows = _live60_rows([c6], dates).get(c6, [])
    adj = _adj_rows([code], dates[0], dates[-1]).get(code, {})
    bar = _bars_map_adj(rows, dates, adj)
    w = _walk_seg(bar, s, e)
    if not w or not w.get("complete"):
        return None
    first = bar[s][0][:10]
    last = bar[e][0][:10]
    return {"month_id": month_id, "year": int(month_id[:4]),
            "first": first, "last": last, "start": first, "end": last,
            "n": e - s + 1, "e_bp": w["e_bp"], "lk": w["lk"], "hk": w["hk"],
            "pts": w["pts"], "source": "nas60"}


EXTRA_PANEL_GLOB = str(ROOT / "slot88_full_market_v1/work/append_*/new_panel.parquet")


def _month_hist_from_panel(code: str, month_id: str, s: int, e: int) -> dict | None:
    """用补库时建的面板（60m → r_chain）还原新月份的同段路径。"""
    dates = _sessions_by_month().get(month_id)
    if not dates:
        return None
    try:
        with _LOCK:
            rows = _con().execute(
                'SELECT mn, h, r_chain FROM read_parquet(?) WHERE identity=? AND month_id=? '
                'AND full_month_int=1 AND NOT any_gap_bad AND NOT any_extreme',
                [EXTRA_PANEL_GLOB, code, month_id]).fetchall()
    except Exception:
        return None
    if not rows:
        return None
    rc = {}
    for mn, h, r in rows:
        if r is None:
            continue
        j = (int(mn) - 1) * 4 + (int(h) - 1)
        if 0 <= j < 88:
            rc[j] = float(r)
    if any(k not in rc for k in range(s, e + 1)):
        return None
    cum, pts = 1.0, []
    n = e - s + 1
    for i, k in enumerate(range(s, e + 1)):
        cum *= (1.0 + rc[k])
        pts.append([round((i + 1) / n, 4), int(round((cum - 1.0) * 10000))])
    ys = [p[1] for p in pts]
    return {"month_id": month_id, "year": int(str(month_id)[:4]),
            "first": dates[s // 4] if s // 4 < len(dates) else None,
            "last": dates[e // 4] if e // 4 < len(dates) else None,
            "start": dates[s // 4] if s // 4 < len(dates) else None,
            "end": dates[e // 4] if e // 4 < len(dates) else None,
            "n": n, "e_bp": pts[-1][1],
            "lk": min(range(n), key=lambda i: ys[i]) + 1,
            "hk": max(range(n), key=lambda i: ys[i]) + 1,
            "pts": pts, "source": "append_panel"}


def segment_path(q: dict) -> dict:
    """只画这一条段：历史各次同段的小时收盘线，不掺整月。"""
    code = _first(q, "code").strip().upper()
    if not CODE_RE.match(code):
        raise SegError("代码格式应为 000001.SZ")
    pillar = _first(q, "pillar")
    if pillar not in PILLARS:
        raise SegError("未知流月柱")
    s = _int(q, "s", 0, 0, 87)
    e = _int(q, "e", 0, 0, 87)
    if s > e:
        s, e = e, s
    offset = _int(q, "offset", 0, -260, 260)
    live60_stamp = int(LIVE60.stat().st_mtime) if LIVE60.is_file() else 0
    key = (code, pillar, s, e, offset, live60_stamp, _parquet_stamp())
    if key in _PATH_CACHE:
        return _PATH_CACHE[key]
    if not FACTS60.is_file():
        raise SegUnavailable("小时线源不存在，无法画这段走势")
    import duckdb
    con = duckdb.connect(str(FACTS60), read_only=True)
    try:
        df = con.execute("""
          SELECT p.month_id, p.mn, p.h, p.qo, p.qc, p.r_chain, p.date
          FROM panel p
          JOIN month_flags f ON f.identity=p.identity AND f.month_id=p.month_id
          JOIN cm c ON c.month_id=p.month_id
          WHERE p.code=? AND p.pillar=?
            AND f.full_month_int=1 AND NOT f.any_gap_bad AND NOT f.any_extreme
          ORDER BY p.month_id, p.mn, p.h
        """, [code, pillar]).fetchall()
    finally:
        con.close()
    hist = []
    for mid in sorted({r[0] for r in df}):
        rows = [r for r in df if r[0] == mid]
        rc, dates = {}, {}
        for _, mn, h, o, c, chain, dt_ in rows:
            slot = (int(mn) - 1) * 4 + (int(h) - 1)
            if chain is not None:
                rc[slot], dates[slot] = float(chain), str(dt_)[:10]
        if any(k not in rc for k in range(s, e + 1)):
            continue
        cum, ys, pts = 1.0, [], [[0.0, 0]]
        for i, k in enumerate(range(s, e + 1)):
            cum *= (1.0 + rc[k])
            y = cum - 1.0
            ys.append(y)
            pts.append([round((i + 1) / (e - s + 1), 4), int(round(y * 10000))])
        lk = min(range(len(ys)), key=lambda i: ys[i]) + 1
        hk = max(range(len(ys)), key=lambda i: ys[i]) + 1
        hist.append({
            "month_id": mid, "year": int(str(mid)[:4]),
            "first": dates.get(s), "last": dates.get(e),
            "start": dates.get(s), "end": dates.get(e),
            "n": e - s + 1, "e_bp": pts[-1][1],
            "lk": lk, "hk": hk, "pts": pts,
        })
    # 补进库的新月份（facts60 里没有）：库里计入了、图上也要有线，否则会「表说 6 次、图画 5 条」
    present = {h["month_id"] for h in hist}
    for mid in sorted(_library_frontier()[1]):
        if mid in present:
            continue
        if _row_has_month(code, pillar, s, e, mid):
            h = _month_hist_from_store(code, mid, s, e) or _month_hist_from_panel(code, mid, s, e)
            if h:
                hist.append(h)
    hist.sort(key=lambda h: h["month_id"])
    ctx = _live_now(offset)
    if ctx.get("status") == "ok" and ctx.get("now_pos") is not None:
        now = ctx["now_pos"]
        if s <= now <= e:
            progress = max(0.0, min(1.0, (now - s) / (e - s + 1)))
            status, text = "ongoing", f"进行中 {progress * 100:.0f}%"
        elif s > now:
            status, text = "upcoming", f"未到（还差 {(s - now) / 4:.2f} 个交易日）"
        else:
            status, text = "passed", f"已过去 {(now - e) / 4:.2f} 个交易日"
    else:
        status, text = "not_current", "非本月"
    live = {"status": status, "pts": [], "note": "本月 60 分钟线未同步，无法画本月这段"}
    dates: list[str] = []
    month_exact = True
    axis: dict = {}
    if ctx.get("status") == "ok" and ctx.get("pillar") == pillar and LIVE_DB.is_file():
        day_bars: dict[str, tuple] = {}
        try:
            lc = sqlite3.connect(LIVE_DB.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                meta = dict(lc.execute("SELECT k,v FROM meta").fetchall())
                start_at = str(meta.get("start_at") or "")[:10]
                latest = str(meta.get("latest_trade_date") or "")
                dates = [r[0] for r in lc.execute(
                    "SELECT DISTINCT date FROM bars WHERE date>=? AND date<=? ORDER BY date",
                    (start_at, latest)).fetchall()]
                code6 = code.split(".")[0]
                day_bars = {r[0]: (float(r[1]), float(r[2])) for r in lc.execute(
                    "SELECT date,open,close FROM bars WHERE code=? AND date>=? AND date<=?",
                    (code6, start_at, latest)).fetchall()}
            finally:
                lc.close()
        except sqlite3.Error:
            dates, day_bars = [], {}
        p60 = _live60_path(code, s, e, dates, {d: v[1] for d, v in day_bars.items()})
        if p60:
            live.update(p60)
            live["status"] = status
        elif dates:
            # 没有 60 分钟线时的兜底：日线近似（旧行为，文案里写明）
            start_day = s // 4 + 1
            end_day = e // 4 + 1
            start_0930 = (s % 4) == 0
            base = None
            pts = []
            base_date = None
            for d in range(start_day, end_day + 1):
                if d > len(dates):
                    break
                dt_ = dates[d - 1]
                if dt_ not in day_bars:
                    continue
                o, c = day_bars[dt_]
                if base is None:
                    base = o if (start_0930 and d == start_day) else c
                    base_date = dt_
                    if start_0930 and d == start_day:
                        pts.append([0.0, 0])
                x = (d * 4 - s) / (e - s + 1)
                x = max(0.0, min(1.0, x))
                pts.append([round(x, 4), int(round((c / base - 1.0) * 10000))])
            if pts:
                live.update({
                    "pts": pts, "e_bp": pts[-1][1], "n_bars": len(pts),
                    "base_date": base_date,
                    "approx": not start_0930,
                    "note": ("本月线从段首开盘=0 起算（日线）" if start_0930
                             else f"本月线为日线近似：从 {base_date} 收盘=0 起算，段首 {SLOT_TIMES[s % 4]} 的价本机没有"),
                })
    if not dates and offset != 0:
        # 往月：真实交易日；未来月：按工作日外推（exact=False）
        ent = _month_entry(offset)
        if ent:
            dates, month_exact = _month_dates(ent["month_id"], ent["start_at"], ent["end_at"])
    axis: dict = {}
    if dates:
        i0, i1 = s // 4, e // 4
        im = (i0 + i1) // 2
        a0 = _date_for_day(dates, i0 + 1, month_exact)
        am = _date_for_day(dates, im + 1, month_exact)
        a1 = _date_for_day(dates, i1 + 1, month_exact)
        axis = {"start": a0[0], "start_exact": a0[1],
                "mid": am[0], "mid_exact": am[1],
                "end": a1[0], "end_exact": a1[1],
                "days": len(dates), "latest": dates[-1], "exact": month_exact}
    name = ""
    try:
        mc = sqlite3.connect(config.ASHARE_DB.resolve().as_uri() + "?mode=ro", uri=True)
        row = mc.execute("SELECT name FROM securities WHERE identity_code=?", (code,)).fetchone()
        mc.close()
        name = row[0] if row else ""
    except sqlite3.Error:
        pass
    up_n = sum(1 for h in hist if h["e_bp"] > 0)
    down_n = sum(1 for h in hist if h["e_bp"] < 0)
    out = {
        "code": code, "name": name, "pillar": pillar, "s": s, "e": e,
        "start_day": s // 4 + 1, "start_time": SLOT_TIMES[s % 4],
        "end_day": e // 4 + 1, "end_time": SLOT_ENDS[e % 4],
        "length_days": round((e - s + 1) / 4, 2),
        "status": status, "status_text": text, "now_text": ctx.get("now_text"),
        "hist": hist, "n_hist": len(hist), "live": live, "axis": axis,
        "stats": {"up_n": up_n, "down_n": down_n, "flat_n": 0,
                  "valid_n": len(hist), "up_rate": (up_n / len(hist)) if hist else None},
    }
    _PATH_CACHE[key] = out
    if len(_PATH_CACHE) > 128:
        _PATH_CACHE.pop(next(iter(_PATH_CACHE)))
    return out


def dispatch(path: str, q: dict) -> dict:
    if path == "/api/seg/meta":
        return meta()
    if path == "/api/seg/query":
        return query(q)
    if path == "/api/seg/count":
        return count(q)
    if path == "/api/seg/board":
        return board(q)
    if path == "/api/seg/path":
        return segment_path(q)
    raise SegError("未知接口")
