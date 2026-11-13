# -*- coding: utf-8 -*-
"""流月卦只读 API：按节气流月窗口聚合 raw OHLC，用冻结起卦器铸卦。

口径（用户 2026-11-11 裁定）：
- 月卦 = 所选流月的上一流月整月K线定卦（与参考系统"上一自然月"规则平行）；
- 旬(1-3)/6段：已走完=定卦，进行中=动态卦(标未定)，未来=无卦，缺数据=不足不Cast；
- 100%段：本段(本月)+历次同段各铸一卦，按段所跨交易日的日线 raw OHLC（60分钟线无高/低）；
- 价格一律 raw 不复权；窗口边界复用 api._win_bounds 的 09:30 归属规则。
全部数据访问只读；不写任何库。
"""
from __future__ import annotations

import csv
import sqlite3
import threading
import time
from bisect import bisect_left, bisect_right
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    import duckdb
except ImportError:  # 与 seg_api 同策略：缺 duckdb 时本页 503，不影响主系统
    duckdb = None

import config
import api
from bagua import (cast_ohlc, gua_side, gaodao_side, consensus,
                   gaodao_text, gaodao_category, gaodao_is_fallback)

TZ = timezone(timedelta(hours=8))
ROOT = config.ROOT
NAS_SOURCE_DB = Path(str(ROOT / "source_input/nas_source.duckdb"))
LIVE_DB = Path(str(ROOT / "source_input/live/current_month.sqlite"))
INDEX_MIRROR = Path(str(ROOT / "source_mirror/正式源/A股指数日线_tushare/index_daily.parquet"))
INDEX_EXT = Path(str(ROOT / "source_mirror_live/index_ext/index_daily_ext.parquet"))
SESSIONS_CSV = Path(str(ROOT / "slot88_full_market_v1/data/market_sessions_ext.csv"))

DISCLAIMER = ("卦象为传统文化展示，不构成投资建议。历史检验（节卦/甲子卦/段卦等研究）"
              "均未发现卦象对收益有预测能力，请勿将其作为选股或买卖依据。")
PRICE_PLANE = "raw"

_LOCK = threading.Lock()


class GuaError(Exception):
    pass


class GuaUnavailable(Exception):
    pass


def _first(q: dict, key: str, default: str = "") -> str:
    v = q.get(key, [default])
    return (v[0] if v else default).strip()


