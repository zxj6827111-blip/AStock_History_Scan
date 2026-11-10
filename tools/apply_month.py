#!/usr/bin/env python3
"""客户机应用「节气月增量包」：校验基版 → 事务内删/插/补 → 校验目标 → 提交。

只用标准库 sqlite3，不需要 duckdb；不改整库、不换文件、不停页面服务。
失败一律 ROLLBACK，旧数据不动。

用法：
  py -3 apply_month.py --pack month_pack_2026-M09.zip --dry-run   # 只校验基版并列出动作
  py -3 apply_month.py --pack month_pack_2026-M09.zip             # 应用
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

BJT = timezone(timedelta(hours=8))
HERE = Path(__file__).resolve().parent

# 包内库名 → 客户机相对路径（与交付布局一致）
TARGETS = {
    "main": "bazi_month_relation_local_v1/database/bazi_month_relation_local_v1.sqlite",
    "v5": "bazi_month_relation_tengod_dev_v1/data/v5_lowpoint_slice.sqlite",
    "cap": "bazi_month_relation_local_v1/extensions/market_cap_tier_v1/database/market_cap_tier_v1.sqlite",
    "etf": "bazi_month_relation_etf_v1/database/bazi_month_relation_etf_v1.sqlite",
}
FP_TABLE = {"main": "monthly_facts", "v5": "v5_months", "cap": "cap_tier_monthly",
            "etf": "monthly_facts"}


def now_bjt() -> str:
    return datetime.now(BJT).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def log(msg: str) -> None:
    print(f"[{now_bjt()}] {msg}", flush=True)


def fingerprint(con: sqlite3.Connection, fp_table: str) -> dict:
    out = {}
    for t in (fp_table, "month_relation_types", "monthly_relations",
              "stats_pillar60", "stats_branch12"):
        try:
            out[f"{t}_rows"] = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        except sqlite3.Error:
            pass
    try:
        out["max_month"] = con.execute(f'SELECT MAX(month_id) FROM "{fp_table}"').fetchone()[0]
    except sqlite3.Error:
        pass
    return out


def table_exists(con: sqlite3.Connection, schema: str, name: str) -> bool:
    try:
        con.execute(f'SELECT 1 FROM "{schema}"."{name}" LIMIT 0')
        return True
    except sqlite3.Error:
        return False


def delete_keys(con, table: str, entry: dict, delta: str = "d", prefix: str = "d") -> None:
    pk = entry["pk"]
    if not pk:
        return
    dk = f"{prefix}_{table}"
    if not table_exists(con, delta, dk):
        return
    on = " AND ".join(f'x."{k}"=tgt."{k}"' for k in pk)
    con.execute(f'DELETE FROM "{table}" AS tgt WHERE EXISTS (SELECT 1 FROM "{delta}"."{dk}" x WHERE {on})')


def insert_rows(con, table: str, entry: dict, delta: str = "d") -> int:
    ut = f"u_{table}"
    if not table_exists(con, delta, ut):
        return 0
    cols = ",".join(f'"{c}"' for c in entry["cols"])
    con.execute(f'INSERT INTO "{table}"({cols}) SELECT {cols} FROM "{delta}"."{ut}"')
    return con.total_changes


def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def patch_rows(con, table: str, entry: dict, delta: str = "d") -> int:
    if not entry.get("patch_cols") or not table_exists(con, delta, f"p_{table}"):
        return 0
    pk, pc = entry["pk"], entry["patch_cols"]
    cols = [c for c in pc] + [k for k in pk]
    n = con.execute(f'SELECT COUNT(*) FROM "{delta}"."p_{table}"').fetchone()[0]
    if sqlite3.sqlite_version_info >= (3, 33, 0):
        sets = ",".join(f"{q(c)}=x.{q(c)}" for c in pc)
        on = " AND ".join(f'x.{q(k)}=tgt.{q(k)}' for k in pk)
        con.execute(f'UPDATE {q(table)} AS tgt SET {sets} FROM (SELECT * FROM "{delta}"."p_{table}") x WHERE {on}')
    else:
        sets = ",".join(f"{q(c)}=?" for c in pc)
        where = " AND ".join(f"{q(k)}=?" for k in pk)
        cur = con.execute(f'SELECT {",".join(q(c) for c in cols)} FROM "{delta}"."p_{table}"')
        while True:
            rows = cur.fetchmany(20000)
            if not rows:
                break
            con.executemany(f'UPDATE {q(table)} SET {sets} WHERE {where}', rows)
    return n


def apply_db(path: Path, delta: Path, dbname: str, manifest: dict, dry: bool) -> None:
    fi = manifest["databases"][dbname]
    con = sqlite3.connect(str(path), timeout=30)
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute(f"ATTACH DATABASE ? AS d", (str(delta),))
    # 给增量表的键建索引：否则 DELETE/UPDATE 的相关子查询会退化成 N²
    for t, e in fi["tables"].items():
        if not e.get("pk"):
            continue
        keys = ",".join(f'"{k}"' for k in e["pk"])
        for prefix in ("u", "d", "p"):
            name = f"{prefix}_{t}"
            if table_exists(con, "d", name):
                con.execute(f'CREATE INDEX IF NOT EXISTS "d"."ix_{name}" ON "{name}"({keys})')
    before = fingerprint(con, FP_TABLE[dbname])
    base_fp = fi["fingerprints"].get("base")
    if base_fp:
        diff = {k: (base_fp.get(k), before.get(k)) for k in base_fp
                if base_fp.get(k) != before.get(k)}
        if diff:
            raise SystemExit(f"[{dbname}] 基版指纹不符，拒绝应用：{json.dumps(diff, ensure_ascii=False)}")
    log(f"[{dbname}] 基版指纹匹配 max_month={before.get('max_month')}")
    if dry:
        for t, e in fi["tables"].items():
            log(f"  {t:26s} u={e['upsert']:,} d={e['delete']:,} p={e['patch']:,}")
        con.close()
        return
    months = set()
    try:
        con.execute("BEGIN IMMEDIATE")
        for t, e in fi["tables"].items():
            if e.get("delete_month"):
                con.execute(f'DELETE FROM "{t}" WHERE month_id=?', (e["delete_month"],))
                months.add(e["delete_month"])
            if e.get("delete_all"):
                con.execute(f'DELETE FROM "{t}"')
            if e["mode"] in ("diff", "diff_rule"):
                delete_keys(con, t, e, prefix="d")
                delete_keys(con, t, e, prefix="u")
            insert_rows(con, t, e)
            patch_rows(con, t, e)
        rs = fi.get("rescale")
        if rs:
            rows = con.execute('SELECT identity_code, factor FROM d.rescale').fetchall()
            for table, cols in rs["targets"]:
                sets = ",".join(f'"{c}"="{c}"*?' for c in cols)
                sql = f'UPDATE "{table}" SET {sets} WHERE identity_code=? AND month_id<?'
                con.executemany(sql, [tuple([f] * len(cols) + [code, rs["month"]])
                                      for code, f in rows])
            log(f"[{dbname}] 锚点缩放 {len(rows)} 只 × {len(rs['targets'])} 表")
        con.execute("CREATE TABLE IF NOT EXISTS _month_pack_version("
                    "month TEXT, applied_at TEXT, pack_sha256 TEXT)")
        con.execute("DELETE FROM _month_pack_version")
        con.execute("INSERT INTO _month_pack_version VALUES(?,?,?)",
                    (manifest["month"], now_bjt(), manifest.get("sha256", "")))
        after = fingerprint(con, FP_TABLE[dbname])
        want = fi["fingerprints"]["target"]
        bad = {k: (want.get(k), after.get(k)) for k in want if want.get(k) != after.get(k)}
        if bad:
            raise SystemExit(f"[{dbname}] 目标指纹不符，回滚：{json.dumps(bad, ensure_ascii=False)}")
        con.execute("COMMIT")
        log(f"[{dbname}] 应用完成 max_month={after.get('max_month')} "
            f"facts={after.get('monthly_facts_rows', after.get('v5_months_rows', '-'))}")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", required=True)
    ap.add_argument("--root", default=str(HERE))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    pack = Path(args.pack)
    root = Path(args.root)
    with zipfile.ZipFile(pack) as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        if manifest.get("schema") != "astock_month_pack_v1":
            raise SystemExit(f"未知包格式：{manifest.get('schema')}")
        log(f"包 {pack.name} 月份={manifest['month']} 构建于 {manifest['built_at_bjt']}")
        tmp = Path(tempfile.mkdtemp(prefix="month_pack_"))
        for f in manifest.get("files", []):
            zp = zf.getinfo(f["path"])
            if zp.file_size != f["bytes"]:
                raise SystemExit(f"包内文件大小不符：{f['path']}")
            zf.extract(f["path"], tmp)
        for dbname, rel in TARGETS.items():
            if dbname not in manifest["databases"]:
                continue
            target = root / rel
            delta = tmp / dbname / "delta.sqlite"
            if not target.is_file():
                log(f"[{dbname}] 目标库不存在，跳过：{target}")
                continue
            log(f"[{dbname}] {target}")
            apply_db(target, delta, dbname, manifest, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
