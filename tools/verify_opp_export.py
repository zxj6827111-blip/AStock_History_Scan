#!/usr/bin/env python3
"""页面五导出件的独立验收：不用浏览器、不 import 前端代码。

三方对照，任何一对不上就 FAIL：
  ① .xlsx 单元格值/数字格式（openpyxl 回读磁盘文件）
  ② 服务端 /api/opportunities 用同一组参数重新查一遍（独立 SQL 路径）
  ③ 走查时抓的 DOM 快照（表头文字、前 3 行可见文本、行数）

用法：/usr/bin/python3 tools/verify_opp_export.py evidence/export_walk
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import openpyxl

BASE = "http://127.0.0.1:8737"
TYPE_CN = {"A_SHARE": "股票", "INDEX": "指数", "ETF": "ETF"}
MARKET_CN = {".SH": "沪市", ".SZ": "深市", ".BJ": "北交所"}
DIR_CN = {"UP": "涨", "DOWN": "跌", "FLAT": "平"}
CODE_CN = {"MONTH_PILLAR": "月柱归属有争议", "YEAR_AND_MONTH_PILLAR": "年柱与月柱归属均有争议",
           "ELIGIBLE_UNDISPUTED": "可入样本（无争议）", "EXCLUDED_NO_PROFILE": "剔除·无命盘",
           "EXCLUDED_BOUNDARY_DISPUTED": "剔除·交节争议"}
P1 = "0.0%"
P2 = "+0.00%;-0.00%;0.00%"

checks: list = []
SAMPLES: list = []


def rec(name: str, ok: bool, detail: str = "") -> bool:
    checks.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (("  | " + detail) if detail else ""))
    return ok


def q(params: dict, dataset: str) -> dict:
    p = {"dataset": dataset, "months": 1, "offset": params["offset"],
         "min_n": params["min_n"], "policy": params["policy"], "sort": params["sort"],
         "dir": params["dir"], "cap": params["cap"]}
    if params.get("grade"):
        p["grade"] = params["grade"]
    if params.get("rate_min") is not None:
        p["rate_min"] = params["rate_min"]
    if params.get("rate_eq"):
        p["rate_eq"] = "1"
    if params.get("both_months"):
        p["both_months"] = "1"
        if params.get("next_rate") and params["next_rate"] != "inherit":
            p["next_rate"] = params["next_rate"]
    if params.get("q"):
        p["q"] = params["q"]
    if params.get("include_delisted"):
        p["include_delisted"] = "1"
    if params.get("include_index"):
        p["include_index"] = "1"
    for k, v in (params.get("relation_filters") or {}).items():
        p[k] = "1"
    url = BASE + "/api/opportunities?" + urllib.parse.urlencode(p)
    with urllib.request.urlopen(url, timeout=120) as r:
        return json.load(r)


def blank(v) -> bool:
    return v is None or v == ""


def norm(v):
    """空串与空单元等价：写手把 '' 落成空单元，读手读回 None，不算差异。"""
    return None if blank(v) else str(v)


def eq_num(a, b, tol=1e-9) -> bool:
    if blank(a) and blank(b):
        return True
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return False
    return abs(float(a) - float(b)) <= tol


def grade(n) -> str:
    n = n or 0
    return "≥6" if n >= 6 else "3–5" if n >= 3 else "<3" if n > 0 else "0 次"


def rel_text(row) -> str:
    if not row["occurrence_count"]:
        return "历史无同柱，不推断"
    if row["has_natal_profile"] == 0:
        return "无命盘 · 不计算关系"
    try:
        items = json.loads(row["relation_types"] or "[]")
    except (TypeError, ValueError):
        items = [row["relation_types"]]
    return " / ".join(items) if items else "（无）"


def expected_cell(col: str, row: dict, params: dict) -> str:
    """一列 → 该行的期望值。列名与取值规则都写在断言里，不依赖前端代码。"""
    pol = params["policy"]
    down = params["dir"] == "down"
    dt = "下跌" if down else "上涨"
    n = row["all_sample_n"] if pol == "ALL" else row["exclude_single_n"]
    if col == "序号":
        return row["rn"]
    if col == "证券代码":
        return row["security_code"]
    if col == "名称":
        return row["name"]
    if col == "市场":
        return next((MARKET_CN[s] for s in MARKET_CN
                     if (row["security_code"] or "").endswith(s)), "")
    if col == "类型":
        return TYPE_CN.get(row["security_type"], "股票")
    if col == "上市日":
        return row["listed_date"]
    if col == "退市日":
        return row["delisted_date"]
    if col == "无命盘":
        return "是" if row["has_natal_profile"] == 0 else ""
    if col == "交节争议":
        bc = row["boundary_conflict"]
        return "" if not bc or bc in ("NONE", "NOT_APPLICABLE") else CODE_CN.get(bc, bc)
    if col == "年柱":
        return row["natal_year_pillar"]
    if col == "月柱":
        return row["natal_month_pillar"]
    if col == "日柱":
        return row["natal_day_pillar"]
    if col == "日主":
        return "" if row["has_natal_profile"] == 0 else (row["day_master_stem"] or "") + (row["day_master_element"] or "")
    if col.startswith("标的五行"):
        if row["has_natal_profile"] == 0:
            return "无命盘"
        return " ".join(row[k] or "—" for k in ("year_stem_element", "year_branch_element",
                                                "month_stem_element", "month_branch_element",
                                                "day_stem_element", "day_branch_element"))
    if col == "流月柱":
        return row["flow_pillar"]
    if col == "流月五行(干/支)":
        return (row["stem_element"] or "—") + "/" + (row["branch_element"] or "—")
    if col == "涨次数":
        return row["up_n_ALL"] if pol == "ALL" else row["up_n_EXCLUDE_SINGLE"]
    if col == "跌次数":
        return row["down_n_ALL"] if pol == "ALL" else row["down_n_EXCLUDE_SINGLE"]
    if col == "平次数":
        return row["flat_n_ALL"] if pol == "ALL" else row["flat_n_EXCLUDE_SINGLE"]
    if col.startswith("出现次数"):
        return n
    if col.startswith("历史" + dt + "占比"):
        return row[("down_rate_ALL" if down else "up_rate_ALL") if pol == "ALL"
                   else ("down_rate_ES" if down else "up_rate_ES")]
    if col == "所选月实际":
        return DIR_CN.get((row["actual_direction"] or "").upper(), "无行情") \
            if row["in_sample"] else "该月无行情"
    if col == "所选月涨跌幅":
        return row["actual_return"] if row["in_sample"] else None
    if col == "所选月相对基准超额":
        return row["actual_excess"] if row["in_sample"] else None
    if col == "所选月交易日数":
        return row["actual_trading_days"] if row["in_sample"] else None
    if col == "所选月样本状态":
        return CODE_CN.get(row["actual_sample_status"], "")
    if col.startswith("同柱占比"):
        return row[("down_rate_excl_ALL" if down else "up_rate_excl_ALL") if pol == "ALL"
                   else ("down_rate_excl_ES" if down else "up_rate_excl_ES")]
    if col == "剔除后分母":
        return row["n_excl_ALL"] if pol == "ALL" else row["n_excl_ES"]
    if col == "平均涨跌幅":
        return row["mean_body_return_ALL"] if pol == "ALL" else row["mean_body_return_ES"]
    if col == "同期超额":
        return row["excess_mean"]
    if col == "样本等级":
        return grade(n)
    if col == "历次年份":
        return "; ".join(y.strip() for y in (row["occurrence_years"] or "").split(";") if y.strip())
    if col == "相邻间隔(年)":
        return row["year_gaps"]
    if col == "是否五年重现":
        return "是" if str(row["is_five_year_repeat"]) == "1" else ""
    if col.startswith("下月"):
        if col.endswith("占比"):
            return row["next_rate"]
        if col.endswith("平均涨跌幅"):
            return row["next_mean"]
        if col.endswith("出现次数"):
            return row["next_n"]
        return row["next_down" if down else "next_up"]
    if col == "命中关系" or "关系" in col:
        return rel_text(row)
    raise AssertionError("没有为该导出列写过期望规则：" + col)


def fmt_for(col: str) -> str | None:
    if col.startswith("历史") and "占比" in col and "（含" in col:
        return P1
    if col.startswith("同柱占比") or (col.startswith("下月") and "占比" in col):
        return P1
    if col in ("平均涨跌幅", "同期超额", "所选月涨跌幅", "所选月相对基准超额") \
       or col == "下月平均涨跌幅":
        return P2
    if col in ("涨次数", "跌次数", "平次数", "剔除后分母", "所选月交易日数", "序号") \
       or col.startswith("出现次数") or (col.startswith("下月") and "次数" in col):
        return "0"
    return None


def compare_rows(head, ws, rows, params):
    """逐格对账。抽成函数是为了能拿「篡改过的表」反着测这套断言本身。"""
    bad_vals, bad_fmt, order_bad = [], [], []
    for ri in range(2, ws.max_row + 1):
        src = rows[ri - 2]                     # 逐行按表上顺序对位，顺序本身也是断言的一部分
        if ws.cell(ri, head.index("证券代码") + 1).value != src["security_code"]:
            order_bad.append(f"行{ri} xlsx={ws.cell(ri, head.index('证券代码') + 1).value!r} "
                             f"表上={src['security_code']!r}")
        for ci, col in enumerate(head, start=1):
            got = ws.cell(ri, ci).value
            want = expected_cell(col, src, params)
            if isinstance(want, (int, float)) and not isinstance(want, bool):
                if not eq_num(got, want, 1e-6):
                    bad_vals.append(f"{col} 行{ri}: xlsx={got!r} 期望={want!r}")
            elif norm(got) != norm(want):
                bad_vals.append(f"{col} 行{ri}: xlsx={got!r} 期望={want!r}")
            want_z = fmt_for(col)
            if want_z:
                cell = ws.cell(ri, ci)
                if isinstance(got, (int, float)) and cell.number_format != want_z:
                    bad_fmt.append(f"{col} 行{ri} 数字格式={cell.number_format!r} 期望={want_z!r}")
                if not isinstance(got, (int, float)) and not blank(got):
                    bad_fmt.append(f"{col} 行{ri} 应该是数值单元，实得 {type(got).__name__}")
    return bad_vals, bad_fmt, order_bad


def verify(tag: str, snap: dict, xlsx: Path) -> None:
    params, ds = snap["params"], snap["dataset"]
    out = q(params, ds)
    month = next((m for m in out["months"] if m["month_id"] == snap["month"]["month_id"]), None)
    if not rec(f"{tag} 服务端按同参数复现同一流月", month is not None,
               f"记录={snap['month']['month_id']} 复现={'(没有)' if month is None else month['month_id']}"):
        return
    rows = [r for r in out["rows"] if r["flow_pillar"] == snap["month"]["pillar"]]
    if rows:
        SAMPLES.append((rows, params))
    rec(f"{tag} 行数：xlsx=DOM=服务端重查", len(rows) == snap["domRows"] == snap["month"]["returned"],
        f"服务端 {len(rows)} / DOM {snap['domRows']} / 接口 returned {snap['month']['returned']}")

    wb = openpyxl.load_workbook(xlsx)
    rec(f"{tag} 工作表", wb.sheetnames == ["机会表", "口径与参数"], str(wb.sheetnames))
    ws = wb["机会表"]
    head = [c.value for c in ws[1]]
    rec(f"{tag} 表头数量 = 记录的导出列数", len(head) == snap["specLen"],
        f"xlsx {len(head)} / 页面记录 {snap['specLen']}")
    rec(f"{tag} 表头逐字 = 页面记录的导出列名", head == snap["specHeaders"])
    rec(f"{tag} 数据行数 = xlsx 行宽", ws.max_row - 1 == snap["domRows"],
        f"xlsx {ws.max_row - 1} 行")
    rec(f"{tag} 有自动筛选", bool(ws.auto_filter.ref))

    bad_vals, bad_fmt, order_bad = compare_rows(head, ws, rows, params)
    rec(f"{tag} 逐格值对账（{ws.max_row - 1} 行 × {len(head)} 列）", not bad_vals,
        f"{len(bad_vals)} 处不符，例：" + " ｜ ".join(bad_vals[:4]) if bad_vals
        else f"{(ws.max_row - 1) * len(head)} 格全对")
    rec(f"{tag} 行顺序与表上一致（含序号=名次）", not order_bad,
        "；".join(order_bad[:3]) if order_bad else f"{ws.max_row - 1} 行逐行同序")
    rec(f"{tag} 数字单元与百分比/整数格式", not bad_fmt,
        "；".join(bad_fmt[:4]) if bad_fmt else "占比/涨跌幅/次数均为数值单元")

    # 反向对照：在内存里篡改两格（不落盘），这套断言必须报错，否则上面的绿是假的
    rate_hdr = next((h for h in head if h.startswith("历史") and "占比" in h), None)
    tam1 = tam2 = ""
    if rate_hdr:
        keep_v = ws.cell(2, head.index(rate_hdr) + 1).value
        ws.cell(2, head.index(rate_hdr) + 1).value = (keep_v or 0.0) + 0.05
        b1, _, _ = compare_rows(head, ws, rows, params)
        tam1 = any(x.startswith(rate_hdr) and "行2" in x for x in b1)
        ws.cell(2, head.index(rate_hdr) + 1).value = keep_v
    keep_c = ws.cell(2, head.index("证券代码") + 1).value
    ws.cell(2, head.index("证券代码") + 1).value = "999999.SZ"
    _, _, o2 = compare_rows(head, ws, rows, params)
    tam2 = bool(o2)
    ws.cell(2, head.index("证券代码") + 1).value = keep_c
    rec(f"{tag} 反向对照：篡改一格就被抓到", bool(rate_hdr) and tam1 and tam2,
        f"改占比={tam1} 改代码={tam2}（未落盘，原文件不动）")

    # ③ 与走查时抓的 DOM 可见文本对照（占比/均值是页面格式化后的字符串）
    dom_bad = []
    for r_i, cells in enumerate(snap["cells"]):
        for dom_label, xlsx_label, tol in (("历史上涨占比", "历史上涨占比", 5e-4),
                                          ("历史下跌占比", "历史下跌占比", 5e-4),
                                          ("平均涨跌幅", "平均涨跌幅", 5e-5)):
            if dom_label not in snap["th"]:
                continue
            txt = cells[snap["th"].index(dom_label)].replace("%", "").replace("+", "").strip()
            try:
                dom_v = float(txt) / 100.0
            except ValueError:
                dom_bad.append(f"{dom_label} DOM 文本读不出数字：{txt!r}")
                continue
            xlsx_col = next((h for h in head if h.startswith(xlsx_label)), None)
            if xlsx_col is None:
                dom_bad.append(f"xlsx 里没有以 {xlsx_label} 开头的列")
                continue
            xlsx_v = ws.cell(r_i + 2, head.index(xlsx_col) + 1).value
            if not isinstance(xlsx_v, (int, float)) or abs(dom_v - float(xlsx_v)) > tol:
                dom_bad.append(f"{dom_label} 行{r_i}: DOM={txt}% xlsx={xlsx_v!r}")
    rec(f"{tag} 前 {len(snap['cells'])} 行与 DOM 文本一致（占比/均值）", not dom_bad,
        "；".join(dom_bad[:3]) if dom_bad else "页面上看到的百分数与表里的数值同一大小")

    meta = list(wb["口径与参数"].iter_rows(values_only=True))
    flat = " ".join(str(a) + str(b) for a, b in ((r[0], r[1] if len(r) > 1 else "") for r in meta))
    need = [snap["month"]["month_id"], snap["month"]["pillar"], params["policy"],
            str(out["calendar_version"]), str(out["input_data_as_of"])]
    miss = [x for x in need if x not in flat]
    rec(f"{tag} 参数页含月份/柱/口径/历法版本/数据截至日", not miss, "缺：" + ",".join(miss) if miss else "齐全")
    n_cav = sum(1 for r in meta if str(r[0]).startswith("提示 "))
    rec(f"{tag} 参数页 caveats 条数 = 接口下发", n_cav == snap["month"]["caveats"],
        f"参数页 {n_cav} 条 / 接口 {snap['month']['caveats']} 条")
    if snap["month"]["actual_summary"]:
        rec(f"{tag} 参数页写明该月实际涨跌平与全市场基准", "本表该月实际" in flat)


def selftest() -> None:
    """反向断言：这套对账必须能翻脸。比较器、口径轴、方向轴各测一次，全过才算上面的绿是真的。"""
    rows = SAMPLES[0][0] if SAMPLES else []
    v = rows[0]["up_rate_ALL"] if rows else 0.8
    rec("SELFTEST 数值比较器会因 5pp 偏差报错",
        eq_num(v, v, 1e-6) and not eq_num(v, v + 0.05, 1e-6), f"{v} vs {v + 0.05}")
    rec("SELFTEST 空单元=空串，但「是」不会被当成空",
        norm("") is None and norm(None) is None and norm("是") != norm(None))
    pol = None
    for rows, _ in SAMPLES:
        for r in rows:
            a = expected_cell("涨次数", r, {"policy": "ALL", "dir": "up"})
            b = expected_cell("涨次数", r, {"policy": "EXCLUDE_SINGLE", "dir": "up"})
            if a != b:
                pol = (r["security_code"], a, b)
                break
        if pol:
            break
    rec("SELFTEST 口径轴不空转（ALL 与排除单交易日月能取到不同的数）",
        pol is not None, str(pol) if pol else "所有样本的两种口径计数都相同")
    dr = None
    for rows, _ in SAMPLES:
        for r in rows:
            a = expected_cell("历史上涨占比（含所选月）", r, {"policy": "ALL", "dir": "up"})
            b = expected_cell("历史下跌占比（含所选月）", r, {"policy": "ALL", "dir": "down"})
            if a != b:
                dr = (r["security_code"], a, b)
                break
        if dr:
            break
    rec("SELFTEST 方向轴不空转（上涨占比与下跌占比能取到不同的数）",
        dr is not None, str(dr) if dr else "")
    rec("SELFTEST 数字格式只加在数值列上", fmt_for("名称") is None and fmt_for("平均涨跌幅") == P2)


def main() -> int:
    ev = Path(sys.argv[1] if len(sys.argv) > 1 else "evidence/export_walk")
    dl = ev / "downloads"
    snaps = sorted(p for p in ev.glob("*.json") if p.name != "export_walk_report.json")
    if not snaps:
        print("没有走查快照：" + str(ev))
        return 2
    print("== 场景数", len(snaps), "==")
    for sp in snaps:
        tag = sp.stem
        snap = json.loads(sp.read_text())
        xlsx = dl / snap["exportedFile"]
        if not rec(f"{tag} 磁盘上有对应下载", xlsx.is_file(), str(xlsx)):
            continue
        verify(tag, snap, xlsx)
    selftest()
    total, ok = len(checks), sum(1 for c in checks if c)
    print(f"\n结果：{ok}/{total} PASS" + ("" if ok == total else "  ← 有 FAIL，不算交付"))
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
