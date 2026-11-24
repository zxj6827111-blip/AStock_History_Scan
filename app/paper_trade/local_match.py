"""本地撮合账本：以真实切片价成交（完全替代雪球账户，不触网）。

价格源：source_input/live/current_month.sqlite（盘中每 30 分钟由现有管道更新）
- 盘中试探：信号所在 30 分钟K线的 close 价成交（"信号出现时的价格"）；
- EOD 计划：执行日 10:05 档的当日合成K线 open 价成交（次日开盘价）。

A股约束（与真实交易一致）：
- T+1：当日买入不可卖（sellable = 持仓 - 当日买入）；
- 涨停买不进 / 跌停卖不出（按前收 × 板块/ST 涨跌幅推算，四舍五入到分）；
- 停牌：当日无K线或成交量为 0 不成交；
- 100 股整手（卖出清仓允许零股）；
- 费用：佣金（万 2.5，最低 5 元）、印花税（卖出 0.05%）、过户费（0.001%）。

账本：accounts（现金）、holdings（数量/均价）、trades（成交明细）、nav（日净值）。
页面/飞书/计划系统全部复用（公开快照由 store 的 sync/nav 生成）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config, store


class LocalMatchError(Exception):
    """本地账本不可用（未初始化/资金异常）。"""


def code6(code: str) -> str:
    return str(code).split(".")[0]


class Prices:
    """切片行情只读访问（每个实例加载所需代码的全部K线，量小）。"""

    def __init__(self, db_path=None):
        self.path = Path(db_path or config.slice_db_path())
        if not self.path.is_file():
            raise LocalMatchError(f"行情切片不存在：{self.path.name}")
        self.bars: dict[str, list[tuple]] = {}

    def load(self, codes) -> "Prices":
        need = sorted({code6(c) for c in codes if c})
        if not need:
            return self
        con = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            con.execute("PRAGMA query_only=ON")
            for c6 in need:
                if c6 in self.bars:
                    continue
                rows = con.execute(
                    "SELECT date, open, close, volume FROM bars WHERE code=? "
                    "ORDER BY date", (c6,)).fetchall()
                if rows:
                    self.bars[c6] = [tuple(r) for r in rows]
        finally:
            con.close()
        return self

    def bar_on(self, code: str, date: str):
        for row in reversed(self.bars.get(code6(code)) or []):
            if row[0] == date:
                return row
        return None

    def latest(self, code: str):
        rows = self.bars.get(code6(code)) or []
        return rows[-1] if rows else None

    def prev_close(self, code: str, date: str):
        prev = None
        for row in self.bars.get(code6(code)) or []:
            if row[0] < date:
                prev = row
            else:
                break
        return float(prev[2]) if prev else None


def prepare_prices(con, extra_codes=()) -> Prices:
    codes = [h["code"] for h in store.holdings_list(con)]
    codes += [c for c in extra_codes if c]
    return Prices().load(codes)


def _limit_pct(code: str, name: str) -> float:
    c = code6(code)
    if "ST" in (name or "").upper():
        return 5.0
    if c.startswith(("300", "301", "688", "689")):
        return 20.0
    return 10.0


def limit_prices(code: str, name: str, prev_close: float):
    pct = _limit_pct(code, name)
    return (round(prev_close * (1 + pct / 100.0), 2),
            round(prev_close * (1 - pct / 100.0), 2))


def calc_fees(side: str, amount: float, fees_cfg: dict):
    commission = max(amount * float(fees_cfg.get("commission_rate", 0.00025)),
                     float(fees_cfg.get("commission_min", 5.0)))
    stamp = amount * float(fees_cfg.get("stamp_rate", 0.0005)) if side == "sell" else 0.0
    transfer = amount * float(fees_cfg.get("transfer_rate", 0.00001))
    return round(commission, 2), round(stamp, 2), round(transfer, 2)


def fill_price(prices: Prices, code: str, today: str, kind: str):
    """成交价：EOD→执行日开盘；盘中试探→信号K线收盘。返回 (price, 拒绝原因)。"""
    bar = prices.bar_on(code, today)
    if bar is None:
        return None, "停牌/当日无行情"
    if float(bar[3] or 0) <= 0:
        return None, "停牌（当日成交量为0）"
    px = float(bar[1]) if kind != "intraday_probe" else float(bar[2])
    if px <= 0:
        return None, "无有效价格"
    return px, ""


def fill_price_now(prices: Prices, code: str, today: str):
    """人工单：只认当日30分钟切片close；无当日bar/停牌不回退昨日。"""
    bar = prices.bar_on(code, today)
    if bar is None:
        return None, None, None, "无当日行情（不回退昨日）"
    if float(bar[3] or 0) <= 0:
        return None, None, None, "停牌（当日成交量为0）"
    px = float(bar[2])
    if px <= 0:
        return None, None, None, "无有效价格"
    return px, prices.prev_close(code, today), str(bar[0]), ""


def execute_manual_buy(con, code: str, name: str, amount: float | None,
                       qty: int | None, px: float,
                       prev_close, cash: float, fees_cfg: dict, today: str,
                       slot: str, order_id: str, reason: str,
                       override_segment: str = "", price_source: str = "slice_30m_close",
                       price_time: str = "") -> dict:
    """人工买入（按金额或按股数）；返回 fill/rejected/cash，不自 commit。"""
    if prev_close:
        up, _ = limit_prices(code, name, float(prev_close))
        if px >= up - 1e-9:
            return {"rejected": "涨停买不进", "cash": cash}
    if qty is not None:
        qty = int(qty)
        if qty <= 0 or qty % 100:
            return {"rejected": "买入股数必须为100股整数倍", "cash": cash}
        gross = qty * px
        fee, _, transfer = calc_fees("buy", gross, fees_cfg)
        if gross + fee + transfer > cash + 1e-9:
            return {"rejected": "现金不足", "cash": cash}
    else:
        lots = int(float(amount) // (px * 100.0))
        while lots > 0:
            qty = lots * 100
            gross = qty * px
            fee, _, transfer = calc_fees("buy", gross, fees_cfg)
            if gross + fee + transfer <= cash + 1e-9:
                break
            lots -= 1
        if lots <= 0:
            return {"rejected": "资金不足或金额不足100股", "cash": cash}
        qty = lots * 100
    gross = qty * px
    fee, _, transfer = calc_fees("buy", gross, fees_cfg)
    cash -= gross + fee + transfer
    h = store.holding_get(con, code) or {"qty": 0, "avg_cost": 0.0}
    new_qty = int(h["qty"]) + qty
    new_cost = ((float(h["avg_cost"] or 0) * int(h["qty"])) + px * qty) / new_qty
    store.holding_set(con, code, name, new_qty, new_cost)
    store.trade_insert(con, today, slot, code, "buy", qty, px, fee, 0.0,
                       transfer, f"manual|{order_id}", reason, name=name,
                       source="manual", manual_order_id=order_id,
                       override_segment=override_segment,
                       price_source=price_source, price_time=price_time)
    return {"fill": {"code": code, "name": name, "side": "buy", "qty": qty,
                     "price": px, "amount": round(gross, 2), "fee": fee,
                     "tax": 0.0, "transfer_fee": transfer},
            "cash": cash}


def execute_manual_sell(con, code: str, name: str, qty: int, px: float,
                        prev_close, cash: float, fees_cfg: dict, today: str,
                        slot: str, order_id: str, reason: str,
                        override_segment: str = "", price_time: str = "") -> dict:
    """人工按股数卖出；T+1/整手约束由调用方先校验，这里再兜底。"""
    h = store.holding_get(con, code)
    if not h or int(h["qty"]) <= 0:
        return {"rejected": "无持仓", "cash": cash}
    held = int(h["qty"])
    sellable = held - store.bought_today(con, code, today)
    if qty <= 0 or qty > sellable:
        return {"rejected": "超过可卖数量或T+1限制", "cash": cash}
    if prev_close:
        _, down = limit_prices(code, h.get("name") or name, float(prev_close))
        if px <= down + 1e-9:
            return {"rejected": "跌停卖不出", "cash": cash}
    gross = qty * px
    fee, tax, transfer = calc_fees("sell", gross, fees_cfg)
    cash += gross - fee - tax - transfer
    store.holding_set(con, code, h.get("name") or name, held - qty,
                      float(h.get("avg_cost") or 0))
    store.trade_insert(con, today, slot, code, "sell", qty, px, fee, tax,
                       transfer, f"manual|{order_id}", reason,
                       name=h.get("name") or name, source="manual",
                       manual_order_id=order_id,
                       override_segment=override_segment,
                       price_source="slice_30m_close", price_time=price_time)
    return {"fill": {"code": code, "name": h.get("name") or name, "side": "sell",
                     "qty": qty, "price": px, "amount": round(gross, 2),
                     "fee": fee, "tax": tax, "transfer_fee": transfer},
            "cash": cash}


def snapshot(con, prices: Prices, today: str | None = None) -> dict:
    """本地账户快照（与雪球回读同结构，供引擎/公开快照复用）。

    每个持仓附带盈亏口径（页面直接用）：
    - price/prev_close/day_pct/day_pnl：**今日**涨跌。今日买入的按买入价算
      （不含开盘前的跳空，那部分你没赚到）；之前持仓的按前一交易日收盘算；
    - avg_cost/pnl/pnl_pct：浮动盈亏（相对本地账本成本）。
    """
    acct = store.get_account(con)
    if acct is None:
        raise LocalMatchError("本地账户未初始化（先运行 cli baseline --adopt）")
    today = today or config.now_bjt().strftime("%F")
    metas = {m["code"]: m for m in store.active_position_meta(con)}
    mv_total = 0.0
    positions = []
    for h in store.holdings_list(con):
        qty = int(h["qty"])
        bar = prices.latest(h["code"])
        avg = float(h["avg_cost"] or 0)
        px = float(bar[2]) if bar and float(bar[2] or 0) > 0 else avg
        bar_date = str(bar[0]) if bar else ""
        prev = prices.prev_close(h["code"], bar_date) if bar_date else None
        # 今日买入的持仓，今日盈亏从买入价起算；否则从昨收起算。
        # 今日没有K线（停牌/行情缺）时不伪造当日涨跌。
        bought_qty = store.bought_today(con, h["code"], today)
        new_position = str((metas.get(h["code"]) or {}).get("entry_date") or "") == today
        basis = avg if (new_position and avg > 0) else (float(prev) if prev else None)
        day_pnl = day_pct = None
        if bar_date == today and basis and basis > 0:
            day_pnl = round(qty * (px - basis), 2)
            day_pct = round((px / basis - 1.0) * 100.0, 2)
        pnl = pnl_pct = None
        if avg > 0:
            pnl = round(qty * (px - avg), 2)
            pnl_pct = round((px / avg - 1.0) * 100.0, 2)
        mv = round(qty * px, 2)
        mv_total += mv
        positions.append({
            "code": h["code"], "name": h["name"], "weight": 0.0,
            "market_value": mv, "qty": qty,
            "today_buy_qty": bought_qty, "sellable_qty": max(0, qty - bought_qty),
            "price_date": bar_date or None,
            "price": round(px, 2), "prev_close": round(float(prev), 2) if prev else None,
            "day_pct": day_pct, "day_pnl": day_pnl,
            "avg_cost": round(avg, 4) if avg > 0 else None,
            "pnl": pnl, "pnl_pct": pnl_pct,
        })
    cash = round(float(acct["cash"]), 2)
    total = round(cash + mv_total, 2)
    for p in positions:
        p["weight"] = round(p["market_value"] / total * 100.0, 2) if total else 0.0
    positions.sort(key=lambda x: -x["weight"])
    day_total = sum(p["day_pnl"] for p in positions if p["day_pnl"] is not None)
    pnl_total = sum(p["pnl"] for p in positions if p["pnl"] is not None)
    return {"ok": True, "cash": cash, "total": total,
            "market_value": round(mv_total, 2),
            "initial": float(acct["initial_cash"] or 0),
            "day_pnl": round(day_total, 2), "unrealized_pnl": round(pnl_total, 2),
            "positions": positions}


def init_account(con, cash: float) -> dict:
    """初始化本地账本（一次性）：建账户，并把雪球时代的开仓台账结清。"""
    existing = store.get_account(con)
    if existing is not None:
        return {"status": "exists", "cash": existing["cash"],
                "initial_cash": existing["initial_cash"]}
    today = config.now_bjt().strftime("%F")
    store.create_account(con, float(cash))
    for m in store.active_position_meta(con):
        store.close_position(con, m["code"], m["strategy"], m["month_id"],
                             "切换本地账本，雪球时代持仓清零", today)
    store.set_baseline(con, [{"mode": "local", "initial_cash": float(cash)}])
    return {"status": "ok", "cash": float(cash), "initial_cash": float(cash)}


def execute_plan(con, plan: dict, today: str, prices: Prices,
                 settings: dict, slot: str = "") -> dict:
    """执行计划（本地撮合）。只写本地账本；返回 fills/rejected 明细。

    先卖后买（释放现金）；权重定量按执行前总资产；费用按 settings.fees。
    """
    acct = store.get_account(con)
    if acct is None:
        raise LocalMatchError("本地账户未初始化（先运行 cli baseline --adopt）")
    kind = str(plan.get("kind") or "eod")
    intents = plan.get("intents") or {}
    fees_cfg = settings.get("fees") or {}
    cash = float(acct["cash"])
    snap0 = snapshot(con, prices, today)
    total_base = float(snap0["total"])
    fills: list[dict] = []
    rejected: list[dict] = []

    def _sell(code: str, name: str, reason: str) -> None:
        nonlocal cash
        h = store.holding_get(con, code)
        if not h or int(h["qty"]) <= 0:
            rejected.append({"code": code, "side": "sell", "reason": "无持仓"})
            return
        sellable = int(h["qty"]) - store.bought_today(con, code, today)
        if sellable <= 0:
            rejected.append({"code": code, "side": "sell",
                             "reason": "T+1：当日买入不可卖"})
            return
        px, why = fill_price(prices, code, today, kind)
        if px is None:
            rejected.append({"code": code, "side": "sell", "reason": why})
            return
        prev = prices.prev_close(code, today)
        if prev:
            _, down = limit_prices(code, h.get("name") or name, prev)
            if px <= down + 1e-9:
                rejected.append({"code": code, "side": "sell",
                                 "reason": "跌停卖不出"})
                return
        amount = sellable * px
        fee, tax, transfer = calc_fees("sell", amount, fees_cfg)
        cash += amount - fee - tax - transfer
        store.holding_set(con, code, h.get("name") or name,
                          int(h["qty"]) - sellable, float(h["avg_cost"] or 0))
        store.trade_insert(con, today, slot, code, "sell", sellable, px,
                           fee, tax, transfer, plan.get("plan_id") or "",
                           reason, name=h.get("name") or name)
        fills.append({"code": code, "name": h.get("name") or name,
                      "side": "sell", "qty": sellable, "price": px,
                      "reason": reason})

    def _buy(code: str, name: str, budget: float, reason: str) -> None:
        nonlocal cash
        px, why = fill_price(prices, code, today, kind)
        if px is None:
            rejected.append({"code": code, "side": "buy", "reason": why})
            return
        prev = prices.prev_close(code, today)
        if prev:
            up, _ = limit_prices(code, name, prev)
            if px >= up - 1e-9:
                rejected.append({"code": code, "side": "buy",
                                 "reason": "涨停买不进"})
                return
        lots = int(budget // (px * 100.0))
        while lots > 0:
            qty = lots * 100
            amount = qty * px
            fee, _, transfer = calc_fees("buy", amount, fees_cfg)
            if amount + fee + transfer <= cash + 1e-9:
                break
            lots -= 1
        if lots <= 0:
            rejected.append({"code": code, "side": "buy",
                             "reason": "资金不足（不足 100 股）"})
            return
        qty = lots * 100
        amount = qty * px
        fee, _, transfer = calc_fees("buy", amount, fees_cfg)
        cash -= amount + fee + transfer
        h = store.holding_get(con, code) or {"qty": 0, "avg_cost": 0.0}
        new_qty = int(h["qty"]) + qty
        new_cost = ((float(h["avg_cost"] or 0) * int(h["qty"])) + px * qty) / new_qty
        store.holding_set(con, code, name or h.get("name"), new_qty, new_cost)
        store.trade_insert(con, today, slot, code, "buy", qty, px,
                           fee, 0.0, transfer, plan.get("plan_id") or "",
                           reason, name=name)
        fills.append({"code": code, "name": name, "side": "buy",
                      "qty": qty, "price": px, "reason": reason})

    # 1) 卖出（先释放现金/仓位）
    for e in intents.get("exits") or []:
        _sell(str(e.get("code")), str(e.get("name") or ""),
              str(e.get("reason") or "退出"))
    # 2) 试探确认补仓（holds 带 weight_target；差额不足一手则跳过）
    for h in intents.get("holds") or []:
        if h.get("weight_target") is None:
            continue
        code = str(h.get("code"))
        hd = store.holding_get(con, code)
        if not hd or int(hd["qty"]) <= 0:
            rejected.append({"code": code, "side": "buy", "reason": "补仓时无持仓"})
            continue
        bar = prices.bar_on(code, today) or prices.latest(code)
        px = float(bar[2]) if bar else float(hd["avg_cost"] or 0)
        if px <= 0:
            rejected.append({"code": code, "side": "buy", "reason": "无有效价格"})
            continue
        target_val = total_base * float(h["weight_target"]) / 100.0
        delta = target_val - int(hd["qty"]) * px
        if delta < px * 100.0:
            continue   # 差额不足一手：视为已到位
        _buy(code, str(hd.get("name") or ""), delta, "收盘确认补仓")
    # 3) 新买
    for b in intents.get("buys") or []:
        weight = float(b.get("weight") or 0.0)
        if weight <= 0:
            continue
        _buy(str(b.get("code")), str(b.get("name") or ""),
             total_base * weight / 100.0, str(b.get("reason") or "信号买入"))

    store.update_account_cash(con, cash)
    snap = snapshot(con, prices, today)
    return {"status": "confirmed", "fills": fills, "rejected": rejected,
            "cash": snap["cash"], "total": snap["total"],
            "market_value": snap["market_value"]}
