#!/usr/bin/env python3
"""P6/P7-B 正式环境（8737）全功能回归矩阵。

只读 HTTP + 只读 SQLite 校验，不写任何数据。
用法：python3 tools/prod_regression.py --base http://127.0.0.1:8737 --out evidence/p6_prod_regression.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path("/Volumes/workSSD/software development/AStock_History_Scan")
DBS = {
    "ashare": ROOT / "bazi_month_relation_local_v1/database/bazi_month_relation_local_v1.sqlite",
    "etf": ROOT / "bazi_month_relation_etf_v1/database/bazi_month_relation_etf_v1.sqlite",
}
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

RESULTS: list[dict] = []


def get(base: str, path: str, **params):
    url = base + path + "?" + urllib.parse.urlencode(params)
    with OPENER.open(url, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def check(name: str, ok: bool, detail, category: str = "") -> None:
    RESULTS.append({"item": name, "category": category, "pass": bool(ok), "detail": detail})
    print(("PASS  " if ok else "FAIL  ") + f"{name}  {detail}")


def db_count(dataset: str, sql: str, params=()) -> int:
    con = sqlite3.connect(f"file:{DBS[dataset]}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    try:
        return con.execute(sql, params).fetchone()[0]
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8737")
    ap.add_argument("--out", default="evidence/p6_prod_regression.json")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    # 1. 数据集与档案
    st_a = get(base, "/api/stock", id="000001.SH", dataset="ashare")
    st_e = get(base, "/api/stock", id="510050.SH", dataset="etf")
    pa = st_a["profile"]
    check("A股档案", (pa["natal_year_pillar"], pa["natal_month_pillar"],
                      pa["natal_day_pillar"], pa["day_master_stem"]) == ("庚午", "戊子", "戊午", "戊"),
          f"{pa['natal_year_pillar']}/{pa['natal_month_pillar']}/{pa['natal_day_pillar']} 日主{pa['day_master_stem']}", "档案")
    check("ETF档案", (st_e["profile"]["natal_year_pillar"],
                      st_e["profile"]["natal_month_pillar"],
                      st_e["profile"]["natal_day_pillar"],
                      st_e["profile"]["day_master_stem"]) == ("乙酉", "戊寅", "戊寅", "戊"),
          "乙酉/戊寅/戊寅 日主戊", "档案")

    # 2. 12 月支
    for ds, ident in (("etf", "510050.SH"), ("ashare", "000001.SH")):
        rows = get(base, "/api/branch12", id=ident, dataset=ds)
        n = sum(r["n"] for r in rows)
        exp = db_count(ds, "SELECT COUNT(*) FROM monthly_facts WHERE identity_code=? AND profile_present=1", (ident,))
        check(f"12月支({ds})", len(rows) == 12 and n == exp, f"rows={len(rows)} sum_n={n} expected={exp}", "12月支")

    # 3. 60 流月柱
    p60_e = get(base, "/api/pillar60", id="510050.SH", dataset="etf")
    p60_a = get(base, "/api/pillar60", id="000001.SH", dataset="ashare")
    check("60柱(ETF)", len(p60_e) == 60, f"rows={len(p60_e)}", "60柱")
    check("60柱(A股)", len(p60_a) == 60, f"rows={len(p60_a)}", "60柱")
    jh = [r for r in p60_e if r["flow_pillar"] == "己亥"][0]
    check("60柱干十神", jh["stem_ten_god"] == "劫财", jh["stem_ten_god"], "十神")
    check("60柱藏干十神", jh.get("hidden_stem_summary") == "壬·偏财 / 甲·七杀",
          jh.get("hidden_stem_summary"), "十神")
    check("60柱计数守恒", jh["occurrence_count"] == 4 and
          jh["up_n_ALL"] + jh["down_n_ALL"] + jh["flat_n_ALL"] == jh["all_sample_n"] == 4,
          f"occ={jh['occurrence_count']} up/down/flat={jh['up_n_ALL']}/{jh['down_n_ALL']}/{jh['flat_n_ALL']}", "60柱")

    # 4. 冲合关系（接口参数为 type / pillar）
    rts = get(base, "/api/relation_types", id="510050.SH", dataset="etf")
    check("关系类型", len(rts) == 19 and rts[0]["months_any"] > 0,
          f"types={len(rts)} first={rts[0]['relation_type']} months={rts[0]['months_any']}", "冲合")
    rp = get(base, "/api/relation_pillars", id="510050.SH", dataset="etf", type="六合")
    pil = rp[0]["flow_pillar"] if rp else ""
    rm = get(base, "/api/relation_months", id="510050.SH", dataset="etf",
             type="六合", pillar=pil)
    check("关系月明细", len(rp) > 0 and len(rm) > 0
          and "stem_ten_god" in rm[0] and "hidden_stem_summary" in rm[0],
          f"六合 pillars={len(rp)} first={pil} months={len(rm)} "
          f"tg={rm[0].get('stem_ten_god') if rm else None}", "冲合")

    # 5. 十神统计与守恒
    for ds, ident, exp_total in (("etf", "510050.SH", 260), ("ashare", "000001.SH", None)):
        d = get(base, "/api/ten_god_stats", id=ident, dataset=ds, sample_mode="ALL")
        tot = sum(g["n"] for g in d["groups"])
        ok = (d.get("status") == "OK" and "error" not in d and len(d["groups"]) == 10
              and tot == d["accounting"]["total_rows"]
              and d["accounting"]["classified_n"] == tot
              and d["accounting"]["unclassified"] == 0
              and (exp_total is None or tot == exp_total))
        check(f"十神统计({ds})", ok,
              f"status={d['status']} groups={len(d['groups'])} sum={tot} acct={d['accounting']}", "十神统计")

    # 6. ALL vs EXCLUDE_SINGLE
    ident = "000536.SZ"
    a = get(base, "/api/ten_god_stats", id=ident, dataset="ashare", sample_mode="ALL")
    e = get(base, "/api/ten_god_stats", id=ident, dataset="ashare", sample_mode="EXCLUDE_SINGLE")
    sd = sum(g["single_day_n"] for g in a["groups"])
    ta, te = a["accounting"]["total_rows"], e["accounting"]["total_rows"]
    check("单交易日口径", ta - te == sd and sd > 0, f"ALL={ta} ES={te} single_day={sd}", "口径")
    con = sqlite3.connect(f"file:{DBS['ashare']}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    exp_sd = con.execute("SELECT SUM(single_day) FROM monthly_facts WHERE identity_code=?", (ident,)).fetchone()[0]
    con.close()
    check("单交易日与库一致", sd == exp_sd, f"api={sd} db={exp_sd}", "口径")

    # 7. 同期指数与超额
    g0 = [g for g in a["groups"] if g["index_comparison"]][0]
    check("同期指数与超额", g0["index_comparison"]["benchmark_n"] > 0 and
          g0["index_comparison"]["excess_mean"] is not None and g0["n"] > 0,
          f"{g0['ten_god']} bench_n={g0['index_comparison']['benchmark_n']} excess={g0['index_comparison']['excess_mean']}", "基准")

    # 8. 当前与未来流月
    cal = get(base, "/api/calendar_now", id="510050.SH", dataset="etf")
    cur = cal["current"]
    nxt = [m for m in cal["months"] if m["offset"] == 1][0]
    check("当前流月十神", cur["ten_god"]["stem"]["ten_god"] == "比肩" and cur["ten_god"]["status"] == "OK",
          f"{cur['month_id']} {cur['month_pillar']} {cur['ten_god']['stem']['ten_god']} status={cur['fact_status']}", "流月")
    check("下一流月十神", nxt["month_pillar"] == "己亥" and nxt["ten_god"]["stem"]["ten_god"] == "劫财"
          and nxt["ten_god"]["hidden_summary"] == "壬·偏财 / 甲·七杀",
          f"{nxt['month_pillar']} {nxt['ten_god']['stem']['ten_god']} {nxt['ten_god']['hidden_summary']}", "流月")
    fut = [m for m in cal["months"] if m["offset"] >= 1]
    # 设计：months 全量带 has_data；仅 current（顶层）与 offset=1 附 fact_status/ten_god
    check("未来月无行情",
          len(fut) > 0
          and all(m.get("has_data") is False for m in fut)
          and not any(k in fut[0] for k in ("body_return", "benchmark_return",
                                            "excess_return", "direction"))
          and nxt["fact_status"] == "NO_HISTORICAL_FACT"
          and cur["fact_status"] == "NO_HISTORICAL_FACT",
          f"future={len(fut)} has_data 全 False；无收益字段；current/next=NO_HISTORICAL_FACT", "流月")

    # 9. 黄金样本
    rows = get(base, "/api/pillar_detail", id="510050.SH", pillar="己亥", dataset="etf")
    exp = [("2006-M10", "UP", 0.185224, 0.041578),
           ("2011-M10", "DOWN", None, None),
           ("2016-M10", "UP", None, None),
           ("2021-M10", "UP", None, None)]
    ok = (len(rows) == 4
          and all(r["stem_ten_god"] == "劫财" for r in rows)
          and all(r["hidden_stem_summary"] == "壬·偏财 / 甲·七杀" for r in rows)
          and [r["month_id"] for r in rows] == [x[0] for x in exp]
          and [r["direction"] for r in rows] == [x[1] for x in exp])
    check("510050己亥四次", ok, " / ".join(f"{r['month_id']}:{r['direction']}" for r in rows), "黄金")
    check("2006-M10收益与超额", abs(rows[0]["body_return"] - 0.1852243276191623) < 1e-12
          and abs(rows[0]["excess_return"] - 0.04157860724299339) < 1e-12,
          f"body={rows[0]['body_return']} excess={rows[0]['excess_return']}", "黄金")

    # 10. 缺档案身份不猜十神
    miss = get(base, "/api/ten_god_stats", id="000003.SZ", dataset="ashare")
    con = sqlite3.connect(f"file:{DBS['ashare']}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    exp_miss = con.execute("SELECT COUNT(*) FROM monthly_facts WHERE identity_code='000003.SZ'").fetchone()[0]
    con.close()
    check("缺档案不猜十神", miss["status"] == "UNAVAILABLE_PROFILE"
          and sum(g["n"] for g in miss["groups"]) == 0
          and miss["unavailable"]["missing_profile"] == exp_miss,
          f"status={miss['status']} missing={miss['unavailable']['missing_profile']}/{exp_miss}", "缺档案")

    passed = sum(1 for r in RESULTS if r["pass"])
    out = {"base": base, "total": len(RESULTS), "passed": passed,
           "failed": len(RESULTS) - passed, "items": RESULTS}
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nTOTAL={len(RESULTS)} PASS={passed} FAIL={len(RESULTS) - passed}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
