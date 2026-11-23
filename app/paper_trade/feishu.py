"""模拟盘飞书卡片：dry-run 优先（只写本地卡片文件），发送仅手动触发。

- 复用生存者报告的飞书本机配置（webhook/签名，BAZI_FEISHU_CONFIG_FILE 可覆盖）；
- 引擎只调 dry_run()：卡片写 app/data/paper_trade/cards/，不联网；
- 发送（send）只由 CLI 手动执行——按项目惯例先预览卡片再发群；
- 卡片是简单 text 消息（带签名），内容绝不含 Cookie/组合代码/请求头。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.request
from pathlib import Path

from . import config

HTTP_TIMEOUT = 5.0


def _cfg_path() -> Path:
    return Path(os.environ.get(
        "BAZI_FEISHU_CONFIG_FILE",
        str(config.OUTER_ROOT / "survivor_report_v1" / "data"
            / "feishu_push.local.json")))


def _load_cfg() -> dict:
    try:
        d = json.loads(_cfg_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _gen_sign(secret: str, ts: str) -> str:
    """飞书自定义机器人签名（与 sr_feishu_push._gen_sign 同实现）。"""
    string_to_sign = f"{ts}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"),
                      digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _text_card(lines: list[str]) -> dict:
    card = {"msg_type": "text", "content": {"text": "\n".join(lines)[:3800]}}
    secret = _load_cfg().get("sign_secret")
    if secret:
        ts = str(int(time.time()))
        card["timestamp"] = ts
        card["sign"] = _gen_sign(str(secret), ts)
    return card


def build_plan_card(plan: dict) -> dict:
    p = report_like(plan)
    lines = [
        "【模拟盘·次日调仓计划】",
        f"执行日 {p.get('execute_trade_date')}（{p.get('segment_id') or '—'}段"
        f" / {p.get('regime')} / 总仓上限 {p.get('total_weight_cap')}%）",
        f"信号源 {p.get('signal_date')} {p.get('source_report')}",
    ]
    buys = p.get("buys") or []
    exits = p.get("exits") or []
    if buys:
        lines.append(f"买入 {len(buys)} 只：")
        lines += [f"· {b.get('name')}（{b.get('strategy')} {b.get('weight')}%）："
                  f"{b.get('reason')}" for b in buys[:8]]
    else:
        lines.append("买入：无")
    if exits:
        lines.append(f"卖出 {len(exits)} 只：")
        lines += [f"· {e.get('code')}（{e.get('strategy')}）：{e.get('reason')}"
                  for e in exits[:8]]
    excluded = p.get("excluded") or []
    if excluded:
        lines.append(f"排除 {len(excluded)} 笔（"
                     + "；".join(f"{x.get('code')}:{x.get('reason')}"
                                 for x in excluded[:4]) + "）")
    return _text_card(lines)


def build_result_card(payload: dict) -> dict:
    lines = [
        "【模拟盘·调仓结果】",
        f"计划 {payload.get('plan_id')}",
        f"状态：{payload.get('status')}",
    ]
    result = payload.get("result") or {}
    for k, v in result.items():
        lines.append(f"{k}: {v}")
    return _text_card(lines)


def build_daily_card(payload: dict) -> dict:
    nav = payload.get("nav") or {}
    lines = ["【模拟盘·日终快照】"]
    if nav:
        lines += [f"日期 {nav.get('trade_date')}",
                  f"总资产 {nav.get('total')}",
                  f"净值 {nav.get('nav')}",
                  f"当日盈亏 {nav.get('day_pnl')}",
                  f"累计盈亏 {nav.get('cum_pnl')}"]
    else:
        lines.append(f"回读 sync_ok={payload.get('sync_ok')}（未落 NAV）")
    return _text_card(lines)


def build_failure_card(payload: dict) -> dict:
    return _text_card([
        "【模拟盘·故障】",
        f"环节 {payload.get('kind')}",
        f"错误 {payload.get('error')}",
        "请检查 Cookie/网络；真实调仓在结果未知时绝不自动重发。",
    ])


def report_like(plan: dict) -> dict:
    """兼容 signals 计划与 store 行两种形态（buys/exits 顶层或 intents 内）。"""
    p = dict(plan)
    if "buys" not in p:
        intents = p.get("intents") or {}
        for k in ("buys", "exits", "holds", "excluded", "warnings"):
            p.setdefault(k, intents.get(k) or [])
    return p


_BUILDERS = {
    "plan": build_plan_card,
    "result": build_result_card,
    "daily": build_daily_card,
    "failure": build_failure_card,
}


def dry_run(kind: str, payload: dict) -> Path | None:
    """构建卡片写本地（不联网）；卡片文件供人工预览与手动发送。"""
    builder = _BUILDERS.get(kind)
    if builder is None:
        return None
    card = builder(payload)
    d = config.private_dir() / "cards"
    d.mkdir(parents=True, exist_ok=True)
    p = d / (time.strftime("%Y%m%dT%H%M%S") + f"_{kind}.json")
    p.write_text(json.dumps({"kind": kind, "card": card}, ensure_ascii=False,
                            indent=1) + "\n", encoding="utf-8")
    return p


def send(card_path: Path) -> dict:
    """手动发送（CLI 专用）：读卡片文件 POST webhook；失败如实返回。"""
    data = json.loads(Path(card_path).read_text(encoding="utf-8"))
    card = data.get("card") or data
    webhook = _load_cfg().get("webhook_url")
    if not webhook:
        return {"ok": False, "error": "飞书配置缺 webhook_url"}
    req = urllib.request.Request(
        webhook, data=json.dumps(card, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:  # noqa: S310
        body = json.loads(r.read().decode("utf-8"))
    return {"ok": body.get("code") in (0, None), "resp": body}
