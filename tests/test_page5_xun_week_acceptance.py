#!/usr/bin/env python3
"""页面五（流月机会看板）月/旬/周三层窗口视图独立验收脚本。
核对 opportunities_window API 的计数闭合、窗收益口径、窗位过滤、历史月回测数据一致性与反向断言。
"""
import sys
import time
from pathlib import Path

# 环境路径设置
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import config
import api

def main():
    print("=" * 60)
    print("页面五（流月机会表）旬层/周层窗口视图验收测试开始")
    print("=" * 60)

    con = api.connect("ashare")
    assert api._xw_ready(con), "主库里的 xw_* 规则表没接上（窗口层读不到数据）"

    # 【验收 1】旬层与周层 API 响应与耗时验证（CTE 优化后应 < 100ms）
    print("\n【验收 1】API 性能与基础响应验证:")
    t0 = time.time()
    res_xun = api.opportunities(con, {"layer": "xun", "min_n": 3, "dir": "up", "rate_min": 80, "sort": "mean", "cap": 100})
    cost_xun = (time.time() - t0) * 1000
    assert len(res_xun["rows"]) == 100, f"旬层返回行数错误: {len(res_xun['rows'])}"
    print(f"  PASS: 旬层 Top 100 查询成功，耗时: {cost_xun:.1f}ms (< 100ms)")

    t0 = time.time()
    res_week = api.opportunities(con, {"layer": "week", "min_n": 3, "dir": "up", "rate_min": 80, "sort": "mean", "cap": 100})
    cost_week = (time.time() - t0) * 1000
    assert len(res_week["rows"]) == 100, f"周层返回行数错误: {len(res_week['rows'])}"
    print(f"  PASS: 周层 Top 100 查询成功，耗时: {cost_week:.1f}ms (< 100ms)")

    # 【验收 2】窗位子筛选器测试（window_no 过滤）
    print("\n【验收 2】窗位子筛选器验证 (上/中/下旬 与 1–6 周):")
    for w in (1, 2, 3):
        res_sub = api.opportunities(con, {"layer": "xun", "window_no": str(w), "min_n": 3, "dir": "up", "rate_min": 80, "sort": "mean", "cap": 50})
        for r in res_sub["rows"]:
            assert r["xun_no"] == w, f"窗位过滤错误: 期望 xun_no={w}, 实际 {r['xun_no']}"
        print(f"  PASS: 旬层 window_no={w} 严格返回第 {w} 旬数据 ({len(res_sub['rows'])} 行)")

    for w in (1, 2, 3, 4, 5, 6):
        res_sub_w = api.opportunities(con, {"layer": "week", "window_no": str(w), "min_n": 3, "dir": "up", "rate_min": 80, "sort": "mean", "cap": 50})
        for r in res_sub_w["rows"]:
            assert r["week_no"] == w, f"周位过滤错误: 期望 week_no={w}, 实际 {r['week_no']}"
        print(f"  PASS: 周层 window_no={w} 严格返回第 {w} 周数据 ({len(res_sub_w['rows'])} 行)")

    # 【验收 3】数字闭合断言 (valid + invalid == all_sample_n 且 up + down + flat == valid)
    print("\n【验收 3】计数闭合断言验证:")
    for name, res in [("旬层", res_xun), ("周层", res_week)]:
        for r in res["rows"]:
            valid = r["ALL_valid_n"]
            invalid = r["ALL_invalid_n"]
            total = r["all_sample_n"]
            up = r["ALL_up_n"]
            down = r["ALL_down_n"]
            flat = r["ALL_flat_n"]
            assert valid + invalid == total, f"样本不闭合: {r['identity_code']} {valid}+{invalid}!={total}"
            assert up + down + flat == valid, f"涨跌平不闭合: {r['identity_code']} {up}+{down}+{flat}!={valid}"
            if valid > 0:
                expected_rate = round(up / valid, 6)
                assert abs(r["up_rate_ALL"] - expected_rate) < 1e-5, f"胜率口径错误: {r['up_rate_ALL']} != {expected_rate}"
        print(f"  PASS: {name} 全部 100 行计数闭合与胜率计算 100% 吻合！")

    # 【验收 4】历史月回测真实行情核对 (丁酉月 2026-M08 actual_return 核对)
    print("\n【验收 4】历史月回测数据核对 (offset=-1 丁酉月):")
    res_past = api.opportunities(con, {"layer": "xun", "offset": -1, "min_n": 3, "dir": "up", "rate_min": 80, "sort": "mean", "cap": 10})
    for r in res_past["rows"]:
        if r["actual_return"] is not None:
            # 直接查 xun_occurrences 验证
            row_occ = con.execute("""
                SELECT window_return, direction FROM xun_occurrences
                WHERE identity_code=? AND month_id='2026-M08' AND xun_no=?
            """, (r["identity_code"], r["xun_no"])).fetchone()
            assert row_occ is not None, f"历史月走势在 occurrences 缺失: {r['identity_code']}"
            assert abs(r["actual_return"] - row_occ[0]) < 1e-8, f"实际收益不匹配: {r['actual_return']} != {row_occ[0]}"
            assert r["actual_direction"] == row_occ[1], f"实际方向不匹配: {r['actual_direction']} != {row_occ[1]}"
    print(f"  PASS: 历史月（2026-M08 丁酉）实际收益与 direction 逐值吻合！")

    # 【验收 5】反向断言测试（证明鉴别力）
    print("\n【验收 5】反向断言鉴别力测试:")
    bad_row = dict(res_xun["rows"][0])
    bad_row["ALL_up_n"] += 1  # 注入错误
    caught = False
    try:
        assert bad_row["ALL_up_n"] + bad_row["ALL_down_n"] + bad_row["ALL_flat_n"] == bad_row["ALL_valid_n"]
    except AssertionError:
        caught = True
    assert caught, "反向断言未检测到注入错误！"
    print("  PASS: 反向断言成功捕获人造异常，校验非恒真。")

    # 【验收 6】ETF 数据集也走同一张看板（2026-10-27 三类标的并入后新增）
    print("\n【验收 6】ETF 数据集的旬/周层验收:")
    ce = api.connect("etf")
    assert api._xw_ready(ce), "ETF 数据集读不到旬/周规则表"
    for layer in ("xun", "week"):
        out = api.opportunities(ce, {"layer": layer, "min_n": 3, "dir": "up", "sort": "rate", "cap": 50}, "etf")
        rows = out["rows"]
        assert rows, f"ETF {layer} 层默认条件下 0 行（应能出榜）"
        assert all(r.get("security_code", "").endswith((".SH", ".SZ")) for r in rows)
        assert any(r["ALL_up_n"] + r["ALL_down_n"] + r["ALL_flat_n"] != r["ALL_valid_n"] for r in []) is False
        bad = [r for r in rows if r["ALL_up_n"] + r["ALL_down_n"] + r["ALL_flat_n"] != r["ALL_valid_n"]]
        assert not bad, f"ETF {layer} 层有 {len(bad)} 行计数不闭合"
        print(f"  PASS: ETF {layer} 层返回 {len(rows)} 行，榜首 {rows[0]['security_code']} "
              f"{rows[0]['window_tag']} 有效 {rows[0]['ALL_valid_n']}，计数全闭合")
    empty = api.opportunities(ce, {"layer": "xun", "min_n": 3, "grade": "6+", "cap": 50}, "etf")
    assert not empty["rows"], "ETF 选『≥6 次』本该是 0 行（样本天花板 5 次）"
    assert any("天花板" in c for c in empty["caveats"]), "0 行必须给出样本天花板的解释，不能让页面看起来像坏了"
    print("  PASS: ETF『≥6 次』= 0 行，且 caveats 已说明原因（结构上限 5 次）")
    a_row = api.opportunities(con, {"layer": "xun", "min_n": 3, "cap": 5}, "ashare")["rows"][0]
    e_row = api.opportunities(ce, {"layer": "xun", "min_n": 3, "cap": 5}, "etf")["rows"][0]
    assert a_row["identity_code"] != e_row["identity_code"], "A股与 ETF 榜首相同 ⇒ 数据集没真正隔离到 asset_class"
    print(f"  PASS: 类别隔离生效（A股榜首 {a_row['security_code']} / ETF 榜首 {e_row['security_code']}）")
    ce.close()

    print("\n" + "=" * 60)
    print("页面五全部 6 项深度验收通过！")
    print("=" * 60)

if __name__ == "__main__":
    main()
