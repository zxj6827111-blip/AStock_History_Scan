#!/usr/bin/env python3
"""把 2026-M09（戊戌）补进 100%段库；口径与 s04/s06/s07 完全一致。

数据来源：data/live_60m.parquet（NAS 60 分钟线，2026-07-13..11-10）
交易日历：data/market_sessions_ext.csv（冻结日历 + 2026-10-15..11-10）

用法：
  python3 s08_append_month.py --stage panel   # 建 2026-M09 面板
  python3 s08_append_month.py --stage rows    # 重算戊戌一柱（5 旧月 + M09）
  python3 s08_append_month.py --stage apply   # 建新 parquet 目录 + 新 sqlite（不切换）
  python3 s08_append_month.py --stage swap    # 原子换入（先停服务）
  python3 s08_append_month.py --stage verify  # 独立抽检（对切换后的正式文件）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import time
from datetime import datetime, timezone, timedelta

import duckdb
import numpy as np
import pandas as pd

ROOT = "/Volumes/workSSD/software development/AStock_History_Scan"
BASE = os.path.join(ROOT, "slot88_full_market_v1")
WORK = os.path.join(BASE, "work/append_2026M09")
FACTS60 = os.path.join(ROOT, "slot88_month_v1/work/facts60.duckdb")
SESSIONS = os.path.join(BASE, "data/market_sessions_ext.csv")
NAS_SRC = os.path.join(ROOT, "source_input/nas_source.duckdb")
LIVE60 = os.path.join(BASE, "data/live_60m.parquet")
OUT_DB = os.path.join(BASE, "data/full_market_segments_n3.sqlite")
PARQ = os.path.join(BASE, "data/parquet_n3")
PARQ_NEW = os.path.join(BASE, "data/parquet_n3_new")
PARQ_PREV = os.path.join(BASE, "data/parquet_n3.prev_20261110")
DB_NEW = os.path.join(BASE, "data/full_market_segments_n3.new.sqlite")
DB_PREV = os.path.join(BASE, "data/full_market_segments_n3.prev_20261110.sqlite")
SEC_DB = os.path.join(ROOT, "bazi_month_relation_local_v1/database/"
                            "bazi_month_relation_local_v1.sqlite")

MONTH = "2026-M09"
PILLAR = "戊戌"
BJT = timezone(timedelta(hours=8))
H_ORDER = {"10:30": 1, "11:30": 2, "14:00": 3, "15:00": 4}

S_ARR, E_ARR = np.triu_indices(88, 0)
LEN = (E_ARR - S_ARR + 1).astype(np.int16)
TIMES = ("09:30", "10:30", "13:00", "14:00")
ENDS = ("10:30", "11:30", "14:00", "15:00")
MIN_N = 3
COLS = ["code", "mkt", "pillar", "direction", "n_months", "n_seen", "n_valid", "n_avail",
        "s", "e", "length_bars", "length_days",
        "start_day", "start_time", "end_day", "end_time",
        "mean_pct", "median_pct", "min_pct", "max_pct",
        "year_min", "year_max", "year_span", "first_month", "last_month",
        "month_ids", "month_rets", "cycle_years"]


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def load_sessions() -> pd.DataFrame:
    df = pd.read_csv(SESSIONS, dtype={"date": str, "month_id": str})
    return df[["date", "month_id", "mn"]].sort_values(["month_id", "mn"])


def load_securities() -> pd.DataFrame:
    con = sqlite3.connect(f"file:{SEC_DB}?mode=ro", uri=True)
    try:
        return pd.read_sql_query(
            "SELECT security_code AS code, identity_code, listed_date, delisted_date "
            "FROM securities WHERE security_type='A_SHARE'", con)
    finally:
        con.close()


def adj_map(duck: duckdb.DuckDBPyConnection, codes: list[str], d0: str, d1: str) -> pd.DataFrame:
    try:
        duck.execute(f"ATTACH '{NAS_SRC}' AS nas (READ_ONLY)")
    except duckdb.BinderException:
        pass
    parts = []
    for i in range(0, len(codes), 500):
        part = codes[i:i + 500]
        ph = ",".join("?" for _ in part)
        parts.append(duck.execute(
            f"SELECT code, date, try_cast(adj_factor AS DOUBLE) AS adj FROM nas.factors "
            f"WHERE code IN ({ph}) AND date>=? AND date<=?", [*part, d0, d1]).df())
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["code", "date", "adj"])


def bars_from_store(dates: list[str]) -> pd.DataFrame:
    con = duckdb.connect()
    df = con.execute(
        'SELECT code, date, "h", "open", "close", "volume" FROM read_parquet(?) '
        'WHERE date>=? AND date<=?', [LIVE60, dates[0], dates[-1]]).df()
    con.close()
    df["mkt"] = np.where(df.code.str.startswith("6"), "SH",
                 np.where(df.code.str.startswith(("4", "8", "9")), "BJ", "SZ"))
    df["identity"] = df.code + "." + df.mkt
    df["date"] = df["date"].astype(str).str[:10]
    return df[["identity", "code", "mkt", "date", "h", "open", "close", "volume"]]


def build_month_panel(sess: pd.DataFrame, sec: pd.DataFrame,
                      duck: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    dates = sess[sess.month_id == MONTH].date.tolist()
    if not dates:
        raise SystemExit(f"交易日历里没有 {MONTH}")
    log(f"{MONTH}: {len(dates)} 个交易日 {dates[0]}→{dates[-1]}")
    bars = bars_from_store(dates)
    log(f"{MONTH}: 原始 60 分钟 bars = {len(bars):,}；股票 {bars.identity.nunique():,}")
    adj = adj_map(duck, sorted(bars.identity.unique().tolist()), dates[0], dates[-1])
    log(f"{MONTH}: 复权因子行 = {len(adj):,}")
    df = bars.merge(adj, left_on=["identity", "date"], right_on=["code", "date"],
                    how="left", suffixes=("", "_a"))
    missing_adj = int(df.adj.isna().sum())
    if missing_adj:
        log(f"{MONTH}: 警告 {missing_adj:,} 行没有复权因子（这些行丢弃）")
    df = df[df.adj.notna() & (df.open > 0) & (df.close > 0) & (df.volume > 0)].copy()
    mn = dict(zip(dates, range(1, len(dates) + 1)))
    df["mn"] = df.date.map(mn)
    df = df[df.mn.notna() & (df.mn <= 22)].copy()
    df["month_id"] = MONTH
    df = df.sort_values(["identity", "mn", "h"])
    df["qo"] = df.open * df.adj
    df["qc"] = df.close * df.adj
    g = df.groupby(["identity", "month_id"], sort=False)
    df["prev_qc"] = g.qc.shift(1)
    df["prev_mn"] = g.mn.shift(1)
    df["r_chain"] = np.where(df.prev_qc.isna(), df.qc / df.qo - 1.0, df.qc / df.prev_qc - 1.0)
    df["gap_days"] = np.where(df.prev_mn.isna(), df.mn - 1,
                              np.maximum(df.mn - df.prev_mn - 1, 0))
    df["gap_bad"] = df.gap_days > 10
    df["extreme"] = df.r_chain.abs() > 0.5
    flags = df.groupby(["identity", "month_id"]).agg(
        n_bars=("h", "size"), max_gap=("gap_days", "max"),
        any_gap_bad=("gap_bad", "max"), any_extreme=("extreme", "max")).reset_index()
    info = {str(r.code): (r.listed_date, r.delisted_date) for r in sec.itertuples()}
    m_start, m_end = dates[0], dates[-1]

    def full_month(ident):
        ld, dd = info.get(str(ident).split(".")[0], (None, None))
        ok_l = pd.isna(ld) or str(ld)[:10] < m_start
        ok_a = pd.isna(dd) or str(dd)[:10] > m_end
        return int(ok_l and ok_a)

    flags["full_month_int"] = [full_month(x) for x in flags.identity]
    flags["month_id"] = MONTH
    df = df.merge(flags[["identity", "month_id", "full_month_int", "any_gap_bad", "any_extreme"]],
                  on=["identity", "month_id"], how="left")
    keep = df[["identity", "code", "mkt", "month_id", "mn", "h", "qo", "qc", "r_chain",
               "volume", "gap_days", "full_month_int", "any_gap_bad", "any_extreme"]].copy()
    log(f"{MONTH}: 面板行 {len(keep):,}；股票 {keep.identity.nunique():,}；"
        f"完整月股票 {int(flags.full_month_int.sum()):,}")
    return keep


def stage_panel() -> None:
    os.makedirs(WORK, exist_ok=True)
    sess = load_sessions()
    sec = load_securities()
    sec["code"] = sec["code"].astype(str).str.zfill(6)
    duck = duckdb.connect()
    out = build_month_panel(sess, sec, duck)
    path = os.path.join(WORK, "new_panel.parquet")
    out.to_parquet(path, index=False)
    meta = {
        "built_at_bjt": datetime.now(BJT).isoformat(timespec="seconds"),
        "month_id": MONTH, "pillar": PILLAR,
        "rows": int(len(out)), "identities": int(out.identity.nunique()),
        "dates": int(out.groupby("month_id").mn.nunique().iloc[0]),
        "full_month": int(out[["identity", "full_month_int"]].drop_duplicates()
                          .full_month_int.sum()),
    }
    with open(os.path.join(WORK, "panel_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    log(f"写出 {path}；{meta}")


def census_group(lg, vd, need, seen_m):
    """与 s04 完全同一套：对数收益的区间累加 + 有效根数。"""
    m = lg.shape[0]
    plog = np.concatenate([np.zeros((m, 1)), np.cumsum(lg, axis=1)], axis=1)
    pval = np.concatenate([np.zeros((m, 1)), np.cumsum(vd.astype(np.int32), axis=1)], axis=1)
    seg_log = plog[:, E_ARR + 1] - plog[:, S_ARR]
    seg_cnt = pval[:, E_ARR + 1] - pval[:, S_ARR]
    avail = seen_m[:, None] & (E_ARR[None, :] < need[:, None])
    ok = avail & (seg_cnt == LEN[None, :])
    ret = np.exp(seg_log) - 1.0
    ret[~ok] = np.nan
    n_valid = ok.sum(axis=0).astype(np.int32)
    n_avail = avail.sum(axis=0).astype(np.int32)
    up = ((ret > 1e-12) & ok).sum(axis=0).astype(np.int32)
    down = ((ret < -1e-12) & ok).sum(axis=0).astype(np.int32)
    strict = (n_valid == n_avail) & (n_valid >= MIN_N)
    all_up = (n_valid > 0) & (up == n_valid)
    all_down = (n_valid > 0) & (down == n_valid)
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        mean = np.where(n_valid > 0, np.nansum(ret, axis=0) / np.maximum(n_valid, 1), np.nan)
        ma = np.ma.masked_invalid(ret)
        med = np.ma.median(ma, axis=0).filled(np.nan)
        mn = np.ma.min(ma, axis=0).filled(np.nan)
        mx = np.ma.max(ma, axis=0).filled(np.nan)
    return all_up & strict, all_down & strict, mean, np.asarray(med), np.asarray(mn), np.asarray(mx), n_valid, n_avail, ok, ret


def stage_rows() -> None:
    duck = duckdb.connect()
    duck.execute(f"ATTACH '{FACTS60}' AS f60 (READ_ONLY)")
    cm = duck.execute("SELECT month_id, pillar, D FROM f60.cm ORDER BY month_id").df()
    old_months = cm[cm.pillar == PILLAR].month_id.tolist()
    months = old_months + [MONTH]
    sess = load_sessions()
    D = dict(zip(cm.month_id, cm.D))
    D[MONTH] = int((sess.month_id == MONTH).sum())
    need4 = np.array([min(int(D[m]), 22) * 4 for m in months], dtype=int)
    log(f"{PILLAR}: months={months} need4={need4.tolist()}")

    idents = duck.execute("SELECT DISTINCT identity FROM f60.panel ORDER BY identity").df().identity.tolist()
    midx = {m: i for i, m in enumerate(months)}
    n = len(idents)
    logret = np.zeros((n, len(months), 88), dtype=np.float64)
    valid = np.zeros((n, len(months), 88), dtype=bool)
    sindex = {x: i for i, x in enumerate(idents)}

    old = duck.execute(f"""
        SELECT p.identity, p.month_id, p.mn, p.h, p.r_chain
        FROM f60.panel p
        JOIN f60.month_flags f ON f.identity=p.identity AND f.month_id=p.month_id
        WHERE p.month_id IN ({','.join('?' for _ in old_months)})
          AND f.full_month_int=1 AND NOT f.any_gap_bad AND NOT f.any_extreme
    """, old_months).df()
    for r in old.itertuples():
        si = sindex.get(r.identity)
        if si is None:
            continue
        j = (int(r.mn) - 1) * 4 + (int(r.h) - 1)
        logret[si, midx[r.month_id], j] = float(np.log1p(r.r_chain))
        valid[si, midx[r.month_id], j] = True
    log(f"旧月份槽位 {len(old):,}")

    new = pd.read_parquet(os.path.join(WORK, "new_panel.parquet"))
    new = new[(new.month_id == MONTH) & (new.full_month_int == 1)
              & (~new.any_gap_bad) & (~new.any_extreme)]
    for r in new.itertuples():
        si = sindex.get(r.identity)
        if si is None:
            continue
        j = (int(r.mn) - 1) * 4 + (int(r.h) - 1)
        if j >= 88 or r.r_chain is None or not np.isfinite(r.r_chain):
            continue
        logret[si, midx[MONTH], j] = float(np.log1p(r.r_chain))
        valid[si, midx[MONTH], j] = True
    log(f"新月份槽位 {len(new):,}")

    meta = duck.execute("SELECT DISTINCT identity, code, mkt FROM f60.panel").df()
    meta_map = {r.identity: (r.code, r.mkt) for r in meta.itertuples()}
    rows = []
    for si, ident in enumerate(idents):
        code, mkt = meta_map.get(ident, (ident, "SH" if ident.endswith(".SH") else "SZ"))
        seen = valid[si].any(axis=1)
        up3, down3, mean, med, mn_, mx, nv, na, ok, ret = census_group(
            logret[si], valid[si], need4, seen)
        month_arr = np.array(months)
        for direction, flags in (("up", up3), ("down", down3)):
            for j in np.where(flags)[0]:
                mm = np.where(ok[:, j])[0]
                ids_s = ";".join(str(month_arr[i]) for i in mm)
                rets_s = ";".join(f"{ret[i, j]:.6f}" for i in mm)
                years = [int(str(month_arr[i])[:4]) for i in mm]
                s_, e_ = int(S_ARR[j]), int(E_ARR[j])
                L = int(LEN[j])
                rows.append((code, mkt, PILLAR, direction, len(months), int(seen.sum()),
                             int(nv[j]), int(na[j]), s_, e_, L, L / 4,
                             s_ // 4 + 1, TIMES[s_ % 4], e_ // 4 + 1, ENDS[e_ % 4],
                             float(mean[j] * 100), float(med[j] * 100),
                             float(mn_[j] * 100), float(mx[j] * 100),
                             min(years), max(years), max(years) - min(years),
                             str(month_arr[mm[0]]), str(month_arr[mm[-1]]),
                             ids_s, rets_s, 5))
    df = pd.DataFrame(rows, columns=COLS)
    path = os.path.join(WORK, f"new_rows_{PILLAR}.parquet")
    df.to_parquet(path, index=False)
    log(f"{PILLAR}: 新行 {len(df):,}（up {int((df.direction=='up').sum()):,} / "
        f"down {int((df.direction=='down').sum()):,}）→ {path}")
    with open(os.path.join(WORK, f"rows_meta_{PILLAR}.json"), "w", encoding="utf-8") as f:
        json.dump({"pillar": PILLAR, "months": months, "rows": int(len(df)),
                   "up": int((df.direction == "up").sum()),
                   "down": int((df.direction == "down").sum()),
                   "stocks": int(df.code.nunique())}, f, ensure_ascii=False, indent=2)


def stage_apply() -> None:
    """建新 parquet 目录与新 sqlite；不切换。"""
    if os.path.exists(PARQ_NEW):
        raise SystemExit(f"{PARQ_NEW} 已存在，先人工确认")
    new = pd.read_parquet(os.path.join(WORK, f"new_rows_{PILLAR}.parquet"))
    duck = duckdb.connect()
    duck.execute(f"ATTACH '{FACTS60}' AS f60 (READ_ONLY)")
    idents = duck.execute("SELECT DISTINCT identity FROM f60.panel ORDER BY identity").df().identity.tolist()
    rank = {x: i for i, x in enumerate(idents)}
    part_idx = new.code.astype(str).map(rank)
    missing = int(part_idx.isna().sum())
    if missing:
        log(f"警告：{missing:,} 行找不到身份，落到最后一片")
        part_idx = part_idx.fillna(len(idents) - 1)
    new["_part"] = (part_idx.astype(int) // 100).astype(int)

    os.makedirs(PARQ_NEW, exist_ok=True)
    files = sorted(f for f in os.listdir(PARQ) if f.startswith("part_") and f.endswith(".parquet"))
    log(f"分片 {len(files)} 个；新行 {len(new):,}")
    con = duckdb.connect()
    con.execute(f"COPY (SELECT * FROM read_parquet('{PARQ}/*.parquet') WHERE pillar=?) "
                f"TO '{os.path.join(WORK, f'backup_{PILLAR}_original.parquet')}' "
                f"(FORMAT PARQUET, COMPRESSION ZSTD)", [PILLAR])
    log(f"旧{PILLAR}行已备份")
    before = con.execute(f"SELECT count(*) FROM read_parquet('{PARQ}/*.parquet')").fetchone()[0]
    other_before = dict(con.execute(
        f"SELECT pillar, count(*) FROM read_parquet('{PARQ}/*.parquet') WHERE pillar<>? GROUP BY 1",
        [PILLAR]).fetchall())
    total_added = 0
    for i, f in enumerate(files):
        path = os.path.join(PARQ, f)
        part = con.execute(f"SELECT * FROM read_parquet('{path}') WHERE pillar<>?", [PILLAR]).df()
        part_add = new[new._part == i].drop(columns=["_part"])
        total_added += len(part_add)
        out = pd.concat([part, part_add], ignore_index=True)
        out.to_parquet(os.path.join(PARQ_NEW, f), index=False, compression="zstd")
        if (i + 1) % 10 == 0 or i == len(files) - 1:
            log(f"  分片 {i+1}/{len(files)}：本片新行 {len(part_add):,}")
    after = con.execute(f"SELECT count(*) FROM read_parquet('{PARQ_NEW}/*.parquet')").fetchone()[0]
    other_after = dict(con.execute(
        f"SELECT pillar, count(*) FROM read_parquet('{PARQ_NEW}/*.parquet') WHERE pillar<>? GROUP BY 1",
        [PILLAR]).fetchall())
    if other_before != other_after:
        raise SystemExit(f"其余柱行数变了：{other_before} vs {other_after}")
    log(f"新 parquet 行数 {after:,}（旧 {before:,}；新写入 {total_added:,}）；其余 58 柱完全一致")

    log(f"复制 SQLite → {DB_NEW}")
    shutil.copy2(OUT_DB, DB_NEW)
    con_db = sqlite3.connect(DB_NEW)
    con_db.execute("PRAGMA synchronous=OFF")
    cur = con_db.cursor()
    cur.execute("BEGIN")
    cur.execute("DELETE FROM segments WHERE pillar=?", (PILLAR,))
    log(f"SQLite 删除旧{PILLAR}行 {cur.rowcount:,} 行")
    cols = ",".join(COLS)
    ph = ",".join("?" for _ in COLS)
    cur.executemany(f"INSERT INTO segments({cols}) VALUES({ph})",
                    new.drop(columns=["_part"]).itertuples(index=False, name=None))
    con_db.commit()
    n = con_db.execute("SELECT count(*) FROM segments WHERE pillar=?", (PILLAR,)).fetchone()[0]
    stats = dict(con_db.execute(
        "SELECT 'n_segments', count(*) FROM segments UNION ALL "
        "SELECT 'n_up', count(*) FROM segments WHERE direction='up' UNION ALL "
        "SELECT 'n_down', count(*) FROM segments WHERE direction='down' UNION ALL "
        "SELECT 'n_stocks', count(DISTINCT code) FROM segments").fetchall())
    stats["n_identities"] = len(idents)
    stats["created_bjt"] = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")
    stats["library_last_month"] = MONTH
    stats["library_extra_months"] = "2026-M06,2026-M07,2026-M08,2026-M09"
    stats["source"] = f"{FACTS60} + {MONTH} panel (live_60m)"
    cur.executemany("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                    [(k, str(v)) for k, v in stats.items()])
    con_db.commit()
    n_n3 = con_db.execute("SELECT count(*) FROM segments").fetchone()[0]
    log(f"SQLite 写入完成：{PILLAR} {n:,} 行；全库 {n_n3:,} 行；meta 已更新")
    con_db.close()
    if n_n3 != after:
        raise SystemExit(f"sqlite 与 parquet 行数不一致：{n_n3:,} vs {after:,}")
    with open(os.path.join(WORK, "apply_meta.json"), "w", encoding="utf-8") as f:
        json.dump({"month_id": MONTH, "pillar": PILLAR,
                   "parquet_before": int(before), "parquet_after": int(after),
                   "new_pillar_rows": int(n), "meta": stats},
                  f, ensure_ascii=False, indent=2)
    log("apply 完成（尚未切换）")


def stage_swap() -> None:
    for p in (PARQ_PREV, DB_PREV):
        if os.path.exists(p):
            raise SystemExit(f"{p} 已存在，先人工确认")
    os.rename(PARQ, PARQ_PREV)
    os.rename(PARQ_NEW, PARQ)
    os.rename(OUT_DB, DB_PREV)
    os.rename(DB_NEW, OUT_DB)
    log(f"已切换：{PARQ_PREV} / {DB_PREV} 为旧副本")


def _raw_rets(code: str, dates: list[str]) -> dict[int, float]:
    """独立重算：从 live parquet 原始 60m + NAS 因子，返回 slot -> 链式收益。"""
    c6 = code.split(".")[0]
    con = duckdb.connect()
    try:
        con.execute(f"ATTACH '{NAS_SRC}' AS nas (READ_ONLY)")
        rows = con.execute(
            'SELECT date, "h", "open", "close" FROM read_parquet(?) WHERE code=? '
            'AND date>=? AND date<=?', [LIVE60, c6, dates[0], dates[-1]]).fetchall()
        fac = dict(con.execute(
            "SELECT date, try_cast(adj_factor AS DOUBLE) FROM nas.factors WHERE code=? "
            "AND date>=? AND date<=?", [code, dates[0], dates[-1]]).fetchall())
    finally:
        con.close()
    day_ix = {d: i + 1 for i, d in enumerate(dates)}
    bars: dict[int, tuple[float, float]] = {}
    for d, h, o, c in rows:
        a = fac.get(str(d)[:10])
        if a is None or not a:
            return {}
        mn = day_ix.get(str(d)[:10])
        if mn is None:
            continue
        bars[(mn - 1) * 4 + (int(h) - 1)] = (float(o) * a, float(c) * a)
    out = {}
    for k in sorted(bars):
        prev = bars.get(k - 1)
        qo, qc = bars[k]
        out[k] = qc / (prev[1] if prev else qo) - 1.0
    return out


def _daily_qfq_ratio(code: str, d0: str, d1: str) -> float | None:
    """日线交叉验证：前复权收盘比（d1 / d0 前一根）。只用于整日段。"""
    sql = ("SELECT b.date, try_cast(b.close AS DOUBLE), try_cast(f.adj_factor AS DOUBLE) "
           "FROM nas.bars b LEFT JOIN nas.factors f ON f.code=b.code AND f.date=b.date "
           "WHERE b.code=? AND b.date{}? ORDER BY b.date DESC LIMIT 1")
    con = duckdb.connect()
    try:
        con.execute(f"ATTACH '{NAS_SRC}' AS nas (READ_ONLY)")
        rows = con.execute(sql.format("<="), [code, d1]).fetchone()
        prev = con.execute(sql.format("<"), [code, d0]).fetchone()
    finally:
        con.close()
    if not rows or not prev or rows[0] != d1:
        return None
    if None in (rows[1], rows[2], prev[1], prev[2]):
        return None
    return (rows[1] * rows[2]) / (prev[1] * prev[2]) - 1.0


def stage_verify() -> None:
    new = pd.read_parquet(os.path.join(WORK, f"new_rows_{PILLAR}.parquet"))
    with_m09 = new[new.month_ids.str.contains(MONTH)]
    log(f"含 {MONTH} 的行 {len(with_m09):,} / {len(new):,}")
    sessions = load_sessions()
    dates = sessions[sessions.month_id == MONTH].date.tolist()
    sample = with_m09.sample(n=min(40, len(with_m09)), random_state=20261110)
    ok = bad = skip = 0
    daily_ok = daily_bad = 0
    details = []
    for r in sample.itertuples():
        mids = r.month_ids.split(";")
        i = mids.index(MONTH)
        stored = float(r.month_rets.split(";")[i])
        rr = _raw_rets(r.code, dates)
        if not rr:
            skip += 1
            continue
        if any(k not in rr for k in range(int(r.s), int(r.e) + 1)):
            skip += 1
            continue
        cum = 1.0
        for k in range(int(r.s), int(r.e) + 1):
            cum *= (1.0 + rr[k])
        got = cum - 1.0
        if abs(round(got, 6) - stored) <= 1e-6:
            ok += 1
        else:
            bad += 1
            details.append({"code": r.code, "s": int(r.s), "e": int(r.e),
                            "stored": stored, "recomputed": round(got, 6)})
        s_, e_ = int(r.s), int(r.e)
        if s_ % 4 == 0 and e_ % 4 == 3 and s_ > 0:
            dr = _daily_qfq_ratio(r.code, dates[s_ // 4], dates[e_ // 4])
            if dr is not None:
                if abs(got - dr) <= 5e-4:
                    daily_ok += 1
                else:
                    daily_bad += 1
                    details.append({"kind": "daily", "code": r.code, "s": s_, "e": e_,
                                    "chain": round(got, 6), "daily": round(dr, 6)})
    log(f"独立重算抽样：ok={ok} bad={bad} skip={skip}；日线交叉：ok={daily_ok} bad={daily_bad}")
    if bad or daily_bad:
        raise SystemExit(f"独立重算不一致：{details[:5]}")

    old = pd.read_parquet(os.path.join(WORK, f"backup_{PILLAR}_original.parquet"))
    old = old[["code", "direction", "s", "e", "month_ids", "month_rets"]]
    nw = new[["code", "direction", "s", "e", "month_ids", "month_rets"]].copy()
    old["_k"] = old.code + "|" + old.direction + "|" + old.s.astype(str) + "|" + old.e.astype(str)
    nw["_k"] = nw.code + "|" + nw.direction + "|" + nw.s.astype(str) + "|" + nw.e.astype(str)
    om = old.set_index("_k")
    changed = unmatched = 0
    for k, n_mids_s, n_rets_s in nw[["_k", "month_ids", "month_rets"]].itertuples(index=False, name=None):
        if k not in om.index:
            unmatched += 1
            continue
        o = om.loc[k]
        if isinstance(o, pd.DataFrame):
            o = o.iloc[0]
        o_mids = str(o.month_ids).split(";")
        n_mids = str(n_mids_s).split(";")
        if (o_mids != n_mids[:len(o_mids)]
                or str(o.month_rets).split(";") != str(n_rets_s).split(";")[:len(o_mids)]):
            changed += 1
    log(f"旧 5 次前缀比对：变化 {changed} 行，未匹配 {unmatched} 行（未匹配=旧月不满足严格 100% 但新月补足后入列，属正常）")
    log(f"n_months 分布：{new.n_months.value_counts().to_dict()}")

    con = duckdb.connect()
    tot = con.execute(f"SELECT count(*) FROM read_parquet('{PARQ}/*.parquet')").fetchone()[0]
    pics = dict(con.execute(f"SELECT pillar, count(*) FROM read_parquet('{PARQ}/*.parquet') "
                            f"GROUP BY 1").fetchall())
    log(f"切换后 parquet 总行 {tot:,}；戊戌 {pics.get(PILLAR, 0):,}；柱数 {len(pics)}")
    report = {"month_id": MONTH, "pillar": PILLAR, "with_m09": int(len(with_m09)),
              "sample_ok": ok, "sample_bad": bad, "sample_skip": skip,
              "old_prefix_changed": changed, "old_prefix_unmatched": unmatched,
              "parquet_total": int(tot), "pillar_rows": int(pics.get(PILLAR, 0))}
    with open(os.path.join(WORK, "verify.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    log("verify 完成")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=["panel", "rows", "apply", "swap", "verify"])
    a = ap.parse_args()
    {"panel": stage_panel, "rows": stage_rows, "apply": stage_apply,
     "swap": stage_swap, "verify": stage_verify}[a.stage]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
