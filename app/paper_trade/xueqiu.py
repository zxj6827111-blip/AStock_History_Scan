"""雪球唯一入口：easytrader 适配层（惰性 import，代码转换与脱敏在这里）。

安全口径：
- Cookie/组合代码只从 xueqiu.local.json（0600，不入 Git）读取；绝不进命令行、
  日志、异常文本或公开快照；
- adjust_weights 的返回值不可信（easytrader 网络失败也返回 None），提交后一律
  以回读对账为准，绝不因返回 None 就重发；
- 单元测试用 FakeXueqiuClient（本文件底部），绝不触网。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import config


class XueqiuError(Exception):
    """配置/参数错误（调用方跳过并告警）。"""


class XueqiuUnavailable(Exception):
    """网络/登录态失效（调用方标记 unknown，先对账再决定，绝不自动重发）。"""


def to_xq_code(code: str) -> str:
    """报告代码 → 雪球代码：000819.SZ → SZ000819；600406.SH → SH600406。"""
    c, _, sfx = str(code).partition(".")
    sfx = sfx.upper()
    if not (len(c) == 6 and c.isdigit() and sfx in ("SH", "SZ")):
        raise XueqiuError(f"不支持的代码 {code}")
    return sfx + c


def to_report_code(xq_code: str) -> str:
    """雪球代码 → 报告代码：SZ000819 → 000819.SZ。"""
    s = str(xq_code or "").upper()
    m = re.match(r"^(SH|SZ)(\d{6})$", s)
    if not m:
        raise XueqiuError(f"无法识别的雪球代码 {xq_code}")
    return f"{m.group(2)}.{m.group(1)}"


def _sanitize(text, limit: int = 300) -> str:
    """错误文本脱敏：抹 token/cookie 再限长（绝不回显登录态）。"""
    s = str(text or "")
    s = re.sub(r"(?i)(xq[a-z_]*token)=[^;\s\'\"]+", r"\1=***", s)
    s = re.sub(r"(?i)cookies?[^,)\n]*", "cookies=***", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


def verify_easytrader() -> tuple[bool, str]:
    """依赖可用性检查（缺 easytrader 只影响交易，不影响页面）。"""
    try:
        import easytrader  # noqa: F401
    except Exception as exc:  # noqa: BLE001  环境各异，统一兜住
        return False, f"easytrader 不可用：{type(exc).__name__}"
    try:
        from easytrader import xqtrader
        need = ("adjust_weights", "balance", "position", "prepare")
        missing = [n for n in need if not hasattr(xqtrader.XueQiuTrader, n)]
        if missing:
            return False, f"xqtrader 缺方法 {missing}"
    except Exception as exc:  # noqa: BLE001
        return False, f"xqtrader 不可用：{type(exc).__name__}"
    return True, "ok"


class XueqiuClient:
    """easytrader 雪球组合客户端（每次实例化重读本机配置）。"""

    def __init__(self, config_path=None, initial_assets=None):
        p = Path(config_path or config.xueqiu_config_path())
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise XueqiuError(
                f"雪球配置缺失/损坏：{p.name}（放 Cookie 与组合代码，0600）") from exc
        self.portfolio_code = str(d.get("portfolio_code") or "").strip()
        self.cookies = str(d.get("cookies") or "").strip()
        if not self.portfolio_code or not self.cookies:
            raise XueqiuError("雪球配置缺 portfolio_code/cookies")
        self.initial_assets = float(initial_assets
                                    or config.settings()["initial_assets"])
        self._user_obj = None

    def _merged_cookies(self) -> str:
        """用户 Cookie + 首页预热拿到的 WAF Cookie（acw_tc 等）。

        雪球 API 会拦没有 WAF Cookie 的请求（400016/418 空响应）；
        预热一次首页由雪球下发 acw_tc 再合并，是社区通行做法。
        预热失败退回原串（不吞错误——真实失败会由 API 调用暴露）。
        """
        cookies = self.cookies
        try:
            import requests  # easytrader 依赖已装
            s = requests.Session()
            s.headers.update({
                "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 Chrome/120 Safari/537.36"),
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": f"https://xueqiu.com/P/{self.portfolio_code}",
            })
            s.get("https://xueqiu.com/", timeout=10)
            existing = {p.split("=", 1)[0].strip()
                        for p in cookies.split(";") if "=" in p}
            extra = [f"{k}={v}" for k, v in s.cookies.get_dict().items()
                     if k not in existing]
            if extra:
                cookies = cookies + "; " + "; ".join(extra)
                self._warmed = True
        except Exception as exc:  # noqa: BLE001  预热失败不阻断（退回原串）
            config.log(f"雪球 WAF Cookie 预热失败（退回原串）：{type(exc).__name__}")
        return cookies

    def _user(self):
        if self._user_obj is not None:
            return self._user_obj
        try:
            import easytrader
        except Exception as exc:  # noqa: BLE001
            raise XueqiuUnavailable(
                f"easytrader 不可用：{type(exc).__name__}") from exc
        user = easytrader.use("xq", initial_assets=self.initial_assets)
        user.prepare(cookies=self._merged_cookies(),
                     portfolio_code=self.portfolio_code)
        # 雪球 WAF 实测（2026-11-23 二分定位）：显式 Host 头会触发 418 空响应，
        # 去掉即可；顺便把 Referer 修正到真实组合（easytrader 默认写死 ZH004612）。
        user.s.headers.pop("Host", None)
        user.s.headers["Referer"] = f"https://xueqiu.com/P/{self.portfolio_code}"
        self._user_obj = user
        return user

    @staticmethod
    def _call(fn):
        try:
            return fn()
        except XueqiuError:
            raise
        except Exception as exc:  # noqa: BLE001  网络失败/登录态失效统一脱敏
            raise XueqiuUnavailable(_sanitize(exc)) from None

    def balance(self) -> dict:
        data = self._call(lambda: self._user().balance)
        d = data[0] if isinstance(data, list) and data else (data or {})

        def _f(*keys):
            for k in keys:
                v = d.get(k)
                if v is not None:
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        continue
            return None

        total = _f("asset_balance", "total_asset", "total")
        if total is None:
            raise XueqiuUnavailable("雪球余额字段缺失（登录态可能已失效）")
        return {"total": total, "cash": _f("current_balance", "enable_balance",
                                          "cash", "available"),
                "market_value": _f("market_value", "marketValue")}

    def position(self) -> list[dict]:
        data = self._call(lambda: self._user().position)
        if isinstance(data, dict):
            data = [data]
        out = []
        for p in data or []:
            raw = str(p.get("stock_code") or p.get("code") or p.get("symbol") or "")
            try:
                code = to_report_code(raw)
            except XueqiuError:
                continue  # 非沪深股票条目（如港股/现金条目）跳过
            weight = p.get("weight")
            if weight is None:
                weight = p.get("percent")
            mv = p.get("market_value")
            if mv is None:
                mv = p.get("marketValue")
            out.append({
                "code": code,
                "name": p.get("stock_name") or p.get("name") or "",
                "weight": float(weight or 0.0),
                "market_value": float(mv or 0.0),
                "qty": p.get("volume") or p.get("amount") or p.get("qty"),
                "price": p.get("price") or p.get("current_price"),
            })
        return out

    def adjust_weights(self, weights: dict[str, float],
                       ignore_minor: float = 0.5) -> None:
        """全篮子目标权重提交（一次 HTTP）。返回值不可信，提交后必须回读对账。"""
        xq = {to_xq_code(c): round(float(w), 2) for c, w in weights.items()}
        self._call(lambda: self._user().adjust_weights(
            xq, ignore_minor=float(ignore_minor), fetch_position=True))


class FakeXueqiuClient:
    """测试替身：in-memory 组合，可注入失败；接口与 XueqiuClient 一致。"""

    def __init__(self, positions=None, cash=500000.0, fail_adjust=False,
                 fail_sync=False, name_prefix="测试"):
        self.total_assets = 1000000.0
        self.basket = {c: float(w) for c, w in (positions or {}).items()}
        self.cash = float(cash)
        self.fail_adjust = bool(fail_adjust)
        self.fail_sync = bool(fail_sync)
        self.calls = {"adjust": []}

    def balance(self):
        if self.fail_sync:
            raise XueqiuUnavailable("注入的回读失败")
        held = sum(self.basket.values())
        return {"total": self.total_assets,
                "cash": round(self.total_assets * (1 - held / 100), 2),
                "market_value": round(self.total_assets * held / 100, 2)}

    def position(self):
        if self.fail_sync:
            raise XueqiuUnavailable("注入的回读失败")
        out = []
        for code, w in self.basket.items():
            out.append({"code": code, "name": f"股票{code[:6]}", "weight": w,
                        "market_value": round(self.total_assets * w / 100, 2),
                        "qty": None, "price": None})
        return out

    def adjust_weights(self, weights, ignore_minor=0.5):
        self.calls["adjust"].append(dict(weights))
        if self.fail_adjust:
            # 与真实 easytrader 一致：网络失败不抛也不返回错误（返回 None），
            # 这里抛 XueqiuUnavailable 模拟“适配层捕获到异常”的最坏路径
            raise XueqiuUnavailable("注入的提交失败")
        self.basket = {c: float(w) for c, w in weights.items() if float(w) > 0}
