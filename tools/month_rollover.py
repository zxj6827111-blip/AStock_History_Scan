#!/usr/bin/env python3
"""节气月自动转月编排器（本机 Mac 侧）。

一次跑完：源刷新 → nas_source → ETF 源 → post_month(staging+promote) → xw → 市值 →
V5 切片 → 月包。每阶段写 stamp，失败停在上一步，旧数据不动；可重复执行（已完成的阶段自动跳过）。

用法：
  python3 tools/month_rollover.py --status          # 只看：库到哪个月、源到哪天、下一轮是否到期
  python3 tools/month_rollover.py --dry-run         # 打印将要执行哪些阶段
  python3 tools/month_rollover.py --execute         # 真正执行
  python3 tools/month_rollover.py --execute --force post_promote   # 重跑某一阶段
  python3 tools/month_rollover.py --watch           # 常驻：每天 20:00 检查一次，到期就跑
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path("/Volumes/workSSD/software development/AStock_History_Scan")
DEV = ROOT / "bazi_month_relation_tengod_dev_v1"
MAIN_DB = ROOT / "bazi_month_relation_local_v1/database/bazi_month_relation_local_v1.sqlite"
CAL = ROOT / "batches/20261016T174105-ff8959dd37/calendar_snapshot.csv"
WORK = DEV / "work" / "rollover"
PY311 = "/Users/zhangxingjin/.cache/stock-analyzer/quality-py311-20261012/bin/python3"
NAS_PY = "/tmp/nasvenv/bin/python"
V5PY = str(ROOT / "v5_lowpoint_ui_isolated/.venv/bin/python")
BJT = timezone(timedelta(hours=8))


def now_bjt() -> datetime:
    return datetime.now(BJT)


def log(msg: str) -> None:
    print(f"[{now_bjt().strftime('%F %T')}] {msg}", flush=True)


def read_cal() -> list[dict]:
    with CAL.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def db_max_month() -> str:
    con = sqlite3.connect(MAIN_DB.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return con.execute("SELECT MAX(month_id) FROM monthly_facts").fetchone()[0]
    finally:
        con.close()


def source_max_date() -> str | None:
    """与调度器同一口径：日更真实产物，不看 README/mtime。"""
    for p, keys in ((ROOT / "source_mirror_live/fetch_manifest.json",
                     ("zip_latest_date_sample", "target_trade_date")),
                    (ROOT / "source_mirror_live/nightly_data_ready.json", ())):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for k in keys:
            v = str(d.get(k) or "")[:10]
            if len(v) == 10:
                return v
        v = str(((d.get("daily") or {}).get("latest_trade_date")) or "")[:10]
        if len(v) == 10:
            return v
    try:
        con = sqlite3.connect((ROOT / "source_input/live/current_month.sqlite").resolve().as_uri()
                              + "?mode=ro", uri=True)
        v = con.execute("SELECT v FROM meta WHERE k='latest_trade_date'").fetchone()[0]
        con.close()
        return v
    except Exception:  # noqa: BLE001
        return None


def last_weekday(d: date) -> date:
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def due_month() -> tuple[str | None, dict]:
    """返回 (到期月, 上下文)。到期 = 月已结束 + 源已覆盖最后一个交易日 + 主库还没有该月。"""
    cur = db_max_month()
    src = source_max_date()
    cal = read_cal()
    future = [r for r in cal if r["month_id"] > cur]
    ctx = {"db_max": cur, "source_max": src, "candidate": future[0]["month_id"] if future else None,
           "now": now_bjt().strftime("%F %T")}
    for r in future[:1]:
        end_at = datetime.fromisoformat(r["end_at"])
        end_d = end_at.date()
        ltd = last_weekday(end_d)
        ctx.update({"end_at": r["end_at"], "last_trading_day": ltd.isoformat()})
        if now_bjt() < end_at:
            ctx["reason"] = "MONTH_NOT_ENDED"
            return None, ctx
        if not src or src < ltd.isoformat():
            ctx["reason"] = "SOURCE_NOT_READY"
            return None, ctx
        ctx["reason"] = "DUE"
        return r["month_id"], ctx
    ctx["reason"] = "NO_FUTURE_MONTH"
    return None, ctx


def stamp_dir(month: str) -> Path:
    p = WORK / month
    p.mkdir(parents=True, exist_ok=True)
    return p


def done(month: str, stage: str) -> bool:
    return (stamp_dir(month) / f"{stage}.done").is_file()


def mark(month: str, stage: str, info: dict | None = None) -> None:
    (stamp_dir(month) / f"{stage}.done").write_text(
        json.dumps({"at": now_bjt().strftime("%F %T"), **(info or {})}, ensure_ascii=False),
        encoding="utf-8")


def run(month: str, stage: str, cmds: list[list[str]], env: dict | None = None,
        force: str | None = None) -> None:
    if done(month, stage) and force != stage:
        log(f"SKIP {stage}（已完成）")
        return
    logdir = stamp_dir(month)
    e = dict(os.environ)
    if env:
        e.update(env)
    t0 = time.time()
    for cmd in cmds:
        log("RUN " + " ".join(cmd))
        with (logdir / f"{stage}.log").open("a", encoding="utf-8") as f:
            f.write(f"\n[{now_bjt():%F %T}] $ {' '.join(cmd)}\n")
            f.flush()
            p = subprocess.run(cmd, env=e, stdout=f, stderr=subprocess.STDOUT)
        if p.returncode != 0:
            raise SystemExit(f"阶段 {stage} 失败（rc={p.returncode}），日志：{logdir/f'{stage}.log'}")
    if stage == "post_staging":
        rep = json.loads((DEV / "work/post" / month / "report.json").read_text(encoding="utf-8"))
        checks = rep.get("checks", {})
        h = checks.get("HISTORY_UNCHANGED", {})
        hist_eq = all(v["formal"]["sha256"] == v["staging"]["sha256"]
                      for v in (h.get("history_tables") or {}).values())
        stats_ok = bool((h.get("stats_reproducible") or {}).get("ok"))
        others = [k for k in ("ROW_COUNTS", "SPOT_RECOMPUTE_3", "RELATION_INCREMENT")
                  if not checks.get(k, {}).get("ok")]
        if not (hist_eq and stats_ok) or others:
            raise SystemExit(f"staging 对账未过（history={hist_eq} stats={stats_ok} 其它={others}），"
                             f"不写 approved；日志 {logdir/'post_staging.log'}")
        (DEV / "work/post" / month / "approved").write_text(
            f"auto-approved by month_rollover at {now_bjt():%F %T}\n", encoding="utf-8")
        log("已写 approved（staging 对账通过）")
    mark(month, stage, {"seconds": round(time.time() - t0, 1)})
    log(f"DONE {stage}（{time.time()-t0:.0f}s）")


def snapshot_bases(month: str) -> Path:
    """月包基版快照：main 用 promote 的写前备份；v5/cap 在开工前快照。"""
    base = stamp_dir(month) / "base"
    base.mkdir(exist_ok=True)
    pairs = [("bazi_month_relation_local_v1.sqlite", MAIN_DB),
             ("v5_lowpoint_slice.sqlite", ROOT / "bazi_month_relation_tengod_dev_v1/data/v5_lowpoint_slice.sqlite"),
             ("market_cap_tier_v1.sqlite",
              ROOT / "bazi_month_relation_local_v1/extensions/market_cap_tier_v1/database/market_cap_tier_v1.sqlite")]
    for name, src in pairs:
        dst = base / name
        if not dst.exists() and src.is_file():
            try:
                os.link(src, dst)
            except OSError:
                import shutil
                shutil.copy2(src, dst)
    return base


def plan_stages(month: str, base: Path, base_month: str) -> list[tuple[str, list[list[str]], dict]]:
    d = DEV
    xw = ROOT / "allstock_xun_week_v1"
    stages = []
    stages.append(("source", [[NAS_PY, str(ROOT / "scripts/fetch_nas_source.py"), "--from-remote-list"]], {}))
    nas_old = stamp_dir(month) / "nas_source.pre_rollover.duckdb"
    stages.append(("nas_source", [
        ["mv", str(ROOT / "source_input/nas_source.duckdb"), str(nas_old)],
        [PY311, str(ROOT / "scripts/build_source_input.py")]], {}))
    stages.append(("etf_source", [
        [NAS_PY, str(ROOT / "bazi_month_relation_etf_v1/catchup_20261106/scripts/01_fetch_source.py")],
        [PY311, str(ROOT / "bazi_month_relation_etf_v1/catchup_20261106/scripts/02_build_read_layer.py")],
        ["cp", str(ROOT / "bazi_month_relation_etf_v1/catchup_20261106/data/etf_source.duckdb"),
         str(ROOT / "bazi_month_relation_etf_v1/data/etf_source.duckdb")]], {}))
    stages.append(("post_staging", [[PY311, str(d / "tools/post_month.py"), "--month", month,
                                     "--as-of", source_max_date() or "", "--mode", "staging"]], {}))
    stages.append(("post_promote", [[PY311, str(d / "tools/post_month.py"), "--month", month,
                                     "--mode", "promote"]], {}))
    stages.append(("xw", [
        [PY311, str(xw / "scripts/w05_build_cells_multi.py")],
        [PY311, str(xw / "scripts/w06_build_rules_db_multi.py")],
        [PY311, str(xw / "scripts/w07_future_windows_multi.py")],
        [PY311, str(xw / "scripts/w08_presync_gates_multi.py")],
        [PY311, str(xw / "scripts/w09_sync_to_formal.py"), "--replace"]],
        {"XW_BACKUP_DIR": str(Path.home() / f"Backups/astock_local_v1_pre_xw_{month}")}))
    stages.append(("cap", [[PY311, str(ROOT / "bazi_month_relation_local_v1/extensions/market_cap_tier_v1/src/sync_after_main_update.py"),
                            "--accept"]],
                  {"CAP_TIER_MIRROR": str(ROOT / "source_mirror/正式源/全A日K_合并版")}))
    stages.append(("v5", [
        [V5PY, str(ROOT / "v5_lowpoint_ui_isolated/tools/extend_slice_month.py"),
         "--month", month, "--validate", base_month],
        [V5PY, str(ROOT / "v5_lowpoint_ui_isolated/tools/extend_slice_apply.py"),
         "--month", month]], {}))
    stages.append(("pack", [[V5PY, str(ROOT / "scripts/build_month_pack.py"),
                             "--month", month, "--base-dir", str(base)]], {}))
    return stages


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--force", default=None)
    args = ap.parse_args()

    def one_round() -> int:
        month, ctx = due_month()
        log(json.dumps(ctx, ensure_ascii=False))
        if not month:
            return 0
        base = snapshot_bases(month)
        stages = plan_stages(month, base, ctx["db_max"])
        rs = ROOT / "exports/month_pkgs" / f"rescale_{month}.csv"
        if not rs.is_file():
            log(f"WARN 缺少锚点缩放表 {rs.name}；月包将不含历史前复权缩放（仅影响有分红/送转的票）")
        if args.dry_run or not args.execute:
            for name, cmds, env in stages:
                log(("SKIP " if done(month, name) else "TODO ") + name)
            return 0
        for name, cmds, env in stages:
            run(month, name, cmds, env, args.force)
        log(f"月转完成：{month}")
        return 0

    if args.status or args.dry_run:
        return one_round()
    if args.watch:
        while True:
            now = now_bjt()
            if 19 <= now.hour <= 23:
                try:
                    one_round()
                except SystemExit as exc:
                    log(f"本轮失败：{exc}；30 分钟后再试")
                except Exception as exc:  # noqa: BLE001
                    log(f"本轮异常：{type(exc).__name__}: {exc}")
                time.sleep(1800)
            else:
                time.sleep(600)
    return one_round()


if __name__ == "__main__":
    raise SystemExit(main())
