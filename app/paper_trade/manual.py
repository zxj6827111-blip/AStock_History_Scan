"""页面人工建仓/清仓：独立于自动 plans 的例外交易通道。

- 只允许本机发布端、mode=live、execution_backend=local 且 manual_enabled=true；
- 预览 → 二次确认 → 单事务写账，client_token 幂等、preview_token 防价格跳变；
- 人工可显式覆盖 S4/S5/S6 禁买，但 T+1/涨跌停/停牌/整手/资金等硬约束绝不绕过；
- 所有成交/拒绝写 manual_orders，trades 标记 source=manual，不进入 plans。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

from . import config, local_match, signals, store

_CODE_RE = re.compile(r"^(\d{6})\.(SH|SZ)$")
_CLIENT_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_SH_PREFIX = ("600", "601", "603", "605", "688", "689")
_SZ_PREFIX = ("000", "001", "002", "003", "300", "301")
_PREVIEWS: dict[str, dict] = {}
_THREAD_LOCK = threading.Lock()


class ManualError(Exception):
    http_status = 400
    code = "INVALID_MANUAL_ORDER"

    def __init__(self, message: str, code: str | None = None, **extra):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.extra = extra

    def payload(self) -> dict:
        return {"ok": False, "error": self.code, "message": self.message, **self.extra}


class ManualForbidden(ManualError):
    http_status = 403
    code = "MANUAL_FORBIDDEN"


class ManualUnavailable(ManualError):
    http_status = 503
    code = "MANUAL_DISABLED"


class ManualRejected(ManualError):
    http_status = 409
    code = "MANUAL_REJECTED"


class ManualBusy(ManualError):
    http_status = 409
    code = "BUSY"


def _text(value, field: str, max_len: int = 120, required: bool = False) -> str:
    value = "" if value is None else str(value)
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value).strip()
    if required and not value:
        raise ManualError(f"{field} 必填", "INVALID_PARAM")
    if len(value) > max_len:
        raise ManualError(f"{field} 超过 {max_len} 字", "INVALID_PARAM")
    return value


def _finite(value, field: str, *, allow_none=False):
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or isinstance(value, str):
        raise ManualError(f"{field} 必须是数字", "INVALID_PARAM")
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise ManualError(f"{field} 必须是数字", "INVALID_PARAM") from exc
    if not math.isfinite(out):
        raise ManualError(f"{field} 必须是有限数字", "INVALID_PARAM")
    return out


def _normalize_payload(payload: dict, *, for_execute: bool = False) -> dict:
    if not isinstance(payload, dict):
        raise ManualError("请求参数不合法", "INVALID_PARAM")
    side = str(payload.get("side") or "")
    if side not in ("buy", "sell"):
        raise ManualError("side 只能是 buy/sell", "INVALID_PARAM")
    code = str(payload.get("code") or "").upper().strip()
    m = _CODE_RE.match(code)
    if not m or not code.startswith(_SH_PREFIX + _SZ_PREFIX):
        raise ManualError("代码需为6位沪深A股代码，如 000001.SZ", "INVALID_CODE")
    if code.endswith(".SH") and not code.startswith(_SH_PREFIX):
        raise ManualError("代码与交易所不匹配", "INVALID_CODE")
    if code.endswith(".SZ") and not code.startswith(_SZ_PREFIX):
        raise ManualError("代码与交易所不匹配", "INVALID_CODE")
    reason = _text(payload.get("reason"), "原因")
    out = {"side": side, "code": code,
           "reason": reason or ("人工建仓" if side == "buy" else "人工减仓"),
           "amount_requested": None, "qty_requested": None, "custom_price": None}
    if side == "buy":
        if payload.get("all"):
            raise ManualError("买入不支持“全部”", "INVALID_PARAM")
        amount_raw = payload.get("amount")
        qty_raw = payload.get("qty")
        if amount_raw is not None and qty_raw is not None:
            raise ManualError("买入金额与买入股数只能二选一", "INVALID_PARAM")
        if amount_raw is None and qty_raw is None:
            raise ManualError("买入需填写金额或股数", "INVALID_PARAM")
        if amount_raw is not None:
            amount = _finite(amount_raw, "买入金额")
            if amount <= 0:
                raise ManualError("买入金额必须大于0", "INVALID_PARAM")
            out["amount_requested"] = round(amount, 2)
        else:
            qty = _finite(qty_raw, "买入股数")
            if qty <= 0 or int(qty) != qty:
                raise ManualError("买入股数必须是正整数", "INVALID_PARAM")
            qty = int(qty)
            if qty % 100 != 0:
                raise ManualError("买入股数必须为100股整数倍", "LOT_SIZE")
            out["qty_requested"] = qty
        if payload.get("price") is not None:
            price = _finite(payload.get("price"), "成交价")
            if not (0 < price < 100000):
                raise ManualError("成交价需在 0~100000 元之间", "INVALID_PARAM")
            out["custom_price"] = round(price, 2)
    else:
        if payload.get("amount") is not None:
            raise ManualError("卖出只能填写股数或全部", "INVALID_PARAM")
        if payload.get("price") is not None:
            raise ManualError("卖出暂不支持自定义价格，只按最新30分钟价成交",
                              "INVALID_PARAM")
        use_all = payload.get("all") is True
        qty = payload.get("qty")
        if use_all and qty is not None:
            raise ManualError("qty 与 all 只能二选一", "INVALID_PARAM")
        if not use_all:
            qty = _finite(qty, "卖出股数")
            if qty <= 0 or int(qty) != qty:
                raise ManualError("卖出股数必须是正整数", "INVALID_PARAM")
            qty = int(qty)
        out.update({"all": use_all, "qty": None if use_all else qty})
    for key, default, positive in (("stop_pct", -5.0, False),
                                   ("target_pct", 3.0, True)):
        if key not in payload:
            value = default
        elif payload.get(key) is None:
            value = None
        else:
            value = _finite(payload.get(key), key)
        if value is not None:
            if positive and not (0 < value < 100):
                raise ManualError("目标涨幅需在 0~100% 之间", "INVALID_PARAM")
            if not positive and not (-100 < value < 0):
                raise ManualError("止损幅度需在 -100%~0 之间", "INVALID_PARAM")
        out[key] = value
    ov = payload.get("override") if isinstance(payload.get("override"), dict) else None
    if ov:
        ov = {"ack": ov.get("ack") is True,
              "note": _text(ov.get("note"), "覆盖说明", max_len=120)}
        out["override"] = ov
    else:
        out["override"] = None
    token = payload.get("client_token")
    if token is not None and not _CLIENT_RE.match(str(token)):
        raise ManualError("client_token 格式不合法", "INVALID_PARAM")
    out["client_token"] = str(token) if token else (
        secrets.token_urlsafe(18) if not for_execute else None)
    if for_execute and not out["client_token"]:
        raise ManualError("client_token 必填", "INVALID_PARAM")
    return out


def _reports_dir() -> Path:
    return config.OUTER_ROOT / "survivor_report_v1" / "reports"


def latest_report() -> dict | None:
    d = _reports_dir()
    if not d.is_dir():
        return None
    paths = sorted(d.glob("survivor_*.json"), key=lambda p: (p.stat().st_mtime, p.name))
    for p in reversed(paths):
        if p.name.endswith(".json"):
            try:
                rep = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if rep.get("schema") == "survivor_report_v1" and rep.get("rows"):
                rep["_source_file"] = p.name
                return rep
    return None


def report_universe(rep: dict | None = None) -> list[dict]:
    rep = rep if rep is not None else latest_report()
    if not rep:
        return []
    return [{"code": str(r.get("code")), "name": r.get("name") or "",
             "group": r.get("group") or ""} for r in rep.get("rows") or []
            if r.get("code")]


def capabilities() -> dict:
    s = config.settings()
    reason = None
    if config.disabled():
        reason = "模拟盘已停用"
    elif not s.get("manual_enabled"):
        reason = "人工交易开关未开启"
    elif str(s.get("mode")) != "live":
        reason = "模拟盘不是 live 模式"
    elif str(s.get("execution_backend") or "xueqiu") != "local":
        reason = "人工交易仅支持本地账本"
    return {"ok": True, "enabled": reason is None, "reason": reason,
            "backend": s.get("execution_backend") or "xueqiu",
            "manual_enabled": bool(s.get("manual_enabled"))}


def _require_enabled() -> dict:
    s = config.settings()
    cap = capabilities()
    if not cap["enabled"]:
        raise ManualUnavailable(cap["reason"] or "人工交易不可用", "MANUAL_DISABLED")
    return s


def _request_hash(ticket: dict) -> str:
    payload = {k: ticket.get(k) for k in (
        "side", "code", "amount_requested", "qty_requested", "amount", "qty",
        "all", "custom_price", "stop_pct", "target_pct",
        "reason", "segment_id", "override")}
    payload["client_token"] = ticket.get("client_token")
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True)
                          .encode("utf-8")).hexdigest()


def _quote(con, code: str, custom_price: float | None = None):
    now = config.now_bjt()
    today = now.strftime("%F")
    prices = local_match.Prices().load([code])
    px, prev, bar_date, why = local_match.fill_price_now(prices, code, today)
    sync = store.latest_sync(con)
    market_as_of = (sync or {}).get("market_as_of")
    hhmm = now.strftime("%H:%M")
    in_hours = "09:30" <= hhmm <= "15:00"
    if custom_price is not None:
        # 自定义价：以最近一根有效30分钟K线为参考，非交易时段也能定价成交。
        ref = prices.latest(code)
        if ref is None or float(ref[3] or 0) <= 0 or float(ref[2] or 0) <= 0:
            raise ManualRejected("该代码无可用行情，无法校验自定义成交价",
                                 "NO_REFERENCE_QUOTE")
        ref_date = str(ref[0])
        ref_close = round(float(ref[2]), 2)
        today_bjt = config.now_bjt().date()
        try:
            ref_age = (today_bjt - datetime.strptime(ref_date, "%Y-%m-%d").date()).days
        except ValueError:
            ref_age = None
        if ref_age is None or ref_age > 15:
            raise ManualRejected(f"参考行情过旧（{ref_date}），拒绝自定义价格成交",
                                 "REFERENCE_TOO_OLD", reference_date=ref_date)
        price_time = (market_as_of
                      if str(market_as_of or "").startswith(today)
                      else f"{today} 最新30分钟档") if px is not None else f"{ref_date} 15:00"
        label = "人工指定价" if px is not None else \
            f"人工指定价（参考 {ref_date} 收盘）"
        # 按参考收盘价成交时，这笔仓位属于参考交易日：入场日/成本/T+1都按该日算，
        # 不把次日高开低开算成你的盈亏。
        return {"price": round(float(custom_price), 2),
                "prev_close": prices.prev_close(code, ref_date),
                "bar_date": ref_date, "price_time": price_time,
                "price_source": "manual_price", "price_label": label,
                "reference_close": ref_close, "reference_date": ref_date,
                "trade_date": ref_date, "backdated": ref_date != today,
                "is_market_hours": in_hours}
    if px is None:
        ref = prices.latest(code)
        ref_close = round(float(ref[2]), 2) if ref is not None else None
        raise ManualRejected(why, "NO_CURRENT_QUOTE",
                             quote_date=bar_date, today=today,
                             reference_close=ref_close,
                             reference_date=str(ref[0]) if ref is not None else None,
                             can_use_custom_price=ref_close is not None)
    price_time = market_as_of if str(market_as_of or "").startswith(today) \
        else f"{today} 最新30分钟档"
    if in_hours:
        try:
            age = (now - datetime.fromisoformat(str(market_as_of))).total_seconds()
        except (TypeError, ValueError):
            age = None
        if age is None or age > 45 * 60:
            raise ManualRejected("行情超过45分钟未更新，禁止人工成交；"
                                 "非交易时段可填写自定义成交价", "QUOTE_STALE",
                                 price_time=price_time)
    return {"price": round(float(px), 2), "prev_close": prev,
            "bar_date": bar_date, "price_time": price_time,
            "price_source": "slice_30m_close",
            "price_label": "当日30分钟收盘价",
            "trade_date": today, "backdated": False,
            "is_market_hours": in_hours}


def _levels(px: float, stop_pct, target_pct):
    stop = round(px * (1 + float(stop_pct) / 100.0), 2) \
        if stop_pct is not None else None
    target = round(px * (1 + float(target_pct) / 100.0), 2) \
        if target_pct is not None else None
    return stop, target


def _calc_buy(amount: float | None, qty: int | None, px: float,
              cash: float, fees_cfg: dict) -> dict:
    if qty is not None:
        qty = int(qty)
        gross = qty * px
        fee, _, transfer = local_match.calc_fees("buy", gross, fees_cfg)
        need = gross + fee + transfer
        if need > cash + 1e-9:
            raise ManualRejected(
                f"现金不足：需要 {need:.2f} 元，可用 {cash:.2f} 元",
                "INSUFFICIENT_CASH", required=round(need, 2),
                cash_available=round(cash, 2))
        return {"qty": qty, "amount": round(gross, 2), "fee": fee, "tax": 0.0,
                "transfer_fee": transfer,
                "cash_after": round(cash - need, 2)}
    lots = int(float(amount) // (px * 100.0))
    while lots > 0:
        qty = lots * 100
        gross = qty * px
        fee, _, transfer = local_match.calc_fees("buy", gross, fees_cfg)
        if gross + fee + transfer <= cash + 1e-9:
            break
        lots -= 1
    if lots <= 0:
        raise ManualRejected("买入金额不足100股或现金不足", "LOT_TOO_SMALL")
    qty = lots * 100
    gross = qty * px
    fee, _, transfer = local_match.calc_fees("buy", gross, fees_cfg)
    return {"qty": qty, "amount": round(gross, 2), "fee": fee, "tax": 0.0,
            "transfer_fee": transfer,
            "cash_after": round(cash - gross - fee - transfer, 2)}


def _build_ticket(con, payload: dict, *, for_execute=False) -> dict:
    t = _normalize_payload(payload, for_execute=for_execute)
    _require_enabled()
    acct = store.get_account(con)
    if acct is None:
        raise ManualUnavailable("本地账户未初始化", "LOCAL_ACCOUNT_MISSING")
    rep = latest_report()
    rows = {str(r.get("code")): r for r in (rep or {}).get("rows") or []}
    latest_plan = (store.plans_recent(con, 1) or [None])[0] or {}
    month_id = str((rep or {}).get("month_id") or latest_plan.get("month_id") or "manual")
    seg_id = str(((rep or {}).get("current_segment") or {}).get("id")
                 or latest_plan.get("segment_id") or "")
    q = _quote(con, t["code"], t.get("custom_price"))
    px = q["price"]
    fees_cfg = config.settings().get("fees") or {}
    cash = float(acct["cash"] or 0)
    if t["side"] == "buy":
        row = rows.get(t["code"])
        if row is None:
            raise ManualRejected("人工建仓仅允许最新报告池中的股票",
                                 "NOT_IN_UNIVERSE")
        name = str(row.get("name") or t["code"])
        if store.holding_get(con, t["code"]) is not None:
            raise ManualRejected("该代码已有持仓；v1不允许人工重复建仓",
                                 "ALREADY_HELD")
        calc = _calc_buy(t.get("amount_requested"), t.get("qty_requested"),
                         px, cash, fees_cfg)
        t.update(calc)
        if q.get("prev_close"):
            up, _ = local_match.limit_prices(t["code"], name, float(q["prev_close"]))
            if px >= up - 1e-9:
                raise ManualRejected(
                    f"成交价 {px:.2f} 已达到涨停价 {up:.2f}，买不进", "LIMIT_UP",
                    price=px, limit_up=up)
        stop, target = _levels(px, t["stop_pct"], t["target_pct"])
        t.update({"name": name, "stop": stop, "target": target,
                  "sellable_qty": 0, "cash_before": round(cash, 2)})
        try:
            rules = signals.load_month_rules(month_id, config.month_rules_dir())
        except (signals.SignalError, OSError) as exc:
            raise ManualUnavailable(f"月度规则不可用：{exc}",
                                    "RULES_UNAVAILABLE") from exc
        override_required = not signals.new_buy_allowed(rules, seg_id)
        t["override_required"] = override_required
        if override_required:
            ov = t.get("override") or {}
            if not ov.get("ack"):
                raise ManualError(
                    f"当前段 {seg_id} 禁止自动建仓，需显式勾选人工覆盖",
                    "OVERRIDE_REQUIRED", segment_id=seg_id)
            t["override"] = {"ack": True,
                             "note": ov.get("note") or f"人工覆盖{seg_id}"}
    else:
        h = store.holding_get(con, t["code"])
        if not h or int(h["qty"]) <= 0:
            raise ManualRejected("无持仓", "NO_POSITION")
        name = str(h.get("name") or rows.get(t["code"], {}).get("name") or t["code"])
        sellable = int(h["qty"]) - store.bought_today(con, t["code"], q["bar_date"])
        if sellable <= 0:
            raise ManualRejected("T+1：当前无可卖数量", "T1_LOCKED",
                                 holding_qty=int(h["qty"]), sellable_qty=0)
        qty = sellable if t["all"] else int(t["qty"])
        if qty > sellable:
            raise ManualRejected("卖出股数超过可卖数量", "QTY_EXCEEDS_SELLABLE",
                                 sellable_qty=sellable)
        if qty != sellable and qty % 100 != 0:
            raise ManualError("部分卖出必须为100股整数倍", "LOT_SIZE")
        gross = qty * px
        fee, tax, transfer = local_match.calc_fees("sell", gross, fees_cfg)
        t.update({"name": name, "qty": qty, "amount": round(gross, 2),
                  "fee": fee, "tax": tax, "transfer_fee": transfer,
                  "cash_before": round(cash, 2),
                  "cash_after": round(cash + gross - fee - tax - transfer, 2),
                  "sellable_qty": sellable, "holding_qty": int(h["qty"]),
                  "avg_cost": h.get("avg_cost"), "override_required": False,
                  "stop": None, "target": None})
    t.update({"month_id": month_id, "segment_id": seg_id,
              "price": px, "price_time": q["price_time"],
              "price_source": q["price_source"], "bar_date": q["bar_date"],
              "price_label": q["price_label"],
              "trade_date": q["trade_date"], "backdated": q["backdated"],
              "reference_close": q.get("reference_close"),
              "reference_date": q.get("reference_date"),
              "prev_close": q["prev_close"],
              "override_required": bool(t.get("override_required", False))})
    t["request_hash"] = _request_hash(t)
    return t


def _fingerprint(t: dict) -> dict:
    return {"request_hash": t["request_hash"], "price": t["price"],
            "qty": t.get("qty"), "cash_before": t.get("cash_before"),
            "sellable_qty": t.get("sellable_qty"),
            "trade_date": t.get("trade_date"),
            "segment_id": t.get("segment_id"), "price_time": t.get("price_time")}


def _public_result(t: dict, status: str, fill: dict | None = None,
                   rejected: str | None = None, duplicate=False) -> dict:
    out = {"ok": status == "filled", "status": status, "duplicate": duplicate,
           "code": t["code"], "name": t.get("name"), "side": t["side"],
           "price": t["price"], "price_time": t.get("price_time"),
           "price_source": t.get("price_source"),
           "price_label": t.get("price_label"),
           "trade_date": t.get("trade_date"),
           "submitted_amount": t.get("amount_requested") if t["side"] == "buy" else None,
           "submitted_qty": (t.get("qty_requested") if t["side"] == "buy"
                             else t.get("qty")),
           "cash": t.get("cash_after"), "segment_id": t.get("segment_id"),
           "override_required": bool(t.get("override_required")),
           "stop": t.get("stop"), "target": t.get("target")}
    if fill:
        out["fill"] = fill
        out["fees"] = {k: fill.get(k) for k in ("fee", "tax", "transfer_fee")}
        out["amount"] = fill.get("amount")
        out["qty"] = fill.get("qty")
    if rejected:
        out["reject_reason"] = rejected
    return out


def preview(payload: dict) -> dict:
    con = store.connect()
    try:
        t = _build_ticket(con, payload)
    finally:
        con.close()
    token = secrets.token_urlsafe(24)
    now = time.time()
    for old in [k for k, v in _PREVIEWS.items() if v["expires"] < now]:
        _PREVIEWS.pop(old, None)
    _PREVIEWS[token] = {**_fingerprint(t), "expires": now + 120}
    warnings = []
    if t["side"] == "sell":
        warnings.append(f"今日买入 {int(t.get('holding_qty') or 0) - int(t.get('sellable_qty') or 0)} 股受T+1限制")
    elif t.get("price_source") == "manual_price":
        if t.get("backdated"):
            warnings.append(
                f"按 {t.get('trade_date')} 收盘价成交：入场日、成本与涨跌基准"
                f"均按该日收盘价 {t['price']:.2f} 计算")
        else:
            warnings.append(t.get("price_label") or "人工指定价")
            warnings.append("自定义价成交仍按今日买入计T+1，次一交易日才可卖")
    return {"ok": True, "preview_token": token, "client_token": t["client_token"],
            "side": t["side"], "code": t["code"], "name": t.get("name"),
            "price": t["price"], "price_time": t["price_time"],
            "price_source": t["price_source"],
            "price_label": t.get("price_label"),
            "trade_date": t.get("trade_date"), "backdated": bool(t.get("backdated")),
            "reference_close": t.get("reference_close"),
            "reference_date": t.get("reference_date"),
            "amount": t.get("amount"), "qty": t.get("qty"),
            "amount_requested": t.get("amount_requested"),
            "qty_requested": t.get("qty_requested") if t["side"] == "buy"
            else t.get("qty"),
            "fees": {k: t.get(k) for k in ("fee", "tax", "transfer_fee")},
            "cash_before": t.get("cash_before"), "cash_after": t.get("cash_after"),
            "sellable_qty": t.get("sellable_qty"), "holding_qty": t.get("holding_qty"),
            "stop": t.get("stop"), "target": t.get("target"),
            "override_required": bool(t.get("override_required")),
            "segment_id": t.get("segment_id"),
            "warnings": warnings}


def execute(payload: dict) -> dict:
    if payload.get("confirm") is not True:
        raise ManualError("必须确认人工订单", "CONFIRM_REQUIRED")
    preview_token = str(payload.get("preview_token") or "")
    if not preview_token:
        raise ManualError("preview_token 缺失，请重新预览", "PREVIEW_REQUIRED")
    with _THREAD_LOCK:
        con = store.connect()
        try:
            token = str(payload.get("client_token") or "")
            if token and _CLIENT_RE.match(token):
                old = store.manual_order_by_token(con, token)
                if old:
                    result = dict(old.get("result") or {})
                    result["duplicate"] = True
                    return result
        finally:
            con.close()
        lock = store.acquire_lock()
        if lock is None:
            raise ManualBusy("模拟盘正忙，请稍后重试", "BUSY")
        try:
            con = store.connect()
            try:
                t = _build_ticket(con, payload, for_execute=True)
                saved = _PREVIEWS.get(preview_token)
                if (not saved or saved["expires"] < time.time()
                        or _fingerprint(t) != {k: saved[k] for k in _fingerprint(t)}):
                    raise ManualRejected("预览已过期或行情/资金发生变化，请重新预览",
                                         "PREVIEW_STALE")
                _PREVIEWS.pop(preview_token, None)
                now = config.now_bjt()
                order_id = (f"mo|{now.strftime('%F')}|{now.strftime('%H%M%S')}|"
                            f"{secrets.token_hex(3)}")
                order = {
                    "order_id": order_id, "client_token": t["client_token"],
                    "created_at": config.iso_bjt(), "trade_date": t["trade_date"],
                    "side": t["side"], "code": t["code"], "name": t.get("name"),
                    "amount_requested": (t.get("amount_requested")
                                         if t["side"] == "buy" else None),
                    "qty_requested": (t.get("qty_requested") if t["side"] == "buy"
                                      else t.get("qty")),
                    "price_source": t["price_source"], "price_time": t["price_time"],
                    "stop_pct": t.get("stop_pct"), "target_pct": t.get("target_pct"),
                    "stop": t.get("stop"), "target": t.get("target"),
                    "segment_id": t.get("segment_id"), "override": t.get("override"),
                    "request_hash": t["request_hash"]}
                fees_cfg = config.settings().get("fees") or {}
                # 按参考收盘价成交的单，slot 记为收盘档，trade_date 记入参考交易日
                slot = "1500" if t.get("backdated") else now.strftime("%H%M")
                override_segment = t["segment_id"] if t.get("override_required") else ""
                account = store.get_account(con)
                cash = float(account["cash"] or 0)
                fill = None
                rejected = None
                with con:
                    if t["side"] == "buy":
                        res = local_match.execute_manual_buy(
                            con, t["code"], t.get("name") or t["code"],
                            t.get("amount_requested"), t.get("qty_requested"),
                            t["price"], t.get("prev_close"), cash, fees_cfg,
                            t["trade_date"], slot, order_id, t["reason"],
                            override_segment=override_segment,
                            price_source=t["price_source"],
                            price_time=t["price_time"])
                        if res.get("fill"):
                            fill = res["fill"]
                            cash = res["cash"]
                            store.update_account_cash(con, cash)
                            store.manual_position_open(
                                con, t["code"], t["month_id"], t["trade_date"],
                                t["price"], stop=t.get("stop"), target=t.get("target"),
                                stop_pct=t.get("stop_pct"),
                                target_pct=t.get("target_pct"))
                        else:
                            rejected = res.get("rejected") or "买入被拒"
                    else:
                        res = local_match.execute_manual_sell(
                            con, t["code"], t.get("name") or t["code"], t["qty"],
                            t["price"], t.get("prev_close"), cash, fees_cfg,
                            t["trade_date"], slot, order_id, t["reason"],
                            override_segment=override_segment,
                            price_time=t["price_time"])
                        if res.get("fill"):
                            fill = res["fill"]
                            cash = res["cash"]
                            store.update_account_cash(con, cash)
                            if store.holding_get(con, t["code"]) is None:
                                for m in store.active_position_meta(con):
                                    if m["code"] == t["code"]:
                                        store.close_position(
                                            con, m["code"], m["strategy"], m["month_id"],
                                            "人工清仓", t["trade_date"])
                        else:
                            rejected = res.get("rejected") or "卖出被拒"
                    order.update({"fill_qty": fill.get("qty") if fill else None,
                                  "price": fill.get("price") if fill else t["price"],
                                  "amount": fill.get("amount") if fill else None,
                                  "fee": fill.get("fee") if fill else 0,
                                  "tax": fill.get("tax") if fill else 0,
                                  "transfer_fee": fill.get("transfer_fee") if fill else 0,
                                  "status": "filled" if fill else "rejected",
                                  "reject_reason": rejected})
                    result = _public_result(t, order["status"], fill, rejected)
                    result.update({"order_id": order_id,
                                   "client_token": t["client_token"]})
                    store.manual_order_insert(con, order, result)
                if fill:
                    try:
                        prices = local_match.prepare_prices(con, [t["code"]])
                        snap = local_match.snapshot(con, prices, now.strftime("%F"))
                        with con:
                            store.save_sync(con, kind="manual", ok=1,
                                            positions=snap["positions"],
                                            cash=snap["cash"], total=snap["total"],
                                            market_value=snap["market_value"],
                                            market_as_of=t["price_time"])
                        from . import report
                        report.refresh_public_snapshot()
                        result["snapshot_refreshed"] = True
                    except Exception as exc:  # noqa: BLE001
                        config.log(f"人工单快照刷新失败 {order_id}：{type(exc).__name__}: {exc}")
                        result["snapshot_refreshed"] = False
                        result.setdefault("warnings", []).append("成交已落账，但页面快照刷新失败")
                config.log(f"人工{ '买入' if t['side']=='buy' else '卖出'} {order_id} "
                           f"{t['code']} {order['status']} {rejected or ''}".strip())
                return result
            finally:
                con.close()
        finally:
            store.release_lock(lock)
