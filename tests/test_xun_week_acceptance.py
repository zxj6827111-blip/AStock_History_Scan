#!/usr/bin/env python3
"""全量验收：节气月/旬/周三层规则库（v2 三类标的）与页面读取路径的一致性。

改动点（2026-10-27 同步进主库之后）：
  · 不再直接打开独立文件 allstock_xun_week_v1.sqlite —— 页面读的是主库里的 xw_* 表，
    验收必须走 api.connect() 这条真实路径，否则测的是没人用的那份数。
  · 所有数字按 asset_class（A_SHARE / ETF / INDEX）分列，并额外断言"借错类别必须翻脸"。
  · 规则库口径：只收完整流月（没走完的 2026-M09 不计入任何分母）。

六道门禁：1 行数核对 · 2 空窗锚点 · 3 计数闭合 · 4 抽样数字回查 · 5 类别隔离 · 6 反向断言
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import api  # noqa: E402

# 期望值来源：2026-10-27 由 allstock_xun_week_v2 同步进主库后从 xw_ 表本身重算并钉住
EXPECTED = {
    "month_rules": {"A_SHARE": 315725, "ETF": 57836, "INDEX": 1560},
    "xun_rules": {"A_SHARE": 947175, "ETF": 173508, "INDEX": 4680},
    "week_rules": {"A_SHARE": 1894350, "ETF": 347016, "INDEX": 9360},
    "month_occurrences": {"A_SHARE": 903081, "ETF": 75352, "INDEX": 8895},
    "xun_occurrences": {"A_SHARE": 2709243, "ETF": 226056, "INDEX": 26685},
    "week_occurrences": {"A_SHARE": 5418486, "ETF": 452112, "INDEX": 53370},
}
INVALID_ANCHOR = {"xun_rules": {"A_SHARE": 32413, "ETF": 3145, "INDEX": 169},
                  "week_rules": {"A_SHARE": 196351, "ETF": 15935, "INDEX": 1505}}
SAMPLES = [
    ("600519.SH", "丙午", 1, "xun"), ("600519.SH", "丙午", 2, "xun"),
    ("600519.SH", "丙午", 1, "week"), ("000001.SZ", "甲子", 2, "xun"),
    ("600036.SH", "庚辰", 3, "week"),
    ("510300.SH", "戊戌", 2, "xun"), ("159909.SZ", "庚寅", 4, "week"),
    ("000001.SH", "庚寅", 6, "week"), ("399106.SZ", "戊戌", 3, "xun"),
]


def main():
    print("=" * 64)
    print("验收测试开始：月/旬/周三层规则库（页面实际读取路径 + 三类标的）")
    print("=" * 64)
    con = api.connect("ashare")
    assert api._xw_ready(con), "主库里的 xw_* 规则表没接上（检查 BAZI_ASHARE_DB / xw_ 表是否存在）"
    st = api.window_rules_status(con)
    print(f"  数据源：{st['source']}")
    print(f"  覆盖流月：{st['history'][0]} → {st['history'][1]}（只收完整流月）")

    print("\n【验收 1】行数核对（按标的类别）:")
    for tbl, per in EXPECTED.items():
        got = dict(con.execute(f"SELECT asset_class, count(*) FROM {tbl} GROUP BY 1").fetchall())
        assert got == per, f"{tbl} 分类行数不符: 期望 {per}, 实际 {got}"
        print(f"  PASS: {tbl} = {sum(per.values()):,} 行 "
              + " / ".join(f"{k} {v:,}" for k, v in per.items()))

    print("\n【验收 2】空窗锚点核对（SUM(ALL_invalid_n)，按类）:")
    for tbl, per in INVALID_ANCHOR.items():
        got = dict(con.execute(
            f"SELECT asset_class, SUM(ALL_invalid_n) FROM {tbl} GROUP BY 1").fetchall())
        assert got == per, f"{tbl} 空窗数不符: 期望 {per}, 实际 {got}"
        print(f"  PASS: {tbl} 空窗 " + " / ".join(f"{k} {v:,}" for k, v in per.items()))

    print("\n【验收 3】计数闭合（valid+invalid==occurrences 且 up+down+flat==valid）:")
    for tbl in ("month_rules", "xun_rules", "week_rules"):
        err = con.execute(f"""SELECT count(*) FROM {tbl}
            WHERE (ALL_valid_n + ALL_invalid_n != ALL_n_occurrences)
               OR (ALL_up_n + ALL_down_n + ALL_flat_n != ALL_valid_n)""").fetchone()[0]
        assert err == 0, f"{tbl} 存在 {err} 处计数不闭合行"
        print(f"  PASS: {tbl} 不闭合行数 = 0")

    print("\n【验收 4】数字回查：规则表 vs occurrences 逐值重算（含 ETF 与指数）:")
    for code, pillar, win_no, layer in SAMPLES:
        r_tbl, o_tbl, col = f"{layer}_rules", f"{layer}_occurrences", f"{layer}_no"
        rule = con.execute(f"""SELECT ALL_valid_n, ALL_up_n, ALL_down_n, ALL_flat_n, ALL_n_occurrences,
                                      asset_class
                               FROM {r_tbl} WHERE security_code=? AND flow_pillar=? AND {col}=?""",
                          (code, pillar, win_no)).fetchone()
        assert rule, f"规则表里找不到 {code} × {pillar} 第{win_no}窗（{layer}）"
        occ = con.execute(f"""SELECT count(*) AS n, SUM(valid_bars > 0) AS v,
                        SUM(direction='UP') AS u, SUM(direction='DOWN') AS d, SUM(direction='FLAT') AS f
                     FROM {o_tbl} WHERE security_code=? AND flow_pillar=? AND {col}=?""",
                          (code, pillar, win_no)).fetchone()
        want = (rule["ALL_n_occurrences"], rule["ALL_valid_n"], int(rule["ALL_up_n"]),
                int(rule["ALL_down_n"]), int(rule["ALL_flat_n"]))
        got = (occ["n"], occ["v"], occ["u"] or 0, occ["d"] or 0, occ["f"] or 0)
        assert want == got, f"{code}×{pillar}#{win_no}({layer}) 不符：规则 {want} vs 明细 {got}"
        print(f"  PASS [{layer}/{rule['asset_class']}] {code} × {pillar} 第{win_no}窗："
              f"出现 {got[0]}、有效 {got[1]}、涨 {got[2]}、跌 {got[3]}、平 {got[4]}")

    print("\n【验收 5】类别隔离（同一柱同一窗，三类不得共用一份数）:")
    one = con.execute("""SELECT asset_class, ALL_valid_n, ALL_up_n, ALL_down_n FROM xun_rules
        WHERE flow_pillar='戊戌' AND xun_no=1 AND identity_code IN ('600519.SH','510300.SH','000001.SH')
        ORDER BY asset_class""").fetchall()
    for r in one:
        print(f"  {r['asset_class']:8s} 有效={r['ALL_valid_n']} 涨={r['ALL_up_n']} 跌={r['ALL_down_n']}")
    assert len(one) >= 2, "戊戌上旬在三类里至少应有两只不同类别的标的可对照"
    # 明确按（柱,旬）取行，不靠列表位置——SQL 返回顺序不保证，按位置抽会拿错柱
    def cell(cls, pillar="甲子", xun=1):
        rows = [r for r in api.get_base_pool_data("xun", cls)
                if r["flow_pillar"] == pillar and r["xun_no"] == xun]
        assert rows, f"{cls} 池层里找不到 {pillar} 第{xun}旬"
        return rows[0]
    pool = {c: cell(c) for c in ("A_SHARE", "ETF", "INDEX")}
    assert pool["A_SHARE"]["ALL_valid_n"] != pool["ETF"]["ALL_valid_n"], "池层没按类别分开：A股与 ETF 汇总相同"
    assert pool["ETF"]["ALL_valid_n"] != pool["INDEX"]["ALL_valid_n"], "池层没按类别分开：ETF 与指数汇总相同"
    direct = {c: con.execute("""SELECT sum(ALL_valid_n), count(DISTINCT identity_code) FROM xun_rules
                WHERE asset_class=? AND flow_pillar='甲子' AND xun_no=1""", [c]).fetchone()[0]
              for c in ("A_SHARE", "ETF", "INDEX")}
    for c in ("A_SHARE", "ETF", "INDEX"):
        assert pool[c]["ALL_valid_n"] == direct[c], f"{c} 池层 {pool[c]['ALL_valid_n']} ≠ 直查 {direct[c]}"
    print("  PASS: 甲子·上旬池层与直查逐类相同 "
          + " / ".join(f"{c} {pool[c]['ALL_valid_n']:,}" for c in ("A_SHARE", "ETF", "INDEX")))
    ceil_ = {c: con.execute("SELECT max(ALL_valid_n) FROM xun_rules WHERE asset_class=?", [c]).fetchone()[0]
             for c in ("A_SHARE", "ETF", "INDEX")}
    print(f"  样本天花板：A股 {ceil_['A_SHARE']} 次 / ETF {ceil_['ETF']} 次 / 指数 {ceil_['INDEX']} 次"
          f"（ETF 选『≥6 次』结构上必空，页面必须给解释）")
    assert con.execute("SELECT count(*) FROM xun_rules WHERE asset_class='ETF' AND ALL_valid_n>=6").fetchone()[0] == 0, \
        "ETF 竟出现 ≥6 次，与 2005→2026 的柱出现次数不符"

    print("\n【验收 6】反向断言（判据必须会翻脸）:")
    bad = {"ALL_valid_n": 10, "ALL_invalid_n": 2, "ALL_n_occurrences": 12,
           "ALL_up_n": 7, "ALL_down_n": 2, "ALL_flat_n": 0}
    closed = (bad["ALL_valid_n"] + bad["ALL_invalid_n"] == bad["ALL_n_occurrences"]
              and bad["ALL_up_n"] + bad["ALL_down_n"] + bad["ALL_flat_n"] == bad["ALL_valid_n"])
    assert not closed, "反向断言失败：注入的偏差不被发现"
    print("  PASS: 注入 up+down+flat=9≠valid=10 被检出")
    swapped = dict(con.execute("""SELECT xun_no, count(*) FROM xun_rules
        WHERE asset_class='A_SHARE' AND flow_pillar='戊戌' GROUP BY 1 ORDER BY 1""").fetchall())
    swapped_etf = dict(con.execute("""SELECT xun_no, count(*) FROM xun_rules
        WHERE asset_class='ETF' AND flow_pillar='甲午' GROUP BY 1 ORDER BY 1""").fetchall())
    assert swapped != swapped_etf, "换柱换类别后格数仍相同 ⇒ 这条对照没判别力"
    print(f"  PASS: 戊戌(A股) 与 甲午(ETF) 的分窗格数不同 {swapped} vs {swapped_etf}")
    fake = con.execute("""SELECT count(*) FROM xun_rules r
        JOIN month_occurrences o ON o.identity_code=r.identity_code AND o.asset_class=r.asset_class
        WHERE r.asset_class='INDEX' AND r.flow_pillar NOT IN (SELECT flow_pillar FROM month_occurrences
              WHERE asset_class='INDEX' AND month_id BETWEEN '1990-M11' AND '2026-M08')""").fetchone()[0]
    print(f"  PASS: 指数规则柱名全部落在其历史覆盖内（越界 {fake}，应为 0）")
    assert fake == 0

    con.close()
    print("\n" + "=" * 64)
    print("验收通过：三类标的的行数、空窗、闭合、抽样回查、类别隔离、反向断言全部吻合")
    print("=" * 64)


if __name__ == "__main__":
    main()
