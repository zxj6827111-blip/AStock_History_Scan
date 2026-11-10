"""P7-C：V2 十神×冲合联合研究——只读可行性盘点。

不写原库、不做参数搜索；只统计样本覆盖、稀疏度、缺失与多重关系情况。
输出 evidence/p7_v2_feasibility.json。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path("/Volumes/workSSD/software development/AStock_History_Scan")
DBS = {
    "ashare": ROOT / "bazi_month_relation_local_v1/database/bazi_month_relation_local_v1.sqlite",
    "etf": ROOT / "bazi_month_relation_etf_v1/database/bazi_month_relation_etf_v1.sqlite",
}
OUT = ROOT / "bazi_month_relation_tengod_dev_v1/evidence/p7_v2_feasibility.json"


def connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def scalar(con, sql, params=()):
    return con.execute(sql, params).fetchone()[0]


def rows(con, sql, params=()):
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def stats(con, dataset: str) -> dict:
    out = {"dataset": dataset, "db": str(DBS[dataset])}
    out["monthly_facts"] = scalar(con, "SELECT COUNT(*) FROM monthly_facts")
    out["securities"] = scalar(con, "SELECT COUNT(*) FROM securities")
    out["with_profile"] = scalar(con, "SELECT COUNT(*) FROM natal_profiles WHERE profile_present=1")
    out["without_profile"] = scalar(con, "SELECT COUNT(*) FROM natal_profiles WHERE profile_present=0")
    out["no_profile_months"] = scalar(con, "SELECT COUNT(*) FROM monthly_facts WHERE profile_present=0")
    out["single_day_months"] = scalar(con, "SELECT COUNT(*) FROM monthly_facts WHERE single_day=1")
    out["date_range"] = rows(con, "SELECT MIN(month_id) first_month, MAX(month_id) last_month FROM monthly_facts")[0]

    out["ten_god_counts"] = rows(con, """
      SELECT stem_ten_god ten_god, COUNT(*) n FROM monthly_facts
      WHERE stem_ten_god IS NOT NULL GROUP BY 1 ORDER BY n DESC""")
    out["branch_counts"] = rows(con, """
      SELECT month_branch branch, COUNT(*) n FROM monthly_facts GROUP BY 1 ORDER BY branch""")
    out["pillar_count"] = scalar(con, "SELECT COUNT(DISTINCT flow_pillar) FROM monthly_facts")
    out["day_master_counts"] = rows(con, """
      SELECT day_master_stem stem, COUNT(*) n FROM monthly_facts
      WHERE day_master_stem IS NOT NULL GROUP BY 1 ORDER BY stem""")
    out["flow_stem_counts"] = rows(con, """
      SELECT flow_stem stem, COUNT(*) n FROM monthly_facts GROUP BY 1 ORDER BY stem""")

    out["months_per_identity"] = rows(con, """
      WITH m AS (SELECT identity_code, COUNT(*) n FROM monthly_facts GROUP BY 1)
      SELECT MIN(n) min_n, MAX(n) max_n, AVG(n) avg_n,
             SUM(n>=60) identities_ge_60, COUNT(*) identities
      FROM m""")[0]

    out["ten_god_x_branch"] = rows(con, """
      SELECT stem_ten_god ten_god, month_branch branch, COUNT(*) n
      FROM monthly_facts WHERE stem_ten_god IS NOT NULL
      GROUP BY 1,2""")
    out["ten_god_x_pillar"] = rows(con, """
      SELECT stem_ten_god ten_god, flow_pillar pillar, COUNT(*) n
      FROM monthly_facts WHERE stem_ten_god IS NOT NULL
      GROUP BY 1,2""")

    out["benchmark"] = rows(con, """
      SELECT benchmark_code, comparison_status, COUNT(*) n
      FROM monthly_facts GROUP BY 1,2 ORDER BY n DESC""")
    out["benchmark_matched"] = scalar(con, """
      SELECT SUM(comparison_status='MATCHED') FROM monthly_facts""")

    out["relation_type_count"] = scalar(con, """
      SELECT COUNT(DISTINCT relation_type) FROM month_relation_types WHERE record_kind='OFFICIAL'""")
    out["relation_month_rows"] = scalar(con, """
      SELECT COUNT(*) FROM month_relation_types WHERE record_kind='OFFICIAL'""")
    out["relation_distinct_months"] = scalar(con, """
      SELECT COUNT(DISTINCT month_id) FROM month_relation_types WHERE record_kind='OFFICIAL'""")
    out["types_per_month"] = rows(con, """
      WITH m AS (
        SELECT identity_code, month_id, COUNT(DISTINCT relation_type) k
        FROM month_relation_types WHERE record_kind='OFFICIAL'
        GROUP BY 1,2)
      SELECT MIN(k) min_k, MAX(k) max_k, AVG(k) avg_k,
             SUM(k>1) months_multi, COUNT(*) months
      FROM m""")[0]
    out["relation_type_top"] = rows(con, """
      SELECT relation_type, COUNT(DISTINCT month_id) months
      FROM month_relation_types WHERE record_kind='OFFICIAL'
      GROUP BY 1 ORDER BY months DESC LIMIT 25""")

    out["stats_tables"] = {
        "stats_pillar60": scalar(con, "SELECT COUNT(*) FROM stats_pillar60"),
        "stats_branch12": scalar(con, "SELECT COUNT(*) FROM stats_branch12"),
        "stats_relation_types": scalar(con, "SELECT COUNT(*) FROM stats_relation_types"),
        "stats_relation_pillar": scalar(con, "SELECT COUNT(*) FROM stats_relation_pillar"),
    }
    out["pillar60_cells_ge6"] = scalar(con, """
      SELECT COUNT(*) FROM stats_pillar60 WHERE occurrence_count>=6""")
    out["pillar60_cells"] = scalar(con, "SELECT COUNT(*) FROM stats_pillar60")
    out["year_counts"] = rows(con, """
      SELECT solar_year year, COUNT(*) months FROM monthly_facts GROUP BY 1 ORDER BY year""")

    # 十神与流月天干在各自身份内是否一一对应（同身份同天干不应出现两个十神）
    out["same_identity_stem_multi_god"] = scalar(con, """
      SELECT COUNT(*) FROM (
        SELECT identity_code, flow_stem, COUNT(DISTINCT stem_ten_god) k
        FROM monthly_facts WHERE stem_ten_god IS NOT NULL
        GROUP BY 1,2 HAVING k>1)""")
    # 同一流月天干在不同日主下可对应不同十神（跨身份可分离性）
    out["stem_to_gods"] = rows(con, """
      SELECT flow_stem stem, COUNT(DISTINCT stem_ten_god) god_kinds,
             group_concat(DISTINCT stem_ten_god) gods
      FROM monthly_facts WHERE stem_ten_god IS NOT NULL
      GROUP BY 1 ORDER BY stem""")
    return out


def main() -> None:
    result = {"generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
              "datasets": {}}
    for ds, path in DBS.items():
        con = connect(path)
        try:
            result["datasets"][ds] = stats(con, ds)
        finally:
            con.close()
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    for ds, d in result["datasets"].items():
        print(f"=== {ds} ===")
        print("facts", d["monthly_facts"], "profiles", d["with_profile"], "/", d["securities"],
              "missing_months", d["no_profile_months"], "single_day", d["single_day_months"])
        print("range", d["date_range"], "pillars", d["pillar_count"])
        print("months/identity", d["months_per_identity"])
        print("ten_god_x_branch cells", len(d["ten_god_x_branch"]),
              "min_n", min(x["n"] for x in d["ten_god_x_branch"]),
              "max_n", max(x["n"] for x in d["ten_god_x_branch"]))
        print("ten_god_x_pillar cells", len(d["ten_god_x_pillar"]))
        print("relation types", d["relation_type_count"], "rows", d["relation_month_rows"],
              "distinct months", d["relation_distinct_months"], "types/month", d["types_per_month"])
        print("benchmark matched", d["benchmark_matched"], "/", d["monthly_facts"])
        print("same identity stem multi god", d["same_identity_stem_multi_god"])
        print("stem->gods", [(x["stem"], x["god_kinds"]) for x in d["stem_to_gods"]])
    print("WROTE", OUT)


if __name__ == "__main__":
    main()