def _int(q: dict, key: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(_first(q, key, str(default)))
    except ValueError:
        raise GuaError(f"参数 {key} 必须是整数")
    if not lo <= v <= hi:
        raise GuaError(f"参数 {key} 超出范围 [{lo}, {hi}]")
    return v


def _stamp(p: Path) -> int:
    try:
        return p.stat().st_mtime_ns
    except OSError:
        return 0


# ---------------------------------------------------------------- 历法时间线

_TIMELINE_CACHE: dict = {"at": 0.0, "data": None}


def _timeline(con) -> list[dict]:
    """过去60 + 本月 + 未来(至2050-12)，offset 制；5 分钟缓存（交节瞬间会换月）。"""
    now = time.monotonic()
    if _TIMELINE_CACHE["data"] is not None and now - _TIMELINE_CACHE["at"] < 300:
        return _TIMELINE_CACHE["data"]
    cal = api.calendar_context(con)
    rows = cal["past"] + ([cal["current"]] if cal["current"] else []) + cal["future"]
    months = []
    for offset, row in zip(range(-len(cal["past"]), len(rows) - len(cal["past"])), rows):
        months.append({**{k: row[k] for k in ("month_id", "solar_year", "month_no",
                                              "start_term", "start_at", "end_at",
                                              "month_pillar")},
                       "offset": offset, "label": api.month_label(offset),
                       "is_current": offset == 0})
    _TIMELINE_CACHE.update(at=now, data=months)
    return months


def _month_by_offset(con, offset: int) -> dict:
    months = _timeline(con)
    for m in months:
        if m["offset"] == offset:
            return m
    raise GuaError(f"月份 offset={offset} 不在可选时间线内")


def _prev_month(con, month: dict) -> dict:
    row = api._rows(con, "SELECT month_id, solar_year, month_no, start_term, start_at,"
                         " end_at, month_pillar FROM calendar_months"
                         " WHERE end_at<=? ORDER BY start_at DESC LIMIT 1",
                    (month["start_at"],))
    if not row:
        raise GuaUnavailable("冻结历法中找不到上一流月")
    return row[0]


# ---------------------------------------------------------------- 交易日历

_SESS_CACHE: dict = {"stamp": 0, "dates": [], "by_date": {}}


def _sessions() -> tuple[list[str], dict]:
    st = _stamp(SESSIONS_CSV)
    if st != _SESS_CACHE["stamp"]:
        dates, by_date = [], {}
        with SESSIONS_CSV.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                dates.append(r["date"])
                by_date[r["date"]] = r["month_id"]
        dates.sort()
        _SESS_CACHE.update(stamp=st, dates=dates, by_date=by_date)
    return _SESS_CACHE["dates"], _SESS_CACHE["by_date"]


def _sessions_between(d0: str, d1: str) -> list[str]:
    dates, _ = _sessions()
    return dates[bisect_left(dates, d0):bisect_right(dates, d1)]


# ---------------------------------------------------------------- 标的解析

def _resolve(con, code: str) -> dict:
    code = code.strip().upper()
    if not code:
        raise GuaError("请输入股票代码或名称")
    row = None
    if len(code) == 6 and code.isdigit():
        rows = api._rows(con, "SELECT identity_code, name, security_type, listed_date,"
                              " delisted_date FROM securities"
                              " WHERE identity_code IN (?||'.SH', ?||'.SZ')",
                         (code, code))
        if len(rows) > 1:
            raise GuaError(f"代码 {code} 对应多个标的，请用全码（如 {code}.SH）")
        row = rows[0] if rows else None
    else:
        rows = api._rows(con, "SELECT identity_code, name, security_type, listed_date,"
                              " delisted_date FROM securities WHERE identity_code=?",
                         (code,))
        row = rows[0] if rows else None
        if row is None:
            rows = api._rows(con, "SELECT identity_code, name, security_type, listed_date,"
                                  " delisted_date FROM securities WHERE name=?", (code,))
            row = rows[0] if rows else None
    if row is None:
        raise GuaError(f"未找到标的：{code}（支持 6 位代码 / 全码 / 名称）")
    return dict(row)


# ---------------------------------------------------------------- 日线取数（raw）

_BARS_CACHE: dict = {}


def _bars_stamp() -> tuple:
    return (_stamp(NAS_SOURCE_DB), _stamp(LIVE_DB), _stamp(INDEX_MIRROR), _stamp(INDEX_EXT))


def _daily_bars(code: str, is_index: bool, d0: str, d1: str) -> list[tuple]:
    """窗口相关日线 (date, open, high, low, close)，升序；同日实时源覆盖历史源。"""
    key = (code, d0, d1, _bars_stamp())
    if key in _BARS_CACHE:
        return _BARS_CACHE[key]
    if duckdb is None:
        raise GuaUnavailable("当前 Python 环境没有 duckdb，读不了日线源")
    merged: dict[str, tuple] = {}
    if is_index:
        for p in (INDEX_MIRROR, INDEX_EXT):
            if not p.is_file():
                continue
            con = duckdb.connect()
            try:
                rows = con.execute(
                    "SELECT trade_date, open, high, low, close FROM read_parquet(?)"
                    " WHERE ts_code=? AND trade_date>=? AND trade_date<=?",
                    [str(p), code, d0.replace("-", ""), d1.replace("-", "")]).fetchall()
            finally:
                con.close()
            for td, o, h, l, c in rows:
                td = str(td)
                dt = f"{td[:4]}-{td[4:6]}-{td[6:8]}"
                if None in (o, h, l, c):
                    continue
                merged[dt] = (dt, float(o), float(h), float(l), float(c))
    else:
        if NAS_SOURCE_DB.is_file():
            con = duckdb.connect(str(NAS_SOURCE_DB), read_only=True)
            try:
                rows = con.execute(
                    "SELECT date, open, high, low, close FROM bars"
                    " WHERE code=? AND date>=? AND date<=? ORDER BY date",
                    [code, d0, d1]).fetchall()
            finally:
                con.close()
            for dt_, o, h, l, c in rows:
                if None in (o, h, l, c):
                    continue
                dt = str(dt_)[:10]
                merged[dt] = (dt, float(o), float(h), float(l), float(c))
        if LIVE_DB.is_file():
            lc = sqlite3.connect(LIVE_DB.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                # 实时库 bars.code 存 6 位码（与 nas_source 的全码不同），须去后缀
                rows = lc.execute(
                    "SELECT date, open, high, low, close FROM bars"
                    " WHERE code=? AND date>=? AND date<=? ORDER BY date",
                    (code.split(".")[0], d0, d1)).fetchall()
            finally:
                lc.close()
            for dt_, o, h, l, c in rows:
                if None in (o, h, l, c):
                    continue
                dt = str(dt_)[:10]
                merged[dt] = (dt, float(o), float(h), float(l), float(c))
    out = [merged[k] for k in sorted(merged)]
    if len(_BARS_CACHE) >= 128:
        _BARS_CACHE.clear()
    _BARS_CACHE[key] = out
    return out


def _data_asof(code: str, is_index: bool) -> str:
    """该标的日线最新日期（各源并集的最大值）。"""
    bars = _daily_bars(code, is_index, "1990-01-01", "2200-12-31")
    return bars[-1][0] if bars else ""


# ---------------------------------------------------------------- 铸卦

def _gua_dict(r) -> dict:
    d = r.to_dict()
    gt = gaodao_text(d["state_id"])
    return {
        "gua_name": d["gua_name"], "full_name": d["full_name"],
        "gua_symbol": d["gua_symbol"], "gua_order": d["gua_order"],
        "upper_name": d["upper_name"], "lower_name": d["lower_name"],
        "upper_symbol": d["upper_symbol"], "lower_symbol": d["lower_symbol"],
        "yao_order": d["yao_order"], "yao_name": d["yao_name"],
        "gua_ci": d["gua_ci"], "yao_ci": d["yao_ci"],
        "changed_hexagram_name": d["changed_hexagram_name"],
        "market_judgement": d["market_judgement"],
        "action_signal": d["action_signal"], "state_id": d["state_id"],
        "gua_side": gua_side(d["action_signal"]),
        # 高岛易断（第二套解读）+ 两套解读的双好/双差共识（同参考系统 consensus.py）
        "gaodao_text": gt, "gaodao_side": gaodao_side(gt),
        "gaodao_category": gaodao_category(d["state_id"]),
        "gaodao_fallback": gaodao_is_fallback(d["state_id"]),
        "consensus": consensus(d["action_signal"], gt),
    }


def _cast_window(no: int, name: str, start, end, bars_all: list[tuple],
                 sec: dict, now: datetime, strict_missing: bool = True) -> dict:
    """单窗口四态：fixed / dynamic / none / insufficient。start/end 为 date。

    strict_missing=True（月/旬/段默认）：缺任一交易日即判 insufficient、不Cast。
    strict_missing=False（历次流月用）：窗口已由 monthly_facts 认证为完整月，个别交易日
    无K线属停牌（当日无成交），按可用K线聚合仍能定卦——不因停牌把整月错报成「数据不足」。
    """
    today = now.date()
    s, e = start.isoformat(), end.isoformat()
    base = {"no": no, "name": name, "start": s, "end": e,
            "status": "none", "ohlc": None, "digit_sums": None,
            "n_bars": 0, "missing_days": [], "gua": None, "note": ""}
    if start > today:
        base["note"] = "未开始"
        return base
    bars = [b for b in bars_all if s <= b[0] <= e]
    base["n_bars"] = len(bars)
    # 上市前的交易日不算缺数据
    eff_start = s
    if sec.get("listed_date") and str(sec["listed_date"])[:10] > s:
        eff_start = str(sec["listed_date"])[:10]
    expected = _sessions_between(eff_start, e)
    have = {b[0] for b in bars}
    missing = [d for d in expected if d not in have]
    if end < today:
        if sec.get("delisted_date") and str(sec["delisted_date"])[:10] < e:
            base.update(status="insufficient", missing_days=missing,
                        note=f"该窗口晚于退市日 {str(sec['delisted_date'])[:10]}，数据不足不Cast")
            return base
        if missing and strict_missing:
            base.update(status="insufficient", missing_days=missing[:31],
                        note=f"缺 {len(missing)} 个交易日，数据不足不Cast")
            return base
        if not bars:
            base.update(status="insufficient", note="窗口内无K线，数据不足不Cast")
            return base
        status = "fixed"
        if missing:
            base["note"] = f"定卦（{len(missing)} 个交易日无成交/停牌，按可用K线聚合）"
    else:
        if not bars:
            base.update(status="insufficient",
                        note="进行中窗口暂无K线（数据源未覆盖），数据不足不Cast")
            return base
        status = "dynamic"
        if missing:
            base["note"] = f"进行中，卦未定；数据尚缺 {len(missing)} 个交易日（含未更新/停牌）"
    o, h, l, c = bars[0][1], max(b[2] for b in bars), min(b[3] for b in bars), bars[-1][4]
    r = cast_ohlc(o, h, l, c)
    base.update(status=status,
                ohlc={"open": r.open_price, "high": r.high_price,
                      "low": r.low_price, "close": r.close_price},
                digit_sums={"open": r.open_digit_sum, "high": r.high_digit_sum,
                            "low": r.low_digit_sum, "close": r.close_digit_sum},
                gua=_gua_dict(r))
    if status == "dynamic" and not base["note"]:
        base["note"] = "进行中，卦未定（随后续K线变动）"
    elif status == "fixed":
        base["note"] = "窗口已走完，定卦"
    return base


# ---------------------------------------------------------------- 端点

def meta() -> dict:
    con = api.connect()
    try:
        months = _timeline(con)
    finally:
        con.close()
    current = next((m for m in months if m["is_current"]), None)
    sources = {}
    if duckdb is not None:
        if NAS_SOURCE_DB.is_file():
            con = duckdb.connect(str(NAS_SOURCE_DB), read_only=True)
            try:
                sources["stock_daily"] = str(con.execute(
                    "SELECT max(date) FROM bars").fetchone()[0])[:10]
            finally:
                con.close()
        if INDEX_MIRROR.is_file():
            con = duckdb.connect()
            try:
                td = con.execute("SELECT max(trade_date) FROM read_parquet(?)",
                                 [str(INDEX_MIRROR)]).fetchone()[0]
                td = str(td)
                sources["index_mirror"] = f"{td[:4]}-{td[4:6]}-{td[6:8]}"
            finally:
                con.close()
    if LIVE_DB.is_file():
        lc = sqlite3.connect(LIVE_DB.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            sources["live"] = str(lc.execute(
                "SELECT max(date) FROM bars").fetchone()[0] or "")[:10]
        finally:
            lc.close()
    if INDEX_EXT.is_file() and duckdb is not None:
        con = duckdb.connect()
        try:
            td = str(con.execute("SELECT max(trade_date) FROM read_parquet(?)",
                                 [str(INDEX_EXT)]).fetchone()[0])
            sources["index_ext"] = f"{td[:4]}-{td[4:6]}-{td[6:8]}"
        finally:
            con.close()
    else:
        sources["index_ext"] = None
    return {"asof": datetime.now(TZ).isoformat(timespec="seconds"),
            "current": current, "months": months, "sources": sources,
            "price_plane": PRICE_PLANE, "disclaimer": DISCLAIMER}


def cast(q: dict) -> dict:
    code_in = _first(q, "code", "000001.SH")
    layer = _first(q, "layer", "month").lower()
    if layer not in ("month", "xun", "week"):
        raise GuaError("layer 必须是 month / xun / week")
    offset = _int(q, "offset", 0, -60, 400)
    now = datetime.now(TZ)
    con = api.connect()
    try:
        sec = _resolve(con, code_in)
        month = _month_by_offset(con, offset)
        anchor = _prev_month(con, month) if layer == "month" else None
    finally:
        con.close()
    code = sec["identity_code"]
    is_index = sec["security_type"] == "INDEX"
    if layer == "month":
        start, end = api._win_bounds(anchor["start_at"], anchor["end_at"], None, None)
        bars = _daily_bars(code, is_index, start.isoformat(), end.isoformat())
        win = _cast_window(1, f"{anchor['month_id']} {anchor['month_pillar']} 整月",
                           start, end, bars, sec, now)
        windows = [win]
    else:
        start_at, end_at = month["start_at"], month["end_at"]
        if layer == "xun":
            specs = [(x, f"{['上旬','中旬','下旬'][x-1]}", (x - 1) * 10,
                      None if x == 3 else x * 10) for x in (1, 2, 3)]
        else:
            specs = [(w, f"第{w}段", (w - 1) * 5, None if w == 6 else w * 5)
                     for w in range(1, 7)]
        d0, d1 = api._win_bounds(start_at, end_at, 0, None)
        bars = _daily_bars(code, is_index, d0.isoformat(), d1.isoformat())
        windows = []
        for no, name, df, dt in specs:
            start, end = api._win_bounds(start_at, end_at, df, dt)
            windows.append(_cast_window(no, name, start, end, bars, sec, now))
    return {"code": code, "name": sec["name"], "security_type": sec["security_type"],
            "layer": layer, "offset": offset, "month": month,
            "anchor_month": anchor, "windows": windows,
            "data_asof": _data_asof(code, is_index),
            "price_plane": PRICE_PLANE, "disclaimer": DISCLAIMER}


def seg_cast(q: dict) -> dict:
    """100%段抽屉卦：本段（本月）+ 历次同段各铸一卦。

    段在 88 格（22 交易日×4 根 60 分钟）上定义，但 60 分钟线只有开/收、无高/低，
    无法据其铸卦；故段卦一律按「段所跨交易日」的日线 raw OHLC 铸（开=首日开、
    高=区间最高、低=区间最低、收=末日收），与月/旬/六段层同口径。各次同段的日期
    区间直接取 seg_api.segment_path 的 hist[].start/end 与本月 axis，不自造边界。
    """
    import seg_api
    code_in = _first(q, "code")
    pillar = _first(q, "pillar")
    s = _int(q, "s", 0, 0, 87)
    e = _int(q, "e", 0, 0, 87)
    if s > e:
        s, e = e, s
    offset = _int(q, "offset", 0, -260, 260)
    try:
        path = seg_api.segment_path({"code": [code_in], "pillar": [pillar],
                                     "s": [str(s)], "e": [str(e)],
                                     "offset": [str(offset)]})
    except seg_api.SegUnavailable as exc:
        raise GuaUnavailable(str(exc))
    except seg_api.SegError as exc:
        raise GuaError(str(exc))
    code = path["code"]
    con = api.connect()
    try:
        sec = _resolve(con, code)
        cur = _month_by_offset(con, 0)
    finally:
        con.close()
    is_index = sec["security_type"] == "INDEX"
    now = datetime.now(TZ)

    hist_guas = []
    for h in path.get("hist", []):
        if not h.get("start") or not h.get("end"):
            continue
        start, end = date.fromisoformat(h["start"]), date.fromisoformat(h["end"])
        bars = _daily_bars(code, is_index, start.isoformat(), end.isoformat())
        win = _cast_window(len(hist_guas) + 1, h["month_id"], start, end, bars, sec, now)
        win["month_id"] = h["month_id"]
        win["year"] = h.get("year")
        win["e_bp"] = h.get("e_bp")
        hist_guas.append(win)

    live_gua = None
    lv, ax = path.get("live", {}), path.get("axis", {})
    if lv.get("status") in ("ongoing", "upcoming", "passed") and ax.get("start") and ax.get("end"):
        start, end = date.fromisoformat(ax["start"]), date.fromisoformat(ax["end"])
        bars = _daily_bars(code, is_index, start.isoformat(), end.isoformat())
        live_gua = _cast_window(0, "本段（本月）", start, end, bars, sec, now)
        live_gua["month_id"] = cur["month_id"]
        live_gua["seg_status"] = lv.get("status")

    return {"code": code, "name": sec["name"], "security_type": sec["security_type"],
            "pillar": pillar, "s": s, "e": e,
            "start_day": path.get("start_day"), "end_day": path.get("end_day"),
            "start_time": path.get("start_time"), "end_time": path.get("end_time"),
            "offset": offset, "current_month_id": cur["month_id"],
            "live_gua": live_gua, "hist_guas": hist_guas, "n_hist": len(hist_guas),
            "data_asof": _data_asof(code, is_index),
            "price_plane": PRICE_PLANE, "disclaimer": DISCLAIMER,
            "cast_basis": "段所跨交易日的日线 raw OHLC（60分钟线无高/低，故用日线）"}


def pillar_guas(q: dict) -> dict:
    """某标的某流月柱的「历次流月」卦：每次按该流月自身整月K的日线 raw OHLC 铸卦。

    来源 api.pillar_detail 同表（monthly_facts WHERE identity_code, flow_pillar），
    取每次的 first_date/last_date 区间（月内实际首末交易日）→ _daily_bars → cast。
    与「本卡锚上一流月」是两回事：这里给的是每个历史流月**自身**的月K卦，供逐次对照。
    """
    code_in = _first(q, "code")
    pillar = _first(q, "pillar")
    con = api.connect()
    try:
        sec = _resolve(con, code_in)
        rows = api._rows(con, "SELECT month_id, first_date, last_date, body_return, direction"
                              " FROM monthly_facts WHERE identity_code=? AND flow_pillar=?"
                              " ORDER BY month_id", (sec["identity_code"], pillar))
    finally:
        con.close()
    is_index = sec["security_type"] == "INDEX"
    now = datetime.now(TZ)
    guas = []
    for i, r in enumerate(rows):
        fd, ld = str(r.get("first_date") or "")[:10], str(r.get("last_date") or "")[:10]
        if not fd or not ld:
            continue
        try:
            d0, d1 = date.fromisoformat(fd), date.fromisoformat(ld)
        except ValueError:
            continue
        bars = _daily_bars(sec["identity_code"], is_index, fd, ld)
        win = _cast_window(i + 1, r["month_id"], d0, d1, bars, sec, now, strict_missing=False)
        win["month_id"] = r["month_id"]
        win["body_return"] = r.get("body_return")
        win["direction"] = r.get("direction")
        guas.append(win)
    return {"code": sec["identity_code"], "name": sec["name"], "pillar": pillar,
            "n": len(guas), "guas": guas, "price_plane": PRICE_PLANE,
            "cast_basis": "各次按该流月自身整月K（raw OHLC）铸卦"}


def dispatch(path: str, q: dict) -> dict:
    if path == "/api/gua/meta":
        return meta()
    if path == "/api/gua/cast":
        return cast(q)
    if path == "/api/gua/seg":
        return seg_cast(q)
    if path == "/api/gua/pillar":
        return pillar_guas(q)
    raise GuaError(f"未知端点：{path}")