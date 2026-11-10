"""北京时间（固定 UTC+8）唯一口径助手：本项目所有「今天 / 当前 / 到点没有」判断只走这里。

Why（用户裁定，2026-10-22）：本机系统时区是 America/Los_Angeles（PDT，与北京差 15 小时，
同一瞬间往往已跨日），所以 naive `datetime.now()` 与「本机日」在业务上一律不可用。
中国自 1991 年后无夏令时，固定偏移与 `ZoneInfo("Asia/Shanghai")` 逐时刻等价，
因此**本模块刻意不引入 zoneinfo/tzdata**（交付机常缺 tzdata，是已实测过的翻车点）。

约定：
  * 内部只用「固定 UTC+8 的 aware datetime」，不用 naive、不用 UTC、不用本机时区；
  * 对外字段一律 `_bjt` 后缀，输出形如 `2026-11-09T20:00:15+08:00` 的串，前端不做时区换算；
  * 库内 `calendar_months.start_at/end_at` 是带 `+08:00` 的串（个别历史导出写成 `+08`），
    统一用 `parse_bjt()` 解析，不做字符串比较。
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

# 固定偏移：不做夏令时推断，不读系统时区，不读 tzdata。
BJT = timezone(timedelta(hours=8), "UTC+08:00")

OPEN_TIME = (9, 30)     # A股开盘（北京时间）
CLOSE_TIME = (15, 0)    # A股收盘（北京时间）：用于「是否已收盘入库」的下限判断

_OFFSET_NO_MINUTES = re.compile(r"([+-]\d{2})$")


def now_bjt() -> datetime:
    """当前北京时间（aware，固定 +08:00）。"""
    return datetime.now(BJT)


def today_bjt() -> date:
    """北京日（不是本机日）。"""
    return now_bjt().date()


def to_bjt(dt: datetime) -> datetime:
    """把任意 datetime 变成北京时间。naive 一律按「已是北京墙上时间」解释（库内约定）。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=BJT)
    return dt.astimezone(BJT)


def parse_bjt(raw, *, assume_bjt: bool = True) -> datetime | None:
    """宽容解析时间串 → 北京时间 aware datetime；解析不了返回 None（绝不抛）。

    覆盖：`2026-11-07T17:52:04.819060+08:00`（库内）、`2026-11-07T17:52:04+08`（简写偏移）、
    `2026-10-14`（日期 → 当日 00:00 北京）、`...Z`（UTC → 折回北京）。
    裸串（无偏移）默认按北京时间解释（assume_bjt=True），因为本项目里裸时间戳不可信。
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return to_bjt(raw)
    if isinstance(raw, date):
        return datetime.combine(raw, datetime.min.time(), tzinfo=BJT)
    s = str(raw).strip()
    if not s:
        return None
    # 纯日期
    try:
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            return datetime.combine(date.fromisoformat(s), datetime.min.time(), tzinfo=BJT)
    except ValueError:
        pass
    cand = s.replace(" ", "T", 1) if " " in s and "T" not in s else s
    if cand.endswith("Z"):
        cand = cand[:-1] + "+00:00"
    cand = _OFFSET_NO_MINUTES.sub(r"\g<1>:00", cand)  # +08 → +08:00
    for attempt in (cand, cand[:19] + "+08:00" if len(cand) >= 19 else cand):
        try:
            dt = datetime.fromisoformat(attempt)
        except ValueError:
            continue
        return to_bjt(dt) if dt.tzinfo is not None else (
            dt.replace(tzinfo=BJT) if assume_bjt else dt)
    return None


def iso_bjt(dt: datetime) -> str:
    """北京时间 → `2026-11-09T20:00:15+08:00`（秒精度，永远带 +08:00，前端零换算）。"""
    return to_bjt(dt).replace(microsecond=0).isoformat()


def date_bjt(dt: datetime) -> str:
    """北京时间 → `2026-11-09`。"""
    return to_bjt(dt).date().isoformat()


def now_iso_bjt() -> str:
    return iso_bjt(now_bjt())


def is_trading_weekday(d: date) -> bool:
    """周一~周五算交易日候选。**不含法定节假日**（库内无交易日历，节假日由源探测兜住）。"""
    return d.weekday() < 5


def market_open_bjt(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time(), tzinfo=BJT).replace(
        hour=OPEN_TIME[0], minute=OPEN_TIME[1])


def market_close_bjt(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time(), tzinfo=BJT).replace(
        hour=CLOSE_TIME[0], minute=CLOSE_TIME[1])


def is_session_closed(d: date, now: datetime | None = None) -> bool:
    """该北京日是否已收盘（未收盘的数据一律当成「还没入库」，不硬跑）。"""
    now = to_bjt(now or now_bjt())
    if d < now.date():
        return True
    if d > now.date():
        return False
    return now >= market_close_bjt(d)


def first_trading_day_on_or_after(when, *, max_days: int = 45) -> date | None:
    """交节时刻所在节气月内的第一个交易日（北京口径、跳过周末）。

    判据：交易日 d 的整根日 K（09:30 开盘 ~ 15:00 收盘）都要落在交节之后，
    所以「交节发生在收盘之后（>=15:00）」当日不算本流月的第一根日 K。
    实测对照（主库 monthly_facts.first_date，2025-M01..2026-M08 共 20 个流月）：
    16/20 逐日相同，4 处差异全是法定节假日（春节、清明、国庆调休）——
    本函数**不建模节假日**，源镜像里没有该日数据时由 SOURCE_NOT_READY 如实拦下。
    例：立冬 2026-11-07T17:52+08 是周六 → 首个交易日 2026-11-09（周一）。
    """
    dt = parse_bjt(when)
    if dt is None:
        return None
    d = dt.date()
    if dt >= market_close_bjt(d):
        d += timedelta(days=1)
    for _ in range(max_days):
        if is_trading_weekday(d) and market_open_bjt(d) >= dt:
            return d
        d += timedelta(days=1)
    return None
