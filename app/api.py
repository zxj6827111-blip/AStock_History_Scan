"""Local read-only query API over bazi_month_relation_local_v1.sqlite.

Every handler is a plain SELECT against the local database: no network, no NAS, no production
server. Values are returned as stored; nothing is re-derived here.
"""
from __future__ import annotations

import math
import os
import sqlite3
from pathlib import Path
import sys
from datetime import datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import config
import ten_god_rules
import ten_god_service as tg

# 开发副本：数据库路径来自 config.py（可用环境变量覆盖），一律只读打开。
DB = config.ASHARE_DB
ETF_DB = config.ETF_DB
CAP_DB = config.CAP_DB
# 时区：Mac/Linux 自带系统时区库，Windows 通常没有、且默认不含 tzdata 包 —— 那样 ZoneInfo 会直接抛错、
# 服务起不来。取不到时区库时退回固定 +08:00：上海自 1991 年后无夏令时，本库数据自 2000 年起，
# 与 ZoneInfo('Asia/Shanghai') 逐时刻等价（已实测对照）。
try:
    TZ = ZoneInfo("Asia/Shanghai")
except (ZoneInfoNotFoundError, OSError):
    TZ = timezone(timedelta(hours=8), "CST")
# 市值档：按自由流通市值（亿元）现算，下界含、上界不含。筛选与展示都走 cap_band_sql()
# 这一份定义，避免两处各写一套而漂移。冻结扩展库里的 tier 列（阈值 1000/500/200/50）
# 仍是今日关注模型的输入特征，这里不改它，只在读出时重新分档。
CAP_BANDS = (
    ("100亿以下", 0.0, 100.0),
    ("100-300亿", 100.0, 300.0),
    ("300-500亿", 300.0, 500.0),
    ("500-1000亿", 500.0, 1000.0),
    ("1000亿以上", 1000.0, None),
)
CAP_BAND_LABELS = tuple(label for label, _, _ in CAP_BANDS)


def cap_band_sql(col: str) -> str:
    """把自由流通市值列表达式翻成五档名称的 CASE 片段（调用方保证 col 来自本模块常量）。"""
    whens = []
    for label, lo, hi in CAP_BANDS:
        cond = f"{col} >= {lo}" if hi is None else f"{col} >= {lo} AND {col} < {hi}"
        whens.append(f"WHEN {cond} THEN '{label}'")
    return "CASE " + " ".join(whens) + " END"


CAP_BAND_COL = "ct.free_mv_yi"
CAP_BAND_EXPR = cap_band_sql(CAP_BAND_COL)

BANNER = "V1.3研究规则｜未经命理最终裁定｜历史描述性统计，非预测"
DEFAULT_DATASET = "ashare"
DATASETS = {
    "ashare": {
        "db": DB,
        "label": "A股",
        "default_identity": "000001.SH",
        "app_title": "全A股 八字冲合关系 × 历史涨跌 本地数据库",
        "search_placeholder": "输入股票代码或名称，如 000001.SH / 茅台",
        "stock_tab_label": "一 · 股票查询",
        "market_label": "全市场",
        "profile_hint": ("本命三柱直接取冻结档案（<span class=\"mono\">完整八字_20261016</span>），"
                         "不依行情重算；本轮不使用本命时柱。日主是天干，地支冲合一律针对本命"
                         "<b>年支 / 月支 / 日支</b>分别判断。"),
        "branch_stats_note": "汇总取冻结 stats_branch12 的",
        "market_stats_note": "全市场 22 类关系基准（冻结 CSV 原样导入，用于对账）",
    },
    "etf": {
        "db": ETF_DB,
        "label": "ETF",
        "default_identity": "510050.SH",
        "app_title": "全ETF 八字冲合关系 × 历史涨跌 本地数据库",
        "search_placeholder": "输入 ETF 代码或名称，如 510050.SH / 上证50ETF",
        "stock_tab_label": "一 · ETF查询",
        "market_label": "全 ETF",
        "profile_hint": ("本命三柱取库内首根 K 线日期（<span class=\"mono\">ETF_FIRSTBAR_v1_20261019</span>），"
                         "不取官方上市日；关系只用年 / 月 / 日三柱。日主是天干，地支冲合一律针对本命"
                         "<b>年支 / 月支 / 日支</b>分别判断。"),
        "branch_stats_note": "汇总取本库 stats_branch12（按 A股冻结表同一 SQL 口径重算）的",
        "market_stats_note": "全 ETF 22 类关系基准（按 A股同口径由 ETF 库计算，用于对账）",
    },
}
DEFAULT_IDENTITY = DATASETS[DEFAULT_DATASET]["default_identity"]

# 六冲 / 六合 官方类型分组，用于页面筛选（口径来自 relation_catalog，不新增规则）
CLASH_TYPES = ("六冲", "天干相冲", "天克地冲", "反吟")
HARMONY_TYPES = ("六合", "三合", "半合", "天干五合", "天合地合", "三会")
PUNISH_TYPES = ("相刑", "三刑", "自刑")
HARM_TYPES = ("相害",)
BREAK_TYPES = ("六破",)

_CAP_ATTACH_WARNED = False


