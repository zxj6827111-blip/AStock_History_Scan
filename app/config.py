"""运行配置：数据库默认按本文件位置推导，可用环境变量覆盖。

本副本不复制 SQLite。所有连接由 api.connect() 以 mode=ro + PRAGMA query_only=ON 打开。
默认根目录取 app/config.py 向上两层（开发机 = 项目根；交付包 = 包根），
因此整包搬到别的机器、别的盘符仍可运行；BAZI_ROOT 可整体覆盖。
"""
from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_ROOT = Path(__file__).resolve().parents[2]
ROOT = Path(os.environ.get("BAZI_ROOT", str(_DEFAULT_ROOT))).resolve()

ASHARE_DB = Path(os.environ.get(
    "BAZI_ASHARE_DB",
    str(ROOT / "bazi_month_relation_local_v1/database/bazi_month_relation_local_v1.sqlite")))

ETF_DB = Path(os.environ.get(
    "BAZI_ETF_DB",
    str(ROOT / "bazi_month_relation_etf_v1/database/bazi_month_relation_etf_v1.sqlite")))

# 市值分档扩展库（可选）：只读附加；缺失时页面不显示市值档，不影响主库查询。
CAP_DB = Path(os.environ.get(
    "BAZI_CAP_DB",
    str(ROOT / "bazi_month_relation_local_v1/extensions/market_cap_tier_v1"
             "/database/market_cap_tier_v1.sqlite")))

# 节气月/旬/周三层规则库（只读）。2026-10-27 起这三层规则已经同步进主库（xw_ 前缀的 11 张表），
# 并且换成 v2 口径：A股 + ETF 1,705 + 指数 26 三类带 asset_class、且只收完整流月。
# 默认就读主库；如果部署包只带独立规则文件，用 BAZI_XUN_WEEK_DB 指到那个文件即可。
XUN_WEEK_DB = Path(os.environ.get("BAZI_XUN_WEEK_DB", str(ASHARE_DB)))
# 独立规则文件（可选，仅用于不共用主库的交付布局）
XUN_WEEK_STANDALONE = Path(os.environ.get(
    "BAZI_XUN_WEEK_STANDALONE",
    str(ROOT / "allstock_xun_week_v1/database/allstock_xun_week_v2.sqlite")))
# 页面顶部的数据集开关 → 规则库里的 asset_class（指数目前不进页面）
WINDOW_ASSET_CLASS = {"ashare": "A_SHARE", "etf": "ETF"}
WINDOW_TABLES = ("month_rules", "xun_rules", "week_rules",
                 "month_occurrences", "xun_occurrences", "week_occurrences")


def check_databases() -> list[str]:
    """启动自检：只确认文件存在与可读，不做任何写入。"""
    problems = []
    for label, path in (("A股", ASHARE_DB), ("ETF", ETF_DB), ("月/旬/周规则库（默认即主库）", XUN_WEEK_DB)):
        if not path.is_file():
            problems.append(f"{label}数据库不存在：{path}")
        elif not os.access(path, os.R_OK):
            problems.append(f"{label}数据库不可读：{path}")
    return problems
