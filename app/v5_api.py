"""只读接入 V5 月内低点研究**切片**（v5_lowpoint_slice.sqlite）。

本模块是「八 · 月内低点研究」栏目的唯一数据入口：全部语句都是参数化 SELECT，标识符只来自
下方常量白名单；连接固定 `mode=ro` + `PRAGMA query_only=ON`，只用标准库 sqlite3，
所以 8737 那个被 PEP 668 封死 pip 的 Homebrew Python 也能直接跑。

切片由 tools/export_v5_slice.py 从 22GB 的 v5_results.duckdb 一次性导出（V5 已冻结，
截止 2026-10-14），切片内 v5_meta 记录源库整文件 sha256 等血缘。
定义一律以 fullmarket_lowpoint_research_v5/00_V5_预注册与数据口径冻结.md 为唯一权威；
标了 self_computed 的字段是本栏目自算，不属于 V5 冻结结论。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

RESEARCH_VERSION = "V5_LOWPOINT_20261019"
DATA_CUTOFF = "2026-10-14"
LAST_FORMAL_MONTH = "2026-M08"
VERDICT = "NO_SUPPORTED_LOWPOINT_SIGNAL"
VERDICT_CN = ("历史低点位置存在差异，但尚无通过训练、验证和历史留出的可靠低点入场信号。"
              "以下全部为事后描述性统计，不是预测买点，不构成买入建议。")

WORKSPACE = Path(__file__).resolve().parent.parent
SLICE_DB = Path(os.environ.get("V5_SLICE_DB", str(WORKSPACE / "data" / "v5_lowpoint_slice.sqlite")))

BRANCH_ORDER = ["寅", "卯", "辰", "巳", "午", "未", "申", "酉", "戌", "亥", "子", "丑"]
BRANCH_CN = dict(zip(BRANCH_ORDER, ["第1月 立春起", "第2月 惊蛰起", "第3月 清明起", "第4月 立夏起",
                                    "第5月 芒种起", "第6月 小暑起", "第7月 立秋起", "第8月 白露起",
                                    "第9月 寒露起", "第10月 立冬起", "第11月 大雪起", "第12月 小寒起"]))
# 本栏目只读展示 V5 冻结组表，n<3/6 的组由冻结字段 group_reliability 标「样本不足」，不在此处拦截
MIN_N = {"B12": 1, "P60": 1}
FAMILIES = ("STOCK", "ETF", "INDEX")
CONDITIONS = ("ALL_VALID", "UP_ONLY_HINDSIGHT", "DOWN_ONLY_HINDSIGHT",
              "ALL_UP_HINDSIGHT", "PIT_ALL_UP", "PIT_GE80_UP")
CONDITION_LABEL = {
    "ALL_VALID": "全部有效", "UP_ONLY_HINDSIGHT": "事后上涨", "DOWN_ONLY_HINDSIGHT": "事后下跌",
    "ALL_UP_HINDSIGHT": "历史全上涨（事后）", "PIT_ALL_UP": "PIT 事前全上涨",
    "PIT_GE80_UP": "PIT 事前≥80%",
}
CONDITION_KIND = {
    "ALL_VALID": "DESCRIPTIVE", "UP_ONLY_HINDSIGHT": "HINDSIGHT",
    "DOWN_ONLY_HINDSIGHT": "HINDSIGHT", "ALL_UP_HINDSIGHT": "HINDSIGHT",
    "PIT_ALL_UP": "POINT_IN_TIME", "PIT_GE80_UP": "POINT_IN_TIME",
}
SEG_CN = {"first": "月初", "middle": "月中", "last": "月末"}
FAMILY_NOTE = {
    "STOCK": "股票原始源无成交量：全部交易标 EXECUTION_UNVERIFIED，不假定开盘可无限成交。",
    "ETF": "只覆盖现存 ETF 历史（ETF_DELISTED_HISTORY_UNAVAILABLE），结论不得外推为历史全部 ETF。",
    "INDEX": "指数点位不可直接交易；V5 只有 000001.SH 与 399106.SZ，且无组统计/并列子表/复权列。",
}
# 组表可排序列：只允许这些标识符进 ORDER BY，值一律走参数
GROUP_SORTS = {
    "n": "n_windows", "up_rate": "n_up * 1.0 / NULLIF(n_windows,0)", "mean": "mean_ret",
    "early": "mean_low_fraction", "late": "-mean_low_fraction",
    "excess": "mean_excess_earliness", "years": "n_years",
}
MONTH_SQL = """SELECT * FROM v5_months WHERE family=? AND security_id=? AND formal=1"""


class V5Error(RuntimeError):
    """区分「切片/驱动不可用」与「该标的确实没有记录」。"""


class V5Unavailable(V5Error):
    pass


_tl = threading.local()


def _db_signature():
    """切片库文件的身份指纹：inode + mtime(ns) + size。文件被原子替换后必然变化。

    取不到（文件不存在/无权限）时返回 None ⇒ 连接会被丢弃重开，由 _connect 给出原有
    「切片不存在」的结构化 V5Unavailable，绝不继续返回旧 inode 的内容。
    """
    try:
        st = SLICE_DB.stat()
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def _connect():
    """每线程一条只读连接：sqlite3 对象不允许跨线程使用，而服务是多线程的。

    线程本地长连接从不重开，会把「换库之后」的读快照一直钉在旧 inode 上，所以这里加了对
    同一数据库文件 inode/mtime 的校验：文件被替换（自动更新走 临时库+`os.replace`）或
    mtime/size 变了，就丢弃这条连接重开。SQL 语句与返回结构一律不变。
    """
    con = getattr(_tl, "con", None)
    sig = _db_signature()
    if con is not None:
        if sig is not None and getattr(_tl, "sig", None) == sig:
            return con
        try:
            con.close()
        except sqlite3.Error:
            pass
        _tl.con = None
    if not SLICE_DB.is_file():
        raise V5Unavailable(
            f"V5 只读切片不存在：{SLICE_DB}\n请先跑 tools/export_v5_slice.py 导出。")
    con = sqlite3.connect(SLICE_DB.resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    if str(con.execute("PRAGMA query_only").fetchone()[0]) != "1":
        con.close()
        raise V5Unavailable("切片连接未生效 query_only，已拒绝使用")
    _tl.con = con
    _tl.sig = _db_signature()
    return con


def _q(sql, params=()):
    return [dict(r) for r in _connect().execute(sql, tuple(params)).fetchall()]


def _meta(key, default=None):
    r = _q("SELECT value FROM v5_meta WHERE key=?", (key,))
    return r[0]["value"] if r else default


# ---------------- 身份与族系 ----------------

def resolve_family(symbol: str, dataset: str) -> tuple[str, dict]:
    """按页面数据集偏好选族，避免 A股/ETF 串台；查不到返回 NONE。支持6位数字补全与名称匹配。"""
    prefer = ("ETF", "STOCK") if dataset == "etf" else ("STOCK", "ETF")
    sym = str(symbol or "").strip()
    if not sym:
        return "NONE", {}
    for fam in (*prefer, *(f for f in FAMILIES if f not in prefer)):
        rows = _q("SELECT * FROM v5_security WHERE family=? AND security_id=?", (fam, sym))
        if rows:
            return fam, rows[0]
        if len(sym) == 6 and sym.isdigit():
            rows = _q("SELECT * FROM v5_security WHERE family=? AND (security_id=? OR security_id LIKE ?)",
                      (fam, sym, sym + ".%"))
            if rows:
                return fam, rows[0]
        rows = _q("SELECT * FROM v5_security WHERE family=? AND name=?", (fam, sym))
        if rows:
            return fam, rows[0]
    return "NONE", {}


# ---------------- 概览 ----------------

def summary(symbol: str, dataset: str) -> dict:
    fam, sec = resolve_family(symbol, dataset)
    if fam == "NONE":
        return {"symbol": symbol, "family": "NONE", "available": False,
                "reason": "V5_NOT_IN_SCOPE",
                "reason_cn": "该标的不属于 V5 冻结样本（股票 5,561 / 现存 ETF 1,698 / 指数 2 条序列）。"}
    symbol = sec["security_id"]
    n_formal = sec["n_formal"] or 0
    ov = {k: sec.get(k) for k in (
        "all_months", "n_formal", "n_up", "n_down", "n_flat", "first_month_all", "last_month_all",
        "first_formal", "last_formal", "n_years", "mean_low_fraction", "mean_low_k",
        "mean_open_fraction", "mean_ret", "seg_first", "seg_middle", "seg_last", "n_low_tie",
        "n_low_ne_open", "n_g1", "n_g2", "n_g3", "n_partial", "n_suspended")}
    med = _q("SELECT median_low_fraction, median_low_k FROM v5_security "
             "WHERE family=? AND security_id=?", (fam, symbol))
    ov["median_low_fraction"] = med[0]["median_low_fraction"] if med else None
    ov["median_low_k"] = med[0]["median_low_k"] if med else None
    ov["low_ne_open_basis"] = ("low_k<>open_k（切片统一口径）" if fam != "INDEX"
                               else "指数无 low_is_open_min 列，此处为 low_k<>open_k 代理口径")
    seg_sum = sum(ov.get(k) or 0 for k in ("seg_first", "seg_middle", "seg_last"))
    ov["seg_conservation_ok"] = (seg_sum == n_formal)
    for k in ("seg_first", "seg_middle", "seg_last"):
        ov[k + "_share"] = ((ov.get(k) or 0) / n_formal) if n_formal else None

    out = {
        "symbol": symbol, "family": fam, "family_label": fam, "available": n_formal > 0,
        "name": sec.get("name"), "research_version": RESEARCH_VERSION,
        "data_cutoff": DATA_CUTOFF, "last_formal_month": LAST_FORMAL_MONTH,
        "verdict": VERDICT, "verdict_cn": VERDICT_CN,
        "identity": {k: sec.get(k) for k in ("security_id", "name", "exchange", "listed_date",
                                             "delisted_date", "category", "subcategory",
                                             "listed_status")},
        "overview": ov, "note": FAMILY_NOTE[fam],
        "slice": str(SLICE_DB), "read_only": True,
    }
    if n_formal == 0:
        out["reason_cn"] = ("V5 库内有该标的的流月记录，但没有任何一个月通过正式样本门"
                            "（完整月 + ≥12 个实际交易日 + 真实 OHLC + 因子完整 + 无极端复权 +"
                            " 月末可退出 + 非退市当月），全部只进排除台账。")
    if n_formal:
        w = sec.get("null_low_weighted")
        if w is not None:
            out["n1_baseline"] = {
                "method": "切片 v5_security.null_low_weighted —— 由每月 N1 精确期望"
                          "（镜像 V5 s04_nulls.py）对正式月取均值；与 10_V5 案例披露值逐位一致",
                "mean_null_low_fraction": w,
                "mean_null_open_fraction": sec.get("null_open_weighted"),
                "excess_earliness_low": (ov["mean_low_fraction"] - w)
                    if ov.get("mean_low_fraction") is not None else None,
                "mean_null_high_fraction": sec.get("null_high_weighted"),
                "coverage": f"{n_formal:,} 窗",
            }
    out["quality_flags"] = _quality_flags(out, fam, sec.get("profile_present"))
    return out


def _quality_flags(s, fam, profile_present):
    """只用预注册 §13 已固定的代码；额外两个是能力边界声明，不是研究结论。"""
    ov = s["overview"]
    flags = []
    if ov.get("n_g2") or ov.get("n_g3"):
        flags.append({"code": "SINGLE_POINT_BAR", "level": "warn",
                      "cn": f"含非纯真实 OHLC 月 {int((ov.get('n_g2') or 0)+(ov.get('n_g3') or 0))} 个"})
    if ov.get("n_low_tie"):
        flags.append({"code": "LOW_TIES", "level": "warn",
                      "cn": f"最低点并列月 {int(ov['n_low_tie'])} 个；V5 单值取最早一天"})
    if ov.get("n_partial"):
        flags.append({"code": "PARTIAL_LISTING_MONTH", "level": "warn",
                      "cn": f"上市首月不完整 {int(ov['n_partial'])} 个"})
    if ov.get("n_suspended"):
        flags.append({"code": "SUSPENDED_DAYS", "level": "warn",
                      "cn": f"含停牌交易日月 {int(ov['n_suspended'])} 个"})
    if fam == "STOCK" and not profile_present:
        flags.append({"code": "NO_PROFILE", "level": "warn",
                      "cn": "无本命档案：只报价格低点，不做十神/冲合，不填补"})
    if fam == "STOCK":
        flags.append({"code": "EXECUTION_UNVERIFIED", "level": "warn",
                      "cn": "股票原始源无成交量：不假定开盘可无限成交"})
    if fam == "ETF":
        flags.append({"code": "ETF_PROFILE_NOT_IN_V5", "level": "info",
                      "cn": "V5 冻结档案只含股票本命盘：本栏目不为 ETF 凭空算十神/四柱/男女命/大运，"
                            "现有 ETF 资料仍按原页面（ETF 库）呈现"})
        flags.append({"code": "ETF_DELISTED_HISTORY_UNAVAILABLE", "level": "warn",
                      "cn": "只有现存 ETF 历史：结论不得外推为历史全部 ETF（幸存者偏差）"})
    if fam == "INDEX":
        flags.append({"code": "INDEX_NOT_TRADABLE", "level": "warn",
                      "cn": "指数点位不可直接交易；V5 指数只有 000001.SH 与 399106.SZ，"
                            "且无组统计/并列子表/复权列"})
    if (s.get("identity") or {}).get("category") in ("BOND", "MONEY"):
        flags.append({"code": "LOW_NOT_INFORMATIVE", "level": "hard",
                      "cn": "债券/货币 ETF：价格低波动、单点价特征，低点位置不可辨识，"
                            "只描述、不与权益类混合做显著性"})
    flags.append({"code": "HINDSIGHT_UPPER_BOUND", "level": "info",
                  "cn": "最低复权 Open 日买入是事后理论上限，不是可执行策略"})
    flags.append({"code": "INDUSTRY_CLASSIFICATION_UNAVAILABLE", "level": "info",
                  "cn": "行业分类不可用：不以名称或代码伪造行业"})
    return flags


# ---------------- 逐年真实证据 ----------------

def years(symbol: str, dataset: str) -> dict:
    fam, sec = resolve_family(symbol, dataset)
    if fam == "NONE":
        return {"rows": [], "family": fam}
    symbol = sec["security_id"]
    rows = _q(f"""SELECT year, count(*) AS n_months,
        sum(up) AS n_up, sum(down) AS n_down, sum(flat) AS n_flat, avg(ret) AS mean_ret,
        avg(low_fraction) AS mean_low_fraction,
        avg(low_k) AS mean_low_k, min(low_k) AS min_low_k, max(low_k) AS max_low_k,
        sum(CASE WHEN low_seg='first' THEN 1 ELSE 0 END) AS seg_first,
        sum(CASE WHEN low_seg='middle' THEN 1 ELSE 0 END) AS seg_middle,
        sum(CASE WHEN low_seg='last' THEN 1 ELSE 0 END) AS seg_last,
        avg(null_low_fraction) AS mean_null_low_fraction,
        avg(high_fraction) AS mean_high_fraction,
        avg(null_high_fraction) AS mean_null_high_fraction
      FROM v5_months WHERE family=? AND security_id=? AND formal=1
      GROUP BY year ORDER BY year""", (fam, symbol))
    vals = _q("""SELECT year, low_fraction FROM v5_months
      WHERE family=? AND security_id=? AND formal=1 AND low_fraction IS NOT NULL
      ORDER BY year, low_fraction""", (fam, symbol))
    byyear = {}
    for v in vals:
        byyear.setdefault(v["year"], []).append(v["low_fraction"])

    def med(a):
        if not a:
            return None
        m = len(a) // 2
        return a[m] if len(a) % 2 else (a[m - 1] + a[m]) / 2

    for r in rows:
        r["median_low_fraction"] = med(byyear.get(r["year"]) or [])
        n = r["n_months"] or 0
        for k in ("seg_first", "seg_middle", "seg_last"):
            r[k + "_share"] = ((r.get(k) or 0) / n) if n else None
        r["up_share"] = ((r.get("n_up") or 0) / n) if n else None
    return {"symbol": symbol, "family": fam, "rows": rows, "total": len(rows)}


# ---------------- 分组表 ----------------

def groups(symbol: str, dataset: str, dimension: str = "B12", condition: str = "ALL_VALID",
           sort: str = "key", min_n=None, page: int = 1, page_size: int = 100) -> dict:
    dim = str(dimension).upper()
    if dim not in MIN_N:
        raise V5Error(f"未知分组维度 {dimension!r}；可用：{', '.join(MIN_N)}")
    dimension = dim
    if condition not in CONDITIONS:
        raise V5Error(f"未知样本条件 {condition!r}；可用：{', '.join(CONDITIONS)}")
    fam, sec = resolve_family(symbol, dataset)
    thr = MIN_N[dimension]
    floor = max(int(min_n), thr) if min_n not in (None, "", 0) else thr
    if fam == "NONE" or fam == "INDEX":
        return {"symbol": symbol, "family": fam, "dimension": dimension, "condition": condition,
                "condition_label": CONDITION_LABEL[condition],
                "condition_kind": CONDITION_KIND[condition], "rows": [], "total": 0, "min_n": floor,
                "empty_reason": ("V5 对<b>指数</b>只保留逐月低点事实，没有 12 月支 / 60 流月柱分组统计"
                                 "与 PIT 资格表，指数点位也不可直接交易。<br>"
                                 "本页面默认标的就是 000001.SH 上证指数，所以一进栏目两个维度都是空的"
                                 "——这不是没有做，是指数没有这两张表。请在顶栏搜索一只股票或 ETF"
                                 "（如 <span class='mono'>600127.SH</span>、"
                                 "<span class='mono'>002008.SZ</span>）再看。")
                                if fam == "INDEX" else "V5 库内没有该标的。"}
    symbol = sec["security_id"]
    order = GROUP_SORTS.get(sort)
    if order is None:
        order = ("CASE group_key " + " ".join(f"WHEN '{b}' THEN {i}"
                 for i, b in enumerate(BRANCH_ORDER)) + " ELSE 99 END"
                 if dimension == "B12" else "group_key")
    size = max(1, min(int(page_size), 200))
    off = (max(1, int(page)) - 1) * size
    rows = _q(f"""SELECT * FROM v5_groups
      WHERE family=? AND security_id=? AND group_type=? AND scondition=? AND n_windows >= ?
      ORDER BY {order} LIMIT ? OFFSET ?""", (fam, symbol, dimension, condition, floor, size, off))
    total = _q("""SELECT count(*) c FROM v5_groups WHERE family=? AND security_id=?
      AND group_type=? AND scondition=? AND n_windows >= ?""",
               (fam, symbol, dimension, condition, floor))[0]["c"]
    for r in rows:
        r["threshold"] = thr
        r["low_sample"] = bool(r["group_low_sample"]) or (dimension == "P60" and 3 <= r["n_windows"] <= 5)
        r["up_rate"] = (r["n_up"] / r["n_windows"]) if r["n_windows"] else None
        r["condition_kind"] = CONDITION_KIND[condition]
        r["occurrences"] = _occurrences(r.pop("month_ids"), fam, symbol)
        r["digest"] = _digest(r)
    return {"symbol": symbol, "family": fam, "dimension": dimension, "condition": condition,
            "condition_label": CONDITION_LABEL[condition],
            "condition_kind": CONDITION_KIND[condition], "min_n": floor, "threshold": thr,
            "rows": rows, "page": int(page), "page_size": size, "total": total,
            "sample_note": ("12 月支与节气月序号一一对应（寅=第1月 … 丑=第12月），等价于月份季节性，"
                            "不得表述为独立命理证据。" if dimension == "B12" else
                            "60 柱约 5 年重复一次，36 年只有约 7 次；3–5 次为低样本。")}


def _occurrences(month_ids, fam, symbol):
    """组内历次月份：一次 IN 查询取回，保持组表 month_ids 的同一分母。"""
    ids = [x for x in (month_ids or "").split(",") if x]
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    rows = _q(f"""SELECT month_id, year, month_pillar, low_day, low_k, n, low_fraction, low_seg,
        low_day_pillar, low_tie_n, open_day, open_k, high_day, high_k, high_fraction,
        high_day_pillar, high_tie_n, null_low_fraction, null_high_fraction, ret, up, down, flat
      FROM v5_months WHERE family=? AND security_id=? AND month_id IN ({ph})
      ORDER BY month_id""", (fam, symbol, *ids))
    return [{"month_id": r["month_id"], "year": r["year"], "pillar": r["month_pillar"],
             "low_day": r["low_day"], "low_k": r["low_k"], "n": r["n"],
             "low_fraction": r["low_fraction"],
             "seg": SEG_CN.get(r["low_seg"], r["low_seg"]), "day_pillar": r["low_day_pillar"],
             "tie_n": r["low_tie_n"], "open_day": r["open_day"], "open_k": r["open_k"],
             "high_day": r["high_day"], "high_k": r["high_k"],
             "high_fraction": r["high_fraction"], "high_day_pillar": r["high_day_pillar"],
             "high_tie_n": r["high_tie_n"], "null_low_fraction": r["null_low_fraction"],
             "null_high_fraction": r["null_high_fraction"], "ret": r["ret"],
             "dir": ("涨" if r["up"] else "跌" if r["down"] else "平")} for r in rows]


def _mean(vals):
    vs = [v for v in vals if v is not None]
    return (sum(vs) / len(vs)) if vs else None


def _mean_excess_days(pts, which):
    """(实际位置 − 该月自己的 N1 期望) × (n−1) 的平均 = 比随机早/晚多少个交易日。
    逐月各用自己的 n 换算，再平均；负=更早。分道基线本身就是自算口径。"""
    vals = [((o[f"{which}_fraction"] - o[f"null_{which}_fraction"]) * (o["n"] - 1))
            for o in pts
            if o.get(f"{which}_fraction") is not None
            and o.get(f"null_{which}_fraction") is not None and o.get("n")]
    return (sum(vals) / len(vals)) if vals else None


def _digest(row: dict) -> dict:
    """把一组的历次低/高点压成点阵数据 + 人话；随机基线一律用【分道】值，不共用整组基线。"""
    occ = row.get("occurrences") or []
    if not occ:
        return {"dots": [], "hdots": [], "lanes": [], "segs": {},
                "plain": "该组在本条件下没有可显示的历次月份。", "pattern": "无样本"}
    segs = {v: 0 for v in SEG_CN.values()}
    for o in occ:
        if o["seg"] in segs:
            segs[o["seg"]] += 1
    xs = [o["low_fraction"] for o in occ if o["low_fraction"] is not None]
    spread = (max(xs) - min(xs)) if len(xs) > 1 else None
    n = len(occ)
    top = max(segs, key=lambda k: segs[k])
    share = segs[top] / n if n else 0
    if n == 1:
        pattern = "只有 1 次，谈不上分布"
    elif share >= 0.6:
        others = "、".join(f"{k}{v} 次" for k, v in segs.items() if v and k != top)
        pattern = f"集中在{top}（{segs[top]}/{n}＝{share:.0%}）" + \
                  (f"，另有 {others}散在其他段" if others else "，全部在这一段")
    else:
        pattern = f"三段都有，没有哪段占多数（最多 {top} {segs[top]}/{n}＝{share:.0%}）"
    lanes = []
    for name, key, mean_key, null_key in (("涨", "up", "up_mean_low", "up_null_low"),
                                          ("跌/平", "down", "down_mean_low", "down_null_low")):
        pts = [o for o in occ if (o["dir"] == "涨") == (key == "up")]
        if not pts:
            continue
        lanes.append({"name": name, "n": len(pts),
                      "mean": row.get(mean_key), "null": row.get(null_key),
                      "pts_mean": _mean([o["low_fraction"] for o in pts]),
                      "pts_null": _mean([o["null_low_fraction"] for o in pts]),
                      "h_mean": _mean([o["high_fraction"] for o in pts]),
                      "h_null": _mean([o["null_high_fraction"] for o in pts]),
                      "excess_days": _mean_excess_days(pts, "low"),
                      "h_excess_days": _mean_excess_days(pts, "high"),
                      "dots": [{"x": o["low_fraction"], "k": o["low_k"], "n": o["n"],
                                "date": o["low_day"], "pillar": o["day_pillar"],
                                "dir": o["dir"], "month_id": o["month_id"], "ret": o["ret"],
                                "tie_n": o["tie_n"], "seg": o["seg"],
                                "null": o["null_low_fraction"]} for o in pts],
                      "hdots": [{"x": o["high_fraction"], "k": o["high_k"], "n": o["n"],
                                 "date": o["high_day"], "pillar": o["high_day_pillar"],
                                 "dir": o["dir"], "month_id": o["month_id"], "ret": o["ret"],
                                 "tie_n": o["high_tie_n"],
                                 "null": o["null_high_fraction"]}
                                for o in pts if o.get("high_fraction") is not None]})
    return {
        "dots": [{"x": o["low_fraction"], "dir": o["dir"], "date": o["low_day"],
                  "pillar": o["day_pillar"], "k": o["low_k"], "n": o["n"], "seg": o["seg"],
                  "month_id": o["month_id"], "ret": o["ret"], "tie_n": o["tie_n"],
                  "null": o["null_low_fraction"]} for o in occ],
        "hdots": [{"x": o["high_fraction"], "dir": o["dir"], "date": o["high_day"],
                   "pillar": o["high_day_pillar"], "k": o["high_k"], "n": o["n"],
                   "month_id": o["month_id"], "ret": o["ret"], "tie_n": o["high_tie_n"],
                   "null": o["null_high_fraction"]}
                  for o in occ if o.get("high_fraction") is not None],
        "lanes": lanes, "segs": segs, "spread": spread,
        "pattern": pattern, "dominant": top if share >= 0.6 else None, "dominant_share": share,
        "high": {"mean": row.get("mean_high_fraction"), "null": row.get("mean_null_high_fraction"),
                 "excess": row.get("mean_high_excess")},
        "order": {"observed": row.get("order_obs"), "null": row.get("order_null"), "n": n},
        "self_computed": "高点与分道基线为本栏目自算（V5 冻结事实未登记）",
        "plain": f"{n} 次：月初 {segs['月初']} · 月中 {segs['月中']} · 月末 {segs['月末']}；{pattern}"
                 + (f"；位置跨度 {min(xs):.2f}–{max(xs):.2f}" if len(xs) > 1 else ""),
    }


# ---------------- 逐月证据 ----------------

def months(symbol: str, dataset: str, dimension: str = "", group: str = "",
           condition: str = "ALL_VALID", page: int = 1, page_size: int = 20,
           year: str = "", low_only: str = "") -> dict:
    fam, sec = resolve_family(symbol, dataset)
    if fam == "NONE":
        return {"rows": [], "total": 0, "family": "NONE", "empty_reason": "V5 库内没有该标的。"}
    symbol = sec["security_id"]
    where, params = ["family=?", "security_id=?", "formal=1"], [fam, symbol]
    if str(year).strip():
        where.append("year = ?")
        params.append(int(year))
    if dimension and group:
        ids = _group_month_ids(fam, symbol, dimension, group, condition)
        if not ids:
            return {"rows": [], "total": 0, "family": fam, "dimension": dimension, "group": group,
                    "empty_reason": "该组在 V5 冻结组表里没有关联月份。"}
        where.append("month_id IN (" + ",".join("?" * len(ids)) + ")")
        params += ids
    if str(low_only).strip() in ("1", "true"):
        where.append("low_k <> open_k")
    w = " AND ".join(where)
    size = max(1, min(int(page_size), 50))
    off = (max(1, int(page)) - 1) * size
    rows = _q(f"SELECT * FROM v5_months WHERE {w} ORDER BY month_id DESC LIMIT ? OFFSET ?",
              params + [size, off])
    total = _q(f"SELECT count(*) c FROM v5_months WHERE {w}", params)[0]["c"]
    if rows:
        mids = [r["month_id"] for r in rows]
        ph = ",".join("?" * len(mids))
        cal = {r["month_id"]: r for r in _q(
            f"SELECT * FROM v5_calendar WHERE month_id IN ({ph})", mids)}
        for r in rows:
            c = cal.get(r["month_id"]) or {}
            r["term_start_at"] = c.get("start_at")
            r["term_end_at"] = c.get("end_at")
            r["term_start_date"] = c.get("start_date")
            r["term_end_date"] = c.get("end_date")
            r["term_complete"] = c.get("complete")
            r["year_pillar"] = c.get("year_pillar")
            for band in ("lowzone1", "lowzone2", "openzone1"):
                pos = [int(x) for x in (r.get(f"{band}_pos") or "").split(",") if x]
                dts = [x for x in (r.get(f"{band}_dates") or "").split(",") if x]
                r[f"{band}_positions"] = pos
                r[f"{band}_dates"] = dts
                r[f"{band}_contiguous"] = (max(pos) - min(pos) + 1 == len(pos)) if pos else None
                if pos and len(pos) != len(dts):
                    r[f"{band}_date_mismatch"] = (f"positions {len(pos)} ≠ dates {len(dts)}，"
                                                  "切片导出异常")
            r["span_trading_days"] = ((r["lowzone1_last_k"] - r["lowzone1_first_k"] + 1)
                                      if r.get("lowzone1_count") else None)
            r.pop("lowzone1_pos", None); r.pop("lowzone2_pos", None); r.pop("openzone1_pos", None)
        tr = _q(f"""SELECT month_id, tie_type, date, k, adj_l, adj_o, day_pillar FROM v5_ties
          WHERE family=? AND security_id=? AND month_id IN ({ph})""", (fam, symbol, *mids))
        tmap: dict = {}
        for t in tr:
            tmap.setdefault((t["month_id"], t["tie_type"]), []).append(t)
        for r in rows:
            r["ties_low"] = tmap.get((r["month_id"], "LOW"), [])
            r["ties_open"] = tmap.get((r["month_id"], "OPEN"), [])
            if len(r["ties_low"]) > 1:
                r["tie_convention_note"] = ("V5 单值取最早并列日；V4 案例表取最后并列日，"
                                            "两者不是同一口径。")
    return {"symbol": symbol, "family": fam, "rows": rows, "total": total, "page": int(page),
            "page_size": size, "dimension": dimension or None, "group": group or None,
            "low_fraction_formula": "low_fraction = (low_k - 1) / (n - 1)，n≥12",
            "position_rule": "first 当 k≤ceil(n/3)；middle 当 k≤ceil(2n/3)；否则 last",
            "band_note": "1%/2% 带宽在看结果前固定，不因结果调整；Low 基准与 Open 基准两套不得混用。",
            "high_basis": ("最高点（high_k / high_fraction / null_high_fraction）与分道随机基线是"
                           "本栏目自算：V5 冻结事实表只登记最低点与最低开盘日，没有最高点，"
                           "因此它不属于 V5 的预注册结论；随机基线镜像 V5 s04_nulls.py 的同一精确式。")}


def _group_month_ids(fam, symbol, dimension, group, condition):
    cond = condition if condition in CONDITIONS else "ALL_VALID"
    r = _q("SELECT month_ids FROM v5_groups WHERE family=? AND security_id=? AND group_type=?"
           " AND group_key=? AND scondition=?", (fam, symbol, dimension.upper(), group, cond))
    return [x for x in (r[0]["month_ids"] or "").split(",") if x] if r else []


# ---------------- 策略分级（全市场口径，非本标的专属） ----------------

STRATEGY_COLS = ("asset_class", "scope", "strategy", "exit_type", "family", "grade",
                 "grade_reason", "full_n_months", "full_n_years", "train_n_months",
                 "validate_n_months", "holdout_n_months", "locked", "holdout_official")
STRATEGY_CLASS = {"STOCK": "STOCK", "ETF": "ETF_EQUITY", "ETF_EQUITY": "ETF_EQUITY"}


def strategies(asset_class: str = "STOCK", limit: int = 200) -> dict:
    raw = (asset_class or "STOCK").upper()
    if raw not in STRATEGY_CLASS:
        raise V5Error(f"未知资产类别 {raw!r}；可用 STOCK 或 ETF（权益 ETF）")
    ac = STRATEGY_CLASS[raw]
    cap = max(1, min(int(limit), 400))
    rows = _q(f"SELECT {', '.join(STRATEGY_COLS)} FROM v5_strategy WHERE asset_class=?"
              f" LIMIT ?", (ac, cap))
    total = _q("SELECT count(*) c FROM v5_strategy WHERE asset_class=?", (ac,))[0]["c"]
    by = {}
    for r in _q("SELECT grade, count(*) c FROM v5_strategy WHERE asset_class=?"
                " GROUP BY grade", (ac,)):
        by[r["grade"]] = r["c"]
    return {"asset_class": ac, "requested_as": raw, "rows": rows, "returned": len(rows),
            "total_rows": total, "grade_counts": by,
            "class_note": ("切片 v5_strategy 原样承接 V5 冻结件 07 的 375 行："
                           "STOCK 189 行 + ETF_EQUITY 186 行；债券/货币 ETF 不进入策略显著性家族"
                           "（预注册 §8.3）。"),
            "supported_oos": by.get("SUPPORTED_OOS", 0),
            "scope_note": ("这是 V5 全市场策略×选择范围×退出 的分级口径，正在接受统计实现复核；"
                           "不是当前标的专属的获胜规则，也不能据此断言因果。"),
            "families": "K1/K3/K5/K8/K10 固定日 · BATCH47 分批4–7日 · DD3/DD5 回撤 · REV5 反转"}


def status() -> dict:
    out = {"research_version": RESEARCH_VERSION, "data_cutoff": DATA_CUTOFF,
           "last_formal_month": LAST_FORMAL_MONTH, "verdict": VERDICT,
           "verdict_cn": VERDICT_CN, "slice": str(SLICE_DB), "read_only": True,
           "driver": "sqlite3 (标准库)", "available": False}
    try:
        con = _connect()
        ro = str(con.execute("PRAGMA query_only").fetchone()[0])
        if ro != "1":
            raise V5Unavailable(f"query_only={ro}，不是只读")
        out["query_only"] = ro
        out["journal_mode"] = con.execute("PRAGMA journal_mode").fetchone()[0]
        out["slice_bytes"] = SLICE_DB.stat().st_size
        for t in ("v5_months", "v5_groups", "v5_ties", "v5_security", "v5_calendar", "v5_strategy"):
            out.setdefault("row_counts", {})[t] = _q(f"SELECT count(*) c FROM {t}")[0]["c"]
        for k in ("source_db", "source_db_bytes", "source_db_sha256_full", "source_csv_sha256",
                  "exported_at", "tool_version", "self_computed", "n1_formula",
                  "column_names", "zone_dates_source"):
            out[k] = _meta(k)
        out["available"] = True
    except V5Unavailable as exc:
        out["error"] = str(exc)
    return out


def dispatch(path: str, q: dict) -> dict:
    """把 /api/v5/* 映射到上面的只读查询；值全部走参数，标识符全部走模块内白名单。"""
    g = lambda k, d="": (q.get(k, [d])[0] if q.get(k) else d)  # noqa: E731
    rest = path[len("/api/v5/"):].strip("/")
    parts = rest.split("/")
    sym = g("id") or g("symbol")
    ds = g("dataset", "ashare")
    if parts[0] == "status":
        return status()
    if parts[0] == "strategies":
        return strategies(g("asset_class") or ("ETF" if ds == "etf" else "STOCK"),
                          int(g("limit", "200") or 200))
    if parts[0] == "years":
        return years(sym, ds)
    if not sym and len(parts) > 1:
        sym = parts[1]          # 兼容 /api/v5/security/{symbol}/... 写法
    leaf = parts[-1] if parts[-1] in ("summary", "groups", "months", "strategies") else parts[0]
    if not sym:
        raise V5Error("缺少参数 id（证券身份代码，如 600127.SH）")
    if leaf == "summary":
        return summary(sym, ds)
    if leaf == "groups":
        return groups(sym, ds, g("dimension", "B12"), g("condition", "ALL_VALID"),
                      g("sort", "key"), (int(g("min_n")) if g("min_n").isdigit() else None),
                      int(g("page", "1") or 1), int(g("page_size", "100") or 100))
    if leaf == "months":
        return months(sym, ds, g("dimension"), g("group"), g("condition", "ALL_VALID"),
                      int(g("page", "1") or 1), int(g("page_size", "20") or 20),
                      g("year"), g("low_only"))
    raise V5Error(f"未知 V5 接口 {path}")
