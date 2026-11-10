"""把 N2 日级影子库按节气月聚合成「流月聚合」CSV（供 /n2 页面的流月视图读取）。

口径（与 N2-5 一致，不新增规则）：
  - 日级涨跌 = qfq_open → qfq_close；方向按 K 线实体；平盘计入分母
  - 月收益（个股）= 该股当月首个交易日开盘 → 最后一个交易日收盘（前复权）
      含 return_valid=0 的个股月份不计入月收益（保留在股票日计数里）
  - 流月柱取自主库 monthly_facts.flow_pillar（整月同值，属日历量，不是行情）
  - 只聚合 A 股；指数对照行不进入本表

用法：
  python3 scripts/export_n2_month_agg.py \
      --db <N2 库> --monthly-db <月级库> --out app/data/n2_month_agg.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import time
from array import array
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
DEFAULT_N2 = (BASE.parent / "daily_relation_shadow_fullmarket_v1/database/"
              "daily_relation_shadow_fullmarket_v1.sqlite")
DEFAULT_MONTHLY = (BASE.parent / "bazi_month_relation_local_v1/database/"
                   "bazi_month_relation_local_v1.sqlite")


def median(sorted_vals) -> float | None:
    n = len(sorted_vals)
    if not n:
        return None
    h = (n - 1) // 2
    return sorted_vals[h] if n % 2 else (sorted_vals[h] + sorted_vals[h + 1]) / 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_N2))
    ap.add_argument("--monthly-db", default=str(DEFAULT_MONTHLY))
    ap.add_argument("--out", default=str(BASE / "app/data/n2_month_agg.csv"))
    ap.add_argument("--live", default=str(BASE.parent / "source_input/live/current_month.sqlite"),
                    help="用于判断最后一个月是否走完；不存在则全部当月都算完整")
    args = ap.parse_args()
    t0 = time.time()

    pillars = {}
    mc = sqlite3.connect(f"file:{args.monthly_db}?mode=ro", uri=True)
    for month_id, pillar in mc.execute(
            "SELECT month_id, flow_pillar FROM monthly_facts GROUP BY month_id"):
        pillars[month_id] = pillar
    mc.close()

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA cache_size=-131072")

    months: dict[str, dict] = {}
    stock_month_returns: dict[str, array] = {m: array("d") for m in pillars}

    cur = con.execute(
        "SELECT identity_code, month_id, trade_date, qfq_open, qfq_close, "
        "open_close_return, day_body_direction, return_valid "
        "FROM daily_facts WHERE asset_class='A_SHARE' "
        "ORDER BY identity_code, trade_date")

    prev_identity = None
    m_first_open = m_first_date = m_last_close = m_last_date = None
    m_days = m_valid = 0

    def flush(identity, month_id):
        nonlocal m_first_open, m_first_date, m_last_close, m_last_date, m_days, m_valid
        if month_id is None:
            return
        st = months.setdefault(month_id, {
            "n_stock_days": 0, "n_up": 0, "n_down": 0, "n_flat": 0,
            "n_valid": 0, "ret_sum": 0.0, "identities": set(), "dates": set(),
            "n_month_up": 0, "n_month_valid": 0, "month_sum": 0.0,
        })
        if m_first_open and m_first_open > 0 and m_valid == m_days:
            r = m_last_close / m_first_open - 1.0
            stock_month_returns.setdefault(month_id, array("d")).append(r)
            st["n_month_valid"] += 1
            st["month_sum"] += r
            if r > 0:
                st["n_month_up"] += 1

    identity = month_id = None
    for (code, mid, date, o, c, ret, direction, valid) in cur:
        if code != identity or mid != month_id:
            flush(identity, month_id)
            identity, month_id = code, mid
            m_first_open, m_first_date = o, date
            m_last_close, m_last_date = c, date
            m_days = 0
            m_valid = 0
        st = months.setdefault(mid, {
            "n_stock_days": 0, "n_up": 0, "n_down": 0, "n_flat": 0,
            "n_valid": 0, "ret_sum": 0.0, "identities": set(), "dates": set(),
            "n_month_up": 0, "n_month_valid": 0, "month_sum": 0.0,
        })
        st["n_stock_days"] += 1
        st["identities"].add(code)
        st["dates"].add(date)
        st["n_valid"] += valid
        st["ret_sum"] += ret if valid else 0.0
        st["n_up"] += direction == "UP"
        st["n_down"] += direction == "DOWN"
        st["n_flat"] += direction == "FLAT"
        m_days += 1
        m_valid += valid
        m_last_close, m_last_date = c, date
    flush(identity, month_id)
    con.close()

    # 当月未走完（当月层切片还停在月中）就不进月聚合，避免拿半个月和整月比
    partial = None
    try:
        lv = sqlite3.connect(f"file:{args.live}?mode=ro", uri=True)
        lmeta = dict(lv.execute("SELECT k, v FROM meta").fetchall())
        lv.close()
        cur_month = lmeta.get("month_id")
        end_at = (lmeta.get("end_at") or "")[:10]
        if cur_month and cur_month in months and end_at:
            last_day = max(months[cur_month]["dates"])
            if end_at > last_day:
                partial = cur_month
    except (sqlite3.Error, OSError):
        partial = None

    rows = []
    pooled = array("d")
    for month_id in sorted(months):
        if month_id == partial:
            continue
        st = months[month_id]
        rets = stock_month_returns.get(month_id, array("d"))
        rets = sorted(rets)
        pooled.extend(rets)
        n_id = len(st["identities"])
        n_valid = st["n_valid"]
        rows.append({
            "month_id": month_id,
            "month_pillar": pillars.get(month_id, ""),
            "first_date": min(st["dates"]) if st["dates"] else "",
            "last_date": max(st["dates"]) if st["dates"] else "",
            "n_dates": len(st["dates"]),
            "n_identities": n_id,
            "n_stock_days": st["n_stock_days"],
            "n_up": st["n_up"],
            "n_down": st["n_down"],
            "n_flat": st["n_flat"],
            "n_return_valid": n_valid,
            "mean_day_pct": (st["ret_sum"] / n_valid * 100.0) if n_valid else None,
            "n_month_valid": st["n_month_valid"],
            "mean_month_pct": (st["month_sum"] / st["n_month_valid"] * 100.0)
                              if st["n_month_valid"] else None,
            "median_month_pct": (median(rets) * 100.0) if rets else None,
            "up_share_month": (st["n_month_up"] / st["n_month_valid"] * 100.0)
                              if st["n_month_valid"] else None,
        })

    pooled = sorted(pooled)
    baseline = {
        "n_months": len(rows),
        "n_month_valid": len(pooled),
        "mean_month_pct": (sum(pooled) / len(pooled) * 100.0) if pooled else None,
        "median_month_pct": (median(pooled) * 100.0) if pooled else None,
        "up_share_month": (sum(1 for v in pooled if v > 0) / len(pooled) * 100.0)
                          if pooled else None,
    }

    out = Path(args.out)
    if not out.is_absolute():
        out = BASE / out
    out.parent.mkdir(parents=True, exist_ok=True)
    cols = ["month_id", "month_pillar", "first_date", "last_date", "n_dates", "n_identities", "n_stock_days",
            "n_up", "n_down", "n_flat", "n_return_valid", "mean_day_pct",
            "n_month_valid", "mean_month_pct", "median_month_pct", "up_share_month"]
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r[k] is None else
                            (f"{r[k]:.6f}" if isinstance(r[k], float) else r[k]))
                        for k in cols})

    (out.parent / "n2_month_agg_baseline.json").write_text(
        json.dumps({"source": "daily_relation_shadow_fullmarket_v1 (A_SHARE)",
                    "definition": ("月收益=该股当月首个交易日开盘→最后一个交易日收盘（前复权）；"
                                   "含 return_valid=0 的个股月份不计入月收益"),
                    **baseline}, ensure_ascii=False, indent=1), "utf-8")

    print(f"months={len(rows)} partial_skipped={partial} baseline={baseline} out={out} "
          f"elapsed={time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
