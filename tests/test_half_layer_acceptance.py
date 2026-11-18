#!/usr/bin/env python3
"""半月层（节半月 / 气半月）专项验收。

覆盖：
  1 当前窗口：half_no / half_name / zq_at / zq_name 与日历一致
  2 机会表：两半各 1 行/柱、窗位过滤严格、计数闭合、历史月 actual 与 occurrences 逐值一致
  3 个股页：window_layer_stock 返回 2 行（节/气），window_layer_pool 返回 120 行
  4 抽屉明细：window_cell_detail 与 half_rules 的计数一致
  5 N≥4：结果全部 valid_n>=4，且是 ≥3 的子集、≥6 的超集
  6 未来层：xw_future_half_view 582 行、每月 2 行、节半月尾=气半月头=中气
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import api
import config


def main() -> int:
    con = api.connect("ashare")
    assert api._half_ready(con), "半月层不可用：half_rules / half_occurrences / zhongqi 没有同时接上"

    # 1 当前窗口
    cal = api.calendar_now(con)
    cw = cal["current_window"]
    assert cw["half_no"] in (1, 2), f"当前半月号异常: {cw}"
    assert cw["half_name"] in ("节半月", "气半月"), f"当前半月名异常: {cw}"
    assert cw["zq_at"], "当前月缺中气时刻"
    print(f"PASS 1 当前月 {cal['current']['month_id']} {cal['current']['month_pillar']} "
          f"→ {cw['half_name']}（中气 {cw['zq_name']} {cw['zq_at']}，{cw['half_left_str']}）")

    # 2 机会表：当前 + 历史月
    cur = api.opportunities(con, {"layer": "half", "min_n": 3, "dir": "up", "cap": 30}, "ashare")
    assert cur["rows"], "半月当前月 0 行"
    for r in cur["rows"]:
        assert r["window_tag"] in ("节半月（节→中气）", "气半月（中气→下节）"), r["window_tag"]
        assert r["ALL_up_n"] + r["ALL_down_n"] + r["ALL_flat_n"] == r["ALL_valid_n"], "计数不闭合"
    print(f"PASS 2a 当前月半月机会表 {len(cur['rows'])} 行，窗位名与计数闭合正常")

    for w in (1, 2):
        sub = api.opportunities(con, {"layer": "half", "window_no": str(w), "min_n": 3,
                                      "dir": "up", "cap": 30}, "ashare")
        assert sub["rows"], f"半月窗位 {w} 0 行"
        assert all(int(r["half_no"]) == w for r in sub["rows"]), f"窗位 {w} 过滤不严格"
    print("PASS 2b 节半月 / 气半月 窗位过滤严格")

    past = api.opportunities(con, {"layer": "half", "offset": -1, "min_n": 3,
                                   "dir": "up", "cap": 10}, "ashare")
    past_mid = past["months"][0]["month_id"]
    for r in past["rows"]:
        if r["actual_return"] is None:
            continue
        row = con.execute(
            "SELECT window_return, direction FROM half_occurrences"
            " WHERE identity_code=? AND month_id=? AND half_no=?",
            (r["identity_code"], past_mid, r["half_no"])).fetchone()
        assert row is not None, f"{r['identity_code']} {past_mid} half {r['half_no']} 在明细缺失"
        assert abs(r["actual_return"] - row[0]) < 1e-9, f"actual 不匹配 {r['actual_return']} != {row[0]}"
        assert r["actual_direction"] == row[1], f"方向不匹配 {r['actual_direction']} != {row[1]}"
    print(f"PASS 2c 历史月 {past_mid} 半月 actual 与 occurrences 逐值一致")

    # 3 个股页
    pick = cur["rows"][0]
    st = api.window_layer_stock(con, "half", pick["identity_code"], {"min_n": 1, "dir": "up"})
    assert st, "个股页半月 0 行"
    assert {int(x["half_no"]) for x in st} == {1, 2}, "个股页半月应同时有节/气两行"
    pool = api.window_layer_pool("half", {"dir": "up"}, "A_SHARE")
    assert len(pool) == 120, f"池层半月的 60 柱×2 应为 120，实际 {len(pool)}"
    print(f"PASS 3 个股页 {pick['identity_code']} 半月 2 行；池层半月 {len(pool)} 行")

    # 4 抽屉明细 vs 规则计数
    detail = api.window_cell_detail(pick["identity_code"], pick["flow_pillar"], "half", pick["half_no"])
    assert detail is not None and len(detail) == int(pick["ALL_valid_n"]), (
        f"明细条数 {len(detail)} 与规则 valid_n {pick['ALL_valid_n']} 不符")
    print(f"PASS 4 抽屉明细 {len(detail)} 条 = 规则 valid_n {pick['ALL_valid_n']}")

    # 5 N≥4
    n3 = api.opportunities(con, {"layer": "half", "min_n": 3, "dir": "up", "cap": 5000}, "ashare")
    n4 = api.opportunities(con, {"layer": "half", "min_n": 4, "dir": "up", "cap": 5000}, "ashare")
    n6 = api.opportunities(con, {"layer": "half", "min_n": 6, "dir": "up", "cap": 5000}, "ashare")
    assert all(int(r["ALL_valid_n"]) >= 4 for r in n4["rows"]), "N≥4 里出现了 <4 的行"
    assert all(int(r["ALL_valid_n"]) >= 6 for r in n6["rows"]), "N≥6 里出现了 <6 的行"
    pillar = n4["rows"][0]["flow_pillar"]
    c3, c4, c6 = (con.execute(
        "SELECT COUNT(*) FROM half_rules WHERE asset_class='A_SHARE' AND flow_pillar=?"
        " AND ALL_valid_n>=?", (pillar, k)).fetchone()[0] for k in (3, 4, 6))
    assert c4 <= c3 and c6 <= c4, f"N≥4 计数不是 ≥3 的子集或 ≥6 的超集：3={c3} 4={c4} 6={c6}"
    print(f"PASS 5 N≥4：{pillar} 计数 3={c3} / 4={c4} / 6={c6}，子集关系成立")

    # 6 未来层
    w = sqlite3.connect(str(config.ASHARE_DB))
    try:
        n = w.execute("SELECT count(*) FROM xw_future_half_view").fetchone()[0]
        bad_m = w.execute("SELECT count(*) FROM (SELECT month_id, count(*) c"
                          " FROM xw_future_half_view GROUP BY 1 HAVING c<>2)").fetchone()[0]
        bad_seq = w.execute("""SELECT count(*) FROM xw_future_half_view a
            JOIN xw_future_half_view b ON a.month_id=b.month_id AND a.half_no=1 AND b.half_no=2
            WHERE a.window_end_at <> b.window_start_at OR a.zq_at <> b.zq_at""").fetchone()[0]
        zq = w.execute("SELECT count(*) FROM xw_zhongqi").fetchone()[0]
    finally:
        w.close()
    assert n == 582, f"未来半月应 582 行，实际 {n}"
    assert bad_m == 0 and bad_seq == 0, f"未来半月结构异常 months={bad_m} seq={bad_seq}"
    print(f"PASS 6 未来半月 {n} 行，每月 2 行、节/气首尾=中气；中气表 {zq} 行")

    # 7 四层共有：整月 + 两半 + 旬 + 段
    tri = api._tri_common(con, {"offset": 0, "min_n": 3, "dir": "up", "policy": "ALL"}, "ashare")
    assert tri["n_half"] > 0, "四层共有里半月边际为 0"
    assert tri["n_common"] <= min(tri["n_month"], tri["n_half"], tri["n_xun"], tri["n_week"]), "四层交集大于任一单层"
    tri_out = api.opportunities(con, {"tri": "1", "offset": 0, "min_n": "3", "dir": "up", "cap": 5}, "ashare")
    assert tri_out["tri"]["n_half"] == tri["n_half"], "页面 tri 的半月边际与 API 不一致"
    tr = tri_out["rows"][0]
    assert len(tr["tri_detail"]["half"]) == 2, "四层共有行缺两半明细"
    print(f"PASS 7 四层共有：整月 {tri['n_month']} / 半 {tri['n_half']} / 旬 {tri['n_xun']} / "
          f"段 {tri['n_week']} → 交集 {tri['n_common']}")

    con.close()
    print("\n半月层专项验收全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
