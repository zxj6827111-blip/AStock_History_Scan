"""模拟盘配置与路径单一来源。

优先级（高→低）：
  1. 环境变量 BAZI_PAPER_*（沙箱/测试覆盖）；
  2. app/data/paper_trade/settings.local.json（本机覆盖，不入 Git）；
  3. 代码内默认（与 settings.example.json 对齐）。

模式：
  disabled  完全不跑（默认，最安全）；
  dry_run   生成计划/快照/日报，绝不向雪球提交调仓；
  live      允许真实调仓，另需 BAZI_PAPER_TRADE_ENABLE=1 与已确认基线（三重开关）。
Kill switch：BAZI_PAPER_DISABLE=1 一票否决，任何入口都不跑。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

BJT = timezone(timedelta(hours=8))

PKG_DIR = Path(__file__).resolve().parent           # app/paper_trade/
APP_DIR = PKG_DIR.parent                             # app/
ROOT = APP_DIR.parent                                # bazi_month_relation_tengod_dev_v1/
OUTER_ROOT = ROOT.parent                             # AStock_History_Scan/

DEFAULTS: dict = {
    "mode": "disabled",                # disabled / dry_run / live
    "execution_backend": "xueqiu",     # xueqiu（雪球组合）| local（本地撮合账本）
    "manual_enabled": False,           # 是否开放页面人工建仓/清仓（仅发布端本地账本）
    "initial_assets": 1000000.0,       # 虚拟净值基数 / 本地账本初始现金
    "execute_time": "09:35",           # 计划执行时刻（BJT；local 后端自动改为 10:05）
    "catchup_until": "10:00",          # 迟到补执行窗口（含）；local 后端自动改为 10:35
    "sync_interval_minutes": 30,       # 交易时段组合回读间隔
    "eod_time": "15:12",               # 收盘后最终快照时刻
    "ignore_minor": 0.5,               # 权重微调忽略阈值（百分点，交 easytrader）
    "weight_tolerance": 1.5,          # 漂移/对账容差（百分点）
    "strategy_priority": ["b8", "s1c", "d_v1", "d_v2"],
    "fees": {"commission_rate": 0.00025, "commission_min": 5.0,
             "stamp_rate": 0.0005, "transfer_rate": 0.00001},
}


def private_dir() -> Path:
    """私有数据目录（Cookie/账本/锁/日志；.gitignore 的 app/data/ 已覆盖）。"""
    return Path(os.environ.get("BAZI_PAPER_PRIVATE_DIR",
                               str(ROOT / "app" / "data" / "paper_trade")))


def public_dir() -> Path:
    """公开只读快照目录（脱敏，随更新包下发客户机）。"""
    return Path(os.environ.get("BAZI_PAPER_PUBLIC_DIR",
                               str(ROOT / "data" / "paper_public")))


def market_sessions_path() -> Path:
    return Path(os.environ.get(
        "BAZI_PAPER_MARKET_SESSIONS",
        str(OUTER_ROOT / "slot88_full_market_v1" / "data" / "market_sessions_ext.csv")))


def slice_db_path() -> Path:
    """行情切片（本地撮合的价格源）：盘中每 30 分钟更新。"""
    return Path(os.environ.get(
        "BAZI_PAPER_SLICE_DB",
        str(OUTER_ROOT / "source_input" / "live" / "current_month.sqlite")))


def month_rules_dir() -> Path:
    return PKG_DIR / "month_rules"


def db_path() -> Path:
    return private_dir() / "paper.sqlite3"


def lock_path() -> Path:
    return private_dir() / "paper.lock"


def log_path() -> Path:
    return private_dir() / "paper.log"


def state_path() -> Path:
    return private_dir() / "paper_state.json"


def xueqiu_config_path() -> Path:
    return private_dir() / "xueqiu.local.json"


def _load_local_settings() -> dict:
    try:
        d = json.loads((private_dir() / "settings.local.json").read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def settings() -> dict:
    local = _load_local_settings()
    out = dict(DEFAULTS)
    out.update(local)
    out["fees"] = {**DEFAULTS["fees"], **(local.get("fees") or {})}
    env_mode = os.environ.get("BAZI_PAPER_MODE", "")
    if env_mode in ("disabled", "dry_run", "live"):
        out["mode"] = env_mode
    env_backend = os.environ.get("BAZI_PAPER_BACKEND", "")
    if env_backend in ("xueqiu", "local"):
        out["execution_backend"] = env_backend
    # 本地账本不需要等 09:35 开盘价：10:05 第一档盘中切片已含当日合成K线开盘价。
    # 本机显式覆盖 execute_time 时不强改。
    if (out["execution_backend"] == "local"
            and "execute_time" not in local
            and not os.environ.get("BAZI_PAPER_EXECUTE_TIME")):
        out["execute_time"] = "10:05"
        if "catchup_until" not in local:
            out["catchup_until"] = "10:35"
    return out


def mode() -> str:
    return str(settings()["mode"])


def disabled() -> bool:
    """kill switch 或 mode=disabled：任何入口直接返回。"""
    return (os.environ.get("BAZI_PAPER_DISABLE") == "1"
            or mode() == "disabled")


def live_arming_ready() -> bool:
    """真实调仓三重开关之一：环境总闸（另两个：mode=live + 已确认基线）。"""
    return os.environ.get("BAZI_PAPER_TRADE_ENABLE") == "1"


def now_bjt() -> datetime:
    return datetime.now(BJT)


def iso_bjt() -> str:
    return now_bjt().isoformat(timespec="seconds")


def log(line: str) -> None:
    try:
        private_dir().mkdir(parents=True, exist_ok=True)
        with log_path().open("a", encoding="utf-8") as f:
            f.write(f"[{iso_bjt()}] {line}\n")
    except OSError:
        pass


def write_json_atomic(path: Path, payload) -> None:
    """原子写 JSON（tmp→replace，对齐 sr_common.write_json 惯例）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")
    tmp.replace(path)
