#!/usr/bin/env python3
"""跨层组合（原四层共有）验收。

覆盖：
  1 默认（四层全选 + 全部命中）与显式参数结果逐数一致
  2 「任一层」= 参与层并集；「至少中两层」的三对并集 = 四块互斥区域之和
  3 两层 + 全部命中 = |A| + |B| - |A∪B|
  4 机会表 in_scope 必须等于 n_common：整月占比 <80% 的「旬+段」票不能被行查询吞掉
  5 tri_layers / tri_min 缺省、非法值回落到安全默认
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import api


def main() -> int:
    con = api.connect("ashare")
    cal = api.calendar_context(con)
    timeline = cal["past"] + ([cal["current"]] if cal["current"] else []) + cal["future"]
    offset = 0
    pillar = timeline[len(cal["past"])]["month_pillar"]
    base = {"offset": str(offset), "min_n": "3", "dir": "up", "policy": "ALL"}
    print(f"目标月 {pillar} offset={offset}")

    d = api._tri_common(con, dict(base), "ashare")
    e = api._tri_common(con, {**base, "tri_layers": "month,half,xun,week",
                              "tri_min": "4"}, "ashare")
    assert d["n_common"] == e["n_common"], (d["n_common"], e["n_common"])
    assert d["layers"] == ["month", "half", "xun", "week"] and d["min_hit"] == 4
    print(f"PASS 1 默认四层全中 {d['n_common']} 只，与显式四层+4 一致")

    one = api._tri_common(con, {**base, "tri_layers": "month,half,xun,week",
                                "tri_min": "1"}, "ashare")
    assert one["n_common"] == d["n_union"], (one["n_common"], d["n_union"])
    print(f"PASS 2 任一层 = 并集 {d['n_union']} 只")

    any2 = api._tri_common(con, {**base, "tri_layers": "month,xun,week",
                                 "tri_min": "2"}, "ashare")
    bd = any2["breakdown"]
    assert any2["n_common"] == (bd["month_xun"] + bd["month_week"] +
                                bd["xun_week"] + bd["all_three"]), (any2["n_common"], bd)
    print(f"PASS 3 月/旬/段至少中2层 {any2['n_common']} 只 = "
          f"月+旬 {bd['month_xun']} + 月+段 {bd['month_week']} + "
          f"旬+段 {bd['xun_week']} + 三层全中 {bd['all_three']}")

    mh = api._tri_common(con, {**base, "tri_layers": "month,half", "tri_min": "2"}, "ashare")
    assert mh["n_common"] == mh["n_month"] + mh["n_half"] - mh["n_union"], mh
    print(f"PASS 4 月+半全中 {mh['n_common']} 只，与 |月|+|半|−|并集| 一致")

    out = api.opportunities(con, {"tri": "1", "tri_layers": "month,xun,week",
                                  "tri_min": "2", "months": "1", "offset": "0",
                                  "min_n": "3", "dir": "up", "policy": "ALL",
                                  "cap": "500", "page": "1"}, "ashare")
    totals = out["months"][0]["totals"]
    assert totals["in_scope"] == out["tri"]["n_common"], (totals["in_scope"],
                                                          out["tri"]["n_common"])
    miss_month = sum(1 for r in out["rows"] if not r.get("tri_month_hit"))
    print(f"PASS 5 机会表 in_scope {totals['in_scope']} = n_common；"
          f"本页整月未命中但靠其它层通过的 {miss_month} 行")

    bad = api._tri_common(con, {**base, "tri_layers": "month,xxx",
                                "tri_min": "99"}, "ashare")
    assert bad["layers"] == ["month"] and bad["min_hit"] == 1, bad
    print("PASS 6 非法层名被忽略，达标数回落为参与层数")

    print("OK 跨层组合 6/6 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
