"""交易日历：读 slot88_full_market_v1/data/market_sessions_ext.csv（只读、带缓存）。

为什么不按「自然日 + 1」：周五信号的下个交易日是周一；法定假日/调休只有日历知道。
日历覆盖不到信号日之后时（该文件随月包走、可能滞后），next_trade_date_fallback
退化为「下一个工作日」推定并返回告警——推定错成休市日时雪球会拒绝调仓，
对账会捕获，不会盲买；月窗已尽则不出计划。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

_CACHE: dict[str, list[str]] = {}


def load_dates(path=None) -> list[str]:
    """升序交易日列表（去重缓存；文件缺失抛 OSError 由调用方处理）。"""
    p = str(Path(path) if path else _default_path())
    if p in _CACHE:
        return _CACHE[p]
    out: list[str] = []
    with open(p, "r", encoding="utf-8") as f:
        next(f, None)  # header: date,month_id,mn,mkt
        for line in f:
            d = line.split(",", 1)[0].strip()
            if d:
                out.append(d)
    out = sorted(set(out))
    _CACHE[p] = out
    return out


def _default_path():
    from . import config
    return config.market_sessions_path()


def is_trade_date(date: str, dates: list[str]) -> bool:
    return date in set(dates)


def is_trade_date_safe(date: str, dates: list[str]) -> tuple[bool, str | None]:
    """真实日历优先；日历滞后覆盖不到时退化为工作日推定。

    - 在日历里 → 交易日（权威）；
    - 日历覆盖了该日但不在日历里 → 真休市（权威拒绝）；
    - 日历止于该日之前（滞后）→ 按工作日推定，返回告警
      （推定错成休市时雪球调仓会被拒，对账会捕获，不会盲买）。
    """
    if not dates:
        return False, None
    if date in set(dates):
        return True, None
    if dates[-1] >= date:
        return False, None
    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return False, None
    if dt.weekday() >= 5:
        return False, None
    return True, (f"交易日历止于 {dates[-1]}，{date} 按工作日推定"
                  f"（若为休市日，雪球调仓会被拒并进入对账）")


def next_trade_date(date: str, dates: list[str]) -> str | None:
    """严格大于 date 的最近交易日（None=日历已尽，不猜）。"""
    for d in dates:
        if d > date:
            return d
    return None


def prev_trade_date(date: str, dates: list[str]) -> str | None:
    for d in reversed(dates):
        if d < date:
            return d
    return None


def last_trade_date_on_or_before(date: str, dates: list[str]) -> str | None:
    out = None
    for d in dates:
        if d <= date:
            out = d
        else:
            break
    return out


def next_trade_date_fallback(date: str, dates: list[str],
                             upper_bound: str = "") -> tuple[str | None, str | None]:
    """真实日历优先；覆盖不到时退化为下一个工作日（不越 upper_bound）。

    返回 (执行日, 告警)。工作日推定若撞上休市，雪球调仓会被拒绝并进入对账；
    upper_bound（月配置末段）之后不出计划。
    """
    d = next_trade_date(date, dates)
    if d:
        return d, None
    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return None, None
    for i in range(1, 15):
        cand_dt = dt + timedelta(days=i)
        if cand_dt.weekday() >= 5:
            continue
        cand = cand_dt.strftime("%Y-%m-%d")
        if upper_bound and cand > upper_bound:
            return None, None    # 月窗已尽：不出计划
        warn = (f"交易日历未覆盖到 {date} 之后（日历止于 "
                f"{dates[-1] if dates else '—'}），执行日 {cand} 为工作日推定；"
                f"若为休市日，雪球调仓会被拒并进入对账")
        return cand, warn
    return None, None