def connect(dataset: str = DEFAULT_DATASET) -> sqlite3.Connection:
    global _CAP_ATTACH_WARNED
    spec = DATASETS.get(dataset)
    if spec is None:
        raise ValueError(f"未知数据集 {dataset!r}；可用：{', '.join(DATASETS)}")
    con = sqlite3.connect(spec["db"].resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    _window_views(con, spec["db"])   # 必须在 query_only 之前：mode=ro 下 query_only=ON 会挡住 TEMP 视图创建
    con.execute("PRAGMA query_only=ON")
    if dataset == "ashare" and CAP_DB.is_file():
        try:
            con.execute("ATTACH DATABASE ? AS cap", (CAP_DB.resolve().as_uri() + "?mode=ro",))
        except sqlite3.Error as exc:
            # 市值档挂不上时页面会自动隐藏该筛选；这里只在首次把原因打到终端，不静默吞掉
            if not _CAP_ATTACH_WARNED:
                _CAP_ATTACH_WARNED = True
                print(f"[提示] 市值分档扩展库挂载失败，市值档筛选将不显示："
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
    return con


def _cap_ready(con) -> bool:
    return any(r[1] == "cap" for r in con.execute("PRAGMA database_list"))


_CEILING_CACHE: dict = {}


def _class_ceiling(con, rules_tbl: str, asset_class: str):
    """该标的类别在同一根流月柱上的出现次数天花板（= max(ALL_valid_n)）与其历史覆盖区间。"""
    key = (rules_tbl, asset_class)
    if key in _CEILING_CACHE:
        return _CEILING_CACHE[key]
    try:
        row = con.execute(f"""SELECT max(ALL_valid_n), min(month_id), max(month_id) FROM (
                SELECT ALL_valid_n, NULL AS month_id FROM {rules_tbl} WHERE asset_class=?
                UNION ALL SELECT NULL, month_id FROM month_occurrences WHERE asset_class=?)""",
                          (asset_class, asset_class)).fetchone()
        val = (row[0] or 0, row[1], row[2])
    except sqlite3.Error:
        val = None
    _CEILING_CACHE[key] = val
    return val


def _window_history_end(con) -> str:
    try:
        return con.execute("SELECT max(month_id) FROM month_occurrences").fetchone()[0] or ""
    except sqlite3.Error:
        return ""


def _window_data_as_of(con, asset_class: str) -> str:
    """各源行情截止日不同（A股/指数 2026-10-14，ETF 2026-10-16），必须按类别报，不能写死一个。"""
    try:
        raw = None
        # 同步进主库时 meta 表带 xw_ 前缀；独立规则文件里它就叫 meta。两种布局都要能读到
        for name in ("main.xw_meta", "xws.meta", "main.meta", "xw.meta"):
            try:
                raw = con.execute(f"SELECT v FROM {name} WHERE k='input_data_as_of'").fetchone()
            except sqlite3.Error:
                continue
            if raw:
                break
        if raw and raw[0].strip().startswith("{"):
            import json as _json
            return _json.loads(raw[0]).get(asset_class, "")
        return raw[0] if raw else ""
    except sqlite3.Error:
        return ""


def _window_views(con, main_db=None) -> bool:
    """把三层规则表接进当前连接，供旧 SQL 用不带前缀的表名读写：

      - 主库里已有 xw_* 表（2026-10-27 同步后的正式布局）→ 建 TEMP 视图 main.xw_* 的别名；
      - 否则若配置指了独立规则文件（v2，带 asset_class）→ 只读挂载该文件，再建 TEMP 视图指向它；
      - 两条都不成立 → 返回 False，页面板块自动隐藏（沿用原有 _xw_ready 语义）。

    TEMP 视图只活在本连接内存里，不写任何数据库文件；连接是 mode=ro 打开的，
    而 query_only=ON 会连 TEMP 对象的 DDL 一起挡掉，所以本函数必须在设 query_only 之前调用。
    """
    ok = False
    try:
        have = {r[0] for r in con.execute(
            "SELECT name FROM main.sqlite_master WHERE type='table' AND name GLOB 'xw_*'")}
            # 用 GLOB 不用 LIKE：SQLite 的 LIKE 里下划线是单字符通配，'xw\_%' 反而会一个都匹配不到
        if {"xw_month_rules", "xw_xun_rules", "xw_week_rules"} <= have:
            for t in config.WINDOW_TABLES:
                con.execute(f"CREATE TEMP VIEW IF NOT EXISTS {t} AS SELECT * FROM main.xw_{t}")
            ok = True
        else:
            # 本数据集的主库（如 ETF 库）没有 xw_* 时，回落到 A 股主库里的同一份同步结果，
            # 再退到独立规则文件——三个数据集永远读同一份数，不会出现两套数对不上
            src, alias = None, None
            if config.ASHARE_DB.is_file() and main_db is not None \
                    and config.ASHARE_DB.resolve() != Path(main_db).resolve():
                src, alias = config.ASHARE_DB, "xws"
            elif config.XUN_WEEK_STANDALONE.is_file():
                src, alias = config.XUN_WEEK_STANDALONE, "xws"
            if src is not None:
                attached = {r[1] for r in con.execute("PRAGMA database_list")}
                if alias not in attached:  # 本函数可能被同一连接重复调用，重复 ATTACH 会报 already in use
                    con.execute(f"ATTACH DATABASE ? AS {alias}", (src.resolve().as_uri() + "?mode=ro",))
                has2 = {r[0] for r in con.execute(
                    f"SELECT name FROM {alias}.sqlite_master WHERE type='table' AND name GLOB 'xw_*'")}
                if {"xw_month_rules", "xw_xun_rules", "xw_week_rules"} <= has2:
                    for t in config.WINDOW_TABLES:
                        con.execute(f"CREATE TEMP VIEW IF NOT EXISTS {t} AS SELECT * FROM {alias}.xw_{t}")
                    ok = True
                else:
                    for t in config.WINDOW_TABLES:
                        try:
                            con.execute(f"CREATE TEMP VIEW IF NOT EXISTS {t} AS SELECT * FROM {alias}.{t}")
                            ok = True
                        except sqlite3.Error:
                            ok = False
                            break
    except sqlite3.Error as exc:
        print(f"[提示] 月/旬/段规则表未接入，窗口层将不显示：{type(exc).__name__}: {exc}", file=sys.stderr)
    return ok


def _xw_ready(con) -> bool:
    """不看 DDL 返回值，直接探一张表——sqlite3.Connection 不允许挂自定义属性，
    所以这里用一次廉价探针而不是缓存标记。"""
    try:
        con.execute("SELECT 1 FROM xun_rules LIMIT 1").fetchone()
        return True
    except sqlite3.Error:
        return False


def _window_rules_source(con, layer: str) -> str:
    """返回带 schema 的真实规则表名。

    TEMP VIEW 一旦参与「计算列 + json_each 左连接」就不会被展开，SQLite 会把
    整张 100 万行视图先物化，查询从 0.1 秒掉到 13 秒。窗口层直接读真实表，
    让 (asset_class, flow_pillar, xun_no) 索引继续生效；独立规则文件布局则读 xws。
    """
    base = "xun_rules" if layer == "xun" else "week_rules"
    xw = f"xw_{base}"
    try:
        if con.execute("SELECT 1 FROM main.sqlite_master WHERE type='table' AND name=?",
                       (xw,)).fetchone():
            return f"main.{xw}"
    except sqlite3.Error:
        pass
    try:
        attached = {r[1] for r in con.execute("PRAGMA database_list")}
    except sqlite3.Error:
        attached = set()
    if "xws" in attached:
        for name in (xw, base):
            try:
                if con.execute("SELECT 1 FROM xws.sqlite_master WHERE type='table' AND name=?",
                               (name,)).fetchone():
                    return f"xws.{name}"
            except sqlite3.Error:
                pass
    return base


def window_rules_status(con) -> dict:
    """给页面用的规则库自述：读的是哪份、含哪些标的类别、口径版本。"""
    if not _xw_ready(con):
        return {"available": False, "note": "未找到月/旬/段规则表"}
    try:
        cls = con.execute("SELECT asset_class, count(*) FROM xun_rules GROUP BY 1 ORDER BY 1").fetchall()
        span = con.execute("SELECT min(month_id), max(month_id) FROM month_occurrences").fetchone()
        fut = con.execute("SELECT count(*) FROM month_rules").fetchone()[0]
    except sqlite3.Error as exc:
        return {"available": False, "note": f"规则表结构不符（需要 asset_class 列）：{exc}"}
    return {"available": True,
            "source": str(config.XUN_WEEK_DB),
            "classes": {r[0]: r[1] for r in cls},
            "history": [span[0], span[1]],
            "month_rule_rows": fut,
            "note": "只收完整流月；档位按 max(上涨率,下跌率) 分档，=100% 可能是整段全跌，方向看多数方向列"}


def _window_class_clause(con, dataset: str):
    """返回 (SQL 片段, 参数字典)。数据集决定借哪一类的历史。"""
    ac = config.WINDOW_ASSET_CLASS.get(dataset)
    if ac is None:
        return "", {}
    return " AND r.asset_class = :xw_class", {"xw_class": ac}


def _rows(con, sql, params=()):
    return [dict(r) for r in con.execute(sql, params).fetchall()]


# ---------------- 十神展示层（只读取时计算，不写库、不改旧字段） ----------------

def profile_day_master(con, identity: str):
    """返回 (日主天干, 状态)。缺档案时状态为 UNAVAILABLE_PROFILE，绝不猜。"""
    row = con.execute("SELECT day_master_stem, profile_present FROM natal_profiles"
                      " WHERE identity_code=?", (identity,)).fetchone()
    if row is None or not row["profile_present"]:
        return None, "UNAVAILABLE_PROFILE"
    stem = row["day_master_stem"]
    if stem not in ten_god_rules.STEM_ELEMENT:
        return None, "UNAVAILABLE_PROFILE"
    return stem, "OK"


def attach_ten_god(row: dict, day_master, pillar, keep_legacy: bool = True) -> dict:
    """给历史事实行追加十神对象；不覆盖/不改名任何旧字段。"""
    obj = tg.get_pillar_ten_gods(day_master, pillar)
    # 只有真正的单月事实行才标事实状态；60 柱聚合行没有 direction，保持 None
    if "direction" in row:
        obj["fact_status"] = tg.fact_status(True, row.get("direction"))
    row["ten_god"] = obj
    row["ten_god_status"] = obj["status"]
    row["flow_hidden_stem_ten_gods"] = obj["hidden_stems"]
    row["hidden_stem_summary"] = obj["hidden_summary"]
    if not keep_legacy:
        row["stem_ten_god"] = obj["stem"]["ten_god"] if obj["status"] == "OK" else None
    return row


def search(con, q: str, limit: int = 40):
    q = (q or "").strip()
    if not q:
        return _rows(con, "SELECT identity_code, security_code, name, security_type,"
                          " has_natal_profile FROM securities ORDER BY identity_code"
                          " LIMIT ?", (limit,))
    like = f"%{q}%"
    exact = q.upper() if q.isascii() else q
    return _rows(con, """
      SELECT identity_code, security_code, name, security_type, has_natal_profile,
           (identity_code=? OR security_code=?) AS exact,
           (name LIKE ?) AS name_hit
      FROM securities
      WHERE security_code LIKE ? OR identity_code LIKE ? OR name LIKE ?
      ORDER BY exact DESC, name_hit DESC, length(identity_code), identity_code
      LIMIT ?""", (exact, exact, like, like, like, like, limit))


def stock(con, identity: str, dataset: str = DEFAULT_DATASET):
    head = _rows(con, "SELECT * FROM securities WHERE identity_code=?", (identity,))
    if not head:
        return None
    prof = _rows(con, "SELECT * FROM natal_profiles WHERE identity_code=?", (identity,))
    months = con.execute("SELECT COUNT(*), SUM(undisputed), SUM(boundary_disputed),"
                         " SUM(profile_present=0), SUM(single_day) FROM monthly_facts"
                         " WHERE identity_code=?", (identity,)).fetchone()
    out = {"security": head[0], "profile": prof[0] if prof else None,
           "month_totals": {"final": months[0], "undisputed": months[1],
                            "disputed": months[2], "no_profile": months[3],
                            "single_day": months[4] or 0},
           "ten_god_rule_version": ten_god_rules.RULE_VERSION,
           "ten_god_hidden_rule_version": ten_god_rules.HIDDEN_RULE_VERSION}
    dm, dm_status = profile_day_master(con, identity)
    out["day_master_status"] = dm_status
    out["day_master"] = dm
    if dataset == "etf":
        fund = None
        try:
            row = con.execute("SELECT * FROM etf_fund_meta WHERE identity_code=?",
                              (identity,)).fetchone()
            fund = dict(row) if row else None
        except sqlite3.OperationalError:
            fund = None
        out["fund_meta"] = fund
    out["cap"] = None
    if _cap_ready(con):
        row = con.execute(
            "SELECT month_id, snap_date, " + cap_band_sql("free_mv_yi") + " AS band,"
            " free_mv_yi, circ_mv_yi, free_ratio,"
            " avg_amt_20d_yi, avg_amt_60d_yi FROM cap.cap_tier_monthly"
            " WHERE identity_code=? ORDER BY month_id DESC LIMIT 1", (identity,)).fetchone()
        if row is not None:
            out["cap"] = dict(row)
    return out


def _int(v, lo: int, hi: int, dflt: int) -> int:
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return dflt
    return min(max(n, lo), hi)


BRANCH_METRIC = {
    "up_rate": "up_frequency",
    "down_rate": "down_frequency",
    "n": "n",
    "mean": "mean_return",
}


def _order_branch_rows(rows, sort, min_n):
    """Same presentation rules as the frozen table: zero-sample branches listed last."""
    if min_n:
        rows = [r for r in rows if (r.get("n") or 0) >= int(min_n)]
    rows.sort(key=lambda r: r.get("branch_ordinal") or 0)
    metric = BRANCH_METRIC.get(sort)
    if metric:
        rows.sort(key=lambda r: (r.get(metric) is None, -(r.get(metric) or 0)))
    rows.sort(key=lambda r: 0 if (r.get("n") or 0) else 1)
    return rows


def branch12(con, identity: str, policy: str, flt: dict | None = None):
    """Frozen 12-branch stats, or the same statistics recomputed over filtered months."""
    flt = flt or {}
    if not any(flt.get(k) for k in ("pillar", "stem_element", "branch_element")):
        rows = _rows(con, """
          SELECT b.*, o.ordinal AS branch_ordinal, 0 AS filtered_view FROM stats_branch12 b
          LEFT JOIN branch_order o ON o.branch=b.branch
          WHERE b.identity_code=? AND b.sample_policy=?""", (identity, policy))
    else:
        rows = _branch12_filtered(con, identity, policy, flt)
    return _order_branch_rows(rows, flt.get("sort"), flt.get("min_n"))


def _branch12_filtered(con, identity: str, policy: str, flt: dict):
    """Re-derive stats_branch12 over a filtered month set.

    Every formula here was verified to reproduce the frozen stats_branch12 exactly when
    the filter is empty (n/up/down/flat, mean, median, single_day_n, benchmark_n,
    excess_mean, outperform_n, sample_grade, main_relations), so the filtered view is a
    narrowing of the same statistics rather than a new definition.
    """
    return _rows(con, """
      WITH base AS (
        SELECT f.month_id, f.month_branch, f.direction, f.body_return, f.excess_return,
               f.single_day, f.comparison_status, f.benchmark_return, p.sample_policy
        FROM monthly_facts f
        CROSS JOIN (SELECT 'ALL' AS sample_policy UNION ALL SELECT 'EXCLUDE_SINGLE') p
        WHERE f.identity_code=:id
          AND (p.sample_policy='ALL' OR f.single_day=0)
          AND (:pillar='' OR f.flow_pillar LIKE :pillar)
          AND (:stem='' OR f.flow_stem_element=:stem)
          AND (:bel='' OR f.flow_branch_element=:bel)
      ),
      agg AS (
        SELECT sample_policy sp, month_branch b, COUNT(*) n,
               SUM(direction='UP') up_n, SUM(direction='DOWN') down_n,
               SUM(direction='FLAT') flat_n, AVG(body_return) mean_return,
               MIN(body_return) min_return, MAX(body_return) max_return,
               SUM(single_day) single_day_n, AVG(excess_return) excess_mean,
               SUM(comparison_status='MATCHED') benchmark_n,
               AVG(benchmark_return) benchmark_mean, SUM(excess_return>0) outperform_n
        FROM base GROUP BY 1, 2
      ),
      med AS (
        SELECT sp, b, AVG(body_return) median_return FROM (
          SELECT sample_policy sp, month_branch b, body_return,
                 ROW_NUMBER() OVER (PARTITION BY sample_policy, month_branch
                                    ORDER BY body_return) rn,
                 COUNT(*) OVER (PARTITION BY sample_policy, month_branch) c
          FROM base)
        WHERE rn IN ((c+1)/2, (c+2)/2) GROUP BY 1, 2
      ),
      rel AS (
        SELECT m.month_branch b, t.relation_type rt, COUNT(*) n
        FROM month_relation_types t
        JOIN (SELECT DISTINCT month_id, month_branch FROM base) m ON m.month_id=t.month_id
        WHERE t.identity_code=:id AND t.record_kind='OFFICIAL' AND t.undisputed=1
        GROUP BY 1, 2
      ),
      r5 AS (
        SELECT b, group_concat(rt||'('||n||')',';') main_relations FROM (
          SELECT b, rt, n, ROW_NUMBER() OVER (PARTITION BY b ORDER BY n DESC, rt) rn FROM rel)
        WHERE rn<=5 GROUP BY 1
      )
      SELECT o.branch, o.ordinal AS branch_ordinal,
             be.element AS branch_element,
             COALESCE(a.n, 0) n, COALESCE(a.up_n, 0) up_n, COALESCE(a.down_n, 0) down_n,
             COALESCE(a.flat_n, 0) flat_n,
             CASE WHEN a.n>0 THEN ROUND(a.up_n*1.0/a.n, 6) END up_frequency,
             CASE WHEN a.n>0 THEN ROUND(a.down_n*1.0/a.n, 6) END down_frequency,
             CASE WHEN a.n>0 THEN ROUND(a.flat_n*1.0/a.n, 6) END flat_frequency,
             a.mean_return, md.median_return, a.min_return, a.max_return,
             CASE WHEN COALESCE(a.n, 0)=0 THEN 'NO_HISTORY' WHEN a.n=1 THEN 'SINGLE_OBSERVATION'
                  WHEN a.n<=3 THEN 'VERY_FEW' WHEN a.n<=5 THEN 'FEW'
                  ELSE 'RELATIVELY_MORE' END sample_grade,
             COALESCE(a.single_day_n, 0) single_day_n,
             COALESCE(a.benchmark_n, 0) benchmark_n, a.benchmark_mean, a.excess_mean,
             COALESCE(a.outperform_n, 0) outperform_n,
             CASE WHEN :policy='ALL' THEN COALESCE(a.n, 0)
                  ELSE COALESCE(a.n, 0)-COALESCE(a.single_day_n, 0) END valid_sample_n,
             r5.main_relations, 1 AS filtered_view,
             '筛选后即时重算：口径与冻结 stats_branch12 一致（主要命中关系取该筛选月份内 '
             ||'OFFICIAL 且无争议前 5 类，与原表一样不随样本口径变化）' AS data_quality_note
      FROM branch_order o
      LEFT JOIN branch_element be ON be.branch=o.branch
      LEFT JOIN agg a ON a.b=o.branch AND a.sp=:policy
      LEFT JOIN med md ON md.b=o.branch AND md.sp=:policy
      LEFT JOIN r5 ON r5.b=o.branch""",
                 {"id": identity, "policy": policy, "pillar": f"%{flt.get('pillar') or ''}%",
                  "stem": flt.get("stem_element") or "",
                  "bel": flt.get("branch_element") or ""})


def branch_detail(con, identity: str, branch: str, policy: str, flt: dict | None = None):
    """Every formal month whose flow pillar carries this branch, with its own result."""
    flt = flt or {}
    sql = """
      SELECT f.month_id, f.solar_year, f.flow_pillar, f.start_term, f.first_date, f.last_date,
             f.trading_days, f.direction, f.body_return, f.single_day, f.partial_listing,
             f.partial_delisting, f.quality_flags, f.research_sample_status,
             f.flow_stem_element, f.flow_branch_element,
             f.stem_ten_god, f.relation_type_list, f.candidate_kind_list,
             f.confirmed_event_count, f.benchmark_code, f.benchmark_return, f.excess_return,
             (SELECT group_concat(t.relation_type, ' ')
                FROM month_relation_types t
                WHERE t.identity_code=f.identity_code AND t.month_id=f.month_id
                  AND t.record_kind='OFFICIAL') AS official_types,
             (SELECT group_concat(t.relation_type, ' ')
                FROM month_relation_types t
                WHERE t.identity_code=f.identity_code AND t.month_id=f.month_id
                  AND t.record_kind='CANDIDATE') AS candidate_types
      FROM monthly_facts f
      WHERE f.identity_code=? AND f.month_branch=?"""
    params: list = [identity, branch]
    if policy == "EXCLUDE_SINGLE":
        sql += " AND f.single_day=0"
    if flt.get("pillar"):
        sql += " AND f.flow_pillar LIKE ?"
        params.append(f"%{flt['pillar']}%")
    if flt.get("stem_element"):
        sql += " AND f.flow_stem_element=?"
        params.append(flt["stem_element"])
    if flt.get("branch_element"):
        sql += " AND f.flow_branch_element=?"
        params.append(flt["branch_element"])
    sql += " ORDER BY f.month_id"
    rows = _rows(con, sql, params)
    day_master, _ = profile_day_master(con, identity)
    for r in rows:
        attach_ten_god(r, day_master, r["flow_pillar"])
    return rows


CALENDAR_COLS = ("month_id, solar_year, month_no, start_term, start_at, end_at,"
                 " month_pillar, calendar_version")


OPP_PAST_MAX = 60      # 往前 60 个节气月 = 正好一个 60 柱周期，每根柱各出现一次
# 未来可选窗口固定到 2050 年 12 月（按 start_at 所在自然年月），不再只放 60 个月。
# 同一根柱每 60 个月（约 5 年）重现一次，往后多出来的月份共用这根柱的历史统计。
OPP_FUTURE_END = "2051-01-01T00:00:00+08:00"


def _compute_month_windows(current: dict, now: datetime) -> dict:
    """按节气交节微秒与当前北京时间，严密推算当前所处旬（1-3）与周（1-6）及剩余倒计时。"""
    start_dt = datetime.fromisoformat(current["start_at"])
    end_dt = datetime.fromisoformat(current["end_at"])
    elapsed_sec = max(0.0, (now - start_dt).total_seconds())
    elapsed_days = elapsed_sec / 86400.0

    weeks = []
    curr_week_no = 6
    for w in range(1, 7):
        w_start = start_dt + timedelta(days=(w - 1) * 5)
        w_end = end_dt if w == 6 else start_dt + timedelta(days=w * 5)
        is_cur = (w_start <= now < w_end) if w < 6 else (now >= w_start)
        if is_cur:
            curr_week_no = w
        is_passed = now >= w_end
        weeks.append({
            "week_no": w,
            "week_name": f"第{w}段",
            "xun_no": 1 if w in (1, 2) else (2 if w in (3, 4) else 3),
            "xun_name": "上旬" if w in (1, 2) else ("中旬" if w in (3, 4) else "下旬"),
            "day_from": (w - 1) * 5,
            "day_to": None if w == 6 else w * 5,
            "start_at": w_start.isoformat(),
            "end_at": w_end.isoformat(),
            "is_current": is_cur,
            "is_passed": is_passed,
        })

    curr_xun_no = 1 if curr_week_no in (1, 2) else (2 if curr_week_no in (3, 4) else 3)
    xun_names = {1: "上旬", 2: "中旬", 3: "下旬"}

    cur_week_obj = weeks[curr_week_no - 1]
    cur_week_end = datetime.fromisoformat(cur_week_obj["end_at"])
    week_sec_left = max(0, int((cur_week_end - now).total_seconds()))
    week_days_left = week_sec_left // 86400
    week_hours_left = (week_sec_left % 86400) // 3600
    week_mins_left = (week_sec_left % 3600) // 60

    if week_days_left > 0:
        week_left_str = f"余 {week_days_left} 天"
    elif week_hours_left > 0:
        week_left_str = f"余 {week_hours_left} 小时"
    else:
        week_left_str = f"余 {week_mins_left} 分钟"

    xun_end = end_dt if curr_xun_no == 3 else start_dt + timedelta(days=curr_xun_no * 10)
    xun_sec_left = max(0, int((xun_end - now).total_seconds()))
    xun_days_left = xun_sec_left // 86400
    xun_hours_left = (xun_sec_left % 86400) // 3600
    xun_mins_left = (xun_sec_left % 3600) // 60

    if xun_days_left > 0:
        xun_left_str = f"余 {xun_days_left} 天"
    elif xun_hours_left > 0:
        xun_left_str = f"余 {xun_hours_left} 小时"
    else:
        xun_left_str = f"余 {xun_mins_left} 分钟"

    return {
        "xun_no": curr_xun_no,
        "xun_name": xun_names[curr_xun_no],
        "week_no": curr_week_no,
        "week_name": f"第{curr_week_no}段",
        "day_offset": round(elapsed_days, 2),
        "days_elapsed": int(elapsed_days),
        "week_days_left": week_days_left,
        "week_left_str": week_left_str,
        "xun_days_left": xun_days_left,
        "xun_left_str": xun_left_str,
        "weeks": weeks,
    }


def calendar_context(con):
    """Current solar-term month plus the neighbouring and next-occurrence pillars.

    Dates come from the frozen calendar_months table (same source as the history),
    never from a freshly computed ephemeris.
    """
    now = datetime.now(TZ)
    stamp = now.isoformat(timespec="microseconds")
    cur = _rows(con, f"SELECT {CALENDAR_COLS} FROM calendar_months"
                     " WHERE start_at<=? AND end_at>? ORDER BY start_at DESC LIMIT 1",
                (stamp, stamp))
    future = _rows(con, f"SELECT {CALENDAR_COLS} FROM calendar_months"
                        " WHERE start_at>? AND start_at<? ORDER BY start_at",
                  (stamp, OPP_FUTURE_END))
    past = _rows(con, f"SELECT {CALENDAR_COLS} FROM calendar_months"
                      " WHERE end_at<=? ORDER BY start_at DESC LIMIT ?", (stamp, OPP_PAST_MAX))
    past.reverse()   # 时间升序，和 future 拼成一条完整时间线
    next_by_pillar = {}
    for row in future:
        row["days_until"] = (datetime.fromisoformat(row["start_at"]).date()
                             - now.date()).days
        # 只认首次命中：窗口超过 60 个月后同一根柱会出现多次，后写会把「下次出现」顶成最后一次
        next_by_pillar.setdefault(row["month_pillar"], row)
    current = cur[0] if cur else None
    if current:
        current["days_until_end"] = (datetime.fromisoformat(current["end_at"]).date()
                                     - now.date()).days
        current["window"] = _compute_month_windows(current, now)
    cal = _rows(con, "SELECT MIN(start_at) AS first_start, MAX(end_at) AS last_end,"
                     " MIN(calendar_version) AS calendar_version FROM calendar_months")[0]
    return {"asof": now.date().isoformat(), "current": current, "future": future,
            "past": past, "next_by_pillar": next_by_pillar, "calendar": cal}


_CALENDAR_CACHE: dict[tuple, dict] = {}


def calendar_now(con, identity: str | None = None, dataset: str = DEFAULT_DATASET):
    """Calendar provenance plus the flat month timeline the opportunity board picks from.

    `months` is one entry per selectable solar-term month, ordered by time ascending, each
    carrying its `offset` from 本月 (negative = 已过去, positive = 未来, 0 = 本月). The page
    never does calendar arithmetic itself: it sends the offset back and the server re-resolves
    it against this same frozen table.
    """
    cache_key = (dataset, identity)
    if cache_key in _CALENDAR_CACHE:
        return _CALENDAR_CACHE[cache_key]
    cal = calendar_context(con)
    span = _rows(con, "SELECT MIN(month_id) AS first_month, MAX(month_id) AS last_month,"
                      " COUNT(DISTINCT month_id) AS months_with_data FROM monthly_facts")[0]
    months = []
    timeline = (cal["past"] + ([cal["current"]] if cal["current"] else [])
                + cal["future"])
    for offset, row in zip(range(-len(cal["past"]), len(timeline) - len(cal["past"])), timeline):
        months.append({**{k: row[k] for k in
                          ("month_id", "solar_year", "month_no", "start_term", "start_at",
                           "end_at", "month_pillar")},
                       "offset": offset,
                       "label": month_label(offset),
                       "is_current": offset == 0 and cal["current"] is not None,
                       "has_data": bool(span["last_month"])
                                   and row["month_id"] <= span["last_month"]})
    identity = identity or DATASETS[dataset]["default_identity"]
    day_master, dm_status = profile_day_master(con, identity)
    if cal["current"]:
        cal["current"]["ten_god"] = tg.get_pillar_ten_gods(day_master,
                                                           cal["current"]["month_pillar"])
        cur_entry = next((m for m in months if m["offset"] == 0), None)
        cal["current"]["fact_status"] = (tg.FACT_HISTORICAL if cur_entry and cur_entry["has_data"]
                                         else tg.FACT_NO_HISTORY)
        cal["current"]["ten_god"]["fact_status"] = cal["current"]["fact_status"]
    # 只给「当前」「下一」流月补十神：未来更远月份页面不展示，避免无谓放大响应
    next_month = next((m for m in months if m["offset"] == 1), None)
    if next_month:
        next_month["ten_god"] = tg.get_pillar_ten_gods(day_master, next_month["month_pillar"])
        next_month["fact_status"] = (tg.FACT_HISTORICAL if next_month["has_data"]
                                     else tg.FACT_NO_HISTORY)
        next_month["ten_god"]["fact_status"] = next_month["fact_status"]
    res = {"asof": cal["asof"], "current": cal["current"], "calendar": cal["calendar"],
           "current_window": cal["current"].get("window") if cal["current"] else None,
           "months": months, "data_span": span, "identity_code": identity,
           "day_master": day_master, "ten_god_status": dm_status,
           "ten_god_rule_version": ten_god_rules.RULE_VERSION}
    if len(_CALENDAR_CACHE) > 128:
        _CALENDAR_CACHE.pop(next(iter(_CALENDAR_CACHE)))
    _CALENDAR_CACHE[cache_key] = res
    return res


def pillar_options(con):
    return _rows(con, """
      SELECT po.flow_pillar, po.ordinal,
             se.element AS stem_element, be.element AS branch_element
      FROM pillar_order po
      LEFT JOIN stem_element se ON se.stem=substr(po.flow_pillar,1,1)
      LEFT JOIN branch_element be ON be.branch=substr(po.flow_pillar,2,1)
      ORDER BY po.ordinal""")


def pillar60(con, identity: str, flt: dict):
    where, params = ["p.identity_code=?"], [identity]
    if flt.get("stem_element"):
        where.append("p.stem_element=?")
        params.append(flt["stem_element"])
    if flt.get("branch_element"):
        where.append("p.branch_element=?")
        params.append(flt["branch_element"])
    if flt.get("pillar"):
        where.append("p.flow_pillar LIKE ?")
        params.append(f"%{flt['pillar']}%")
    min_n = _int(flt.get("min_n"), 0, 60, 0)
    if min_n:
        where.append("p.occurrence_count>=?")
        params.append(min_n)
    for key, names in (("clash", CLASH_TYPES), ("harmony", HARMONY_TYPES),
                       ("punish", PUNISH_TYPES), ("harm", HARM_TYPES),
                       ("broken", BREAK_TYPES)):
        if flt.get(key) == "1":
            ph = ",".join("?" * len(names))
            where.append(f"""EXISTS(SELECT 1 FROM stats_relation_pillar x
                  WHERE x.identity_code=p.identity_code AND x.record_kind='OFFICIAL'
                    AND x.flow_pillar=p.flow_pillar AND x.relation_type IN ({ph}))""")
            params.extend(names)
        if flt.get(key) == "0":
            ph = ",".join("?" * len(names))
            where.append(f"""NOT EXISTS(SELECT 1 FROM stats_relation_pillar x
                  WHERE x.identity_code=p.identity_code AND x.record_kind='OFFICIAL'
                    AND x.flow_pillar=p.flow_pillar AND x.relation_type IN ({ph}))""")
            params.extend(names)
    if flt.get("both_clash_harmony") == "1":
        ph1, ph2 = ",".join("?" * len(CLASH_TYPES)), ",".join("?" * len(HARMONY_TYPES))
        where.append(f"""EXISTS(SELECT 1 FROM stats_relation_pillar x
                  WHERE x.identity_code=p.identity_code AND x.record_kind='OFFICIAL'
                    AND x.flow_pillar=p.flow_pillar AND x.relation_type IN ({ph1}))
              AND EXISTS(SELECT 1 FROM stats_relation_pillar y
                  WHERE y.identity_code=p.identity_code AND y.record_kind='OFFICIAL'
                    AND y.flow_pillar=p.flow_pillar AND y.relation_type IN ({ph2}))""")
        params.extend(CLASH_TYPES + HARMONY_TYPES)
    if flt.get("day_master_element"):
        where.append("""(SELECT day_master_element FROM natal_profiles n
                         WHERE n.identity_code=p.identity_code)=?""")
        params.append(flt["day_master_element"])
    direction = "down" if flt.get("dir") == "down" else "up"
    rate_raw = str(flt.get("rate_min") or "").strip()
    rate_min = _int(rate_raw, 0, 100, -1) if rate_raw else None
    rate_eq = flt.get("rate_eq") == "1" and rate_min == 100
    if rate_min is not None:
        rate_col = ("p.down_n_ALL*1.0/NULLIF(p.all_sample_n,0)" if direction == "down"
                    else "p.up_n_ALL*1.0/NULLIF(p.all_sample_n,0)")
        where.append("p.all_sample_n > 0")
        if rate_eq:
            where.append(f"({rate_col} = 1.0)")
        else:
            where.append(f"({rate_col} >= ?)")
            params.append(rate_min / 100.0)

    sort_val = flt.get("sort")
    if sort_val == "up_rate":
        if direction == "down":
            order = "down_rate DESC NULLS LAST, mean_body_return_ALL ASC NULLS LAST"
        else:
            order = "up_rate DESC NULLS LAST, mean_body_return_ALL DESC NULLS LAST"
    elif sort_val == "mean":
        if direction == "down":
            order = "mean_body_return_ALL ASC NULLS LAST"
        else:
            order = "mean_body_return_ALL DESC NULLS LAST"
    elif sort_val == "n":
        order = "occurrence_count DESC"
    elif sort_val == "branch":
        order = "br_ord, po.ordinal"
    else:
        order = "po.ordinal"
    rows = _rows(con, f"""
      SELECT p.*, CASE WHEN p.all_sample_n>0 THEN ROUND(p.up_n_ALL*1.0/p.all_sample_n,6) END
                    AS up_rate,
             CASE WHEN p.all_sample_n>0 THEN ROUND(p.down_n_ALL*1.0/p.all_sample_n,6) END
                    AS down_rate,
             np.day_master_stem, np.day_master_element, np.natal_year_pillar,
             np.natal_month_pillar, np.natal_day_pillar, np.profile_present
      FROM stats_pillar60 p LEFT JOIN natal_profiles np ON np.identity_code=p.identity_code
      LEFT JOIN pillar_order po ON po.flow_pillar=p.flow_pillar
      LEFT JOIN branch_order br_ord ON br_ord.branch=p.branch
      WHERE {' AND '.join(where)} ORDER BY {order}""", params)
    if rows:
        cal = calendar_context(con)
        for r in rows:
            r["next_occurrence"] = cal["next_by_pillar"].get(r["flow_pillar"])
            r["current_occurrence"] = (dict(cal["current"]) if cal["current"]
                                       and cal["current"]["month_pillar"] == r["flow_pillar"]
                                       else None)
            r["calendar_asof"] = cal["asof"]
            r["calendar_version"] = cal["calendar"]["calendar_version"]
            r["calendar_last_end"] = cal["calendar"]["last_end"]
    # 十神：按该身份日主 × 本行流月柱即时计算；缺档案返回 UNAVAILABLE_PROFILE
    dm, dm_status = profile_day_master(con, identity)
    for r in rows:
        attach_ten_god(r, dm, r["flow_pillar"], keep_legacy=False)
        r["day_master_stem"] = dm
        r["day_master_status"] = dm_status
    return rows


# ---------------- 页面五：本月与下月流月机会（全市场横向表） ----------------
OPP_MONTHS_MAX = 6
OPP_CAP_DEFAULT = 100
OPP_CAP_MAX = 500
OPP_ROWS_TOTAL_MAX = 1200
# 排序表达式只允许取自这里，用户输入永不进 SQL 文本；末两键保证截断结果可复现
# （戊戌 ≥3 里有 191 只并列 100%，无兜底键时 LIMIT 截断集会随执行计划抖动）。
# up_rate / up_rate_excl 是历史兼容键名：统计哪个方向由 dir 决定（dir=down 取 down_*）。
# dir 缺省为 up，与加入方向轴之前逐行一致。
def opp_metric(direction: str) -> dict:
    hit = "down" if direction == "down" else "up"
    return {
        "up_rate": {"ALL": f"{hit}_rate_ALL", "EXCLUDE_SINGLE": f"{hit}_rate_ES"},
        "up_rate_excl": {"ALL": f"{hit}_rate_excl_ALL",
                         "EXCLUDE_SINGLE": f"{hit}_rate_excl_ES"},
        "n": {"ALL": "occurrence_count", "EXCLUDE_SINGLE": "occurrence_count"},
        "mean": {"ALL": "mean_body_return_ALL", "EXCLUDE_SINGLE": "mean_body_return_ES"},
        "excess": {"ALL": "excess_mean", "EXCLUDE_SINGLE": "excess_mean"},
        "stability": {"ALL": "stability_score", "EXCLUDE_SINGLE": "stability_score"},
    }
# 占比并列时按平均涨幅再排（否则全 100% 的一大批票会退化成按代码排）
OPP_TIEBREAK = {"ALL": "mean_body_return_ALL", "EXCLUDE_SINGLE": "mean_body_return_ES"}
OPP_GROUP_ORDER = {"branch": "branch_ord, pillar_ord, identity_code",
                   "identity": "identity_code"}
# 表头多列排序：只接受这些键；每个键的方向只接受 asc/desc，用户输入永不进 SQL 文本。
OPP_MSORT_KEYS = ("up_rate", "mean", "excess", "min_gain", "max_gain", "stability", "consistency")


def _parse_msort(raw) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for part in str(raw or "").split(","):
        key, sep, direction = part.partition(":")
        key = key.strip()
        direction = direction.strip().lower()
        if not sep or key not in OPP_MSORT_KEYS or direction not in ("asc", "desc"):
            continue
        if any(k == key for k, _ in out):
            continue
        out.append((key, direction))
        if len(out) >= 6:
            break
    return out


def _msort_order_expr(msort: list[tuple[str, str]], direction: str, policy: str) -> str:
    """Build a validated ORDER BY for the header multi-sort chips.

    asc on stability/consistency means best-first (稳/一致 first), because those
    columns are badges rather than raw numbers; asc on numeric keys is low-first.
    """
    metrics = opp_metric(direction)
    keys: list[str] = []
    stab_order = cons_order = None
    for key, order in msort:
        if key in ("up_rate", "mean", "excess"):
            keys.append(f"{metrics[key][policy]} {order.upper()} NULLS LAST")
        elif key == "min_gain":
            keys.append(f"stab_min_ret {order.upper()} NULLS LAST")
        elif key == "max_gain":
            keys.append(f"stab_max_ret {order.upper()} NULLS LAST")
        elif key == "stability":
            # 档位是用户键；稳健分只能作为所有用户键之后的兜底，
            # 不能在下一用户键之前插入，否则“稳健度→平均涨跌幅”会变成“稳健度→稳健分→平均涨跌幅”。
            if stab_order is None:
                stab_order = order
            keys.append("CASE WHEN stab_level_rank = 3 THEN 1 ELSE 0 END ASC")
            keys.append(f"stab_level_rank {'ASC' if order == 'asc' else 'DESC'} NULLS LAST")
        elif key == "consistency":
            if cons_order is None:
                cons_order = order
            keys.append("CASE WHEN cons_level_rank = 3 THEN 1 ELSE 0 END ASC")
            keys.append(f"cons_level_rank {'ASC' if order == 'asc' else 'DESC'} NULLS LAST")
    if stab_order is not None:
        keys.append(f"stability_score {'DESC' if stab_order == 'asc' else 'ASC'} NULLS LAST")
    if cons_order is not None:
        keys.append(f"cons_cv {'ASC' if cons_order == 'asc' else 'DESC'} NULLS LAST")
        keys.append("cons_n DESC NULLS LAST")
    keys.append("occurrence_count DESC")
    keys.append("identity_code")  # 截断结果可复现
    return ", ".join(keys)
OPP_GRADE_TXT = {"0": "0 次", "1-2": "1–2 次", "3-5": "3–5 次", "6+": "≥6 次"}
# 稳健度阈值只在这里写一份；SQL 筛选、Python 判定、导出说明都引用同一组数。
# 安全边际按同柱振幅缩放（margin ÷ max(振幅, 2%)），否则低波动的 ETF 会被 A股绝对阈值全判弱。
STAB_RANGE_FLOOR = 0.02
STAB_UP_WEAK_MRATIO = 0.15
STAB_UP_GOOD_MRATIO = 0.30
STAB_UP_WEAK_SHARE = 0.70
STAB_UP_GOOD_SHARE = 0.50
STAB_DOWN_HIGH_RANGE = 0.40
STAB_DOWN_LOW_RANGE = 0.20
STAB_DOWN_HIGH_SHARE = 0.70
STAB_DOWN_LOW_SHARE = 0.50
# V2 历史幅度一致性：只看所选方向的月份，CV 分档；1% 是独立幅度底线。
CONS_GOOD_CV = 0.50
CONS_BAD_CV = 0.80
CONS_FLOOR = 0.01


def stability_level(direction: str, n, margin, share, rng) -> tuple[str, str, str]:
    """Return (level, 中文, reason). 同柱历史证据，不含个股整体波动。"""
    if not n or margin is None or share is None:
        return "none", "—", "样本不足，无法评估"
    if direction == "down":
        high = rng is not None and (rng >= STAB_DOWN_HIGH_RANGE
                                    or share >= STAB_DOWN_HIGH_SHARE)
        low = rng is not None and (rng < STAB_DOWN_LOW_RANGE
                                   and share < STAB_DOWN_LOW_SHARE)
        actual = -margin  # stab_margin 是距 0 的绝对边际；显示时还原成最轻一次跌幅
        if high:
            why = []
            if rng is not None and rng >= STAB_DOWN_HIGH_RANGE:
                why.append(f"同柱振幅 {rng*100:.0f}%（≥40%）")
            if share >= STAB_DOWN_HIGH_SHARE:
                why.append(f"最惨一次占跌幅 {share*100:.0f}%（≥70%）")
            return "high", "高", "；".join(why) + " → 历史跌幅集中/振幅大"
        if low:
            return "low", "低", (f"同柱振幅 {rng*100:.0f}%；最惨一次占 "
                                 f"{share*100:.0f}%；未见过度集中")
        return "mid", "中", (f"同柱振幅 {rng*100:.0f}%；最惨一次占 {share*100:.0f}%；"
                             f"最轻一次 {actual*100:+.2f}%")
    mratio = margin / max(rng or 0.0, STAB_RANGE_FLOOR)
    weak = mratio < STAB_UP_WEAK_MRATIO or share >= STAB_UP_WEAK_SHARE
    good = mratio >= STAB_UP_GOOD_MRATIO and share <= STAB_UP_GOOD_SHARE
    if weak:
        why = []
        if n <= 3:
            why.append(f"样本仅 {n} 次")
        if mratio < STAB_UP_WEAK_MRATIO:
            why.append(f"最差一次 {margin*100:+.2f}%，边际/振幅 {mratio:.2f}（<0.15）")
        if share >= STAB_UP_WEAK_SHARE:
            why.append(f"最好一次占累计 {share*100:.0f}%（≥70%）")
        return "weak", "弱", "；".join(why)
    if good:
        return "stable", "稳", (f"最差一次 {margin*100:+.2f}%，边际/振幅 {mratio:.2f}；"
                                f"最好一次占 {share*100:.0f}%；边际厚、集中度低")
    return "mid", "一般", (f"最差一次 {margin*100:+.2f}%，边际/振幅 {mratio:.2f}；"
                           f"最好一次占 {share*100:.0f}%；证据一般")


def consistency_reason(direction, k, total, cv, drop, min_dir, floor, worst, reverse_n):
    """Human-readable V2 reason. Direction-conditional; drop is display-only."""
    if not k or k < 2:
        return f"方向样本不足（k={k or 0}）"
    up = direction != "down"
    parts = [f"历史{'上涨' if up else '下跌'} {k}/{total}"]
    parts.append(f"CV {cv:.2f}" if cv is not None else "CV —")
    if min_dir is not None:
        parts.append(f"最小{'涨幅' if up else '跌幅'} {min_dir*100:+.2f}%")
    parts.append(f"反向/非方向 {reverse_n} 次")
    if worst is not None:
        parts.append(f"全月最差 {worst*100:+.2f}%")
    parts.append("幅度达标" if floor else "幅度不足（<1%）")
    if drop is not None:
        parts.append(f"drop {drop*100:.1f}%（仅展示）")
    return "；".join(parts)


OPP_COLUMNS = """
      p.identity_code, p.security_code, COALESCE(s.name, p.display_name) AS name,
      s.security_type, s.listed_date, s.delisted_date, s.has_natal_profile,
      s.formal_month_count, s.last_month_id, s.processing_status,
      p.flow_pillar, p.stem, p.branch, p.stem_element, p.branch_element,
      p.occurrence_count, p.all_sample_n, p.exclude_single_n, p.single_day_n,
      p.up_n_ALL, p.down_n_ALL, p.flat_n_ALL,
      p.up_n_EXCLUDE_SINGLE, p.down_n_EXCLUDE_SINGLE, p.flat_n_EXCLUDE_SINGLE,
      ROUND(p.up_n_ALL*1.0/NULLIF(p.all_sample_n,0), 6)                 AS up_rate_ALL,
      ROUND(p.up_n_EXCLUDE_SINGLE*1.0/NULLIF(p.exclude_single_n,0), 6)  AS up_rate_ES,
      ROUND(p.down_n_ALL*1.0/NULLIF(p.all_sample_n,0), 6)               AS down_rate_ALL,
      ROUND(p.down_n_EXCLUDE_SINGLE*1.0/NULLIF(p.exclude_single_n,0), 6) AS down_rate_ES,
      p.mean_body_return_ALL, p.median_body_return_ALL, p.mean_body_return_EXCLUDE_SINGLE
        AS mean_body_return_ES,
      p.min_body_return_ALL, p.max_body_return_ALL,
      /* 稳健度：只用同一根柱的历史出现，按当前 policy（ALL / EXCLUDE_SINGLE）聚合。
         agg 在查询里按所选柱一次算好；这里只带原始量，衍生量在 scored CTE 里算。 */
      a.n                                                                AS stab_n,
      a.min_ret                                                          AS stab_min_ret,
      a.max_ret                                                          AS stab_max_ret,
      a.sum_ret                                                          AS stab_sum_ret,
      a.sum_pos                                                          AS stab_sum_pos,
      a.sum_neg                                                          AS stab_sum_neg,
      a.sum_sq                                                           AS stab_sum_sq,
      /* V2 历史幅度一致性：方向月份计数/平方和/最小最大幅度；sum_pos/sum_neg 已在上方。
         up_* 只看正收益；dn_* 只看负收益并取绝对值。衍生量在 scored2 里算。 */
      a.up_cnt                                                           AS cons_up_n,
      a.up_sq                                                            AS cons_up_sq,
      a.up_min                                                           AS cons_up_min,
      a.up_max                                                           AS cons_up_max,
      a.dn_cnt                                                           AS cons_dn_n,
      a.dn_sq                                                            AS cons_dn_sq,
      a.dn_min                                                           AS cons_dn_min,
      a.dn_max                                                           AS cons_dn_max,
      CASE WHEN :policy='ALL' THEN p.all_sample_n ELSE p.exclude_single_n END AS score_n,
      CASE WHEN :dir='down'
           THEN (CASE WHEN :policy='ALL' THEN p.down_n_ALL ELSE p.down_n_EXCLUDE_SINGLE END)
           ELSE (CASE WHEN :policy='ALL' THEN p.up_n_ALL ELSE p.up_n_EXCLUDE_SINGLE END)
      END                                                                AS score_hit,
      p.excess_mean, p.outperform_n, p.sample_marker_ALL, p.sample_grade_ALL,
      p.is_five_year_repeat, p.occurrence_years, p.year_gaps,
      p.relation_types, p.candidate_kinds, p.undisputed_relation_months,
      p.boundary_disputed_months, p.blocked_no_profile_months,
      p.confirmed_relation_events, p.candidate_structure_events,
      p.relation_rule_version, p.policy_approval_status, p.input_data_as_of,
      /* 所选月自身的事实行（月+柱唯一，最多一行）；该月没有行情时为全 NULL */
      f.month_id IS NOT NULL                                       AS in_sample,
      f.direction                                                  AS actual_direction,
      f.body_return                                                AS actual_return,
      f.excess_return                                              AS actual_excess,
      f.single_day                                                 AS actual_single_day,
      f.trading_days                                               AS actual_trading_days,
      f.research_sample_status                                     AS actual_sample_status,
      /* 把所选月从同柱历史里摘出去后的分母/分子：只有该月确实落在当前口径的样本内才减 1。
         全库 333,780 个 (身份,柱) 组已核对 all_sample_n / exclude_single_n / up_n 与
         monthly_facts 逐行计数完全一致，所以这一步是冻结值的减法，不是重算统计。 */
      p.all_sample_n - (f.month_id IS NOT NULL)                    AS n_excl_ALL,
      p.up_n_ALL - CASE WHEN f.month_id IS NOT NULL AND f.direction='UP'
                        THEN 1 ELSE 0 END                          AS up_excl_ALL,
      p.down_n_ALL - CASE WHEN f.month_id IS NOT NULL AND f.direction='DOWN'
                          THEN 1 ELSE 0 END                        AS down_excl_ALL,
      p.exclude_single_n - CASE WHEN f.month_id IS NOT NULL AND f.single_day=0
                                THEN 1 ELSE 0 END                  AS n_excl_ES,
      p.up_n_EXCLUDE_SINGLE - CASE WHEN f.month_id IS NOT NULL AND f.single_day=0
                                     AND f.direction='UP' THEN 1 ELSE 0 END AS up_excl_ES,
      p.down_n_EXCLUDE_SINGLE - CASE WHEN f.month_id IS NOT NULL AND f.single_day=0
                                       AND f.direction='DOWN' THEN 1 ELSE 0 END AS down_excl_ES,
      CASE WHEN p.all_sample_n - (f.month_id IS NOT NULL) > 0
           THEN ROUND((p.up_n_ALL - CASE WHEN f.month_id IS NOT NULL AND f.direction='UP'
                                         THEN 1 ELSE 0 END) * 1.0
                      / (p.all_sample_n - (f.month_id IS NOT NULL)), 6) END AS up_rate_excl_ALL,
      CASE WHEN p.exclude_single_n - CASE WHEN f.month_id IS NOT NULL AND f.single_day=0
                                          THEN 1 ELSE 0 END > 0
           THEN ROUND((p.up_n_EXCLUDE_SINGLE - CASE WHEN f.month_id IS NOT NULL
                                                      AND f.single_day=0
                                                      AND f.direction='UP'
                                                    THEN 1 ELSE 0 END) * 1.0
                      / (p.exclude_single_n - CASE WHEN f.month_id IS NOT NULL
                                                     AND f.single_day=0
                                                   THEN 1 ELSE 0 END), 6) END AS up_rate_excl_ES,
      CASE WHEN p.all_sample_n - (f.month_id IS NOT NULL) > 0
           THEN ROUND((p.down_n_ALL - CASE WHEN f.month_id IS NOT NULL AND f.direction='DOWN'
                                           THEN 1 ELSE 0 END) * 1.0
                      / (p.all_sample_n - (f.month_id IS NOT NULL)), 6) END AS down_rate_excl_ALL,
      CASE WHEN p.exclude_single_n - CASE WHEN f.month_id IS NOT NULL AND f.single_day=0
                                          THEN 1 ELSE 0 END > 0
           THEN ROUND((p.down_n_EXCLUDE_SINGLE - CASE WHEN f.month_id IS NOT NULL
                                                        AND f.single_day=0
                                                        AND f.direction='DOWN'
                                                      THEN 1 ELSE 0 END) * 1.0
                      / (p.exclude_single_n - CASE WHEN f.month_id IS NOT NULL
                                                     AND f.single_day=0
                                                   THEN 1 ELSE 0 END), 6) END AS down_rate_excl_ES,
      n.boundary_conflict,
      n.natal_year_pillar, n.natal_month_pillar, n.natal_day_pillar,
      n.year_stem_element, n.year_branch_element,
      n.month_stem_element, n.month_branch_element,
      n.day_stem_element, n.day_branch_element,
      n.day_master_stem, n.day_master_element,
      po.ordinal AS pillar_ord, bo.ordinal AS branch_ord
"""


def _int(v, lo: int, hi: int, dflt: int) -> int:
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return dflt
    return min(max(n, lo), hi)


def month_label(ordinal: int) -> str:
    """相对本月的第 ordinal 个节气月叫什么。本月算第 1 个，所以前后对称。"""
    if ordinal == 0:
        return "本月"
    if ordinal == 1:
        return "下月"
    if ordinal == -1:
        return "上月"
    return (f"第 {ordinal + 1} 个月" if ordinal > 0 else f"往前第 {-ordinal} 个月")


_OPP_CACHE: dict[tuple, dict] = {}


def opportunities(con, flt: dict, dataset: str = DEFAULT_DATASET):
    """Cross-market board for one selectable solar-term month, straight off frozen stats.

    Every number here is a read of stats_pillar60 / monthly_facts / securities /
    natal_profiles; nothing is re-derived. When the selected month lies in the past the board
    additionally shows what that month actually did, plus the same-pillar frequency with that
    month taken out of the sample — because the plain frequency already contains it.
    """
    layer = flt.get("layer") or flt.get("window_layer") or "month"
    if layer in ("xun", "week"):
        return opportunities_window(con, flt, layer, dataset)

    live_sig = _live_meta_cached()[1] if dataset == "ashare" else ()
    cache_key = (dataset, tuple(sorted((k, str(v)) for k, v in flt.items() if v is not None)),
                 live_sig)
    if cache_key in _OPP_CACHE:
        return _OPP_CACHE[cache_key]
    market_label = DATASETS[dataset]["market_label"]
    months_n = _int(flt.get("months"), 1, OPP_MONTHS_MAX, 2)
    min_n = _int(flt.get("min_n"), 0, 60, 3)
    policy = flt.get("policy") if flt.get("policy") in ("ALL", "EXCLUDE_SINGLE") else "ALL"
    cap = _int(flt.get("cap"), 1, OPP_CAP_MAX, OPP_CAP_DEFAULT)
    cap = min(cap, max(50, OPP_ROWS_TOTAL_MAX // months_n))
    page = _int(flt.get("page"), 1, 100000, 1)
    row_off = (page - 1) * cap
    cap_tier = flt.get("cap_tier") if flt.get("cap_tier") in CAP_BAND_LABELS else ""
    sort = flt.get("sort") or "mean"
    direction = "down" if flt.get("dir") == "down" else "up"
    msort = _parse_msort(flt.get("msort"))
    if msort:
        sort = msort[0][0]  # 兼容旧字段；真正的排序由 msort 决定
    dir_txt = "下跌" if direction == "down" else "上涨"
    grade = flt.get("grade") if flt.get("grade") in ("0", "1-2", "3-5", "6+") else ""
    rate_raw = str(flt.get("rate_min") or "").strip()
    rate_min = _int(rate_raw, 0, 100, -1) if rate_raw else None
    rate_eq = flt.get("rate_eq") == "1" and rate_min == 100
    # 稳健度筛选：exclude_bad=排除弱（上涨）/排除反转高（下跌），only_good=只看稳/只看反转低。
    stab = flt.get("stab") if flt.get("stab") in ("exclude_bad", "only_good") else ""
    # V2 历史幅度一致性筛选：only_good_floor = 一致且最小方向幅度 >= 1%。
    cons = flt.get("cons") if flt.get("cons") in (
        "exclude_bad", "only_good", "only_good_floor") else ""
    # 下月占比可单独设：inherit(默认，沿用本月) / none(不限) / 50..90 / eq100
    nr_raw = str(flt.get("next_rate") or "").strip()
    if nr_raw in ("", "inherit"):
        next_rate_min, next_rate_eq = rate_min, rate_eq
    elif nr_raw == "none":
        next_rate_min, next_rate_eq = None, False
    elif nr_raw == "eq100":
        next_rate_min, next_rate_eq = 100, True
    else:
        next_rate_min = _int(nr_raw, 0, 100, -1)
        next_rate_min = next_rate_min if next_rate_min >= 0 else None
        next_rate_eq = False
    both = flt.get("both_months") == "1"
    if both:                      # 「连续两月」只对单月视图有意义：下月 = 所选月的下一根柱
        months_n = 1

    cal = calendar_context(con)
    offset = _int(flt.get("offset"), -OPP_PAST_MAX, len(cal["future"]), 0)
    timeline = cal["past"] + ([cal["current"]] if cal["current"] else []) + cal["future"]
    cur_idx = len(cal["past"])
    seq = timeline[cur_idx + offset:cur_idx + offset + months_n]
    if not seq:
        return {"banner": BANNER, "dataset": dataset, "market_label": market_label,
                "months": [], "rows": [], "caveats": [],
                "asof": cal["asof"], "params": {}}
    cap_mid = None
    use_cap = dataset == "ashare" and _cap_ready(con)
    if use_cap:
        row = con.execute("SELECT MAX(month_id) FROM cap.cap_tier_monthly"
                          " WHERE month_id <= ?", (seq[0]["month_id"],)).fetchone()
        cap_mid = row[0] if row else None
        use_cap = cap_mid is not None
    pillars = [m["month_pillar"] for m in seq]
    next_m = None
    if both:
        nxt = _rows(con, "SELECT month_id, month_pillar, start_term, start_at, end_at"
                         " FROM calendar_months WHERE month_id > ? ORDER BY month_id LIMIT 1",
                    (seq[0]["month_id"],))
        next_m = nxt[0] if nxt else None
        both = next_m is not None

    np: dict = {"policy": policy, "min_n": min_n, "dir": direction,
                "delisted": "1" if flt.get("include_delisted") == "1" else "",
                "idx": "1" if flt.get("include_index") == "1" else "",
                "q": (flt.get("q") or "").strip(),
                "ql": f"%{(flt.get('q') or '').strip()}%", "cap": cap,
                "row_off": row_off, "row_end": row_off + cap}

    def placeholders(prefix, values):
        keys = []
        for i, v in enumerate(values):
            k = f"{prefix}{i}"
            np[k] = v
            keys.append(":" + k)
        return ",".join(keys)

    ph = placeholders("pil", pillars)
    mid_ph = placeholders("mid", [m["month_id"] for m in seq])
    # 出现次数口径（与「出现次数」列、门槛、样本等级筛选共用同一个 n）
    n_expr = "CASE WHEN :policy='ALL' THEN p.all_sample_n ELSE p.exclude_single_n END"
    hit_all = "down_n_ALL" if direction == "down" else "up_n_ALL"
    hit_es = "down_n_EXCLUDE_SINGLE" if direction == "down" else "up_n_EXCLUDE_SINGLE"
    rate_expr = (f"(p.{hit_all}*1.0/NULLIF(p.all_sample_n,0))" if policy == "ALL"
                 else f"(p.{hit_es}*1.0/NULLIF(p.exclude_single_n,0))")
    # 每个 OR 组必须整体加括号：AND 优先级高于 OR，裸写会让整条筛选被逃逸掉
    where = [f"p.flow_pillar IN ({ph})",
             f"({n_expr} >= :min_n)",
             "(:delisted='1' OR s.delisted_date IS NULL)",
             "(:idx='1' OR s.security_type <> 'INDEX')",
             "(:q='' OR s.name LIKE :ql OR s.security_code LIKE :ql OR p.identity_code = :q)"]
    # 100%段模式：先由段库选出本页股票，再把这些代码交给机会表同一套行查询
    code_list = [x.strip().upper() for x in str(flt.get("codes") or "").split(",") if x.strip()][:500]
    if code_list:
        code_keys = []
        for i, c in enumerate(code_list):
            k = f"code{i}"
            np[k] = c
            code_keys.append(":" + k)
        where.append(f"p.identity_code IN ({','.join(code_keys)})")
    # 市值档：按所选流月（含本月/未来月时回落到最近一个已完结流月）的自由流通市值快照。
    cap_join = cap_cols = ""
    if use_cap:
        np["cap_mid"] = cap_mid
        cap_join = (" LEFT JOIN cap.cap_tier_monthly ct"
                    " ON ct.identity_code=p.identity_code AND ct.month_id=:cap_mid")
        cap_cols = (f", {CAP_BAND_EXPR} AS cap_tier, ct.free_mv_yi AS cap_free_mv_yi,"
                    " ct.avg_amt_20d_yi AS cap_avg_amt_20d_yi")
        if cap_tier:
            np["cap_tier"] = cap_tier
            where.append(f"({CAP_BAND_EXPR} = :cap_tier)")
    else:
        cap_tier = ""
    if grade == "0":
        where.append(f"({n_expr} = 0)")
    elif grade == "1-2":
        where.append(f"({n_expr} BETWEEN 1 AND 2)")
    elif grade == "3-5":
        where.append(f"({n_expr} BETWEEN 3 AND 5)")
    elif grade == "6+":
        where.append(f"({n_expr} >= 6)")
    if rate_min is not None:
        if rate_eq:
            where.append(f"({rate_expr} = 1.0)")
        else:
            np["rate_min"] = rate_min / 100.0
            where.append(f"({rate_expr} >= :rate_min)")
    if stab:
        # 阈值与 Python stability_level() 共用同一组常量；这里只引用 agg 的原始量，
        # 不引用 SELECT 别名（SQLite 不允许在同一层 WHERE 里引用别名）。
        if direction == "up":
            bad = (f"(a.min_ret / MAX(a.max_ret - a.min_ret, {STAB_RANGE_FLOOR!r})"
                   f" < {STAB_UP_WEAK_MRATIO!r}"
                   f" OR a.max_ret/NULLIF(a.sum_pos,0) >= {STAB_UP_WEAK_SHARE!r})")
            good = (f"(a.min_ret / MAX(a.max_ret - a.min_ret, {STAB_RANGE_FLOOR!r})"
                    f" >= {STAB_UP_GOOD_MRATIO!r}"
                    f" AND a.max_ret/NULLIF(a.sum_pos,0) <= {STAB_UP_GOOD_SHARE!r})")
        else:
            bad = (f"((a.max_ret - a.min_ret) >= {STAB_DOWN_HIGH_RANGE!r}"
                   f" OR ABS(a.min_ret)/NULLIF(ABS(a.sum_neg),0) >= {STAB_DOWN_HIGH_SHARE!r})")
            good = (f"((a.max_ret - a.min_ret) < {STAB_DOWN_LOW_RANGE!r}"
                    f" AND ABS(a.min_ret)/NULLIF(ABS(a.sum_neg),0) < {STAB_DOWN_LOW_SHARE!r})")
        where.append(f"NOT {bad}" if stab == "exclude_bad" else good)
    # 「连续两月」：同一套出现次数/样本等级/上涨占比条件，再加到下一根柱上
    next_join = next_cols = ""
    if both:
        np["next_pil"] = next_m["month_pillar"]
        n1 = "CASE WHEN :policy='ALL' THEN p1.all_sample_n ELSE p1.exclude_single_n END"
        r1 = (f"(p1.{hit_all}*1.0/NULLIF(p1.all_sample_n,0))" if policy == "ALL"
              else f"(p1.{hit_es}*1.0/NULLIF(p1.exclude_single_n,0))")
        cond = ["p1.identity_code = p.identity_code", "p1.flow_pillar = :next_pil",
                f"({n1} >= :min_n)"]
        if grade == "0":
            cond.append(f"({n1} = 0)")
        elif grade == "1-2":
            cond.append(f"({n1} BETWEEN 1 AND 2)")
        elif grade == "3-5":
            cond.append(f"({n1} BETWEEN 3 AND 5)")
        elif grade == "6+":
            cond.append(f"({n1} >= 6)")
        if next_rate_min is not None:
            if next_rate_eq:
                cond.append(f"({r1} = 1.0)")
            elif next_rate_min == rate_min:
                cond.append(f"({r1} >= :rate_min)")
            else:
                np["next_rate_min"] = next_rate_min / 100.0
                cond.append(f"({r1} >= :next_rate_min)")
        next_join = " JOIN stats_pillar60 p1 ON " + " AND ".join(cond)
        next_cols = """
      , p1.flow_pillar AS next_pillar, p1.all_sample_n AS next_n
      , CASE WHEN :policy='ALL' THEN p1.up_n_ALL ELSE p1.up_n_EXCLUDE_SINGLE END AS next_up
      , CASE WHEN :policy='ALL' THEN p1.down_n_ALL ELSE p1.down_n_EXCLUDE_SINGLE END AS next_down
      , CASE WHEN :policy='ALL' THEN p1.all_sample_n ELSE p1.exclude_single_n END AS next_den
      , CASE WHEN :policy='ALL'
             THEN ROUND((CASE WHEN :dir='down' THEN p1.down_n_ALL ELSE p1.up_n_ALL END)
                        *1.0/NULLIF(p1.all_sample_n,0),6)
             ELSE ROUND((CASE WHEN :dir='down' THEN p1.down_n_EXCLUDE_SINGLE
                              ELSE p1.up_n_EXCLUDE_SINGLE END)
                        *1.0/NULLIF(p1.exclude_single_n,0),6)
        END AS next_rate
      , CASE WHEN :policy='ALL' THEN p1.mean_body_return_ALL
             ELSE p1.mean_body_return_EXCLUDE_SINGLE END AS next_mean
      , p1.excess_mean AS next_excess"""
    for key, prefix, names in (("clash", "cl", CLASH_TYPES), ("harmony", "ha", HARMONY_TYPES),
                               ("punish", "pu", PUNISH_TYPES), ("harm", "hm", HARM_TYPES),
                               ("broken", "br", BREAK_TYPES)):
        if flt.get(key) == "1":
            where.append(f"""EXISTS(SELECT 1 FROM stats_relation_pillar x
                  WHERE x.identity_code=p.identity_code AND x.record_kind='OFFICIAL'
                    AND x.flow_pillar=p.flow_pillar AND x.relation_type IN (
                      {placeholders(prefix, names)}))""")
    if flt.get("both_clash_harmony") == "1":
        where.append(f"""EXISTS(SELECT 1 FROM stats_relation_pillar x
                  WHERE x.identity_code=p.identity_code AND x.record_kind='OFFICIAL'
                    AND x.flow_pillar=p.flow_pillar AND x.relation_type IN (
                      {placeholders("bc", CLASH_TYPES)}))
              AND EXISTS(SELECT 1 FROM stats_relation_pillar y
                  WHERE y.identity_code=p.identity_code AND y.record_kind='OFFICIAL'
                    AND y.flow_pillar=p.flow_pillar AND y.relation_type IN (
                      {placeholders("bh", HARMONY_TYPES)}))""")
    rel = [x.strip() for x in (flt.get("rel") or "").split(",") if x.strip()]
    if rel:
        found = [r[0] for r in con.execute(
            "SELECT DISTINCT relation_type FROM relation_catalog WHERE kind='OFFICIAL'"
            f" AND relation_type IN ({','.join('?' * len(rel))})", rel)]
        if found:
            where.append(f"""EXISTS(SELECT 1 FROM stats_relation_pillar z
                  WHERE z.identity_code=p.identity_code AND z.record_kind='OFFICIAL'
                    AND z.flow_pillar=p.flow_pillar AND z.relation_type IN (
                      {placeholders("rl", found)}))""")
    scope = " AND ".join(where)
    cons_filter = {
        "exclude_bad": "cons_n >= 2 AND cons_level <> '差异大'",
        "only_good": "cons_n >= 2 AND cons_level = '一致'",
        "only_good_floor": "cons_n >= 2 AND cons_level = '一致' AND cons_floor = 1",
    }.get(cons, "1=1")

    if msort:
        order_expr = _msort_order_expr(msort, direction, policy)
    elif sort == "consistency":
        order_expr = "cons_cv ASC NULLS LAST, cons_n DESC, identity_code"
    elif sort in OPP_GROUP_ORDER:
        order_expr = OPP_GROUP_ORDER[sort]
    else:
        metrics = opp_metric(direction)
        if sort not in metrics:
            sort = "mean"
        # 下跌方向把均值/超额翻成升序，否则「跌得最多」仍会被排到表尾、截断后看不见。
        main_desc = "ASC" if direction == "down" and sort in ("mean", "excess") else "DESC"
        keys = [f"{metrics[sort][policy]} {main_desc} NULLS LAST"]
        # 占比并列时按平均涨幅再排。但「剔除所选月」是回测口径，不能用含该月的均值做次键
        # （那会把"该月涨得最多"的票挑到前面，等于把答案漏进排序），只能用样本量与代码。
        if sort not in ("mean", "up_rate_excl", "stability"):
            tie_desc = "ASC" if direction == "down" else "DESC"
            keys.append(f"{OPP_TIEBREAK[policy]} {tie_desc} NULLS LAST")
        if sort != "n":
            keys.append("occurrence_count DESC")
        keys.append("identity_code")   # 最后一键保证截断结果可复现
        order_expr = ", ".join(keys)

    # 同柱逐月收益聚合：只按所选柱算，一次查询；policy=EXCLUDE_SINGLE 时同步剔除单交易日月。
    agg_cte = f"""
        SELECT f.identity_code, f.flow_pillar,
               COUNT(*) AS n, MIN(f.body_return) AS min_ret, MAX(f.body_return) AS max_ret,
               SUM(f.body_return) AS sum_ret,
               SUM(CASE WHEN f.body_return > 0 THEN f.body_return ELSE 0 END) AS sum_pos,
               SUM(CASE WHEN f.body_return < 0 THEN f.body_return ELSE 0 END) AS sum_neg,
               SUM(f.body_return*f.body_return) AS sum_sq,
               SUM(CASE WHEN f.body_return > 0 THEN 1 ELSE 0 END) AS up_cnt,
               SUM(CASE WHEN f.body_return > 0 THEN f.body_return*f.body_return ELSE 0 END) AS up_sq,
               MIN(CASE WHEN f.body_return > 0 THEN f.body_return END) AS up_min,
               MAX(CASE WHEN f.body_return > 0 THEN f.body_return END) AS up_max,
               SUM(CASE WHEN f.body_return < 0 THEN 1 ELSE 0 END) AS dn_cnt,
               SUM(CASE WHEN f.body_return < 0 THEN f.body_return*f.body_return ELSE 0 END) AS dn_sq,
               MIN(CASE WHEN f.body_return < 0 THEN -f.body_return END) AS dn_min,
               MAX(CASE WHEN f.body_return < 0 THEN -f.body_return END) AS dn_max
        FROM monthly_facts f
        WHERE f.flow_pillar IN ({ph})
          AND f.eligible=1 AND f.fact_status='FINAL' AND f.body_return IS NOT NULL
          AND (:policy='ALL' OR f.single_day=0)
        GROUP BY f.identity_code, f.flow_pillar"""
    rows = _rows(con, f"""
      WITH agg AS ({agg_cte}),
      base AS (SELECT {OPP_COLUMNS}{next_cols}{cap_cols}
        FROM stats_pillar60 p
        JOIN securities s ON s.identity_code=p.identity_code{next_join}{cap_join}
        LEFT JOIN agg a ON a.identity_code=p.identity_code AND a.flow_pillar=p.flow_pillar
        LEFT JOIN natal_profiles n ON n.identity_code=p.identity_code
        LEFT JOIN pillar_order po ON po.flow_pillar=p.flow_pillar
        LEFT JOIN branch_order bo ON bo.branch=p.branch
        LEFT JOIN monthly_facts f ON f.identity_code=p.identity_code
          AND f.month_id IN ({mid_ph}) AND f.flow_pillar=p.flow_pillar
        WHERE {scope}),
      scored AS (SELECT *,
          CASE WHEN stab_n > 0 THEN
            CASE WHEN :dir='down' THEN -stab_max_ret ELSE stab_min_ret END
          END AS stab_margin,
          CASE WHEN stab_n > 0 THEN
            CASE WHEN :dir='down' THEN ABS(stab_min_ret)/NULLIF(ABS(stab_sum_neg),0)
                 ELSE stab_max_ret/NULLIF(stab_sum_pos,0) END
          END AS stab_extreme_share,
          CASE WHEN stab_n > 1 THEN stab_max_ret - stab_min_ret END AS stab_range,
          CASE WHEN stab_n > 1 THEN
            CASE WHEN :dir='down' THEN (stab_sum_ret - stab_min_ret)/(stab_n - 1)
                 ELSE (stab_sum_ret - stab_max_ret)/(stab_n - 1) END
          END AS stab_mean_ex,
          CASE WHEN stab_n > 1 THEN
            SQRT(MAX(stab_sum_sq/stab_n - (stab_sum_ret/stab_n)*(stab_sum_ret/stab_n), 0))
          END AS stab_std,
          CASE WHEN stab_n > 0 THEN
            (CASE WHEN :dir='down' THEN -stab_max_ret ELSE stab_min_ret END)
            / MAX(stab_max_ret - stab_min_ret, 0.02)
          END AS stab_margin_ratio,
          CASE WHEN score_n > 0 THEN score_hit*1.0/score_n END AS stab_rate,
          CASE WHEN score_n > 0 THEN
            100.0 * (
              0.40 * (
                ( (score_hit*1.0/score_n) + 3.8416/(2.0*score_n)
                  - 1.959963984540054 * SQRT(MAX(
                      ((score_hit*1.0/score_n) * (1.0 - score_hit*1.0/score_n)) / score_n
                      + 3.8416/(4.0*score_n*score_n), 0)) )
                / (1.0 + 3.8416/score_n)
              )
              + 0.35 * MIN(MAX(
                  (CASE WHEN :dir='down' THEN -stab_max_ret ELSE stab_min_ret END)
                  / MAX(stab_max_ret - stab_min_ret, 0.02), 0) / 0.50, 1)
              + 0.25 * (1.0 - MIN(MAX(
                  CASE WHEN :dir='down' THEN ABS(stab_min_ret)/NULLIF(ABS(stab_sum_neg),0)
                       ELSE stab_max_ret/NULLIF(stab_sum_pos,0) END, 0), 1.0))
            )
          END AS stability_score,
          CASE WHEN :dir='down' THEN cons_dn_n ELSE cons_up_n END AS cons_n,
          CASE WHEN :dir='down' THEN -stab_sum_neg ELSE stab_sum_pos END AS cons_sum,
          CASE WHEN :dir='down' THEN cons_dn_sq ELSE cons_up_sq END AS cons_sq,
          CASE WHEN :dir='down' THEN cons_dn_min ELSE cons_up_min END AS cons_min,
          CASE WHEN :dir='down' THEN cons_dn_max ELSE cons_up_max END AS cons_max,
          CASE WHEN (:dir='down' AND cons_dn_n > 0) OR (:dir<>'down' AND cons_up_n > 0)
               THEN (CASE WHEN :dir='down' THEN -stab_sum_neg ELSE stab_sum_pos END)
                    / NULLIF(CASE WHEN :dir='down' THEN cons_dn_n ELSE cons_up_n END, 0)
          END AS cons_mean
        FROM base),
      scored2 AS (SELECT *,
          CASE WHEN cons_n > 1 AND cons_mean > 0 THEN
            SQRT(MAX((cons_sq - cons_n*cons_mean*cons_mean)/(cons_n - 1), 0))
          END AS cons_std,
          CASE WHEN cons_n > 1 AND cons_mean > 0 THEN
            SQRT(MAX((cons_sq - cons_n*cons_mean*cons_mean)/(cons_n - 1), 0))
            / cons_mean
          END AS cons_cv,
          CASE WHEN cons_n > 1 AND cons_mean > 0 THEN
            (cons_mean - (cons_sum - cons_max)/(cons_n - 1)) / cons_mean
          END AS cons_drop,
          CASE WHEN cons_n > 0 THEN (cons_min >= 0.01) END AS cons_floor,
          CASE WHEN stab_n IS NULL OR stab_n = 0 OR stab_margin IS NULL OR stab_extreme_share IS NULL THEN 3
               WHEN :dir='down' THEN
                 CASE WHEN stab_range IS NOT NULL
                            AND (stab_range >= 0.40 OR stab_extreme_share >= 0.70) THEN 2
                      WHEN stab_range IS NOT NULL
                            AND (stab_range < 0.20 AND stab_extreme_share < 0.50) THEN 0
                      ELSE 1 END
               ELSE
                 CASE WHEN stab_margin / MAX(stab_range, 0.02) < 0.15
                            OR stab_extreme_share >= 0.70 THEN 2
                      WHEN stab_margin / MAX(stab_range, 0.02) >= 0.30
                            AND stab_extreme_share <= 0.50 THEN 0
                      ELSE 1 END
          END AS stab_level_rank
        FROM scored),
      scored3 AS (SELECT *,
          CASE WHEN cons_n < 2 OR cons_cv IS NULL THEN '—'
               WHEN cons_cv <= 0.50 THEN '一致'
               WHEN cons_cv > 0.80 THEN '差异大'
               ELSE '一般' END AS cons_level,
          CASE WHEN cons_n < 2 OR cons_cv IS NULL THEN 3
               WHEN cons_cv <= 0.50 THEN 0
               WHEN cons_cv > 0.80 THEN 2
               ELSE 1 END AS cons_level_rank
        FROM scored2),
      filtered AS (SELECT * FROM scored3 WHERE {cons_filter}),
      scoped AS (SELECT *,
          COUNT(*) OVER (PARTITION BY flow_pillar)                            AS scope_in_scope,
          SUM(boundary_disputed_months > 0) OVER (PARTITION BY flow_pillar)   AS scope_disputed,
          SUM(has_natal_profile = 0) OVER (PARTITION BY flow_pillar)          AS scope_no_profile,
          SUM(delisted_date IS NOT NULL) OVER (PARTITION BY flow_pillar)      AS scope_delisted
        FROM filtered),
      ranked AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY flow_pillar ORDER BY {order_expr})
                   AS rn FROM scoped)
      SELECT * FROM ranked WHERE rn > :row_off AND rn <= :row_end
      ORDER BY pillar_ord, rn""", np)
    for r in rows:
        level, level_cn, reason = stability_level(
            direction, r.get("stab_n"), r.get("stab_margin"),
            r.get("stab_extreme_share"), r.get("stab_range"))
        r["stability_level"] = level
        r["stability_level_cn"] = level_cn
        r["stability_reason"] = reason
        total = r.get("stab_n") or 0
        k = r.get("cons_n") or 0
        reverse_n = max(total - k, 0)
        r["cons_total"] = total
        r["cons_reverse_n"] = reverse_n
        r["cons_worst_all"] = r.get("stab_min_ret")
        r["consistency_reason"] = consistency_reason(
            direction, k, total, r.get("cons_cv"), r.get("cons_drop"),
            r.get("cons_min"), r.get("cons_floor"), r.get("stab_min_ret"), reverse_n)

    dist = _rows(con, f"""
      SELECT p.flow_pillar, COUNT(*) AS all_identities,
             SUM(p.occurrence_count=0) AS n0,
             SUM(p.occurrence_count BETWEEN 1 AND 2) AS n12,
             SUM(p.occurrence_count BETWEEN 3 AND 5) AS n35,
             SUM(p.occurrence_count>=6) AS n6,
             SUM(s.delisted_date IS NOT NULL) AS delisted_all,
             SUM(s.delisted_date IS NOT NULL AND (CASE WHEN :policy='ALL' THEN p.all_sample_n
                   ELSE p.exclude_single_n END) >= :min_n) AS delisted_enough,
             SUM(s.has_natal_profile=0 AND (CASE WHEN :policy='ALL' THEN p.all_sample_n
                   ELSE p.exclude_single_n END) >= :min_n) AS noprofile_enough,
             SUM(p.boundary_disputed_months>0) AS disputed_all
      FROM stats_pillar60 p JOIN securities s ON s.identity_code=p.identity_code
      WHERE p.flow_pillar IN ({ph}) GROUP BY p.flow_pillar""", np)
    latest = con.execute("SELECT MAX(last_month_id) FROM securities").fetchone()[0]
    facts_total = con.execute("SELECT COUNT(*) FROM monthly_facts").fetchone()[0]
    # 空结果时 rows[0] 不存在；数据截止日仍要从冻结统计读，否则空状态会显示“行情数据截至 —”。
    asof_data = (rows[0]["input_data_as_of"] if rows else con.execute(
        "SELECT MAX(input_data_as_of) FROM stats_pillar60").fetchone()[0])

    # 「门槛内」必须和表体用同一套过滤。一致性条件在 scored3 之后才生效，而旧的那条独立计数
    # 只带 WHERE（稳健度在里面、一致性不在里面），两个同类筛选因此口径分叉：一致性筛选时
    # 表已全列，说明仍写「其余 N 只未显示」。计数改由主查询的窗口列带出来，顺带省掉一次扫描。
    tmap = {}
    for r in rows:
        pk = r["flow_pillar"]
        if pk not in tmap:
            tmap[pk] = {"in_scope": r["scope_in_scope"], "disputed": r["scope_disputed"],
                        "no_profile": r["scope_no_profile"], "delisted": r["scope_delisted"]}
        for k in ("scope_in_scope", "scope_disputed", "scope_no_profile", "scope_delisted"):
            r.pop(k, None)
    dmap = {d["flow_pillar"]: d for d in dist}
    out_months = []
    today = datetime.now(TZ).date()
    for i, m in enumerate(seq):
        p = m["month_pillar"]
        ordinal = offset + i
        fact_rows = con.execute("SELECT COUNT(*) FROM monthly_facts WHERE month_id=?",
                                (m["month_id"],)).fetchone()[0]
        edge = (datetime.fromisoformat(m["end_at"] if ordinal == 0 and cal["current"]
                                       else m["start_at"]).date() - today).days
        in_scope = (tmap.get(p) or {}).get("in_scope") or 0
        page_rows = sum(1 for r in rows if r["flow_pillar"] == p)
        summ = None
        if fact_rows:
            mstat = _rows(con, "SELECT SUM(direction='UP')*1.0/COUNT(*) AS up_rate,"
                               " AVG(body_return) AS mean_return FROM monthly_facts"
                               " WHERE month_id=?", (m["month_id"],))[0]
            listed = [r for r in rows if r["flow_pillar"] == p]
            summ = {"listed": len(listed),
                    "with_data": sum(1 for r in listed if r["in_sample"]),
                    "up": sum(1 for r in listed if r["actual_direction"] == "UP"),
                    "down": sum(1 for r in listed if r["actual_direction"] == "DOWN"),
                    "flat": sum(1 for r in listed if r["actual_direction"] == "FLAT"),
                    "mean_return": (sum(r["actual_return"] for r in listed
                                        if r["actual_return"] is not None)
                                    / max(sum(1 for r in listed if r["actual_return"] is not None),
                                          1)) if listed else None,
                    "market_up_rate": mstat["up_rate"],
                    "market_mean_return": mstat["mean_return"]}
        out_months.append({
            "ordinal": ordinal, "month_id": m["month_id"], "pillar": p,
            "label": month_label(ordinal),
            "start_term": m["start_term"], "start_at": m["start_at"], "end_at": m["end_at"],
            "is_current": ordinal == 0 and cal["current"] is not None,
            "is_past": ordinal < 0 or (ordinal == 0 and not cal["current"]),
            "days_to_flip": edge, "days_from_today": edge,
            "market_rows_in_month": fact_rows,
            "actual_summary": summ,
            "totals": {"identities": (dmap.get(p) or {}).get("all_identities"),
                       "zero_sample": (dmap.get(p) or {}).get("n0"),
                       "sample_1_2": (dmap.get(p) or {}).get("n12"),
                       "sample_3_5": (dmap.get(p) or {}).get("n35"),
                       "sample_ge6": (dmap.get(p) or {}).get("n6"),
                       "in_scope": in_scope,
                       "disputed": (tmap.get(p) or {}).get("disputed") or 0,
                       "disputed_all": (dmap.get(p) or {}).get("disputed_all") or 0,
                       "no_profile": (tmap.get(p) or {}).get("no_profile") or 0,
                       "delisted": (tmap.get(p) or {}).get("delisted") or 0,
                       "delisted_all": (dmap.get(p) or {}).get("delisted_all") or 0,
                       "delisted_enough": (dmap.get(p) or {}).get("delisted_enough") or 0,
                       "noprofile_enough": (dmap.get(p) or {}).get("noprofile_enough") or 0,
                       "returned": page_rows,
                       "page": page, "page_size": cap,
                       "pages": ((in_scope + cap - 1) // cap) if in_scope else 0,
                       "capped": bool(in_scope and page_rows < in_scope)},
        })

    first = out_months[0]
    live_info = None
    if dataset == "ashare" and first["is_current"] and rows:
        live_info = _attach_live_current(
            rows, "month", first["month_id"], first["pillar"],
            seq[0]["start_at"], seq[0]["end_at"])
    thr_txt = f"≥{min_n} 次" if min_n else "不限（含 0 次出现）"
    extra = []
    if grade:
        extra.append(f"样本等级 {OPP_GRADE_TXT[grade]}")
    if rate_min is not None:
        extra.append(f"历史{dir_txt}占比 =100%" if rate_eq
                     else f"历史{dir_txt}占比 ≥{rate_min}%")
    if cons:
        extra.append({"exclude_bad": "一致性 排除差异大",
                      "only_good": "一致性 只看一致",
                      "only_good_floor": "一致性 只看一致且最小幅度≥1%"}[cons])
    extra_txt = ("另加筛选：" + "、".join(extra) + "。") if extra else ""
    d0 = first["totals"]
    share_label = f"{d0['identities']:,} 只 ETF" if dataset == "etf" else "数千只票"
    past_month = first["ordinal"] < 0
    mlabel = month_label(first["ordinal"])
    rel_head = "该柱历史触发关系（该月已发生）" if past_month else "该月将触发关系（取自历史同柱）"
    future_last = cal["future"][-1] if cal["future"] else None
    future_txt = (f"{future_last['start_at'][:4]} 年 {int(future_last['start_at'][5:7])} 月"
                  if future_last else "本月")
    notes = [
        f"本页只做一件事：把{market_label} {d0['identities']:,} 个证券身份，按当前选中的节气月"
        f"（{mlabel} {first['pillar']}，{first['start_term']} {first['start_at'][:10]} → "
        f"{first['end_at'][:10]}）横向列出该柱的历史统计。历法日期一律取冻结 "
        f"calendar_months（{cal['calendar']['calendar_version']}），页面不自推算历；"
        f"一次只显示一个流月，往前 {OPP_PAST_MAX} 个月正好是一个完整的 60 柱周期"
        f"（每根柱各出现一次），往后可以选到 {future_txt}（同一根柱每 60 个月重复一次），"
        "「未来」不含本月。",
    ]
    if past_month:
        notes.append(
            f"该月（{first['month_id']}）的行情已经在库里：{first['market_rows_in_month']:,} 行事实数据，"
            f"库内行情截至 {asof_data or '—'}，最后一个有数据的流月 {latest}。表中新增加「该月实际」"
            f"（该月的事实涨跌与相对基准超额）和「不含该月占比」两列，后者是把该月从同柱样本里"
            f"摘出去之后的频率，取数全部来自冻结 monthly_facts / stats_pillar60，页面不重算统计。")
        notes.append(
            f"同温样本必须点破：「历史{dir_txt}占比」的分母含所选月本身，所以按它排在前面的标的，"
            f"该月{dir_txt}的机会本来就更高——这是自证，不是验证。要回测就把排序换成"
            f"「历史{dir_txt}占比（不含该月）」，再对照「该月实际」列看。")
    else:
        if live_info and live_info.get("status") == "ok":
            live_txt = (f"「实际走势」列另读日更切片：显示截至 {live_info['latest_trade_date']} 的"
                        "临时值（流月首日开盘→最新收盘），该月未走完，不是历史统计。")
        elif live_info:
            live_txt = (f"本月实际走势的日更切片未就绪：{live_info.get('reason') or '—'}。")
        else:
            live_txt = ""
        notes.append(
            f"{mlabel}（{first['month_id']}）尚未计入历史库：库内历史行情截至 {asof_data or '—'}，"
            f"最后一个完整流月是 {latest}，该月历史事实行数 {first['market_rows_in_month']}。"
            f"下表历史统计只收完整流月，不含本月。{live_txt}")
    if both:
        next_rate_txt = ("不限" if next_rate_min is None else
                         "=100%" if next_rate_eq else f"≥{next_rate_min}%")
        cur_rate_txt = ("不限" if rate_min is None else
                        "=100%" if rate_eq else f"≥{rate_min}%")
        notes.append(
            f"叠加下月：本次结果同时要求该票在下一根柱 {next_m['month_pillar']}"
            f"（{next_m['start_term']} {next_m['start_at'][:10]} → {next_m['end_at'][:10]}）上"
            f"也满足出现次数 {thr_txt}"
            + (f"、样本等级 {OPP_GRADE_TXT[grade]}" if grade else "")
            + f"、{dir_txt}占比 {next_rate_txt}；本月柱的{dir_txt}占比条件是 {cur_rate_txt}。"
            "要「两个月都 100%」，就把本月设成 =100%、下月也设成 =100%。"
            "两根柱的样本都来自同一只票的同一段历史，不是两次独立试验；"
            "这只是把两个历史频率叠在一起看，不构成「会连续两个月" + dir_txt + "」的概率，"
            "也不代表两根柱之间有因果关系。")
    if dataset == "etf":
        rel_note = (
            f"「{rel_head}」是从同一根流月柱的历史出现推得的：本库 {d0['identities']:,} 个身份"
            f"共 {facts_total:,} 行 monthly_facts，同柱关系集合直接取自本库 "
            f"month_relation_types。该票该柱历史 0 次出现时只显示「历史无同柱，不推断」，"
            f"不做推断，也不借用他票命盘。")
        scope_note = (
            f"ETF 源只含存续基金，无退市身份；全部 {d0['identities']:,} 只均有命盘、无交节争议。"
            f"「包含已退市」「包含指数」两项在 ETF 数据集为空转，页面已隐藏。"
            f"当前口径内无命盘身份 0 只。")
    else:
        rel_note = (
            f"「{rel_head}」是从同一根流月柱的历史出现推得的：已全库逐行核对 "
            f"{d0['identities']:,} 个身份共 {facts_total:,} 行 monthly_facts，"
            f"同一身份同柱各次出现的 (record_kind, relation_type) 集合与该柱聚合集合完全一致，"
            f"0 例不一致。该票该柱历史 0 次出现时只显示「历史无同柱，不推断」，"
            f"不做推断，也不借用他票命盘；无命盘身份显示「无命盘 · 不计算关系」。")
        scope_note = (
            f"已退市身份默认排除（选中柱 {first['pillar']} 上历史 {thr_txt} 的退市票有 "
            f"{d0['delisted_enough']:,} 只、"
            f"该柱全市场退市票 {d0['delisted_all']:,} 只，可勾选「包含已退市」回看历史统计"
            + ("，勾选后该月若它们已退市则「该月实际」显示为无行情）" if past_month else
               "，但它们该月届时无行情") + f"；"
            f"该柱上存在交节争议月的身份全市场有 {d0['disputed_all']:,} 只、"
            f"当前口径内 {d0['disputed']:,} 只，"
            f"它们「该月属于哪根柱」本身在冻结历法下有争议，表中逐行标注。"
            f"柱统计的分母含交节争议月，而关系层不含，因此本页占比与页面四可以合法不同。"
            f"当前口径内无命盘身份 {d0['no_profile']:,} 只（该柱历史 {thr_txt} 的无命盘身份全市场有 "
            f"{d0['noprofile_enough']:,} 只，它们多为已退市或近端新股，默认口径下已被排除；"
            f"无命盘票只看行情统计、不算冲合关系）。")
    notes += [
        rel_note,
        f"「历史{dir_txt}占比」是事后频率，不是概率：等于该票该柱历史上{dir_txt}的月份数"
        " ÷ 该柱历史出现月份数，"
        "平盘计入分母；涨跌幅是流月首个交易日开盘→最后交易日收盘，不是行情软件的月涨幅。"
        "同一根柱每 60 个月（约 5 年）重现一次，这是历法结构（60柱÷12月），不是预测能力。",
        f"小样本会严重误导：选中柱 {first['pillar']} 出现 0 次的有 {d0['zero_sample']:,} 只、"
        f"1–2 次 {d0['sample_1_2']:,} 只、3–5 次 {d0['sample_3_5']:,} 只、≥6 次仅 {d0['sample_ge6']:,} 只。"
        f"本页门槛为出现次数 {thr_txt}，门槛内共 {d0['in_scope']:,} 只；"
        f"表格每页 {cap} 名、共 {d0['pages'] or 1} 页，当前第 {page} 页，"
        f"翻页可看完全部符合条件的行（翻页不改筛选，换筛选/排序会回到第 1 页）。"
        + extra_txt
        + f"{dir_txt}占比 100% 且样本只有 3 个，绝不等于这只票该月会{dir_txt}。",
        ("「稳健度」只看同一根柱的历史证据：样本数、最差一次涨幅、最好一次占累计涨幅；"
         "安全边际按同柱振幅缩放（最差一次 ÷ max(同柱振幅,2%)）：稳＝边际/振幅≥0.30 且 "
         "最好一次≤50%，弱＝边际/振幅<0.15 或 最好一次≥70%，其余一般。"
         "下跌方向改用「历史跌幅差异」：同柱振幅≥40% 或 最惨一次占跌幅≥70% 标为高；"
         "这是历史形状描述，不是反弹概率；同月对照后差异不稳健，不能当作下一次上涨的依据。"
         "「稳健分」= 40% 样本置信 + 35% 相对安全边际（边际/振幅） + 25% 集中度，"
         "只作排序辅助，不是概率。个股整体波动只在点行抽屉里展示，不参与筛选。"),
        ("「历史幅度一致性」只看所选方向的月份：上涨取正收益，下跌取跌幅绝对值；"
         "CV = 样本标准差 ÷ 均值；一致 = CV≤0.50，差异大 = CV>0.80，一般 = 中间；"
         "最小方向幅度≥1% 是独立底线（达标/不足），drop 只展示、不参与分档；"
         "k<2 显示样本不足。该标签描述历史形状，不是下一次上涨概率，也不把反向月份算进 CV；"
         "反向次数与全月最差收益必须同屏看。"),
        scope_note,
        f"合规提示：全部数字为历史描述性统计，样本高度重叠（同一节气月被{share_label}共享），"
        "不构成概率、不构成吉凶评级、不构成买卖建议。",
    ]
    if use_cap:
        notes.insert(len(notes) - 1,
                     f"市值档口径：自由流通市值 = 不复权收盘 × 自由流通股本，取 {cap_mid} 月末快照"
                     "（所选月未完结时回落到最近一个已完结流月）；固定阈值 1000/500/200/50 亿，"
                     "不是分位数，也不是流通市值 circ_mv。")
    marks = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫"
    caveats = [f"{marks[i]} {t}" if i < len(notes) - 1 else t for i, t in enumerate(notes)]
    res = {"banner": BANNER, "dataset": dataset, "market_label": market_label,
            "asof": cal["asof"], "latest_month_with_data": latest,
            "input_data_as_of": asof_data, "rel_head": rel_head,
            "next_month": next_m,
            "live_month": live_info,
            "month_kind": "past" if past_month else "present_or_future",
            "calendar_version": cal["calendar"]["calendar_version"],
            "months": out_months, "rows": rows,
            "params": {"months": months_n, "offset": offset, "min_n": min_n,
                       "policy": policy, "sort": sort, "dir": direction,
                       "msort": [{"key": k, "dir": d} for k, d in msort],
                       "grade": grade, "rate_min": rate_min, "rate_eq": rate_eq,
                       "stab": stab,
                       "cons": cons,
                       "both_months": both, "next_rate": nr_raw or "inherit",
                       "cap": cap, "page": page, "q": np["q"],
                       "cap_tier": cap_tier,
                       "cap_asof_month": cap_mid if use_cap else None,
                       "include_delisted": np["delisted"] == "1",
                       "include_index": np["idx"] == "1",
                       "relation_filters": {k: flt.get(k) for k in
                                            ("clash", "harmony", "punish", "harm", "broken",
                                             "both_clash_harmony", "rel") if flt.get(k) == "1"
                                            or (k == "rel" and rel)},
                       "pillars": pillars},
            "caveats": caveats}
    if len(_OPP_CACHE) > 64:
        _OPP_CACHE.pop(next(iter(_OPP_CACHE)))
    _OPP_CACHE[cache_key] = res
    return res


def opportunities_window(con, flt: dict, layer: str, dataset: str = DEFAULT_DATASET) -> dict:
    """流月机会表：旬层（3旬）/ 周层（6周）机会排行榜。直接读取 xw.xun_rules / xw.week_rules。"""
    market_label = DATASETS.get(dataset, {}).get("market_label", "全市场")
    xw_class = class_of_dataset(dataset)
    if not _xw_ready(con):
        return {
            "banner": BANNER, "dataset": dataset, "market_label": market_label,
            "months": [], "rows": [],
            "caveats": ["月/旬/段规则表当前不可读（主库里的 xw_ 表与独立规则文件都没找到），"
                        "请切回月层或检查 BAZI_XUN_WEEK_DB 配置。"],
            "asof": "", "params": flt
        }

    cal = calendar_context(con)
    offset = _int(flt.get("offset"), -OPP_PAST_MAX, len(cal["future"]), 0)
    timeline = cal["past"] + ([cal["current"]] if cal["current"] else []) + cal["future"]
    cur_idx = len(cal["past"])
    seq = timeline[cur_idx + offset:cur_idx + offset + 1]
    if not seq:
        return {
            "banner": BANNER, "dataset": dataset, "market_label": market_label,
            "months": [], "rows": [], "caveats": [],
            "asof": cal["asof"], "params": {}
        }

    target_m = seq[0]
    pillar = target_m["month_pillar"]
    month_id = target_m["month_id"]
    is_current = (offset == 0 and cal["current"] is not None)
    is_past = (offset < 0) or (offset == 0 and not cal["current"])

    min_n = _int(flt.get("min_n"), 0, 60, 3)
    policy = flt.get("policy") if flt.get("policy") in ("ALL", "EXCLUDE_SINGLE") else "ALL"
    cap = _int(flt.get("cap"), 1, OPP_CAP_MAX, 100)
    page = _int(flt.get("page"), 1, 100000, 1)
    row_off = (page - 1) * cap
    cap_tier = flt.get("cap_tier") if flt.get("cap_tier") in CAP_BAND_LABELS else ""
    direction = "down" if flt.get("dir") == "down" else "up"
    grade = flt.get("grade") if flt.get("grade") in ("0", "1-2", "3-5", "6+") else ""
    rate_raw = str(flt.get("rate_min") or "").strip()
    rate_min = _int(rate_raw, 0, 100, -1) if rate_raw else None
    rate_eq = flt.get("rate_eq") == "1" and rate_min == 100
    q = (flt.get("q") or "").strip()
    window_no = str(flt.get("window_no") or "").strip()
    msort = _parse_msort(flt.get("msort"))
    if msort:
        sort = msort[0][0]
    else:
        sort = flt.get("sort") or "mean"

    cap_mid = None
    if _cap_ready(con):
        row = con.execute("SELECT MAX(month_id) FROM cap.cap_tier_monthly WHERE month_id <= ?", (month_id,)).fetchone()
        cap_mid = row[0] if row else None

    # 表名不带前缀：由 api._window_views() 在连接上建的 TEMP 视图决定实际读哪份
    #（主库 xw_* 优先，其次独立规则文件），这样 A股/ETF 两个数据集走的是同一条取数路径
    rules_tbl = _window_rules_source(con, layer)
    occ_tbl = "xun_occurrences" if layer == "xun" else "week_occurrences"
    occ_cond = "act.xun_no = r.xun_no" if layer == "xun" else "act.xun_no = r.xun_no AND act.week_no = r.week_no"
    win_col = "r.xun_no" if layer == "xun" else "r.week_no"

    if layer == "xun":
        win_tag_expr = "CASE r.xun_no WHEN 1 THEN '上旬 (0–10日)' WHEN 2 THEN '中旬 (10–20日)' ELSE '下旬 (20–下节)' END"
        week_col_select = "NULL as week_no, NULL as week_name,"
    else:
        win_tag_expr = "'第' || r.week_no || '段 (' || r.day_from || '–' || COALESCE(r.day_to, '下节') || '日)'"
        week_col_select = "r.week_no, r.week_name,"

    params: dict = {
        "pillar": pillar,
        "xw_class": xw_class,
        "min_n": min_n,
        "delisted": "1" if flt.get("include_delisted") == "1" else "",
        "idx": "1" if flt.get("include_index") == "1" else "",
        "q": q,
        "ql": f"%{q}%",
        "cap": cap,
        "fetch": row_off + cap,
        "cap_mid": cap_mid,
        "month_id": month_id
    }

    where = [
        "r.flow_pillar = :pillar",
        "r.asset_class = :xw_class",
        "(:delisted='1' OR s.delisted_date IS NULL)",
        "(:idx='1' OR s.security_type <> 'INDEX')",
        "(:q='' OR s.name LIKE :ql OR s.security_code LIKE :ql OR r.identity_code = :q)"
    ]

    only_win = None
    if window_no:
        try:
            wn = int(window_no)
            params["window_no"] = wn
            where.append(f"{win_col} = :window_no")
            only_win = wn
        except ValueError:
            pass
    else:
        only_win = None

    n_col = "r.ALL_valid_n" if policy == "ALL" else "r.EXCLUDE_SINGLE_valid_n"
    where.append(f"{n_col} >= :min_n")

    if grade == "0":
        where.append(f"{n_col} = 0")
    elif grade == "1-2":
        where.append(f"({n_col} BETWEEN 1 AND 2)")
    elif grade == "3-5":
        where.append(f"({n_col} BETWEEN 3 AND 5)")
    elif grade == "6+":
        where.append(f"{n_col} >= 6")

    rate_col = ("r.ALL_down_rate" if direction == "down" else "r.ALL_up_rate") if policy == "ALL" else \
               ("r.EXCLUDE_SINGLE_down_rate" if direction == "down" else "r.EXCLUDE_SINGLE_up_rate")
    mean_col = "r.ALL_mean_return_pct" if policy == "ALL" else "r.EXCLUDE_SINGLE_mean_return_pct"

    if rate_min is not None:
        if rate_eq:
            where.append(f"{rate_col} = 1.0")
        else:
            params["rate_min"] = rate_min / 100.0
            where.append(f"{rate_col} >= :rate_min")

    if cap_tier and cap_mid:
        params["cap_tier"] = cap_tier
        where.append(f"{CAP_BAND_EXPR} = :cap_tier")

    def placeholders(prefix, values):
        keys = []
        for i, v in enumerate(values):
            k = f"{prefix}_{i}"
            params[k] = v
            keys.append(":" + k)
        return ",".join(keys)

    for key, prefix, names in (("clash", "cl", CLASH_TYPES), ("harmony", "ha", HARMONY_TYPES),
                               ("punish", "pu", PUNISH_TYPES), ("harm", "hm", HARM_TYPES),
                               ("broken", "br", BREAK_TYPES)):
        if flt.get(key) == "1":
            where.append(f"""EXISTS(SELECT 1 FROM stats_relation_pillar x
                  WHERE x.identity_code=r.identity_code AND x.record_kind='OFFICIAL'
                    AND x.flow_pillar=r.flow_pillar AND x.relation_type IN ({placeholders(prefix, names)}))""")
    if flt.get("both_clash_harmony") == "1":
        where.append(f"""EXISTS(SELECT 1 FROM stats_relation_pillar x
                  WHERE x.identity_code=r.identity_code AND x.record_kind='OFFICIAL'
                    AND x.flow_pillar=r.flow_pillar AND x.relation_type IN ({placeholders("bc", CLASH_TYPES)}))
              AND EXISTS(SELECT 1 FROM stats_relation_pillar y
                  WHERE y.identity_code=r.identity_code AND y.record_kind='OFFICIAL'
                    AND y.flow_pillar=r.flow_pillar AND y.relation_type IN ({placeholders("bh", HARMONY_TYPES)}))""")
    rel = [x.strip() for x in (flt.get("rel") or "").split(",") if x.strip()]
    if rel:
        found = [row[0] for row in con.execute(
            "SELECT DISTINCT relation_type FROM relation_catalog WHERE kind='OFFICIAL'"
            f" AND relation_type IN ({','.join('?' * len(rel))})", rel)]
        if found:
            where.append(f"""EXISTS(SELECT 1 FROM stats_relation_pillar z
                  WHERE z.identity_code=r.identity_code AND z.record_kind='OFFICIAL'
                    AND z.flow_pillar=r.flow_pillar AND z.relation_type IN ({placeholders("rl", found)}))""")

    where_sql = " AND ".join(where)

    if msort:
        keys = []
        for key, order in msort:
            o_dir = order.upper()
            if key == "mean":
                desc = "ASC" if (order == "asc" if direction != "down" else order == "desc") else "DESC"
                keys.append(f"{mean_col} {desc} NULLS LAST")
            elif key == "up_rate":
                keys.append(f"{rate_col} {o_dir} NULLS LAST")
            elif key == "min_gain":
                desc = "ASC" if (order == "asc" if direction != "down" else order == "desc") else "DESC"
                keys.append(f"r.ALL_worst_return_pct {desc} NULLS LAST")
            elif key == "max_gain":
                desc = "ASC" if (order == "asc" if direction != "down" else order == "desc") else "DESC"
                keys.append(f"r.ALL_best_return_pct {desc} NULLS LAST")
            elif key == "n":
                keys.append(f"{n_col} {o_dir} NULLS LAST")
        if not any(k == "mean" for k, _ in msort):
            desc = "ASC" if direction == "down" else "DESC"
            keys.append(f"{mean_col} {desc} NULLS LAST")
        keys.append(f"{n_col} DESC NULLS LAST")
        keys.append("r.identity_code")
        keys.append(f"{win_col}")
        order_by = ", ".join(keys)
    elif sort == "mean":
        desc = "ASC" if direction == "down" else "DESC"
        order_by = f"{mean_col} {desc} NULLS LAST, {rate_col} DESC NULLS LAST, {n_col} DESC NULLS LAST, r.identity_code, {win_col}"
    elif sort in ("up_rate", "rate"):
        order_by = f"{rate_col} DESC NULLS LAST, {mean_col} DESC NULLS LAST, {n_col} DESC NULLS LAST, r.identity_code, {win_col}"
    elif sort == "n":
        order_by = f"{n_col} DESC NULLS LAST, {mean_col} DESC NULLS LAST, {rate_col} DESC NULLS LAST, r.identity_code, {win_col}"
    elif sort == "min_gain":
        desc = "ASC" if direction == "down" else "DESC"
        order_by = f"r.ALL_worst_return_pct {desc} NULLS LAST, {mean_col} DESC NULLS LAST, r.identity_code, {win_col}"
    elif sort == "max_gain":
        desc = "ASC" if direction == "down" else "DESC"
        order_by = f"r.ALL_best_return_pct {desc} NULLS LAST, {mean_col} DESC NULLS LAST, r.identity_code, {win_col}"
    else:
        desc = "ASC" if direction == "down" else "DESC"
        order_by = f"{mean_col} {desc} NULLS LAST, {rate_col} DESC NULLS LAST, {n_col} DESC NULLS LAST, r.identity_code, {win_col}"

    cap_join = "LEFT JOIN cap.cap_tier_monthly ct ON ct.identity_code=r.identity_code AND ct.month_id=:cap_mid" if cap_mid else ""
    cap_select = f"{CAP_BAND_EXPR} as cap_tier, ct.free_mv_yi as cap_free_mv_yi, ct.avg_amt_20d_yi as cap_avg_amt_20d_yi," if cap_mid else "NULL as cap_tier, NULL as cap_free_mv_yi, NULL as cap_avg_amt_20d_yi,"

    rules_src = rules_tbl
    live_map = {}
    if dataset == "ashare" and is_current:
        table, _, _ = _live_window_table(layer, month_id, target_m["start_at"],
                                         target_m["end_at"])
        has_done = bool(table) and any(
            w["status"] == "done" and (only_win is None or win == only_win)
            for win, w in table["windows"].items())
        if has_done:
            try:
                _build_adj_rules(con, layer, pillar, xw_class, only_win, table)
                rules_src = "adj_rules"
                live_map = _live_overlay_map(layer, month_id, target_m["start_at"],
                                             target_m["end_at"])
            except sqlite3.Error as exc:
                print(f"[提示] 本月已走完窗口并库失败，回退到历史统计："
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)

    sql = f"""
    WITH top_rows AS (
        SELECT r.*
        FROM {rules_src} r
        JOIN securities s ON s.identity_code = r.identity_code
        {cap_join}
        WHERE {where_sql}
        ORDER BY {order_by}
        LIMIT :fetch
    )
    SELECT 
        r.identity_code, r.security_code, r.name, r.flow_pillar,
        {win_tag_expr} as window_tag,
        r.xun_no, r.xun_name,
        {week_col_select}
        r.day_from, r.day_to,
        r.ALL_n_occurrences as all_sample_n,
        r.ALL_valid_n, r.ALL_invalid_n,
        r.ALL_up_n, r.ALL_up_n as up_n_ALL,
        r.ALL_down_n, r.ALL_down_n as down_n_ALL,
        r.ALL_flat_n, r.ALL_flat_n as flat_n_ALL,
        r.ALL_up_rate as up_rate_ALL,
        r.ALL_down_rate as down_rate_ALL,
        ROUND(r.ALL_mean_return_pct / 100.0, 6) as mean_body_return_ALL,
        ROUND(r.ALL_median_return_pct / 100.0, 6) as median_body_return_ALL,
        ROUND(r.ALL_worst_return_pct / 100.0, 6) as min_body_return_ALL,
        ROUND(r.ALL_best_return_pct / 100.0, 6) as ALL_best_return_pct,
        r.EXCLUDE_SINGLE_valid_n,
        r.EXCLUDE_SINGLE_valid_n as exclude_single_n,
        r.EXCLUDE_SINGLE_up_n,
        r.EXCLUDE_SINGLE_up_n as up_n_EXCLUDE_SINGLE,
        r.EXCLUDE_SINGLE_down_n,
        r.EXCLUDE_SINGLE_down_n as down_n_EXCLUDE_SINGLE,
        (r.EXCLUDE_SINGLE_valid_n - r.EXCLUDE_SINGLE_up_n - r.EXCLUDE_SINGLE_down_n) as flat_n_EXCLUDE_SINGLE,
        r.EXCLUDE_SINGLE_up_rate as up_rate_ES,
        ROUND(r.EXCLUDE_SINGLE_down_n * 1.0 / NULLIF(r.EXCLUDE_SINGLE_valid_n, 0), 6) as down_rate_ES,
        ROUND(r.EXCLUDE_SINGLE_mean_return_pct / 100.0, 6) as mean_body_return_ES,
        r.sample_grade, r.band,
        s.security_type, s.delisted_date,
        p.natal_year_pillar, p.natal_month_pillar, p.natal_day_pillar,
        p.day_master_stem, p.day_master_element,
        p.day_master_element as natal_element,
        p.year_stem_element, p.year_branch_element,
        p.month_stem_element, p.month_branch_element,
        p.day_stem_element, p.day_branch_element,
        (CASE WHEN p.identity_code IS NOT NULL THEN 1 ELSE 0 END) as has_natal_profile,
        {cap_select}
        (SELECT GROUP_CONCAT(DISTINCT relation_type) 
         FROM stats_relation_pillar rp 
         WHERE rp.identity_code = r.identity_code AND rp.flow_pillar = r.flow_pillar AND rp.record_kind = 'OFFICIAL') as relation_types,
        act.window_return as actual_return,
        act.direction as actual_direction
    FROM top_rows r
    JOIN securities s ON s.identity_code = r.identity_code
    LEFT JOIN natal_profiles p ON p.identity_code = r.identity_code
    {cap_join}
    LEFT JOIN {occ_tbl} act ON act.identity_code = r.identity_code AND act.flow_pillar = r.flow_pillar AND {occ_cond} AND act.month_id = :month_id
    ORDER BY {order_by}
    """

    total = con.execute(
        f"SELECT COUNT(*) FROM {rules_src} r"
        f" JOIN securities s ON s.identity_code = r.identity_code"
        f" {cap_join}"
        f" WHERE {where_sql}", params).fetchone()[0]
    pool = [dict(row) for row in con.execute(sql, params).fetchall()]
    # 先按 SQL 主排序切页、再给这一页补列，最后页内重排。稳健分/一致性/超额只在 Python 里算，
    # 不能拿来跨页排序：排序池会随页码增大，同一行会在相邻页重复出现、另一些行永远翻不到。
    rows = pool[row_off:row_off + cap]
    _attach_monthlike_fields(con, rows, layer, pillar, xw_class, direction, live_map)

    def _row_sort_key(r, key, order):
        rev = (order == "desc")
        if key == "mean":
            v = r.get("mean_body_return_ALL")
            return (-(v if v is not None else -999999)) if rev else (v if v is not None else 999999)
        if key == "up_rate":
            v = r.get("up_rate_ALL")
            return (-(v if v is not None else -999999)) if rev else (v if v is not None else 999999)
        if key == "excess":
            v = r.get("excess_mean")
            return (-(v if v is not None else -999999)) if rev else (v if v is not None else 999999)
        if key == "stability":
            s_map = {"good": 0, "medium": 1, "weak": 2, "none": 3}
            rank = s_map.get(r.get("stability_level"), 3)
            sc = r.get("stability_score") or 0.0
            return (rank, -sc) if order == "asc" else (-rank, sc)
        if key == "consistency":
            c_map = {"一致": 0, "一般": 1, "差异大": 2, "—": 3}
            rank = c_map.get(r.get("cons_level"), 3)
            cv = r.get("cons_cv") if r.get("cons_cv") is not None else 999999
            return (rank, cv) if order == "asc" else (-rank, -cv)
        if key == "min_gain":
            v = r.get("stab_min_ret") if r.get("stab_min_ret") is not None else r.get("min_body_return_ALL")
            return (-(v if v is not None else -999999)) if rev else (v if v is not None else 999999)
        if key == "max_gain":
            v = r.get("stab_max_ret") if r.get("stab_max_ret") is not None else r.get("ALL_best_return_pct")
            return (-(v if v is not None else -999999)) if rev else (v if v is not None else 999999)
        if key == "n":
            v = r.get("ALL_valid_n")
            return (-(v if v is not None else -999999)) if rev else (v if v is not None else 999999)
        return 0

    if msort and any(k in ("excess", "stability", "consistency") for k, _ in msort):
        rows.sort(key=lambda r: tuple(_row_sort_key(r, k, o) for k, o in msort))
    elif not msort and sort in ("excess", "stability", "consistency"):
        order = "asc" if sort in ("stability", "consistency") else ("asc" if direction == "down" else "desc")
        rows.sort(key=lambda r: (_row_sort_key(r, sort, order), _row_sort_key(r, "mean", "desc")))

    for r in rows:
        stem = r.get("flow_stem") or (r["flow_pillar"][:1] if r.get("flow_pillar") else "")
        branch = r.get("flow_branch") or (r["flow_pillar"][1:2] if r.get("flow_pillar") else "")
        r["stem_element"] = STEM_ELEMENT_MAP.get(stem, "")
        r["branch_element"] = BRANCH_ELEMENT_MAP.get(branch, "")

    live_info = None
    if dataset == "ashare" and is_current and rows:
        live_info = _attach_live_current(
            rows, layer, month_id, pillar, target_m["start_at"], target_m["end_at"])

    m_info = dict(target_m)
    m_info["ordinal"] = offset
    m_info["pillar"] = pillar
    m_info["is_current"] = is_current
    m_info["is_past"] = is_past
    m_info["days_to_flip"] = target_m.get("days_until_end") or target_m.get("days_until") or 0
    m_info["label"] = "本月" if is_current else ("历史月" if is_past else "未来月")
    m_info["market_rows_in_month"] = total if is_past else 0
    m_info["totals"] = {
        "identities": total,
        "in_scope": total,
        "returned": len(rows),
        "page": page, "page_size": cap,
        "pages": ((total + cap - 1) // cap) if total else 0,
        "capped": bool(total and len(rows) < total)
    }

    caveats = [
        f"当前窗口层级：【{'旬层' if layer == 'xun' else '段层'}】· 所选流月【{pillar}】· 标的类别【{xw_class}】",
        "窗收益 = 该窗内首根有效日K开盘 → 末根有效日K收盘（非月涨幅拆分）；空窗不计入分母；平盘严格要求涨跌幅 = 0。",
        "规则库只收完整流月；本月已经走完的旬/段窗口按日更切片补进历史统计（并进分母），没走完的窗口不计入。"
    ]
    if (msort and any(k in ("excess", "stability", "consistency") for k, _ in msort)) \
            or (not msort and sort in ("excess", "stability", "consistency")):
        caveats.append(
            "旬/段层的「稳健分 / 一致性 / 同期超额」要取数后才算，只能在当前页内重排；"
            "翻页按平均涨跌幅这条主排序推进，跨页不保证这个键全局有序。换成平均涨跌/占比/出现次数排序则整表有序。")
    if live_info:
        if live_info.get("status") == "ok":
            caveats.append(
                f"「实际」列对本月按日更切片计算：已走完的窗口是最终值，并已补进上面的历史统计；"
                f"未走完的是截至 {live_info['latest_trade_date']} 的临时值，不计入历史。")
            caveats.append(
                "筛选与排序都按补入后的口径重算；已走完窗口为反向时，占比/均值会下降，"
                "可能因此跌出当前的占比门槛。")
            caveats.append(
                "同期超额仍按有指数基线的历次窗口计算；本月已走完窗口暂缺指数日更，不计入超额。")
        else:
            caveats.append(f"本月日更切片未就绪：{live_info.get('reason') or '—'}")
    if not rows and grade == "6+":
        ceil_ = _class_ceiling(con, rules_tbl, xw_class)
        if ceil_ is not None and ceil_[0] < 6:
            caveats.append(
                f"这一档空是样本天花板，不是页面坏了：{xw_class} 在同一根流月柱上最多只出现过 {ceil_[0]} 次"
                f"（该类别覆盖 {ceil_[1]}→{ceil_[2]}），请改选『3–5 次』。")

    return {
        "banner": BANNER,
        "dataset": dataset,
        "market_label": market_label,
        "asof": cal["asof"],
        "latest_month_with_data": _window_history_end(con),
        "input_data_as_of": _window_data_as_of(con, xw_class),
        "rel_head": "该流月窗口命中关系",
        "next_month": None,
        "live_month": live_info,
        "month_kind": "past" if is_past else "present_or_future",
        "calendar_version": target_m.get("calendar_version", "jieqi-moshier-2.10.03-v1"),
        "months": [m_info],
        "rows": rows,
        "params": {
            "sort": sort,
            "msort": [{"key": k, "dir": d} for k, d in msort],
            "min_n": min_n,
            "grade": grade,
            "rate_min": rate_min,
            "rate_eq": rate_eq,
            "policy": policy,
            "cap": cap, "page": page,
            "cap_tier": cap_tier,
            "layer": layer,
            "window_no": window_no
        },
        "caveats": caveats
    }


def pillar_detail(con, identity: str, pillar: str):
    """Each historical occurrence of one flow pillar for this stock, with its relation facts."""
    rows = _rows(con, """
      SELECT f.month_id, f.solar_year, f.month_no, f.start_term, f.first_date, f.last_date,
             f.trading_days, f.direction, f.body_return, f.return_decimal, f.single_day,
             f.partial_listing, f.partial_delisting, f.quality_flags, f.endpoint_basis,
             f.computation_status, f.research_sample_status, f.undisputed,
             f.stem_ten_god, f.confirmed_event_count, f.candidate_structure_count,
             f.benchmark_code, f.benchmark_return, f.excess_return, f.comparison_status,
             f.raw_open, f.raw_close, f.qfq_open, f.qfq_close
      FROM monthly_facts f WHERE f.identity_code=? AND f.flow_pillar=?
      ORDER BY f.month_id""", (identity, pillar))
    for r in rows:
        r["relations"] = _rows(con, """
          SELECT record_kind, relation_type, family, natal_position, flow_element,
                 natal_element, svu, origin_type, structure_combo, present_branches,
                 missing_branches, candidate_transformation_element,
                 transformation_established, transformation_candidate_disputed,
                 establishment_rule_id, partial_kind, wang_branch_present,
                 occupied_positions, fact_direction_marker, confirmed, note
          FROM monthly_relations WHERE identity_code=? AND month_id=?
          ORDER BY record_kind, relation_type, natal_position""", (identity, r["month_id"]))
    day_master, _ = profile_day_master(con, identity)
    for r in rows:
        attach_ten_god(r, day_master, pillar)
        computed = r["ten_god"]["stem"]["ten_god"] if r["ten_god_status"] == "OK" else None
        r["stem_ten_god_computed"] = computed
        r["stem_ten_god_consistent"] = (None if computed is None or r["stem_ten_god"] is None
                                        else computed == r["stem_ten_god"])
    return rows


def stock_stability(con, identity: str):
    """One identity's whole-history monthly volatility, for the drawer only.

    This is deliberately not part of the stability score/filter: the walk-forward test showed
    stock-level volatility does not lower the next same-pillar hit rate, only the drawdown size.
    """
    r = _rows(con, """
      SELECT COUNT(*) AS n, SUM(direction='UP') AS up_n, SUM(direction='DOWN') AS down_n,
             SUM(direction='FLAT') AS flat_n, AVG(body_return) AS mean_ret,
             MIN(body_return) AS min_ret, MAX(body_return) AS max_ret,
             SUM(body_return*body_return) AS sum_sq,
             SUM(body_return <= -0.30) AS n_dn30, SUM(body_return <= -0.40) AS n_dn40,
             SUM(body_return >= 0.30) AS n_up30, SUM(body_return >= 0.50) AS n_up50
      FROM monthly_facts
      WHERE identity_code=? AND eligible=1 AND fact_status='FINAL' AND body_return IS NOT NULL
    """, (identity,))
    if not r or not r[0]["n"]:
        return {"identity_code": identity, "n": 0}
    x = r[0]
    n = x["n"]
    std = math.sqrt(max(x["sum_sq"] / n - x["mean_ret"] * x["mean_ret"], 0))
    kind = ("暴涨暴跌型" if x["max_ret"] >= 0.50 and x["min_ret"] <= -0.40
            else "高波动型" if std >= 0.15 else "常规波动")
    top_up = _rows(con, """
      SELECT month_id, flow_pillar, body_return FROM monthly_facts
      WHERE identity_code=? AND eligible=1 AND fact_status='FINAL' AND body_return IS NOT NULL
      ORDER BY body_return DESC LIMIT 3""", (identity,))
    top_down = _rows(con, """
      SELECT month_id, flow_pillar, body_return FROM monthly_facts
      WHERE identity_code=? AND eligible=1 AND fact_status='FINAL' AND body_return IS NOT NULL
      ORDER BY body_return ASC LIMIT 3""", (identity,))
    return {"identity_code": identity, "n": n, "up_n": x["up_n"], "down_n": x["down_n"],
            "flat_n": x["flat_n"], "mean_ret": x["mean_ret"], "min_ret": x["min_ret"],
            "max_ret": x["max_ret"], "std": std, "n_dn30": x["n_dn30"],
            "n_dn40": x["n_dn40"], "n_up30": x["n_up30"], "n_up50": x["n_up50"],
            "kind": kind, "top_up": top_up, "top_down": top_down}


def relation_types(con, identity: str, record_kind: str):
    return _rows(con, """
      SELECT t.*, c.group_name, c.kind
      FROM stats_relation_types t
      LEFT JOIN relation_catalog c ON c.relation_type=t.relation_type
      WHERE t.identity_code=? AND t.record_kind=?
      ORDER BY CASE c.kind WHEN 'OFFICIAL' THEN 0 ELSE 1 END, t.months_undisputed DESC""",
                 (identity, record_kind))


def relation_pillars(con, identity: str, record_kind: str, rtype: str):
    return _rows(con, """
      SELECT p.*, o.ordinal AS pillar_ordinal,
             (SELECT np.day_master_stem FROM natal_profiles np
               WHERE np.identity_code=p.identity_code) AS day_master
      FROM stats_relation_pillar p
      LEFT JOIN pillar_order o ON o.flow_pillar=p.flow_pillar
      WHERE p.identity_code=? AND p.record_kind=? AND p.relation_type=?
      ORDER BY p.months_undisputed DESC, o.ordinal""", (identity, record_kind, rtype))


def relation_months(con, identity: str, record_kind: str, rtype: str, pillar: str = ""):
    sql = """
      SELECT t.month_id, t.solar_year, t.flow_pillar, t.month_branch, t.positions,
             t.event_count, t.direction, t.body_return, t.single_day, t.undisputed,
             t.boundary_disputed, t.research_sample_status,
             f.start_term, f.first_date, f.last_date, f.trading_days, f.stem_ten_god,
             f.benchmark_code, f.benchmark_return, f.excess_return
      FROM month_relation_types t JOIN monthly_facts f
        ON f.identity_code=t.identity_code AND f.month_id=t.month_id
      WHERE t.identity_code=? AND t.record_kind=? AND t.relation_type=?"""
    params: list = [identity, record_kind, rtype]
    if pillar:
        sql += " AND t.flow_pillar=?"
        params.append(pillar)
    sql += " ORDER BY t.month_id"
    rows = _rows(con, sql, params)
    for r in rows:
        r["events"] = _rows(con, """
          SELECT relation_type, natal_position, family, flow_stem, flow_branch,
                 flow_element, natal_element, svu, origin_type,
                 structure_combo, present_branches, missing_branches,
                 candidate_transformation_element, transformation_established,
                 transformation_candidate_disputed, establishment_rule_id, partial_kind,
                 wang_branch_present, occupied_positions, fact_direction_marker, note
          FROM monthly_relations
          WHERE identity_code=? AND month_id=? AND record_kind=? AND relation_type=?
          ORDER BY natal_position""", (identity, r["month_id"], record_kind, rtype))
    day_master, _ = profile_day_master(con, identity)
    for r in rows:
        attach_ten_god(r, day_master, r["flow_pillar"])
    return rows


def month_detail(con, identity: str, month_id: str):
    facts = _rows(con, "SELECT * FROM monthly_facts WHERE identity_code=? AND month_id=?",
                  (identity, month_id))
    if not facts:
        return None
    facts[0]["events"] = _rows(con, """
      SELECT * FROM monthly_relations WHERE identity_code=? AND month_id=?
      ORDER BY record_kind, relation_type, natal_position""", (identity, month_id))
    np_ = _rows(con, "SELECT * FROM natal_profiles WHERE identity_code=?", (identity,))
    facts[0]["profile"] = np_[0] if np_ else None
    day_master, _ = profile_day_master(con, identity)
    attach_ten_god(facts[0], day_master, facts[0]["flow_pillar"])
    return facts[0]


# ---------------- 页面五：十神历史涨跌（描述性统计） ----------------
TEN_GOD_SAMPLE_MODES = ("ALL", "EXCLUDE_SINGLE")
TEN_GOD_SAMPLE_GRADE = (
    (0, "NO_HISTORY"), (1, "SINGLE_OBSERVATION"), (3, "VERY_FEW"),
    (5, "FEW"), (10 ** 9, "RELATIVELY_MORE"))
DENOMINATOR_NOTE = (
    "n = 同筛选下的有效历史月事实数（每条 monthly_facts 恰归 1 类流月天干十神）；"
    "up_rate = up / n，平盘计入分母，与冻结 60 柱/12 支统计口径一致；"
    "平均涨跌幅只对 body_return 非空样本求均值（见 return_n），缺失不按 0 代填。")


def _ten_god_filters(identity: str, sample_mode: str, flow_branch: str,
                     relation_type: str):
    """构造月事实筛选；关系筛选用 EXISTS，绝不 join 放大计数。"""
    where = ["f.identity_code=?"]
    params: list = [identity]
    if sample_mode == "EXCLUDE_SINGLE":
        where.append("f.single_day=0")
    if flow_branch:
        where.append("f.month_branch=?")
        params.append(flow_branch)
    if relation_type:
        where.append("""EXISTS(SELECT 1 FROM month_relation_types t
                        WHERE t.identity_code=f.identity_code AND t.month_id=f.month_id
                          AND t.record_kind='OFFICIAL' AND t.relation_type=?)""")
        params.append(relation_type)
    return where, params


def _grade(n: int) -> str:
    for limit, label in TEN_GOD_SAMPLE_GRADE:
        if n <= limit:
            return label
    return "RELATIVELY_MORE"


def ten_god_stats(con, identity: str, sample_mode: str = "ALL", flow_branch: str = "",
                  relation_type: str = ""):
    """按「流月天干十神」聚合已冻结历史事实。10 类固定全列，不重复计数。"""
    if sample_mode not in TEN_GOD_SAMPLE_MODES:
        raise ValueError(f"sample_mode 只能是 {'/'.join(TEN_GOD_SAMPLE_MODES)}")
    day_master, dm_status = profile_day_master(con, identity)
    where, params = _ten_god_filters(identity, sample_mode, flow_branch, relation_type)

    # 有效样本：有十神的历史行。缺档案行单独统计，不归入第 11 类。
    base_where = list(where)
    grouped = _rows(con, f"""
      SELECT f.stem_ten_god AS ten_god, COUNT(*) AS n,
             SUM(f.direction='UP') AS up_n, SUM(f.direction='DOWN') AS down_n,
             SUM(f.direction='FLAT') AS flat_n, SUM(f.single_day) AS single_day_n,
             SUM(f.direction IS NULL) AS no_price_n,
             SUM(f.body_return IS NOT NULL) AS return_n, AVG(f.body_return) AS mean_return,
             SUM(f.comparison_status='MATCHED') AS benchmark_n,
             AVG(CASE WHEN f.comparison_status='MATCHED' THEN f.benchmark_return END)
               AS benchmark_mean,
             AVG(f.excess_return) AS excess_mean, SUM(f.excess_return>0) AS outperform_n
      FROM monthly_facts f
      WHERE {' AND '.join(base_where)} AND f.stem_ten_god IS NOT NULL
            AND f.stem_ten_god<>''
      GROUP BY 1""", params)
    by_god = {r["ten_god"]: r for r in grouped}

    groups = []
    for god in ten_god_rules.TEN_GODS:
        r = by_god.get(god)
        n = int(r["n"]) if r else 0
        no_price = int(r["no_price_n"] or 0) if r else 0
        groups.append({
            "ten_god": god, "n": n,
            "up": int(r["up_n"] or 0) if r else 0,
            "down": int(r["down_n"] or 0) if r else 0,
            "flat": int(r["flat_n"] or 0) if r else 0,
            "up_rate": (round(r["up_n"] / n, 6) if r and n else None),
            "mean_return": r["mean_return"] if r else None,
            "return_n": int(r["return_n"] or 0) if r else 0,
            "single_day_n": int(r["single_day_n"] or 0) if r else 0,
            "no_price_n": no_price,
            "fact_status": (tg.FACT_NO_PRICE if n and no_price == n
                            else (tg.FACT_HISTORICAL if n else tg.FACT_NO_HISTORY)),
            "benchmark_n": int(r["benchmark_n"] or 0) if r else 0,
            "index_comparison": ({
                "benchmark_n": int(r["benchmark_n"] or 0),
                "benchmark_mean_return": r["benchmark_mean"],
                "excess_mean": r["excess_mean"],
                "outperform_n": int(r["outperform_n"] or 0),
            } if r and r["benchmark_n"] else None),
            "index_comparison_reason": (None if (r and r["benchmark_n"])
                                        else "NO_MATCHED_BENCHMARK"),
            "sample_grade": _grade(n),
            "stability_note": ("样本 n=%d，不足以判断稳定性" % n) if 0 < n < 6
                              else ("无历史样本" if n == 0 else ""),
        })

    total_row = _rows(con, f"""
      SELECT COUNT(*) AS total_rows,
             SUM(f.stem_ten_god IS NULL OR f.stem_ten_god='') AS missing_profile,
             SUM(f.stem_ten_god IS NOT NULL AND f.stem_ten_god<>''
                 AND f.direction IS NULL) AS invalid_price
      FROM monthly_facts f WHERE {' AND '.join(where)}""", params)[0]
    classified = sum(g["n"] for g in groups)
    total_rows = int(total_row["total_rows"] or 0)
    return {
        "status": "OK" if dm_status == "OK" else "UNAVAILABLE_PROFILE",
        "rule_version": ten_god_rules.RULE_VERSION,
        "hidden_rule_version": ten_god_rules.HIDDEN_RULE_VERSION,
        "identity_code": identity,
        "day_master": day_master,
        "sample_mode": sample_mode,
        "basis": "EXISTING_VALID_MONTHLY_FACTS",
        "filters": {"flow_branch": flow_branch or None,
                    "relation_type": relation_type or None},
        "groups": groups,
        "unavailable": {
            "missing_profile": int(total_row["missing_profile"] or 0),
            "invalid_price": int(total_row["invalid_price"] or 0),
        },
        "accounting": {
            "total_rows": total_rows,
            "classified_n": classified,
            "missing_profile": int(total_row["missing_profile"] or 0),
            "unclassified": total_rows - classified - int(total_row["missing_profile"] or 0),
        },
        "denominator_note": DENOMINATOR_NOTE,
        "prediction": False,
    }


def ten_god_months(con, identity: str, ten_god: str, sample_mode: str = "ALL",
                   flow_branch: str = "", relation_type: str = ""):
    """某一种流月天干十神下的逐月历史明细（复用冻结月事实，不重算月线）。"""
    if ten_god not in ten_god_rules.TEN_GODS:
        raise ValueError(f"未知十神：{ten_god!r}")
    where, params = _ten_god_filters(identity, sample_mode, flow_branch, relation_type)
    where.append("f.stem_ten_god=?")
    params.append(ten_god)
    rows = _rows(con, f"""
      SELECT f.month_id, f.solar_year, f.month_no, f.flow_pillar, f.month_branch,
             f.start_term, f.first_date, f.last_date, f.trading_days,
             f.direction, f.body_return, f.single_day, f.partial_listing,
             f.partial_delisting, f.research_sample_status, f.stem_ten_god,
             f.benchmark_code, f.benchmark_return, f.excess_return, f.comparison_status,
             (SELECT group_concat(t.relation_type,' ')
                FROM month_relation_types t
                WHERE t.identity_code=f.identity_code AND t.month_id=f.month_id
                  AND t.record_kind='OFFICIAL') AS official_types
      FROM monthly_facts f WHERE {' AND '.join(where)} ORDER BY f.month_id""", params)
    day_master, _ = profile_day_master(con, identity)
    for r in rows:
        attach_ten_god(r, day_master, r["flow_pillar"])
    return {"status": "OK", "ten_god": ten_god, "identity_code": identity,
            "sample_mode": sample_mode, "n": len(rows), "rows": rows,
            "rule_version": ten_god_rules.RULE_VERSION, "prediction": False}


def market_relation_types(con):
    return _rows(con, """SELECT * FROM relation_type_market_stats
                         ORDER BY CASE item_kind WHEN 'OFFICIAL_22' THEN 0
                                 WHEN 'CANDIDATE_STRUCTURE' THEN 1 ELSE 2 END, category_id""")


def catalog(con):
    return _rows(con, "SELECT * FROM relation_catalog ORDER BY kind, ordinal")


def sources(con):
    """「来源与版本」页：库内 path 是开发机的绝对路径，交付显示层隐去本机前缀。

    数据库内容一个字都不改；开发机要看原路径，设环境变量 BAZI_SOURCE_PATHS=full。
    """
    rows = _rows(con, "SELECT kind, name, path, sha256, version, rows, note"
                      " FROM source_versions ORDER BY kind, name")
    for r in rows:
        r["path"] = _mask_local_path(r.get("path"))
    return rows


def _mask_local_path(p):
    if not p or os.environ.get("BAZI_SOURCE_PATHS") == "full":
        return p
    parts = [x for x in str(p).replace("\\", "/").split("/") if x]
    return "…/" + "/".join(parts[-2:]) if len(parts) > 2 else str(p)


_META_CACHE: dict[str, dict] = {}


def meta(con, dataset: str = DEFAULT_DATASET):
    if dataset in _META_CACHE:
        return _META_CACHE[dataset]
    spec = DATASETS[dataset]
    q = lambda s: con.execute(s).fetchone()[0]  # noqa: E731
    index_count = q("SELECT COUNT(*) FROM securities WHERE security_type='INDEX'")
    delisted_count = q("SELECT COUNT(*) FROM securities WHERE delisted_date IS NOT NULL")
    no_profile_count = q("SELECT COUNT(*) FROM securities WHERE has_natal_profile=0")
    facts_total = q("SELECT COUNT(*) FROM monthly_facts")
    stats_counts = {
        "stats_pillar60": q("SELECT COUNT(*) FROM stats_pillar60"),
        "stats_branch12": q("SELECT COUNT(*) FROM stats_branch12"),
        "stats_relation_types": q("SELECT COUNT(*) FROM stats_relation_types"),
        "stats_relation_pillar": q("SELECT COUNT(*) FROM stats_relation_pillar"),
    }
    market_rows = q("SELECT COUNT(*) FROM relation_type_market_stats")
    stats_ready = (stats_counts["stats_pillar60"] > 0
                   and stats_counts["stats_branch12"] > 0
                   and stats_counts["stats_relation_types"] > 0
                   and stats_counts["stats_relation_pillar"] > 0
                   and market_rows > 0)
    stats_run = None
    if dataset == "etf":
        try:
            row = con.execute("SELECT * FROM etf_stats_run "
                              "ORDER BY built_at_utc DESC LIMIT 1").fetchone()
            stats_run = dict(row) if row else None
        except sqlite3.OperationalError:
            stats_run = None
    res = {
        "banner": BANNER,
        "dataset": dataset,
        "dataset_label": spec["label"],
        "default_identity": spec["default_identity"],
        "app_title": spec["app_title"],
        "search_placeholder": spec["search_placeholder"],
        "stock_tab_label": spec["stock_tab_label"],
        "market_label": spec["market_label"],
        "profile_hint": spec["profile_hint"],
        "branch_stats_note": spec["branch_stats_note"],
        "market_stats_note": spec["market_stats_note"],
        "type_labels": {"A_SHARE": "股票", "INDEX": "指数", "ETF": "ETF"},
        "securities": q("SELECT COUNT(*) FROM securities"),
        "with_profile": q("SELECT SUM(has_natal_profile) FROM securities"),
        "without_profile": q("SELECT SUM(has_natal_profile=0) FROM securities"),
        "monthly_facts": facts_total,
        "facts_total": facts_total,
        "undisputed": q("SELECT SUM(undisputed) FROM monthly_facts"),
        "disputed": q("SELECT SUM(boundary_disputed) FROM monthly_facts"),
        "blocked": q("SELECT SUM(profile_present=0) FROM monthly_facts"),
        "monthly_relations": q("SELECT COUNT(*) FROM monthly_relations"),
        "official_events": q("SELECT COUNT(*) FROM monthly_relations WHERE record_kind='OFFICIAL'"),
        "candidate_events": q(
            "SELECT COUNT(*) FROM monthly_relations WHERE record_kind='CANDIDATE'"),
        **stats_counts,
        "market_relation_types": market_rows,
        "stats_ready": stats_ready,
        "stats_run": stats_run,
        "index_count": index_count,
        "delisted_count": delisted_count,
        "no_profile_count": no_profile_count,
        "noop_controls": {
            "include_index": {"hidden": True if dataset == "etf" else index_count == 0,
                              "count": index_count},
            "include_delisted": {"hidden": True if dataset == "etf" else delisted_count == 0,
                                 "count": delisted_count},
        },
        "rule_versions": [r[0] for r in con.execute(
            "SELECT DISTINCT relation_rule_version FROM monthly_facts")],
        "ten_god_rule_version": ten_god_rules.RULE_VERSION,
        "ten_god_hidden_rule_version": ten_god_rules.HIDDEN_RULE_VERSION,
        "ten_god_rule_source": ten_god_rules.rule_manifest(),
    }
    _META_CACHE[dataset] = res
    return res


def invalidate_caches() -> int:
    """清空三个模块级结果缓存，返回清掉的条数。

    背景：连接是每请求新开的（换库后自然读到新库），但下面三个 dict 没有失效机制，
    原子替换 SQLite 文件后页面会一直返回旧统计直到进程重启。自动更新流水线跑完后
    （或任何换库动作之后）调用本函数即可，不需要重启服务。本函数不改任何查询口径。
    """
    cleared = len(_META_CACHE) + len(_CALENDAR_CACHE) + len(_OPP_CACHE) + len(_POOL_CACHE)
    _META_CACHE.clear()
    _CALENDAR_CACHE.clear()
    _OPP_CACHE.clear()
    _POOL_CACHE.clear()
    return cleared


# ---------------- 节气月 / 旬 / 周 三层规则库连接与接口 ----------------

_POOL_CACHE: dict[str, list[dict]] = {}

PILLARS_60 = [
    "甲子", "乙丑", "丙寅", "丁卯", "戊辰", "己巳", "庚午", "辛未", "壬申", "癸酉",
    "甲戌", "乙亥", "丙子", "丁丑", "戊寅", "己卯", "庚辰", "辛巳", "壬午", "癸未",
    "甲申", "乙酉", "丙戌", "丁亥", "戊子", "己丑", "庚寅", "辛卯", "壬辰", "癸巳",
    "甲午", "乙未", "丙申", "丁酉", "戊戌", "己亥", "庚子", "辛丑", "壬寅", "癸卯",
    "甲辰", "乙巳", "丙午", "丁未", "戊申", "己酉", "庚戌", "辛亥", "壬子", "癸丑",
    "甲寅", "乙卯", "丙辰", "丁巳", "戊午", "己未", "庚申", "辛酉", "壬戌", "癸亥"
]
PILLAR_ORD_MAP = {p: i for i, p in enumerate(PILLARS_60)}
BRANCH_ORD_MAP = {b: i for i, b in enumerate("子丑寅卯辰巳午未申酉戌亥")}

STEM_ELEMENT_MAP = {
    "甲": "木", "乙": "木", "丙": "火", "丁": "火", "戊": "土",
    "己": "土", "庚": "金", "辛": "金", "壬": "水", "癸": "水"
}
BRANCH_ELEMENT_MAP = {
    "寅": "木", "卯": "木", "巳": "火", "午": "火", "申": "金",
    "酉": "金", "亥": "水", "子": "水", "辰": "土", "戌": "土", "丑": "土", "未": "土"
}


def connect_xun_week() -> sqlite3.Connection:
    """月/旬/周规则库连接：与页面用同一条路径（主库里的 xw_* 表，缺失时回落独立规则文件）。
    旧版直接打开 allstock_xun_week_v1.sqlite 并用不带前缀的表名；那份库已被 v2 + 主库同步取代，
    所以这里改为复用 connect() 的 TEMP 视图接入，保证页面与池层读的是同一份数。"""
    con = connect(DEFAULT_DATASET)
    if not _xw_ready(con):
        raise FileNotFoundError(f"月/旬/段规则表不可用：主库 {config.ASHARE_DB} 与 "
                                f"独立文件 {config.XUN_WEEK_STANDALONE} 都没有可读的 xw_* 表")
    return con


def class_of_dataset(dataset: str) -> str:
    """页面顶部的 A股/ETF 开关 → 规则库 asset_class；未知数据集按 A 股处理。"""
    return config.WINDOW_ASSET_CLASS.get(dataset, "A_SHARE")


def get_base_pool_data(layer: str, asset_class: str = "A_SHARE") -> list[dict]:
    """池层宏观汇总（60柱 / 180旬窗 / 360周窗），按标的类别分别缓存。

    必须分类算：A股 5,560 只、ETF 1,692 只、指数 26 支的分布完全不同，
    混在一起的"平均"既不是 A 股也不是 ETF。"""
    ckey = f"{layer}|{asset_class}"
    if ckey in _POOL_CACHE:
        return _POOL_CACHE[ckey]

    con = connect_xun_week()
    try:
        if layer == "month":
            sql = """
            SELECT flow_pillar, flow_stem, flow_branch,
                   count(distinct identity_code) as covered_stocks,
                   SUM(ALL_n_occurrences) as total_occurrences,
                   SUM(ALL_valid_n) as valid_n,
                   SUM(ALL_invalid_n) as invalid_n,
                   SUM(ALL_up_n) as up_n,
                   SUM(ALL_down_n) as down_n,
                   SUM(ALL_flat_n) as flat_n,
                   ROUND(SUM(ALL_up_n) * 1.0 / NULLIF(SUM(ALL_valid_n), 0), 4) as up_rate,
                   ROUND(SUM(ALL_down_n) * 1.0 / NULLIF(SUM(ALL_valid_n), 0), 4) as down_rate,
                   ROUND(AVG(ALL_mean_return_pct), 2) as mean_return,
                   ROUND(AVG(ALL_median_return_pct), 2) as median_return
            FROM month_rules
            WHERE asset_class = ?
            GROUP BY flow_pillar
            """
            rows = con.execute(sql, (asset_class,)).fetchall()
            out = []
            for r in rows:
                p = r["flow_pillar"]
                out.append({
                    "flow_pillar": p,
                    "flow_stem": r["flow_stem"],
                    "flow_branch": r["flow_branch"],
                    "stem_element": STEM_ELEMENT_MAP.get(r["flow_stem"], ""),
                    "branch_element": BRANCH_ELEMENT_MAP.get(r["flow_branch"], ""),
                    "window_tag": "整月",
                    "window_pos": 0,
                    "covered_stocks": r["covered_stocks"],
                    "occurrence_count": r["total_occurrences"],
                    "ALL_n_occurrences": r["total_occurrences"],
                    "ALL_valid_n": r["valid_n"],
                    "all_sample_n": r["valid_n"],
                    "exclude_single_n": r["valid_n"],
                    "ALL_invalid_n": r["invalid_n"],
                    "ALL_up_n": r["up_n"],
                    "ALL_down_n": r["down_n"],
                    "ALL_flat_n": r["flat_n"],
                    "up_n_ALL": r["up_n"],
                    "down_n_ALL": r["down_n"],
                    "flat_n_ALL": r["flat_n"],
                    "up_rate": r["up_rate"] or 0.0,
                    "down_rate": r["down_rate"] or 0.0,
                    "mean_body_return_ALL": r["mean_return"] or 0.0,
                    "median_body_return_ALL": r["median_return"] or 0.0,
                    "is_five_year_repeat": "1",
                    "sample_marker_ALL": "GE6",
                    "ord": PILLAR_ORD_MAP.get(p, 99) * 10
                })
        elif layer == "week":
            sql = """
            SELECT flow_pillar, flow_stem, flow_branch, xun_no, week_no, week_name, day_from, day_to,
                   count(distinct identity_code) as covered_stocks,
                   SUM(ALL_n_occurrences) as total_occurrences,
                   SUM(ALL_valid_n) as valid_n,
                   SUM(ALL_invalid_n) as invalid_n,
                   SUM(ALL_up_n) as up_n,
                   SUM(ALL_down_n) as down_n,
                   SUM(ALL_flat_n) as flat_n,
                   ROUND(SUM(ALL_up_n) * 1.0 / NULLIF(SUM(ALL_valid_n), 0), 4) as up_rate,
                   ROUND(SUM(ALL_down_n) * 1.0 / NULLIF(SUM(ALL_valid_n), 0), 4) as down_rate,
                   ROUND(AVG(ALL_mean_return_pct), 2) as mean_return,
                   ROUND(AVG(ALL_median_return_pct), 2) as median_return
            FROM week_rules
            WHERE asset_class = ?
            GROUP BY flow_pillar, week_no
            """
            rows = con.execute(sql, (asset_class,)).fetchall()
            out = []
            for r in rows:
                p = r["flow_pillar"]
                w_no = r["week_no"]
                day_to_str = str(r["day_to"]) if r["day_to"] is not None else "下节"
                w_tag = f"第{w_no}段 {r['day_from']}–{day_to_str}"
                out.append({
                    "flow_pillar": p,
                    "flow_stem": r["flow_stem"],
                    "flow_branch": r["flow_branch"],
                    "stem_element": STEM_ELEMENT_MAP.get(r["flow_stem"], ""),
                    "branch_element": BRANCH_ELEMENT_MAP.get(r["flow_branch"], ""),
                    "xun_no": r["xun_no"],
                    "week_no": w_no,
                    "week_name": r["week_name"],
                    "day_from": r["day_from"],
                    "day_to": day_to_str,
                    "window_tag": w_tag,
                    "window_pos": w_no,
                    "covered_stocks": r["covered_stocks"],
                    "occurrence_count": r["total_occurrences"],
                    "ALL_n_occurrences": r["total_occurrences"],
                    "ALL_valid_n": r["valid_n"],
                    "all_sample_n": r["valid_n"],
                    "exclude_single_n": r["valid_n"],
                    "ALL_invalid_n": r["invalid_n"],
                    "ALL_up_n": r["up_n"],
                    "ALL_down_n": r["down_n"],
                    "ALL_flat_n": r["flat_n"],
                    "up_n_ALL": r["up_n"],
                    "down_n_ALL": r["down_n"],
                    "flat_n_ALL": r["flat_n"],
                    "up_rate": r["up_rate"] or 0.0,
                    "down_rate": r["down_rate"] or 0.0,
                    "mean_body_return_ALL": r["mean_return"] or 0.0,
                    "median_body_return_ALL": r["median_return"] or 0.0,
                    "is_five_year_repeat": "1",
                    "sample_marker_ALL": "GE6",
                    "ord": PILLAR_ORD_MAP.get(p, 99) * 10 + w_no
                })
        else:  # xun
            sql = """
            SELECT flow_pillar, flow_stem, flow_branch, xun_no, xun_name, day_from, day_to,
                   count(distinct identity_code) as covered_stocks,
                   SUM(ALL_n_occurrences) as total_occurrences,
                   SUM(ALL_valid_n) as valid_n,
                   SUM(ALL_invalid_n) as invalid_n,
                   SUM(ALL_up_n) as up_n,
                   SUM(ALL_down_n) as down_n,
                   SUM(ALL_flat_n) as flat_n,
                   ROUND(SUM(ALL_up_n) * 1.0 / NULLIF(SUM(ALL_valid_n), 0), 4) as up_rate,
                   ROUND(SUM(ALL_down_n) * 1.0 / NULLIF(SUM(ALL_valid_n), 0), 4) as down_rate,
                   ROUND(AVG(ALL_mean_return_pct), 2) as mean_return,
                   ROUND(AVG(ALL_median_return_pct), 2) as median_return
            FROM xun_rules
            WHERE asset_class = ?
            GROUP BY flow_pillar, xun_no
            """
            rows = con.execute(sql, (asset_class,)).fetchall()
            out = []
            for r in rows:
                p = r["flow_pillar"]
                x_no = r["xun_no"]
                day_to_str = str(r["day_to"]) if r["day_to"] is not None else "下节"
                w_tag = f"{r['xun_name']} {r['day_from']}–{day_to_str}"
                out.append({
                    "flow_pillar": p,
                    "flow_stem": r["flow_stem"],
                    "flow_branch": r["flow_branch"],
                    "stem_element": STEM_ELEMENT_MAP.get(r["flow_stem"], ""),
                    "branch_element": BRANCH_ELEMENT_MAP.get(r["flow_branch"], ""),
                    "xun_no": x_no,
                    "xun_name": r["xun_name"],
                    "day_from": r["day_from"],
                    "day_to": day_to_str,
                    "window_tag": w_tag,
                    "window_pos": x_no,
                    "covered_stocks": r["covered_stocks"],
                    "occurrence_count": r["total_occurrences"],
                    "ALL_n_occurrences": r["total_occurrences"],
                    "ALL_valid_n": r["valid_n"],
                    "all_sample_n": r["valid_n"],
                    "exclude_single_n": r["valid_n"],
                    "ALL_invalid_n": r["invalid_n"],
                    "ALL_up_n": r["up_n"],
                    "ALL_down_n": r["down_n"],
                    "ALL_flat_n": r["flat_n"],
                    "up_n_ALL": r["up_n"],
                    "down_n_ALL": r["down_n"],
                    "flat_n_ALL": r["flat_n"],
                    "up_rate": r["up_rate"] or 0.0,
                    "down_rate": r["down_rate"] or 0.0,
                    "mean_body_return_ALL": r["mean_return"] or 0.0,
                    "median_body_return_ALL": r["median_return"] or 0.0,
                    "is_five_year_repeat": "1",
                    "sample_marker_ALL": "GE6",
                    "ord": PILLAR_ORD_MAP.get(p, 99) * 10 + x_no
                })
        _POOL_CACHE[ckey] = out
        return out
    finally:
        con.close()


def window_layer_pool(layer: str, flt: dict, asset_class: str = "A_SHARE") -> list[dict]:
    """页面三（六十柱池）的月/旬/周池层汇总；类别由数据集开关决定。"""
    if layer not in ("month", "xun", "week"):
        layer = "xun"
    base = get_base_pool_data(layer, asset_class)

    direction = "down" if flt.get("dir") == "down" else "up"
    rate_raw = str(flt.get("rate_min") or "").strip()
    rate_min = float(rate_raw) / 100.0 if rate_raw and float(rate_raw) > 0 else 0.0
    rate_eq = flt.get("rate_eq") == "1" or rate_min >= 1.0

    p_filter = (flt.get("pillar") or "").strip()
    stem_el = flt.get("stem_element")
    branch_el = flt.get("branch_element")

    filtered = []
    for r in base:
        if p_filter and p_filter not in r["flow_pillar"]:
            continue
        if stem_el and r["stem_element"] != stem_el:
            continue
        if branch_el and r["branch_element"] != branch_el:
            continue
        rate = r["down_rate"] if direction == "down" else r["up_rate"]
        if rate_eq:
            if rate < 0.9999:
                continue
        elif rate_min > 0:
            if rate < rate_min:
                continue
        filtered.append(dict(r))

    sort_key = flt.get("sort") or "pillar"
    if sort_key == "up_rate":
        if direction == "down":
            filtered.sort(key=lambda x: (-x["down_rate"], x["mean_body_return_ALL"], x["ord"]))
        else:
            filtered.sort(key=lambda x: (-x["up_rate"], -x["mean_body_return_ALL"], x["ord"]))
    elif sort_key == "mean":
        if direction == "down":
            filtered.sort(key=lambda x: (x["mean_body_return_ALL"], x["ord"]))
        else:
            filtered.sort(key=lambda x: (-x["mean_body_return_ALL"], x["ord"]))
    elif sort_key in ("coverage", "n"):
        filtered.sort(key=lambda x: (-x["covered_stocks"], x["ord"]))
    elif sort_key == "branch":
        filtered.sort(key=lambda x: (BRANCH_ORD_MAP.get(x["flow_branch"], 99), x["ord"]))
    else:  # jiazi (六十甲子默认顺序)
        filtered.sort(key=lambda x: x["ord"])

    return filtered


WINDOW_RULES_TABLE = {"month": "month_rules", "xun": "xun_rules", "week": "week_rules"}


def _relation_pillar_filter(con, identity: str, flt: dict):
    """把页面三那六个冲合勾选翻成"哪些流月柱算命中"。

    判据与 pillar60 完全同源（同一张 stats_relation_pillar、同样 record_kind='OFFICIAL'），
    所以月层和旬/周层勾同一个框筛出来的是同一批柱，不会两套数。
    """
    groups = (("clash", CLASH_TYPES), ("harmony", HARMONY_TYPES), ("punish", PUNISH_TYPES),
              ("harm", HARM_TYPES), ("broken", BREAK_TYPES))
    need, ban = [], []
    for key, names in groups:
        if flt.get(key) == "1":
            need.append(names)
        elif flt.get(key) == "0":
            ban.append(names)
    if flt.get("both_clash_harmony") == "1":
        need += [CLASH_TYPES, HARMONY_TYPES]
    if not need and not ban:
        return None

    def hit(names):
        ph = ",".join("?" * len(names))
        return {r[0] for r in con.execute(
            "SELECT DISTINCT flow_pillar FROM stats_relation_pillar"
            f" WHERE identity_code=? AND record_kind='OFFICIAL' AND relation_type IN ({ph})",
            (identity, *names))}

    need_sets = [hit(n) for n in need]
    ban_sets = [hit(n) for n in ban]
    return lambda p: all(p in s for s in need_sets) and all(p not in s for s in ban_sets)


def window_layer_stock(con, layer: str, identity: str, flt: dict | None = None) -> list[dict]:
    """页面三「只看本票」：该身份自己在 月/旬/周 每一窗的历史统计。

    池层 window_layer_pool 是全类别加总，换一支票数字一模一样；这里读 xw_*_rules 里
    该 identity_code 自己的行，所以每一行都是这支票真实出现过的窗口。
    """
    flt = flt or {}
    tbl = WINDOW_RULES_TABLE.get(layer)
    if tbl is None:
        raise ValueError(f"未知窗口层级 {layer!r}；可用：month, xun, week")
    if not _xw_ready(con):
        return []
    src = _rows(con, f"SELECT * FROM {tbl} WHERE identity_code=?", (identity,))
    if not src:
        return []
    next_by_pillar = (calendar_context(con).get("next_by_pillar") or {})
    day_master, _dm_status = profile_day_master(con, identity)

    rows = []
    for r in src:
        pillar = r["flow_pillar"]
        if layer == "month":
            tag, pos = "整月", 0
        elif layer == "xun":
            to = r["day_to"] if r["day_to"] is not None else "下节"
            tag, pos = f"{r['xun_name']} {r['day_from']}–{to}", int(r["xun_no"] or 0)
        else:
            to = r["day_to"] if r["day_to"] is not None else "下节"
            tag, pos = f"第{r['week_no']}段 {r['day_from']}–{to}", int(r["week_no"] or 0)
        row = {
            "identity_code": r["identity_code"],
            "security_code": r["security_code"],
            "display_name": r["name"],
            "flow_pillar": pillar,
            "flow_stem": r["flow_stem"],
            "flow_branch": r["flow_branch"],
            "stem_element": STEM_ELEMENT_MAP.get(r["flow_stem"], ""),
            "branch_element": BRANCH_ELEMENT_MAP.get(r["flow_branch"], ""),
            "xun_no": r["xun_no"], "xun_name": r["xun_name"],
            "week_no": r["week_no"], "week_name": r["week_name"],
            "day_from": r["day_from"], "day_to": r["day_to"],
            "window_tag": tag, "window_pos": pos,
            "ALL_n_occurrences": r["ALL_n_occurrences"],
            "ALL_valid_n": r["ALL_valid_n"],
            "ALL_invalid_n": r["ALL_invalid_n"],
            "ALL_up_n": r["ALL_up_n"], "ALL_down_n": r["ALL_down_n"], "ALL_flat_n": r["ALL_flat_n"],
            "up_rate": r["ALL_up_rate"], "down_rate": r["ALL_down_rate"], "flat_rate": r["ALL_flat_rate"],
            "mean_body_return_ALL": r["ALL_mean_return_pct"],
            "median_body_return_ALL": r["ALL_median_return_pct"],
            "best_return_ALL": r["ALL_best_return_pct"],
            "worst_return_ALL": r["ALL_worst_return_pct"],
            "EXCLUDE_SINGLE_valid_n": r["EXCLUDE_SINGLE_valid_n"],
            "EXCLUDE_SINGLE_up_n": r["EXCLUDE_SINGLE_up_n"],
            "EXCLUDE_SINGLE_down_n": r["EXCLUDE_SINGLE_down_n"],
            "EXCLUDE_SINGLE_up_rate": r["EXCLUDE_SINGLE_up_rate"],
            "majority_direction": r["majority_direction"],
            "direction_rate": r["direction_rate"],
            "band": r["band"], "sample_grade": r["sample_grade"],
            "baseline_up_rate": r["baseline_up_rate"],
            "deviation_from_baseline_up_rate": r["deviation_from_baseline_up_rate"],
            "baseline_window_up_rate": r["baseline_window_up_rate"],
            "deviation_from_baseline_window_up_rate": r["deviation_from_baseline_window_up_rate"],
            "occurrence_count": r["ALL_n_occurrences"],
            "all_sample_n": r["ALL_valid_n"],
            "exclude_single_n": r["EXCLUDE_SINGLE_valid_n"],
            "sample_marker_ALL": r["sample_grade"],
            "rule_version": r["version"], "calendar_version": r["calendar_version"],
            "next_occurrence": next_by_pillar.get(pillar),
            "ord": PILLAR_ORD_MAP.get(pillar, 99) * 10 + pos,
        }
        attach_ten_god(row, day_master, pillar, keep_legacy=False)
        rows.append(row)

    direction = "down" if flt.get("dir") == "down" else "up"
    rate_raw = str(flt.get("rate_min") or "").strip()
    try:
        rate_min = float(rate_raw) / 100.0 if rate_raw and float(rate_raw) > 0 else 0.0
    except ValueError:
        rate_min = 0.0
    rate_eq = flt.get("rate_eq") == "1" or rate_min >= 1.0
    try:
        min_n = int(flt.get("min_n") or 0)
    except ValueError:
        min_n = 0
    p_filter = (flt.get("pillar") or "").strip()
    stem_el, branch_el = flt.get("stem_element"), flt.get("branch_element")
    rel_ok = _relation_pillar_filter(con, identity, flt)

    kept = []
    for r in rows:
        if p_filter and p_filter not in r["flow_pillar"]:
            continue
        if rel_ok and not rel_ok(r["flow_pillar"]):
            continue
        if stem_el and r["stem_element"] != stem_el:
            continue
        if branch_el and r["branch_element"] != branch_el:
            continue
        if min_n and (r["ALL_valid_n"] or 0) < min_n:
            continue
        rate = r["down_rate"] if direction == "down" else r["up_rate"]
        rate = rate if rate is not None else 0.0
        if rate_eq:
            if rate < 0.9999:
                continue
        elif rate_min > 0 and rate < rate_min:
            continue
        kept.append(r)

    sort_val = flt.get("sort") or "pillar"
    rev = -1 if direction == "up" else 1
    if sort_val in ("up_rate", "rate"):
        kept.sort(key=lambda x: (-(x["direction_rate"] or 0), x["ord"]))
    elif sort_val == "mean":
        kept.sort(key=lambda x: (rev * (x["mean_body_return_ALL"] or 0), x["ord"]))
    elif sort_val in ("n", "coverage"):
        kept.sort(key=lambda x: (-(x["ALL_valid_n"] or 0), x["ord"]))
    elif sort_val == "branch":
        kept.sort(key=lambda x: (BRANCH_ORD_MAP.get(x["flow_branch"], 99), x["ord"]))
    else:
        kept.sort(key=lambda x: x["ord"])
    return kept


def _attach_monthlike_fields(con, rows, layer, pillar, asset_class, direction, live_map=None):
    """把月层有、旬/周层缺的 5 列按**同一口径**补齐：
         当时胜率 / 同期超额 / 稳健度 / 一致性 / 历次年份
       月层这些量在一条 60 列聚合 SQL 里算；旬/周只对返回的（≤300）行取一次明细，
       然后调用同一套 stability_level() / consistency_reason() 和同一组阈值常量，
       保证同一只票同一根柱在月层与旬/周层得到相同的判定。"""
    import statistics
    if not rows:
        return rows
    tbl = "xun_occurrences" if layer == "xun" else "week_occurrences"
    wcol = "xun_no" if layer == "xun" else "week_no"
    ids = sorted({r["identity_code"] for r in rows})
    ph = ",".join("?" * len(ids))
    occ = _rows(con, f"""SELECT identity_code, {wcol} AS win, month_id, solar_year,
                                window_return AS ret, direction
                         FROM {tbl}
                         WHERE asset_class=? AND flow_pillar=? AND identity_code IN ({ph})
                           AND window_return IS NOT NULL""", [asset_class, pillar] + ids)
    idx = {}
    for r in _rows(con, f"""SELECT identity_code, {wcol} AS win, month_id, window_return AS ret
                            FROM {tbl}
                            WHERE asset_class='INDEX' AND flow_pillar=?
                              AND identity_code IN ('000001.SH', '399106.SZ')""", [pillar]):
        mk = "000001.SH" if r["identity_code"].endswith(".SH") else "399106.SZ"
        idx[(mk, r["month_id"], r["win"])] = r["ret"]
    groups = {}
    for o in occ:
        groups.setdefault((o["identity_code"], o["win"]), []).append(o)

    up = direction != "down"
    live_map = live_map or {}
    for row in rows:
        win = row["xun_no"] if layer == "xun" else row["week_no"]
        g = list(groups.get((row["identity_code"], win), []))
        live = live_map.get(((row["identity_code"] or "").split(".")[0], win))
        if live is not None:
            g.append({"identity_code": row["identity_code"], "win": win,
                      "month_id": live["month_id"], "solar_year": live["solar_year"],
                      "ret": live["ret"], "direction": live["direction"],
                      "valid_bars": live["n_bars"], "is_live": True})
        rets = [o["ret"] for o in g]
        n = len(rets)
        row["occurrence_years"] = ";".join(str(y) for y in sorted({o["solar_year"] for o in g}))
        row["stab_n"] = n
        row["median_body_return_ALL"] = statistics.median(rets) if rets else None
        row["sample_grade"] = ("0 次" if n == 0 else "1 次" if n == 1 else
                               "2 次" if n == 2 else "3–5 次" if n <= 5 else "≥6 次")
        if n:
            mx, mn = max(rets), min(rets)
            rng = mx - mn
            pos, neg = [x for x in rets if x > 0], [x for x in rets if x < 0]
            sum_pos, sum_neg = sum(pos), sum(neg)
            if up:
                margin = mn
                share = (mx / sum_pos) if sum_pos > 0 else None
            else:
                margin = -mx            # stability_level() 内部 actual = -margin = 最轻一次跌幅
                share = (abs(mn) / abs(sum_neg)) if sum_neg < 0 else None
            level, level_cn, reason = stability_level(direction, n, margin, share, rng)
            row.update(stability_level=level, stability_level_cn=level_cn, stability_reason=reason,
                       stab_margin=margin, stab_range=rng, stab_extreme_share=share,
                       stab_margin_ratio=(margin / max(rng, STAB_RANGE_FLOOR)),
                       stab_min_ret=mn, stab_max_ret=mx)
            k = len(pos if up else neg)
            row.update(cons_n=k, cons_total=n, cons_reverse_n=n - k, cons_worst_all=mn)
            if k >= 2:
                sel = pos if up else [-x for x in neg]
                mean_k = sum(sel) / k
                std_k = statistics.stdev(sel) if k > 1 else 0.0
                cv = (std_k / mean_k) if mean_k > 0 else None
                drop = ((mean_k - (sum(sel) - max(sel)) / (k - 1)) / mean_k) if k > 1 and mean_k > 0 else None
                row.update(cons_cv=cv, cons_drop=drop, cons_level=(
                    "—" if cv is None else ("一致" if cv <= 0.50 else ("差异大" if cv > 0.80 else "一般"))))
                row["consistency_reason"] = consistency_reason(
                    direction, k, n, cv, drop, min(sel), (min(sel) >= 0.01), mn, n - k)
            else:
                row.update(cons_cv=None, cons_drop=None, cons_level="—",
                           consistency_reason=f"方向样本不足（k={k}）")
        else:
            row.update(stability_level="none", stability_level_cn="—", stability_reason="样本不足，无法评估",
                       cons_level="—", consistency_reason="方向样本不足（k=0）", cons_cv=None)
        # 当时胜率：剔除所选月那一次（公式同月层 up_rate_excl_ALL）
        # 本月已走完的窗口已经并进上面的分母，这里同样把它作为「所选月那次」剔除。
        act = row.get("actual_return")
        act_dir = row.get("actual_direction")
        if act is None and live is not None:
            act_dir = live["direction"]
        has_sel = act is not None or live is not None
        denom = (row.get("ALL_valid_n") or 0) - (1 if has_sel else 0)
        hit = (row.get("ALL_up_n") if up else row.get("ALL_down_n")) or 0
        if has_sel:
            hit -= 1 if (act_dir == "UP" if up else act_dir == "DOWN") else 0
        row["up_rate_excl_ALL"] = (hit / denom) if denom > 0 else None
        # 同期超额：与同市场指数同窗做差（月层的 excess_return 同义）
        ex = [o["ret"] - idx[("000001.SH" if o["identity_code"].endswith(".SH") else "399106.SZ",
                              o["month_id"], o["win"])]
              for o in g
              if ("000001.SH" if o["identity_code"].endswith(".SH") else "399106.SZ",
                  o["month_id"], o["win"]) in idx]
        row["benchmark_n"] = len(ex)
        row["excess_mean"] = (sum(ex) / len(ex)) if ex else None
    return rows


def window_cell_detail(identity: str, pillar: str, layer: str, win_no) -> list[dict]:
    """某一格（标的 × 柱 × 层 × 窗位）的历次明细。
       抽屉里的卡片、条形图、"最差一次"必须和点开的那一层同源，否则会出现"上面是周、下面是整月"。"""
    con = connect_xun_week()
    try:
        if layer == "month":
            sql = """SELECT month_id, solar_year, CAST(first_ts AS VARCHAR) first_ts, CAST(last_ts AS VARCHAR) last_ts,
                            valid_bars, window_return AS ret, window_return AS body_return, window_return AS window_return, direction
                     FROM month_occurrences WHERE identity_code=? AND flow_pillar=? ORDER BY month_id"""
            return _rows(con, sql, (identity, pillar))
        tbl, col = ("xun_occurrences", "xun_no") if layer == "xun" else ("week_occurrences", "week_no")
        sql = f"""SELECT month_id, solar_year, CAST(first_ts AS VARCHAR) first_ts, CAST(last_ts AS VARCHAR) last_ts,
                         valid_bars, window_return AS ret, window_return AS body_return, window_return AS window_return, direction
                  FROM {tbl} WHERE identity_code=? AND flow_pillar=? AND {col}=? ORDER BY month_id"""
        return _rows(con, sql, (identity, pillar, int(win_no or 0)))
    finally:
        con.close()


def stock_window_rules(identity: str, pillar: str) -> dict:
    """个股月内窗位小表服务：返回该标的×该流月柱在 3旬 / 6周的统计规则与历次穿透记录。"""
    con = connect_xun_week()
    try:
        # 1. 旬规则 (3 窗)
        x_rows = _rows(con, """
            SELECT flow_pillar, xun_no, xun_name, day_from, day_to,
                   ALL_n_occurrences, ALL_valid_n, ALL_invalid_n,
                   ALL_up_n, ALL_down_n, ALL_flat_n, ALL_up_rate, ALL_down_rate,
                   ROUND(ALL_mean_return_pct, 2) as mean_return,
                   ROUND(ALL_median_return_pct, 2) as median_return
            FROM xun_rules
            WHERE (identity_code=? OR security_code=?) AND flow_pillar=?
            ORDER BY xun_no
        """, (identity, identity, pillar))

        for r in x_rows:
            day_to_str = str(r["day_to"]) if r["day_to"] is not None else "下节"
            r["window_tag"] = f"{r['xun_name']} {r['day_from']}–{day_to_str}"
            r["ALL_up_n"] = int(r["ALL_up_n"]) if r["ALL_up_n"] is not None else 0
            r["ALL_down_n"] = int(r["ALL_down_n"]) if r["ALL_down_n"] is not None else 0
            r["ALL_flat_n"] = int(r["ALL_flat_n"]) if r["ALL_flat_n"] is not None else 0

        # 2. 周规则 (6 窗)
        w_rows = _rows(con, """
            SELECT flow_pillar, xun_no, week_no, week_name, day_from, day_to,
                   ALL_n_occurrences, ALL_valid_n, ALL_invalid_n,
                   ALL_up_n, ALL_down_n, ALL_flat_n, ALL_up_rate, ALL_down_rate,
                   ROUND(ALL_mean_return_pct, 2) as mean_return,
                   ROUND(ALL_median_return_pct, 2) as median_return
            FROM week_rules
            WHERE (identity_code=? OR security_code=?) AND flow_pillar=?
            ORDER BY week_no
        """, (identity, identity, pillar))

        for r in w_rows:
            day_to_str = str(r["day_to"]) if r["day_to"] is not None else "下节"
            r["window_tag"] = f"第{r['week_no']}段 {r['day_from']}–{day_to_str}"
            r["ALL_up_n"] = int(r["ALL_up_n"]) if r["ALL_up_n"] is not None else 0
            r["ALL_down_n"] = int(r["ALL_down_n"]) if r["ALL_down_n"] is not None else 0
            r["ALL_flat_n"] = int(r["ALL_flat_n"]) if r["ALL_flat_n"] is not None else 0

        # 3. 旬逐次穿透证据
        x_occ = _rows(con, """
            SELECT solar_year, month_id, flow_pillar, xun_no, day_from, day_to,
                   valid_bars, first_ts, last_ts, qfq_open, qfq_close,
                   ROUND(window_return * 100, 2) as window_return_pct, direction
            FROM xun_occurrences
            WHERE (identity_code=? OR security_code=?) AND flow_pillar=?
            ORDER BY solar_year, xun_no
        """, (identity, identity, pillar))

        # 4. 周逐次穿透证据
        w_occ = _rows(con, """
            SELECT solar_year, month_id, flow_pillar, xun_no, week_no, day_from, day_to,
                   valid_bars, first_ts, last_ts, qfq_open, qfq_close,
                   ROUND(window_return * 100, 2) as window_return_pct, direction
            FROM week_occurrences
            WHERE (identity_code=? OR security_code=?) AND flow_pillar=?
            ORDER BY solar_year, week_no
        """, (identity, identity, pillar))

        return {
            "identity_code": identity,
            "pillar": pillar,
            "xun_rules": x_rows,
            "week_rules": w_rows,
            "xun_occ": x_occ,
            "week_occ": w_occ
        }
    finally:
        con.close()


# ---------------- 旬/周「同窗逐日路径」：历史各年 + 平均 + 本月进行中 ----------------
# 历史日线取自冻结源 nas_source.duckdb（raw × 累计因子）；当月线取自日更产出的当月层切片。
# 窗口边界规则：交节时刻后的第一个「09:30 归属日」= 第 0 天，旬/周按自然日偏移切 10 日 / 5 日。
# 该规则已用 8,636,434 条 xw_*_occurrences 全量核对（first_ts/last_ts 全部落在 [start,end] 内）。
LIVE_DB = Path(os.environ.get(
    "BAZI_LIVE_DB", str(config.ROOT / "source_input" / "live" / "current_month.sqlite")))
NAS_SOURCE_DB = Path(os.environ.get(
    "BAZI_NAS_SOURCE_DB", str(config.ROOT / "source_input" / "nas_source.duckdb")))
WIN_TABLES = {
    # 月层只有一个窗口：day_from/day_to 为空 = 交节后第 0 天 → 窗尾（整月）
    "month": ("xw_month_occurrences", None, "xw_month_rules", 1),
    "xun": ("xw_xun_occurrences", "xun_no", "xw_xun_rules", 3),
    "week": ("xw_week_occurrences", "week_no", "xw_week_rules", 6),
}


def _win_bounds(start_at, end_at, day_from, day_to):
    s, e = datetime.fromisoformat(start_at), datetime.fromisoformat(end_at)
    d0 = s.date() if s.time() < dt_time(9, 30) else s.date() + timedelta(days=1)
    start = d0 + timedelta(days=int(day_from or 0))
    if day_to is None:
        end = e.date() if e.time() >= dt_time(9, 30) else e.date() - timedelta(days=1)
    else:
        end = d0 + timedelta(days=int(day_to)) - timedelta(days=1)
    return start, end


def _calendar_map(con, month_ids):
    ids = sorted({m for m in month_ids if m})
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    return {r["month_id"]: (r["start_at"], r["end_at"]) for r in _rows(
        con, f"SELECT month_id, start_at, end_at FROM calendar_months WHERE month_id IN ({ph})", ids)}


_STOCK_DAILY_CACHE: dict[str, list] = {}


def _stock_daily(identity: str):
    """该身份的冻结历史日线（按日期升序）；返回 (rows, 原因)。不给页面任何写入口。"""
    if identity in _STOCK_DAILY_CACHE:
        return _STOCK_DAILY_CACHE[identity], ""
    if not NAS_SOURCE_DB.is_file():
        return None, f"历史日线源不存在：{NAS_SOURCE_DB}"
    try:
        import duckdb
    except Exception as exc:  # noqa: BLE001
        return None, f"当前 Python 环境没有 duckdb，读不了历史日线（{type(exc).__name__}）"
    try:
        d = duckdb.connect(str(NAS_SOURCE_DB), read_only=True)
        try:
            rows = d.execute(
                "SELECT b.date, b.open, b.close, f.adj_factor FROM bars b "
                "JOIN factors f ON f.code=b.code AND f.date=b.date "
                "WHERE b.code=? ORDER BY b.date", [identity]).fetchall()
        finally:
            d.close()
    except Exception as exc:  # noqa: BLE001
        return None, f"历史日线读取失败（{type(exc).__name__}: {exc}）"
    out = []
    for dt_, o, c, fa in rows:
        if o is None or c is None or fa is None:
            continue
        out.append((str(dt_)[:10], float(o), float(c), float(fa)))
    if len(_STOCK_DAILY_CACHE) >= 24:
        _STOCK_DAILY_CACHE.clear()
    _STOCK_DAILY_CACHE[identity] = out
    return out, ""


def _live_meta():
    if not LIVE_DB.is_file():
        return {}
    try:
        lc = sqlite3.connect(LIVE_DB.resolve().as_uri() + "?mode=ro", uri=True)
        lc.row_factory = sqlite3.Row
        try:
            return {r["k"]: r["v"] for r in lc.execute("SELECT k, v FROM meta")}
        finally:
            lc.close()
    except sqlite3.Error:
        return {}


_LIVE_META_CACHE: dict = {"sig": None, "meta": {}}


def _live_meta_cached() -> tuple[dict, tuple]:
    """日更切片元信息 + 文件签名。签名进机会表缓存键：19:05 日更换文件后旧响应自动失效。"""
    try:
        st = LIVE_DB.stat()
    except OSError:
        return {}, ()
    sig = (st.st_mtime_ns, st.st_size)
    if _LIVE_META_CACHE.get("sig") == sig:
        return _LIVE_META_CACHE.get("meta") or {}, sig
    meta = _live_meta()
    _LIVE_META_CACHE["sig"] = sig
    _LIVE_META_CACHE["meta"] = meta
    return meta, sig


_LIVE_WIN_CACHE: dict = {}
_ADJ_COLS = (
    "asset_class", "identity_code", "security_code", "name", "flow_pillar",
    "xun_no", "xun_name", "week_no", "week_name", "day_from", "day_to",
    "ALL_n_occurrences", "ALL_valid_n", "ALL_invalid_n",
    "ALL_up_n", "ALL_down_n", "ALL_flat_n",
    "ALL_up_rate", "ALL_down_rate", "ALL_flat_rate",
    "ALL_mean_return_pct", "ALL_median_return_pct",
    "ALL_worst_return_pct", "ALL_best_return_pct",
    "EXCLUDE_SINGLE_valid_n", "EXCLUDE_SINGLE_up_n",
    "EXCLUDE_SINGLE_down_n", "EXCLUDE_SINGLE_up_rate",
    "EXCLUDE_SINGLE_mean_return_pct", "sample_grade", "band",
)
_LIVE_WIN_SPECS = {
    "month": ((1, 0, None),),
    "xun": ((1, 0, 10), (2, 10, 20), (3, 20, None)),
    "week": ((1, 0, 5), (2, 5, 10), (3, 10, 15), (4, 15, 20), (5, 20, 25), (6, 25, None)),
}


def _live_window_table(layer, month_id, start_at, end_at):
    """读一次当月日更切片，算出本月各窗每只票的实际结果；按切片签名缓存。"""
    meta, sig = _live_meta_cached()
    live_mid = str(meta.get("month_id") or "")
    asof = str(meta.get("latest_trade_date") or "")
    if live_mid != month_id:
        return None, meta, f"日更切片停在 {live_mid or '—'}，还没更新到 {month_id}"
    if not asof:
        return None, meta, "日更切片没有最新交易日"
    specs = _LIVE_WIN_SPECS.get(layer) or _LIVE_WIN_SPECS["month"]
    key = (sig, layer, month_id, str(start_at), str(end_at))
    cached = _LIVE_WIN_CACHE.get(key)
    if cached is not None:
        return cached, meta, ""
    try:
        lc = sqlite3.connect(LIVE_DB.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            dates = [str(x[0])[:10] for x in lc.execute(
                "SELECT DISTINCT date FROM bars ORDER BY date").fetchall()]
            bars: dict[str, list] = {}
            for code, d, o, c in lc.execute(
                    "SELECT code, date, open, close FROM bars ORDER BY code, date").fetchall():
                if o is None or c is None:
                    continue
                bars.setdefault(str(code), []).append((str(d)[:10], float(o), float(c)))
        finally:
            lc.close()
    except sqlite3.Error as exc:
        return None, meta, f"当月层读取失败（{type(exc).__name__}: {exc}）"

    windows = {}
    for win, day_from, day_to in specs:
        ws, we = _win_bounds(start_at, end_at, day_from, day_to)
        wsd, wes = ws.isoformat(), we.isoformat()
        wdates = [d for d in dates if wsd <= d <= wes]
        if asof < wsd:
            status = "waiting"
        elif asof >= wes:
            status = "done"
        else:
            status = "ongoing"
        windows[win] = {"start": wsd, "end": wes, "status": status,
                        "elapsed": sum(1 for d in wdates if d <= asof),
                        "total_days": len(wdates)}
    by_code: dict[str, dict] = {}
    for code, pts in bars.items():
        entry = {}
        for win, w in windows.items():
            sel = [p for p in pts if w["start"] <= p[0] <= w["end"] and p[0] <= asof]
            if not sel:
                entry[win] = {"ret": None, "n_bars": 0, "direction": None}
                continue
            base = sel[0][1]
            ret = (sel[-1][2] / base - 1) if base else None
            direction = ("UP" if (ret or 0) > 0 else "DOWN" if (ret or 0) < 0 else "FLAT") \
                if ret is not None else None
            entry[win] = {"ret": round(ret, 6) if ret is not None else None,
                          "n_bars": len(sel), "direction": direction}
        by_code[code] = entry
    table = {"windows": windows, "by_code": by_code, "asof": asof,
             "month_id": live_mid, "days": len(dates),
             "pillar": meta.get("pillar"), "built_at_bjt": meta.get("built_at_bjt")}
    if len(_LIVE_WIN_CACHE) > 8:
        _LIVE_WIN_CACHE.clear()
    _LIVE_WIN_CACHE[key] = table
    return table, meta, ""


def _build_adj_rules(con, layer, pillar, xw_class, only_win, live_table) -> int:
    """把「本月已走完窗口」补进规则聚合，写进 TEMP 表 adj_rules。

    只改当前柱/当前窗的聚合值，不碰正式库；TEMP 表用完即随连接销毁。
    返回写入行数。"""
    rules = _window_rules_source(con, layer)
    win_col = "xun_no" if layer == "xun" else "week_no"
    cols = ", ".join(_ADJ_COLS)
    where = "flow_pillar=? AND asset_class=?"
    args = [pillar, xw_class]
    if only_win is not None:
        where += f" AND {win_col}=?"
        args.append(only_win)
    rows = con.execute(f"SELECT {cols} FROM {rules} WHERE {where}", args).fetchall()
    live = {}
    for win, w in live_table["windows"].items():
        if w["status"] != "done":
            continue
        for code, entry in live_table["by_code"].items():
            cell = entry.get(win)
            if cell is not None:
                live[(code, win)] = cell
    out = []
    for r in rows:
        d = dict(r)
        cell = live.get((str(d["identity_code"])[:6], d[win_col]))
        if cell is None:
            out.append(tuple(d[c] for c in _ADJ_COLS))
            continue
        n = int(cell.get("n_bars") or 0)
        ret = cell.get("ret")
        direction = cell.get("direction")
        valid = 1 if (n > 0 and ret is not None) else 0
        up = 1 if (valid and direction == "UP") else 0
        down = 1 if (valid and direction == "DOWN") else 0
        flat = 1 if (valid and direction == "FLAT") else 0
        valid_es = 1 if (valid and n > 1) else 0
        old_valid = int(d["ALL_valid_n"] or 0)
        new_valid = old_valid + valid
        new_up = int(d["ALL_up_n"] or 0) + up
        new_down = int(d["ALL_down_n"] or 0) + down
        new_flat = int(d["ALL_flat_n"] or 0) + flat
        d["ALL_n_occurrences"] = int(d["ALL_n_occurrences"] or 0) + 1
        d["ALL_valid_n"] = new_valid
        d["ALL_invalid_n"] = int(d["ALL_invalid_n"] or 0) + (1 - valid)
        d["ALL_up_n"], d["ALL_down_n"], d["ALL_flat_n"] = new_up, new_down, new_flat
        d["ALL_up_rate"] = (new_up / new_valid) if new_valid else None
        d["ALL_down_rate"] = (new_down / new_valid) if new_valid else None
        d["ALL_flat_rate"] = (new_flat / new_valid) if new_valid else None
        if new_valid:
            d["ALL_mean_return_pct"] = (
                float(d["ALL_mean_return_pct"] or 0) * old_valid
                + (float(ret) * 100.0 if valid else 0.0)) / new_valid
        if valid:
            rp = float(ret) * 100.0
            worst, best = d["ALL_worst_return_pct"], d["ALL_best_return_pct"]
            d["ALL_worst_return_pct"] = rp if worst is None else min(float(worst), rp)
            d["ALL_best_return_pct"] = rp if best is None else max(float(best), rp)
        old_es_valid = int(d["EXCLUDE_SINGLE_valid_n"] or 0)
        es_valid = old_es_valid + valid_es
        es_up = int(d["EXCLUDE_SINGLE_up_n"] or 0) + (up if valid_es else 0)
        es_down = int(d["EXCLUDE_SINGLE_down_n"] or 0) + (down if valid_es else 0)
        d["EXCLUDE_SINGLE_valid_n"] = es_valid
        d["EXCLUDE_SINGLE_up_n"], d["EXCLUDE_SINGLE_down_n"] = es_up, es_down
        d["EXCLUDE_SINGLE_up_rate"] = (es_up / es_valid) if es_valid else None
        if es_valid:
            d["EXCLUDE_SINGLE_mean_return_pct"] = (
                float(d["EXCLUDE_SINGLE_mean_return_pct"] or 0) * old_es_valid
                + (float(ret) * 100.0 if valid_es else 0.0)) / es_valid
        d["sample_grade"] = ("0 次" if new_valid == 0 else "1 次" if new_valid == 1 else
                             "2 次" if new_valid == 2 else "3–5 次" if new_valid <= 5 else "≥6 次")
        out.append(tuple(d[c] for c in _ADJ_COLS))

    con.execute("PRAGMA query_only=OFF")
    try:
        con.execute("DROP TABLE IF EXISTS temp.adj_rules")
        con.execute("CREATE TEMP TABLE adj_rules (" + ", ".join(_ADJ_COLS) + ")")
        con.executemany("INSERT INTO adj_rules VALUES (" + ",".join("?" * len(_ADJ_COLS)) + ")",
                        out)
    finally:
        con.execute("PRAGMA query_only=ON")
    return len(out)


def _live_overlay_map(layer, month_id, start_at, end_at):
    """已走完且有行情的窗口 → 供 Python 侧重算稳健度/一致性/历次年份。"""
    table, _, _ = _live_window_table(layer, month_id, start_at, end_at)
    if table is None:
        return {}
    year = int(str(month_id)[:4]) if str(month_id)[:4].isdigit() else 0
    out = {}
    for win, w in table["windows"].items():
        if w["status"] != "done":
            continue
        for code, entry in table["by_code"].items():
            cell = entry.get(win) or {}
            if int(cell.get("n_bars") or 0) <= 0 or cell.get("ret") is None:
                continue
            out[(code, win)] = {"ret": cell["ret"], "n_bars": cell["n_bars"],
                                "direction": cell["direction"],
                                "month_id": month_id, "solar_year": year}
    return out


def _attach_live_current(rows, layer, month_id, pillar, start_at, end_at) -> dict:
    """给机会表行挂 live_actual（只读日更切片；统计口径由调用方决定是否并进历史）。"""
    if not rows:
        return {"status": "empty", "reason": "本屏没有行"}
    if not LIVE_DB.is_file():
        return {"status": "not_ready", "reason": f"当月层切片不存在：{LIVE_DB}"}
    table, meta, why = _live_window_table(layer, month_id, start_at, end_at)
    if table is None:
        return {"status": "stale" if "停在" in why else "not_ready",
                "reason": why,
                "month_id": str(meta.get("month_id") or ""),
                "latest_trade_date": str(meta.get("latest_trade_date") or "")}
    asof = table["asof"]
    windows = table["windows"]
    attached = 0
    for r in rows:
        if r.get("flow_pillar") != pillar:
            continue
        if layer == "month":
            win = 1
        elif layer == "xun":
            win = int(r.get("xun_no") or 0)
        else:
            win = int(r.get("week_no") or 0)
        w = windows.get(win)
        if not w:
            continue
        base_info = {"window_start": w["start"], "window_end": w["end"],
                     "latest_trade_date": asof, "month_id": table["month_id"],
                     "source": "current_month.sqlite"}
        if w["status"] == "waiting":
            r["live_actual"] = {**base_info, "status": "waiting"}
            attached += 1
            continue
        code = (r.get("identity_code") or "").split(".")[0]
        cell = (table["by_code"].get(code) or {}).get(win) or {}
        if not cell.get("n_bars"):
            r["live_actual"] = {**base_info, "status": "no_bars"}
            attached += 1
            continue
        r["live_actual"] = {
            **base_info,
            "status": w["status"],
            "ret": cell["ret"],
            "direction": cell["direction"],
            "asof": min(asof, w["end"]),
            "n_bars": cell["n_bars"],
            "elapsed": w["elapsed"],
            "total_days": w["total_days"],
        }
        attached += 1
    return {"status": "ok", "month_id": table["month_id"],
            "pillar": table["pillar"] or pillar,
            "latest_trade_date": asof,
            "built_at_bjt": table["built_at_bjt"],
            "days": table["days"], "attached": attached,
            "source": "current_month.sqlite"}


def _natural_week(identity):
    """本周（自然周 周一→周日）的实际逐日收盘；只作当日走势，不参与旬/周规则统计。"""
    meta = _live_meta()
    asof = str(meta.get("latest_trade_date") or "")
    if not asof:
        return None
    try:
        a = datetime.fromisoformat(asof).date()
        mon = a - timedelta(days=a.weekday())
        sun = mon + timedelta(days=6)
        today = datetime.now(TZ).date()
        lc = sqlite3.connect(LIVE_DB.resolve().as_uri() + "?mode=ro", uri=True)
        lc.row_factory = sqlite3.Row
        try:
            rows = lc.execute(
                "SELECT date, open, close FROM bars WHERE code=? AND date>=? AND date<=? ORDER BY date",
                (identity.split(".")[0], mon.isoformat(), min(a, sun).isoformat())).fetchall()
        finally:
            lc.close()
    except sqlite3.Error:
        return None
    out = {"start": mon.isoformat(), "end": sun.isoformat(), "asof": asof,
           "today": today.isoformat(),
           "today_x": round((today - mon).days / 6, 3) if mon <= today <= sun else None,
           "built_at_bjt": meta.get("built_at_bjt")}
    if not rows:
        return {**out, "n_bars": 0, "pts": []}
    base = float(rows[0]["open"])
    span = max(1, (sun - mon).days)
    pts = [[0.0, 0]]
    for b in rows:
        x = (datetime.fromisoformat(b["date"] + "T09:30:00")
             - datetime.fromisoformat(mon.isoformat() + "T09:30:00")).total_seconds() / (span * 86400)
        pts.append([round(max(0.0, min(1.0, x)), 3),
                    int(round((float(b["close"]) / base - 1) * 10000))])
    return {**out, "n_bars": len(rows), "pts": pts, "e_bp": pts[-1][1],
            "first": rows[0]["date"], "last": rows[-1]["date"]}


def _live_snapshot(identity, pillar, rule_day_from, rule_day_to, cur, month_id):
    """当月层切片 + 当前窗口的逐日线。状态一定是 started/done 之外显式的一种，不猜。"""
    if cur is None:
        return {"status": "no_current_month"}
    if month_id and month_id != cur["month_id"]:
        return {"status": "not_current_month",
                "reason": f"所选是 {month_id}，不是本月 {cur['month_id']}"}
    if cur["month_pillar"] != pillar:
        return {"status": "not_current_pillar",
                "reason": f"当前流月是 {cur['month_pillar']}，本柱不是当前柱"}
    if not LIVE_DB.is_file():
        return {"status": "live_not_ready",
                "reason": "当月层尚未生成（source_input/live/current_month.sqlite 不存在）"}
    try:
        lc = sqlite3.connect(LIVE_DB.resolve().as_uri() + "?mode=ro", uri=True)
        lc.row_factory = sqlite3.Row
        try:
            meta = {r["k"]: r["v"] for r in lc.execute("SELECT k, v FROM meta")}
            if str(meta.get("month_id") or "") != cur["month_id"]:
                return {"status": "live_not_ready",
                        "reason": f"当月层还停在 {meta.get('month_id') or '—'}，尚未更新到本月"}
            asof = str(meta.get("latest_trade_date") or "")
            if not asof:
                return {"status": "live_not_ready", "reason": "当月层没有最新交易日"}
            ws, we = _win_bounds(cur["start_at"], cur["end_at"], rule_day_from, rule_day_to)
            out = {"start": ws.isoformat(), "end": we.isoformat(), "asof": asof,
                   "month_id": meta.get("month_id"), "built_at_bjt": meta.get("built_at_bjt"),
                   "source": "current_month.sqlite"}
            if asof < ws.isoformat():
                out.update(status="waiting", days_to_start=(ws - datetime.fromisoformat(asof).date()).days)
                return out
            last = min(asof, we.isoformat())
            bars = lc.execute(
                "SELECT date, open, close FROM bars WHERE code=? AND date>=? AND date<=? ORDER BY date",
                (identity.split(".")[0], ws.isoformat(), last)).fetchall()
            out["status"] = "done" if asof >= we.isoformat() else "ongoing"
            if not bars:
                out.update(n_bars=0, pts=[], no_bars=True)
                return out
            base = float(bars[0]["open"])
            span = max(1, (we - ws).days)
            pts = [[0.0, 0]]
            rets = []
            for b in bars:
                x = (datetime.fromisoformat(b["date"] + "T09:30:00")
                     - datetime.fromisoformat(ws.isoformat() + "T09:30:00")).total_seconds() \
                    / (span * 86400)
                r = float(b["close"]) / base - 1
                pts.append([round(max(0.0, min(1.0, x)), 3), int(round(r * 10000))])
                rets.append(r)
            out.update(n_bars=len(bars), pts=pts, e_bp=pts[-1][1],
                       lk=min(range(len(rets)), key=lambda i: rets[i]) + 1,
                       hk=max(range(len(rets)), key=lambda i: rets[i]) + 1)
            return out
        finally:
            lc.close()
    except sqlite3.Error as exc:
        return {"status": "live_not_ready", "reason": f"当月层读取失败（{type(exc).__name__}）"}


def window_path(identity: str, pillar: str, layer: str, win_no, month_id: str | None = None,
                dataset: str = DEFAULT_DATASET) -> dict:
    """某一格（标的 × 柱 × 层 × 窗位）的逐日路径。

    历史：该身份在该柱、该窗位的每一次历史出现，各自从「窗首日开盘 = 0%」出发；
    当月：当前流月的同一窗位（进行中/已完成/未开始/不是当前柱，四态明确）。
    """
    layer = str(layer).lower()
    if layer not in WIN_TABLES:
        layer = "week"
    tbl, wcol, rules_tbl, max_win = WIN_TABLES[layer]
    if layer == "month":
        win_no = 1
    else:
        try:
            win_no = int(win_no)
        except (TypeError, ValueError):
            raise ValueError("窗位必须是整数 1..6")
        if not 1 <= win_no <= max_win:
            raise ValueError(f"{'旬' if layer == 'xun' else '段'}层窗位必须在 1..{max_win} 之间")
    win_sql = f" AND {wcol}=?" if wcol else ""
    win_params = (win_no,) if wcol else ()
    asset = class_of_dataset(dataset)
    con = connect_xun_week()
    try:
        occ = _rows(con, f"""SELECT month_id, solar_year, day_from, day_to, valid_bars,
                                    window_return, direction
                             FROM {tbl}
                             WHERE identity_code=? AND flow_pillar=?{win_sql}
                               AND asset_class=? AND valid_bars>0
                               AND window_return IS NOT NULL
                             ORDER BY month_id""",
                    (identity, pillar) + win_params + (asset,))
        rules = _rows(con, f"""SELECT * FROM {rules_tbl}
                               WHERE identity_code=? AND flow_pillar=?{win_sql} AND asset_class=?
                               LIMIT 1""", (identity, pillar) + win_params + (asset,))
        root = rules[0] if rules else {}
        stamp = datetime.now(TZ).isoformat(timespec="microseconds")
        cur = _rows(con, "SELECT month_id, start_at, end_at, month_pillar FROM calendar_months"
                         " WHERE start_at<=? AND end_at>? ORDER BY start_at DESC LIMIT 1",
                    (stamp, stamp))
        nxt = _rows(con, "SELECT month_id, start_at, start_term FROM calendar_months"
                         " WHERE month_pillar=? AND start_at>? ORDER BY start_at LIMIT 1",
                    (pillar, stamp))
        cals = _calendar_map(con, [o["month_id"] for o in occ])
    finally:
        con.close()

    cur = cur[0] if cur else None
    day_from, day_to = root.get("day_from"), root.get("day_to")
    if layer == "month":
        day_from, day_to = 0, None
    elif day_from is None or (layer == "week" and win_no < 6 and day_to is None) \
            or (layer == "xun" and win_no < 3 and day_to is None):
        step = 5 if layer == "week" else 10
        day_from = (win_no - 1) * step
        day_to = None if win_no == max_win else win_no * step
    if layer == "month":
        win_name = root.get("xun_name") or "全月"
    elif layer == "xun":
        win_name = root.get("xun_name") or f"第{win_no}旬"
    else:
        win_name = f"第{win_no}段"
    stats = {"up_n": root.get("ALL_up_n"), "down_n": root.get("ALL_down_n"),
             "flat_n": root.get("ALL_flat_n"), "valid_n": root.get("ALL_valid_n"),
             "up_rate": root.get("ALL_up_rate"),
             "mean_return_pct": root.get("ALL_mean_return_pct"),
             "day_from": day_from, "day_to": day_to}
    base = {"identity_code": identity, "name": root.get("name") or identity,
            "pillar": pillar, "layer": layer, "win_no": win_no, "win_name": win_name,
            "stats": stats, "n_hist": len(occ),
            "current_pillar": cur["month_pillar"] if cur else None,
            "current_month": cur["month_id"] if cur else None,
            "next_occurrence": nxt[0] if nxt else None,
            "natural_week": _natural_week(identity) if asset == "A_SHARE" else None,
            "source_hist": f"{NAS_SOURCE_DB.name}（冻结至 2026-10-14）"}

    if asset == "ETF":
        return {**base, "available": False,
                "reason": "ETF 日线不在本机（本地只有月/旬/段统计）。本图先只支持 A 股。"}
    if asset == "INDEX":
        return {**base, "available": False,
                "reason": "指数点位不可直接交易；本图先只支持 A 股个股。"}
    if asset != "A_SHARE":
        return {**base, "available": False, "reason": f"不支持的标的类别：{asset}"}
    if not occ:
        return {**base, "available": False, "reason": "该格没有可绘制的历次窗口。"}

    daily, why = _stock_daily(identity)
    if daily is None:
        return {**base, "available": False, "reason": why}

    import bisect
    dates = [x[0] for x in daily]
    hist, mismatch = [], 0
    for o in occ:
        se = cals.get(o["month_id"])
        if not se:
            continue
        ws, we = _win_bounds(se[0], se[1], o["day_from"], o["day_to"])
        lo = bisect.bisect_left(dates, ws.isoformat())
        hi = bisect.bisect_right(dates, we.isoformat())
        bars = daily[lo:hi]
        if not bars:
            continue
        if len(bars) != int(o["valid_bars"] or 0):
            mismatch += 1
        b0 = bars[0][1] * bars[0][3]
        span = max(1, (we - ws).days)
        pts = [[0.0, 0]]
        rets = []
        for d_, _, c_, fa_ in bars:
            x = (datetime.fromisoformat(d_ + "T09:30:00")
                 - datetime.fromisoformat(ws.isoformat() + "T09:30:00")).total_seconds() \
                / (span * 86400)
            r = (c_ * fa_) / b0 - 1
            pts.append([round(max(0.0, min(1.0, x)), 3), int(round(r * 10000))])
            rets.append(r)
        hist.append({"year": o["solar_year"], "month_id": o["month_id"],
                     "start": ws.isoformat(), "end": we.isoformat(),
                     "first": bars[0][0], "last": bars[-1][0],
                     "n": len(bars), "stored_n": int(o["valid_bars"] or 0),
                     "e_bp": pts[-1][1],
                     "stored_bp": int(round((o["window_return"] or 0) * 10000)),
                     "lk": min(range(len(rets)), key=lambda i: rets[i]) + 1,
                     "hk": max(range(len(rets)), key=lambda i: rets[i]) + 1,
                     "dir": o["direction"], "pts": pts})

    if not hist:
        return {**base, "available": False,
                "reason": "历次窗口的日线在冻结源里都缺失（或复权因子不全），无法绘制。"}
    return {**base, "available": True, "hist": hist, "mismatch": mismatch,
            "live": _live_snapshot(identity, pillar, day_from, day_to, cur, month_id)}
