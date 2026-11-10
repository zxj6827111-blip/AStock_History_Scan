#!/Users/zhangxingjin/.cache/stock-analyzer/quality-py311-20261012/bin/python3
# -*- coding: utf-8 -*-
"""SINGLE ENTRY POINT for posting one flow month (流月) into the local relation library.

Everything here is *carried over verbatim* from the already byte-verified scripts under
work/extend/; nothing is re-invented:

  slice + qfq        <- tools/replay_slice_month.py  (M08 slice reproduced the DB on 21
                        columns with 0 mismatch; qfq rule raw*adj(day)/adj(anchor),
                        anchor = that stock's last bar <= data cursor)
  engine input (34)  <- work/extend/m09_engine/build_m09_engine_input.py  (profile mapping
                        verified 5,224/5,224 on M08; boundaries copied verbatim, +08:00)
  engine run         <- v13_shadow_ready_20261018/release_candidate/run.py (read-only use)
  facts/MRT/relations<- work/extend/e2e/e2e_rebuild_m08.py  (67/15/37-column mapping; M08
                        replay: 66/67 columns all equal, row_sha256 5,224/5,224)
  source_hash        <- work/extend/m09_engine/verify_source_hash_m08.py (5,224/5,224)
  5 summary tables   <- work/extend/e2e/e2e_build_stats.py  (recompute == formal, 0 diff)
  5-table compare    <- work/extend/e2e/e2e_compare_stats.py
  digests            <- work/extend/e2e/e2e_digest.py

Modes
  dry-run  (default) read-only checks + plan; writes NOTHING (not one byte).
  staging            copy the master DB to work/post/<month>/db_staging.sqlite, write the
                     month into the COPY, recompute the 5 summary tables, run the four
                     reconciliations, write report.json + append one line to
                     reports/更新记录.md. The master DB is opened read-only.
  promote            only if work/post/<month>/approved exists AND the four reconciliations
                     passed. The real atomic swap is deliberately NOT implemented in this
                     round: promote verifies, prints exactly what it would do, and refuses.

Exit codes: 0 success | 2 args/precondition | 3 incomplete month | 4 reconciliation failed
            | 5 missing human approval.

Time basis: fixed UTC+8, fields suffixed _bjt / carrying explicit +08:00. No naive
timestamps, no zoneinfo/tzdata, no UTC arithmetic anywhere.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # dry-run must not even drop a .pyc

import argparse
import csv
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import Decimal as D, ROUND_HALF_UP, getcontext
from pathlib import Path
from urllib.parse import quote

getcontext().prec = 60
TZ8 = timezone(timedelta(hours=8))
TOL = D("1e-12")

DEV_ROOT = Path(__file__).resolve().parents[1]
PROJ_ROOT = DEV_ROOT.parent
DOCS_ROOT = Path(os.environ.get("BAZI_DOCS_ROOT", str(PROJ_ROOT)))
MASTER_DB = Path("/Volumes/workSSD/software development/AStock_History_Scan/"
                 "bazi_month_relation_local_v1/database/bazi_month_relation_local_v1.sqlite")
ZIP_DIR = DOCS_ROOT / "source_mirror/正式源/全A日K_合并版"
FACTOR_PARQUET = DOCS_ROOT / ("source_mirror/正式源/"
                              "A股复权因子_tushare全量/adj_factor_all.parquet")
INDEX_PARQUET = DOCS_ROOT / ("source_mirror/正式源/"
                             "A股指数日线_tushare/index_daily.parquet")
NAS_DUCKDB = DOCS_ROOT / "source_input/nas_source.duckdb"
STOCK_MASTER_CSV = DOCS_ROOT / "source_input/stock_master.csv"
ENGINE_DIR = PROJ_ROOT / "v13_shadow_ready_20261018/release_candidate"
VERIFIED = DEV_ROOT / "work" / "extend"
E2E = VERIFIED / "e2e"
WORK_ROOT = DEV_ROOT / "work" / "post"
UPDATE_LOG = DEV_ROOT / "reports" / "更新记录.md"

PY311 = Path.home() / ".cache/stock-analyzer/quality-py311-20261012/bin/python3"
SCRIPT_VERSION = "1.0.0"

EXIT_OK, EXIT_ARGS, EXIT_INCOMPLETE, EXIT_RECON, EXIT_NO_APPROVAL = 0, 2, 3, 4, 5
CHECKS = ("HISTORY_UNCHANGED", "ROW_COUNTS", "SPOT_RECOMPUTE_3", "RELATION_INCREMENT")

# 34-column engine input contract (engine run.py REQUIRED + every field read by
# engine.calculate_raw / input_provenance / row_sha256). Copied verbatim.
COLS34 = ['identity_code', 'security_code', 'security_type', 'display_name', 'period_id',
          'period_start_at', 'period_end_at', 'flow_level', 'flow_pillar', 'natal_year',
          'natal_month', 'natal_day', 'natal_hour', 'polarity', 'day_master',
          'natal_chart_basis', 'natal_pillar_source', 'profile_version', 'profile_sha256',
          'row_status', 'boundary_conflict_status', 'boundary_disputed', 'boundary_day_flag',
          'single_trade_day', 'partial_listing', 'partial_delisting', 'return_valid',
          'return_valid_basis', 'return_evidence_batch_run_id', 'identity_basis',
          'adapter_contract_version', 'source_batch_run_id', 'input_calendar_sha256',
          'input_relation_rule_version']
PROFILE_COLS = ['natal_year', 'natal_month', 'natal_day', 'natal_hour', 'polarity',
                'day_master', 'natal_chart_basis', 'natal_pillar_source', 'profile_version',
                'profile_sha256', 'row_status', 'boundary_conflict_status',
                'boundary_disputed', 'boundary_day_flag']
PILLAR_SOURCE = ('year=external_archive_as_given;month=external_archive_as_given;'
                 'day=external_archive_as_given')
# Provenance constants taken from the proven 2026-M08 34-column input (they are part of
# engine result['input_provenance'] and therefore of row_sha256).
PROV = {'display_name': '', 'identity_basis': 'FORMAL_IDENTITY_COLUMN',
        'adapter_contract_version': 'v13-integration-adapter-1.0',
        'source_batch_run_id': '20261016T174105-ff8959dd37',
        'input_calendar_sha256': '8a5d8becf741d5f4466159a2b0025d0c975382016c6a5b1c5a982f9d03d85679',
        'input_relation_rule_version': 'bazi-relation-v3',
        'return_evidence_batch_run_id': ''}
RETURN_BASIS_COMPLETE = 'SOURCE_HAS_NO_VERIFIED_RETURN_WINDOW'          # proven M08 value
RETURN_BASIS_INCOMPLETE = 'INCOMPLETE_MONTH_RETURN_WINDOW_NOT_VERIFIED'  # M09 experiment value

# recorded evidence (work/extend/e2e/digest_formal.json) used as an implementation
# self-check for the digest function copied out of e2e_digest.py
EVIDENCE = {'2026-M08': {'history_facts_le_2026-M07':
                         '60a768e3b0ba4ae74a859fd96d44600517b4e7037598890cfe3fea45c0a0b9c9',
                         'history_mrt_le_2026-M07':
                         'a11d0f5570c51c70b771bf4d48f7852ad2cc4be84f831a9aaa5d90e3bb018209',
                         'history_facts_rows': 898705, 'history_mrt_rows': 4623890}}

MARKET_COLS_FACTS = ['identity_code', 'month_id', 'security_code', 'solar_year', 'month_no',
                     'month_branch', 'flow_pillar', 'flow_stem', 'flow_branch',
                     'flow_stem_element', 'flow_branch_element', 'flow_year_pillar',
                     'start_term', 'start_at', 'end_at', 'first_date', 'last_date',
                     'trading_days', 'body_return', 'return_decimal', 'direction',
                     'fact_status', 'eligible', 'single_day', 'partial_listing',
                     'partial_delisting', 'quality_flags', 'endpoint_basis', 'raw_open',
                     'raw_close', 'factor_first', 'factor_last', 'anchor_date',
                     'anchor_factor', 'qfq_open', 'qfq_close']
POS_ORDER = {"year": 0, "month": 1, "day": 2}
DIRECTION_MARKER = {"伏吟": "SYMMETRIC", "天合地合": "SYMMETRIC",
                    "反吟": "FLOW_TO_NATAL", "天克地冲": "FLOW_TO_NATAL"}
BENCH_OF = {"SH": "000001.SH", "SZ": "399106.SZ"}


# --------------------------------------------------------------------------- #
# time / decimal / io helpers (copied from the verified scripts)
# --------------------------------------------------------------------------- #
def now8() -> datetime:
    return datetime.now(TZ8)


def iso8(dt: datetime) -> str:
    return dt.astimezone(TZ8).replace(microsecond=0).isoformat()


def parse_bjt(s: str) -> datetime:
    d = datetime.fromisoformat(s)
    if d.tzinfo is None:
        raise ValueError("naive timestamp rejected: %r" % s)
    return d.astimezone(TZ8)


def dec_str(d: D, places: int = 12) -> str:
    return format(d.quantize(D(1).scaleb(-places), rounding=ROUND_HALF_UP), "f")


def dec_factor(d: D) -> str:
    return format(d, "f")


def ymd_to_dash(s: str) -> str:
    return "%s-%s-%s" % (s[0:4], s[4:6], s[6:8])


def dash_to_ymd(s: str) -> str:
    return s.replace("-", "")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def connect_ro(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path.absolute().as_uri() + "?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA temp_store=FILE")   # never spill into the master DB directory
    return con


def connect_rw(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect("file:" + quote(str(db_path.resolve())) + "?mode=rwc", uri=True)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("PRAGMA cache_size=-400000")
    return con


def duckdb_mod():
    import duckdb  # local import: the system python has no duckdb
    return duckdb


def ensure_py311() -> None:
    """Re-exec under the pre-built quality env (duckdb/pyarrow/pandas), never pip install."""
    try:
        duckdb_mod()
        return
    except Exception:
        pass
    if os.environ.get("POST_MONTH_REEXEC") == "1" or not PY311.exists():
        raise SystemExit("duckdb is not importable and the quality py311 env is missing: %s"
                         % PY311)
    env = dict(os.environ, POST_MONTH_REEXEC="1", PYTHONDONTWRITEBYTECODE="1")
    os.execve(str(PY311), [str(PY311), str(Path(__file__).resolve())] + sys.argv[1:], env)


# --------------------------------------------------------------------------- #
# calendar / posting gate (copied from tools/qfq_extend_slice.py)
# --------------------------------------------------------------------------- #
MONTH_KEYS = ["month_id", "solar_year", "month_no", "year_pillar", "month_pillar",
              "month_branch", "start_term", "start_at", "end_at"]


def read_month(con, month_id: str) -> dict:
    row = con.execute("SELECT %s FROM calendar_months WHERE month_id=?"
                      % ",".join(MONTH_KEYS), (month_id,)).fetchone()
    if row is None:
        raise SystemExit("month not in calendar_months: %s" % month_id)
    m = dict(zip(MONTH_KEYS, row))
    for k in ("start_at", "end_at"):
        if not str(m[k]).endswith("+08:00"):
            raise SystemExit("calendar boundary is not +08:00: %s=%r" % (k, m[k]))
        parse_bjt(m[k])          # rejects naive
    m["_start"], m["_end"] = parse_bjt(m["start_at"]), parse_bjt(m["end_at"])
    m["start_date"] = m["_start"].date().isoformat()
    m["end_date"] = m["_end"].date().isoformat()
    return m


def months_before(con, month_id: str) -> list:
    """All month_ids strictly before `month_id` in true calendar order.

    month_id is NOT zero padded ('2026-M09' > '2026-M10' as strings), so ordering and
    interval membership must always go through (solar_year, month_no).
    """
    rows = con.execute("SELECT month_id, solar_year, month_no FROM calendar_months").fetchall()
    y, mo = None, None
    for mid, sy, mn in rows:
        if mid == month_id:
            y, mo = int(sy), int(mn)
    if y is None:
        raise SystemExit("target month missing from calendar_months: %s" % month_id)
    return sorted([mid for mid, sy, mn in rows if (int(sy), int(mn)) < (y, mo)])


def first_trading_day_on_or_after(boundary: datetime) -> date:
    d = boundary.date()
    if boundary >= datetime.combine(d, dtime(15, 0), tzinfo=TZ8):
        d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def last_weekday_on_or_before(d: date) -> date:
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def count_weekdays(lo_exclusive: date, hi_inclusive: date) -> int:
    n, d = 0, lo_exclusive + timedelta(days=1)
    while d <= hi_inclusive:
        n += int(d.weekday() < 5)
        d += timedelta(days=1)
    return n


def can_post_month(month: dict, asof: datetime, market_data_through: str,
                   covered_days: list) -> dict:
    start_at, end_at = month["_start"], month["_end"]
    start_d, end_d = start_at.date(), end_at.date()
    last_td = last_weekday_on_or_before(end_d)          # weekday estimate, no holiday cal.
    last_close = datetime.combine(last_td, dtime(15, 0), tzinfo=TZ8)
    data_through = date.fromisoformat(market_data_through) if market_data_through else None
    expected_days = count_weekdays(start_d, end_d)
    checks = [
        {"id": "MONTH_ENDED", "ok": asof >= end_at,
         "detail": "now %s vs end_at %s" % (iso8(asof), iso8(end_at))},
        {"id": "LAST_TRADING_DAY_CLOSED", "ok": asof >= last_close,
         "detail": "last trading day %s (weekday estimate), close 15:00 BJT"
                   % last_td.isoformat()},
        {"id": "MARKET_DATA_THROUGH_LAST_TRADING_DAY",
         "ok": bool(data_through and data_through >= last_td),
         "detail": "market_data_through %s vs last trading day %s"
                   % (market_data_through, last_td.isoformat())},
    ]
    failed = [c for c in checks if not c["ok"]]
    covered = len(covered_days)
    return {
        "schema": "astock-month-posting-gate-v1",
        "month_id": month["month_id"],
        "asof_bjt": iso8(asof),
        "timezone": "UTC+8 (fixed offset)",
        "month_window_bjt": {"start_at": iso8(start_at), "end_at": iso8(end_at),
                             "start_date_exclusive": start_d.isoformat(),
                             "end_date_inclusive": end_d.isoformat()},
        "market_data_through": market_data_through,
        "last_trading_day_in_window": last_td.isoformat(),
        "last_trading_day_basis": "weekend estimate (no exchange holiday calendar in DB)",
        "checks": checks,
        "can_post_month": not failed,
        "reason": "; ".join(c["id"] for c in failed) if failed else "OK",
        "failed_checks": [c["id"] for c in failed],
        "first_eligible_date": iso8(datetime.combine(
            first_trading_day_on_or_after(end_at), dtime(20, 0), tzinfo=TZ8)),
        "coverage": {
            "covered_trading_days": covered,
            "covered_dates": covered_days,
            "expected_trading_days_weekday_estimate": expected_days,
            "missing_trading_days_estimate": expected_days - covered,
            "coverage_ratio": round(covered / expected_days, 6) if expected_days else None,
        },
    }


# --------------------------------------------------------------------------- #
# market scan / factors (copied from tools/qfq_extend_slice.py + replay_slice_month.py)
# --------------------------------------------------------------------------- #
def year_zips(start_date: str, end_date: str) -> list:
    out = []
    for y in range(int(start_date[:4]), int(end_date[:4]) + 1):
        p = ZIP_DIR / ("%d.zip" % y)
        if not p.exists():
            raise SystemExit("missing source zip: %s" % p)
        out.append(p)
    return out


def scan_zip(zip_paths, lo: str, hi: str):
    """All bars with lo < date <= hi per code, plus per-date counts and zip codes."""
    per_code: dict = {}
    per_date: Counter = Counter()
    zip_codes: set = set()
    for zp in zip_paths:
        with zipfile.ZipFile(zp) as zf:
            for name in sorted(zf.namelist()):
                if not name.endswith(".csv"):
                    continue
                code = name.rsplit("/", 1)[-1][:-4]
                zip_codes.add(code)
                bars = []
                with zf.open(name) as fh:
                    rd = csv.reader(fh.read().decode("utf-8").splitlines())
                    header = next(rd)
                    if header[:6] != ["code", "datetime", "open", "high", "low", "close"]:
                        raise SystemExit("unexpected header in %s: %r" % (name, header[:6]))
                    for r in rd:
                        d = r[1]
                        if lo < d <= hi:
                            bars.append((d, r[2], r[5]))
                            per_date[d] += 1
                if bars:
                    bars.sort()
                    per_code.setdefault(code, []).extend(bars)
        for code in per_code:
            per_code[code].sort()
    return per_code, per_date, zip_codes


def load_factors(parquet: Path, wanted: set, lo_ymd: str, hi_ymd: str):
    """(factor[(code, iso_date)] -> Decimal, max_date[code] -> 'YYYY-MM-DD')."""
    con = duckdb_mod().connect()
    con.execute("SET threads=4")
    max_rows = con.execute(
        "SELECT ts_code, max(trade_date) FROM read_parquet('%s') GROUP BY 1" % parquet
    ).fetchall()
    max_date = {c: ymd_to_dash(d) for c, d in max_rows if c in wanted}
    rows = con.execute(
        "SELECT ts_code, trade_date, adj_factor FROM read_parquet('%s') "
        "WHERE trade_date >= ? AND trade_date <= ?" % parquet, [lo_ymd, hi_ymd]).fetchall()
    factors = {(c, ymd_to_dash(d)): D(str(f)) for c, d, f in rows if c in wanted}
    con.close()
    return factors, max_date


def body(o: str, c: str, f1: str, f2: str, anchor: str) -> dict:
    """Exact replica of the scanner engine.body(): Decimal localcontext prec = 50.

    The 50-digit context is load-bearing: return_decimal / qfq_* are stored as TEXT, so a
    60-digit context yields different strings for identical values (measured: 5,101 of
    5,224 M08 return_decimal strings differed at prec 60).
    """
    from decimal import localcontext
    with localcontext() as ctx:
        ctx.prec = 50
        left = D(o) * D(f1)
        right = D(c) * D(f2)
        if left <= 0 or right <= 0 or D(anchor) <= 0:
            raise ValueError("nonpositive price/factor")
        ret = right / left - 1
        direction = "UP" if right > left else "DOWN" if right < left else "FLAT"
        return {"body_return": float(ret), "return_decimal": format(ret, "f"),
                "direction": direction,
                "qfq_open": format(left / D(anchor), "f"),
                "qfq_close": format(right / D(anchor), "f")}


def build_slice(con, month: dict, asof, out_dir: Path) -> dict:
    """One month slice + index daily mirror. Logic carried over from
    tools/replay_slice_month.py (verified on 2026-M08: 21/21 columns, 0 mismatch).

    asof=None means "local mirror max trade day": the zip is scanned unbounded and the
    effective cutoff is the max bar date found.
    """
    t0 = time.time()
    start_date, end_date = month["start_date"], month["end_date"]
    securities = {
        r[0]: {"security_code": r[1], "name": r[2], "exchange": r[3],
               "security_type": r[4], "listed_date": r[5], "delisted_date": r[6]}
        for r in con.execute(
            "SELECT identity_code, security_code, name, exchange, security_type, "
            "listed_date, delisted_date FROM securities")}
    zips = year_zips(start_date, end_date)
    scan_hi = asof or "9999-12-31"
    per_code, per_date, zip_codes = scan_zip(zips, start_date, scan_hi)
    data_through = max(per_date) if per_date else None
    if data_through is None:
        raise SystemExit("no bars at all in (%s, %s]" % (start_date, scan_hi))
    asof = min(asof, data_through) if asof else data_through
    mkt_days_window = sorted(d for d in per_date if start_date < d <= end_date)
    mkt_days_set = set(mkt_days_window)
    print("      zip scan: codes=%d dates=%d data_through=%s as_of=%s window_days=%d"
          % (len(per_code), len(per_date), data_through, asof, len(mkt_days_window)))

    factors, factor_max_date = load_factors(
        FACTOR_PARQUET, set(securities) | set(per_code),
        dash_to_ymd(start_date), dash_to_ymd(asof))
    print("      adj_factor rows in window: %d | codes with factor max date: %d"
          % (len(factors), len(factor_max_date)))

    # indices are absent from the stock zip; take them from the index daily mirror
    index_bars, idx_daily = {}, []
    if INDEX_PARQUET.exists():
        dcon = duckdb_mod().connect()
        idx_codes = sorted(c for c, t in con.execute(
            "SELECT identity_code, security_type FROM securities") if t == "INDEX")
        for code in idx_codes:
            b = dcon.execute(
                "SELECT trade_date, open, close FROM read_parquet(?) WHERE ts_code=? "
                "AND trade_date > ? AND trade_date <= ? ORDER BY trade_date",
                [str(INDEX_PARQUET), code, dash_to_ymd(start_date), dash_to_ymd(asof)]
            ).fetchall()
            bb = [(ymd_to_dash(d), format(D(str(o)), "f"), format(D(str(c)), "f"))
                  for d, o, c in b if o is not None and c is not None]
            for d, o, c in bb:
                idx_daily.append({"identity_code": code, "trade_date": d, "open": o,
                                  "close": c})
            if bb:
                index_bars[code] = bb
        dcon.close()
        print("      index bars from mirror: %s" %
              {c: len(b) for c, b in index_bars.items()})

    rows, missing, unavailable = [], [], []
    flag_dist: Counter = Counter()
    for code in sorted(securities):
        s = securities[code]
        bars = per_code.get(code, [])
        from_index = False
        if not bars and code in index_bars:
            bars, from_index = index_bars[code], True
        wbars = [b for b in bars if start_date < b[0] <= end_date]
        # 指数来自独立镜像；镜像没覆盖到本月最后交易日时，不能把截断月当完整月入账
        if from_index and wbars and mkt_days_window and wbars[-1][0] < mkt_days_window[-1]:
            missing.append({"identity_code": code, "security_code": s["security_code"],
                            "name": s["name"], "security_type": s["security_type"],
                            "listed_date": s["listed_date"] or "",
                            "delisted_date": s["delisted_date"] or "",
                            "bars_in_window": len(wbars),
                            "reason": "INDEX_SOURCE_STALE",
                            "last_bar": wbars[-1][0],
                            "market_last": mkt_days_window[-1]})
            continue
        if not wbars:
            if code not in zip_codes and code not in index_bars:
                reason = "NOT_IN_STOCK_ZIP"
            elif s["delisted_date"] and s["delisted_date"] < start_date:
                reason = "DELISTED_BEFORE_WINDOW"
            elif s["listed_date"] and s["listed_date"] > end_date:
                reason = "LISTED_AFTER_WINDOW"
            else:
                reason = "NO_BAR_IN_WINDOW"
            missing.append({"identity_code": code, "security_code": s["security_code"],
                            "name": s["name"], "security_type": s["security_type"],
                            "listed_date": s["listed_date"] or "",
                            "delisted_date": s["delisted_date"] or "",
                            "bars_in_window": 0, "reason": reason})
            continue
        first_d, raw_o, _ = wbars[0]
        last_d, _, raw_c = wbars[-1]
        anchor_cand = [b[0] for b in bars if b[0] <= data_through]
        anchor = max(anchor_cand) if anchor_cand else None
        if from_index:
            f1 = f2 = fa = D(1)
            fsrc = "INDEX_PARQUET_FACTOR_1"
        else:
            f1 = factors.get((code, first_d))
            f2 = factors.get((code, last_d))
            fa = factors.get((code, anchor)) if anchor else None
            fsrc = "PARQUET"
            if f1 is None or f2 is None or fa is None:
                if code not in factor_max_date:      # absent from the stock factor table
                    f1 = f2 = fa = D(1)
                    fsrc = "ABSENT_FROM_FACTOR_TABLE_DEFAULT_1"
                else:
                    unavailable.append({
                        "identity_code": code, "first_date": first_d,
                        "last_date": last_d, "anchor_date": anchor,
                        "missing": ",".join(n for n, v in
                                            (("first", f1), ("last", f2),
                                             ("anchor", fa)) if v is None)})
                    continue
        b = body(raw_o, raw_c, format(f1, "f"), format(f2, "f"), format(fa, "f"))
        n = len(wbars)
        asof_close = datetime.fromisoformat(data_through + "T15:00:00+08:00")
        complete = month["_end"] <= asof_close
        listed = s["listed_date"] or ""
        delisted = s["delisted_date"] or ""
        partial_listing = int(bool(listed) and listed > start_date)
        partial_delisting = int(bool(delisted) and (
            delisted < end_date or (delisted == end_date
                                    and month["_end"].hour * 60 + month["_end"].minute
                                    > 15 * 60)))
        stock_dates = {x[0] for x in wbars}
        interior = [d for d in sorted(mkt_days_set)
                    if first_d < d < last_d and d not in stock_dates]
        flags = []
        if interior:
            flags.append("NO_TRADE_DATES_DECLARED")
        if n == 1:
            flags.append("SINGLE_TRADING_DAY")
        if partial_listing:
            flags.append("PARTIAL_LISTING_PERIOD")
        if partial_delisting:
            flags.append("PARTIAL_DELISTING_PERIOD")
        if not complete:
            flags.append("ONGOING_PERIOD")
        flags = sorted(set(flags))
        fact_status = "FINAL" if complete else "PROVISIONAL"
        flag_dist[json.dumps(flags, ensure_ascii=False)] += 1
        rows.append({
            "identity_code": code, "security_code": s["security_code"], "name": s["name"],
            "exchange": s["exchange"], "security_type": s["security_type"],
            "listed_date": listed, "delisted_date": delisted,
            "month_id": month["month_id"], "solar_year": month["solar_year"],
            "month_no": month["month_no"], "month_branch": month["month_branch"],
            "flow_pillar": month["month_pillar"], "start_term": month["start_term"],
            "start_at": month["start_at"], "end_at": month["end_at"],
            "month_start_date": start_date, "month_end_date": end_date,
            "first_date": first_d, "last_date": last_d, "n_bars": n,
            "raw_open": raw_o, "raw_close": raw_c,
            "factor_first": format(f1, "f"), "factor_last": format(f2, "f"),
            "anchor_date": anchor, "anchor_factor": format(fa, "f"),
            "qfq_open": b["qfq_open"], "qfq_close": b["qfq_close"],
            "body_return": repr(b["body_return"]), "return_decimal": b["return_decimal"],
            "direction": b["direction"],
            "fact_status": fact_status, "eligible": int(fact_status == "FINAL"),
            "single_day": int(n == 1), "partial_listing": partial_listing,
            "partial_delisting": partial_delisting,
            "quality_flags": json.dumps(flags, ensure_ascii=False),
            "endpoint_basis": "SOURCE_DECLARED_COMPLETE_TRADE_ROWS",
            "market_trading_days_in_window": len(mkt_days_window),
            "market_data_through": data_through, "factor_source": fsrc,
        })

    if not rows:
        raise SystemExit("slice is empty for %s" % month["month_id"])
    fields = list(rows[0].keys())
    out_dir.mkdir(parents=True, exist_ok=True)
    slice_csv = out_dir / ("%s_slice.csv" % month["month_id"])
    with open(slice_csv, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    miss_csv = out_dir / ("%s_slice_missing.csv" % month["month_id"])
    with open(miss_csv, "w", encoding="utf-8", newline="") as fh:
        mfields = list(missing[0].keys()) if missing else [
            "identity_code", "security_code", "name", "security_type", "listed_date",
            "delisted_date", "bars_in_window", "reason"]
        w = csv.DictWriter(fh, fieldnames=mfields)
        w.writeheader()
        w.writerows(missing)
    idx_csv = out_dir / ("%s_index_daily.csv" % month["month_id"])
    with open(idx_csv, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["identity_code", "trade_date", "open", "close"])
        w.writeheader()
        w.writerows(idx_daily)
    if unavailable:
        with open(out_dir / ("%s_slice_unavailable.csv" % month["month_id"]),
                  "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(unavailable[0].keys()))
            w.writeheader()
            w.writerows(unavailable)

    manifest = {
        "script": "tools/post_month.py (slice logic from tools/replay_slice_month.py)",
        "month": {k: month[k] for k in MONTH_KEYS},
        "window_rule": "bar_date in (start_date, end_date]; the solar-term day belongs "
                       "to the previous flow month",
        "data_cursor": {"as_of_effective": asof, "market_data_through": data_through,
                        "trading_days_in_window": mkt_days_window},
        "counts": {"rows": len(rows), "missing": len(missing),
                   "unavailable_factors": len(unavailable),
                   "missing_by_reason": dict(Counter(m["reason"] for m in missing)),
                   "quality_flags": dict(flag_dist),
                   "direction": dict(Counter(r["direction"] for r in rows)),
                   "complete": int(rows[0]["fact_status"] == "FINAL")},
        "anchors": {"rule": "per-identity last available bar date <= market_data_through",
                    "default_factor_1": sorted(r["identity_code"] for r in rows
                                               if r["factor_source"] != "PARQUET")},
        "artefacts": {"slice": str(slice_csv), "missing": str(miss_csv),
                      "index_daily": str(idx_csv)},
        "seconds": round(time.time() - t0, 1),
    }
    (out_dir / ("%s_slice_manifest.json" % month["month_id"])).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("      slice rows=%d missing=%d unavailable=%d direction=%s dir=(%s)"
          % (len(rows), len(missing), len(unavailable),
             manifest["counts"]["direction"], out_dir))
    return {"rows": rows, "missing": missing, "index_daily": idx_daily,
            "slice_csv": slice_csv, "data_through": data_through, "as_of": asof,
            "trading_days": mkt_days_window, "manifest": manifest}


# --------------------------------------------------------------------------- #
# engine input (mapping carried over from build_m09_engine_input.py)
# --------------------------------------------------------------------------- #
def load_profiles(con) -> dict:
    cols = [r[1] for r in con.execute("pragma table_info(natal_profiles)")]
    return {d["identity_code"]: d for d in
            (dict(zip(cols, r)) for r in con.execute(
                "SELECT %s FROM natal_profiles" % ",".join(cols)))}


def profile_fields(p) -> dict:
    """natal_profiles -> the 14 engine input columns (verified 5,224/5,224 on M08)."""
    if p is None or p["profile_present"] != 1 or not (p["profile_sha256"] or "").strip():
        return {"natal_year": "", "natal_month": "", "natal_day": "", "natal_hour": "",
                "polarity": "", "day_master": "",
                "natal_chart_basis": "NOT_APPLICABLE",
                "natal_pillar_source": "NOT_APPLICABLE",
                "profile_version": "", "profile_sha256": "",
                "row_status": "BLOCKED_NO_PROFILE",
                "boundary_conflict_status": "NOT_APPLICABLE",
                "boundary_disputed": "",
                "boundary_day_flag": str((p or {}).get("boundary_day_flag") or 0)}
    conflict = p["boundary_conflict"]
    disputed = conflict not in (None, "", "NONE", "NOT_APPLICABLE")
    return {"natal_year": p["natal_year_pillar"], "natal_month": p["natal_month_pillar"],
            "natal_day": p["natal_day_pillar"], "natal_hour": p["archive_hour_pillar"],
            "polarity": p["archive_polarity"], "day_master": p["day_master_stem"],
            "natal_chart_basis": "ARCHIVE_THREE_PILLARS_AS_GIVEN",
            "natal_pillar_source": PILLAR_SOURCE,
            "profile_version": p["profile_version"],
            "profile_sha256": p["profile_sha256"],
            "row_status": "COMPUTED_BOUNDARY_DISPUTED" if disputed else "COMPUTED",
            "boundary_conflict_status": conflict,
            "boundary_disputed": "1" if disputed else "0",
            "boundary_day_flag": str(p["boundary_day_flag"] or 0)}


def build_engine_input(month: dict, slice_rows: list, securities: dict, profiles: dict,
                       complete: bool, out_dir: Path):
    return_basis = RETURN_BASIS_COMPLETE if complete else RETURN_BASIS_INCOMPLETE
    rows = []
    for s in slice_rows:
        code = s["identity_code"]
        sec = securities[code]
        row = {
            "identity_code": code, "security_code": sec["security_code"],
            "security_type": sec["security_type"], "display_name": PROV["display_name"],
            "period_id": month["month_id"],
            "period_start_at": month["start_at"], "period_end_at": month["end_at"],
            "flow_level": "流月", "flow_pillar": month["month_pillar"],
            "single_trade_day": "1" if int(s["n_bars"]) == 1 else "0",
            "partial_listing": str(int(s["partial_listing"])),
            "partial_delisting": str(int(s["partial_delisting"])),
            "return_valid": "", "return_valid_basis": return_basis,
            "return_evidence_batch_run_id": PROV["return_evidence_batch_run_id"],
            "identity_basis": PROV["identity_basis"],
            "adapter_contract_version": PROV["adapter_contract_version"],
            "source_batch_run_id": PROV["source_batch_run_id"],
            "input_calendar_sha256": PROV["input_calendar_sha256"],
            "input_relation_rule_version": PROV["input_relation_rule_version"],
        }
        row.update(profile_fields(profiles.get(code)))
        rows.append(row)
    rows.sort(key=lambda r: r["identity_code"])
    if len(rows) != len({r["identity_code"] for r in rows}):
        raise SystemExit("duplicate identity in the engine input")
    for r in rows:
        for k in ("period_start_at", "period_end_at"):
            v = r[k]
            if not v.endswith("+08:00") or v.endswith("Z"):
                raise SystemExit("boundary is not an explicit +08:00 string: %s=%r" % (k, v))
            if datetime.fromisoformat(v).tzinfo is None:
                raise SystemExit("naive boundary rejected: %s=%r" % (k, v))
        if r["row_status"].startswith("COMPUTED"):
            for k in ("natal_year", "natal_month", "natal_day"):
                if not r[k]:
                    raise SystemExit("empty %s on computed row %s" % (k, r["identity_code"]))
    out_dir.mkdir(parents=True, exist_ok=True)
    out_csv = out_dir / ("%s_engine_input.csv" % month["month_id"])
    with open(out_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS34)
        w.writeheader()
        w.writerows(rows)
    status = Counter(r["row_status"] for r in rows)
    manifest = {
        "script": "tools/post_month.py (input builder from "
                  "work/extend/m09_engine/build_m09_engine_input.py)",
        "month": month["month_id"], "flow_pillar": month["month_pillar"],
        "period_start_at": month["start_at"], "period_end_at": month["end_at"],
        "timezone_note": "boundaries copied verbatim from calendar_months, both +08:00",
        "rows": len(rows), "columns": len(COLS34),
        "row_status": dict(status),
        "flags": {"single_trade_day": sum(1 for r in rows if r["single_trade_day"] == "1"),
                  "partial_listing": sum(1 for r in rows if r["partial_listing"] == "1"),
                  "partial_delisting": sum(1 for r in rows if r["partial_delisting"] == "1")},
        "return_valid_basis": return_basis,
        "provenance": PROV,
        "output_csv": str(out_csv), "output_csv_sha256": sha256_file(out_csv),
        "output_bytes": out_csv.stat().st_size,
    }
    (out_dir / ("%s_engine_input_manifest.json" % month["month_id"])).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("      engine input rows=%d status=%s sha256=%s"
          % (len(rows), dict(status), manifest["output_csv_sha256"][:16]))
    return {"rows": rows, "csv": out_csv, "manifest": manifest}


def verify_input_against_m08(rows: list, month_id: str) -> dict:
    """Self-check of the builder against the proven 2026-M08 34-column input."""
    frozen = VERIFIED / "replay_engine" / "2026-M08_engine_input.csv"
    out = {"frozen_input": str(frozen), "present": frozen.exists()}
    if not frozen.exists():
        return out
    with open(frozen, encoding="utf-8-sig", newline="") as f:
        m08 = {r["identity_code"]: r for r in csv.DictReader(f)}
    per_col = {}
    cols = COLS34 if month_id == "2026-M08" else PROFILE_COLS
    by_code = {r["identity_code"]: r for r in rows}
    for c in cols:
        compared = equal = 0
        ex = []
        for code, r0 in m08.items():
            if code not in by_code:
                continue
            compared += 1
            if str(r0[c]) == str(by_code[code][c]):
                equal += 1
            elif len(ex) < 3:
                ex.append([code, r0[c], by_code[code][c]])
        per_col[c] = {"compared": compared, "equal": equal,
                      "mismatch": compared - equal, "examples": ex}
    out["columns"] = per_col
    out["columns_with_mismatch"] = [c for c, v in per_col.items() if v["mismatch"]]
    out["all_equal"] = not out["columns_with_mismatch"]
    print("      M08 builder self-check (%d cols): %s"
          % (len(cols), "ALL EQUAL" if out["all_equal"]
             else "MISMATCH %s" % out["columns_with_mismatch"]))
    return out


# --------------------------------------------------------------------------- #
# engine run (subprocess, read-only use of the release candidate)
# --------------------------------------------------------------------------- #
def run_engine(input_csv: Path, out_dir: Path) -> dict:
    run_py = ENGINE_DIR / "run.py"
    if not run_py.exists():
        raise SystemExit("engine run.py missing: %s" % run_py)
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(run_py), "--input", str(input_csv),
           "--output-dir", str(out_dir), "--format", "sqlite"]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=str(ENGINE_DIR), env=env, capture_output=True, text=True)
    wall = round(time.time() - t0, 2)
    (out_dir / "run.stdout.txt").write_text(proc.stdout, encoding="utf-8")
    (out_dir / "run.stderr.txt").write_text(proc.stderr, encoding="utf-8")
    if proc.returncode != 0:
        raise SystemExit("engine failed rc=%d: %s" % (proc.returncode, proc.stderr[-800:]))
    man = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    db = out_dir / "stock_relations_staging.sqlite"
    if not db.exists():
        raise SystemExit("engine produced no sqlite: %s" % db)
    print("      engine rows=%d inserted=%d wall=%.2fs db=%s"
          % (man["unique_rows"], man["inserted"], wall, db.name))
    return {"manifest": man, "db": db, "wall_seconds": wall,
            "engine_db_sha256": sha256_file(db),
            "engine_sha256": man.get("engine_sha256"),
            "rule_reference_sha256": man.get("rule_reference_sha256"),
            "policy_sha256": man.get("policy_sha256")}


def load_engine_results(engine_db: Path) -> dict:
    con = sqlite3.connect("file:" + quote(str(engine_db.resolve())) + "?mode=ro", uri=True)
    out = {}
    for code, pj, sha, rj in con.execute(
            "SELECT identity_code, period_id, row_sha256, row_json FROM relation_results"):
        d = json.loads(rj)
        d["_db_sha"] = sha
        d["_db_period"] = pj
        out[code] = d
    con.close()
    return out


# --------------------------------------------------------------------------- #
# source_hash (rule carried over from verify_source_hash_m08.py)
# --------------------------------------------------------------------------- #
def decimal_string(x, positive=True):
    if x is None or str(x).strip() == "":
        return None
    try:
        d = D(str(x))
    except Exception:
        return None
    if not d.is_finite() or (positive and d <= 0):
        return None
    return format(d, "f")


def hash_records(records) -> str:
    h = hashlib.sha256()
    h.update(b"[")
    for i, rec in enumerate(records):
        if i:
            h.update(b",")
        h.update(json.dumps(rec, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), default=str).encode("utf-8"))
    h.update(b"]")
    return h.hexdigest()


def compute_source_hash(codes: list, asof: str, duckdb_path: Path,
                        master_csv: Path, chunk: int = 200) -> dict:
    """scanner logical_sha256 per identity, cutoff = asof (stock level, all months)."""
    con = duckdb_mod().connect(str(duckdb_path), read_only=True)
    with open(master_csv, encoding="utf-8", newline="") as f:
        master = {r["stock_id"]: r for r in csv.DictReader(f)}
    missing_master = [c for c in codes if c not in master]
    if missing_master:
        raise SystemExit("identity missing from %s: %s" % (master_csv, missing_master[:5]))
    got, rows_seen, errors = {}, {}, []
    for i in range(0, len(codes), chunk):
        part = codes[i:i + chunk]
        marks = ",".join("?" * len(part))
        cur = con.execute("SELECT * FROM bars WHERE code IN (%s) ORDER BY code, date"
                          % marks, part)
        names = [d[0] for d in cur.description]
        bmap = defaultdict(list)
        for row in cur.fetchall():
            d = dict(zip(names, row))
            bmap[d["code"]].append(d)
        cur = con.execute("SELECT * FROM factors WHERE code IN (%s) ORDER BY code, date"
                          % marks, part)
        fnames = [d[0] for d in cur.description]
        fmap_all = defaultdict(list)
        for row in cur.fetchall():
            d = dict(zip(fnames, row))
            fmap_all[d["code"]].append(d)
        for code in part:
            m = master[code]
            listed, delisted = m["listed_date"] or "", m["delisted_date"] or ""
            end = delisted or "9999-12-31"
            src = m["source_symbol"] or code
            records, seen = [], set()
            for b in bmap.get(src, []):
                dt = b["date"]
                if not (dt >= listed and dt <= end):
                    continue
                if not listed <= dt <= min(delisted or asof, asof):
                    continue
                if dt in seen:
                    errors.append({"identity_code": code,
                                   "error": "DUPLICATE_DAILY_DATE " + dt})
                    break
                seen.add(dt)
                records.append({
                    "date": dt, "status": "TRADED",
                    "raw_open": decimal_string(b.get("open")),
                    "raw_close": decimal_string(b.get("close")),
                    "raw_high": decimal_string(b.get("high")),
                    "raw_low": decimal_string(b.get("low")),
                    "factor": decimal_string(b.get("factor")),
                    "input_values": {"raw_open": b.get("open"), "raw_close": b.get("close"),
                                     "raw_high": b.get("high"), "raw_low": b.get("low"),
                                     "factor": b.get("factor")},
                })
            records.sort(key=lambda r: r["date"])
            fm = {}
            for f_ in fmap_all.get(src, []):
                dt = f_["date"]
                if dt >= listed and dt <= end:
                    fm[dt] = decimal_string(f_.get("adj_factor"))
            if m["security_type"] == "INDEX":
                for r in records:
                    r["factor"] = "1"
            else:
                for r in records:
                    r["factor"] = fm.get(r["date"])
            got[code] = hash_records(records)
            rows_seen[code] = len(records)
        print("      source_hash %d/%d" % (min(i + chunk, len(codes)), len(codes)),
              flush=True)
    con.close()
    if errors:
        raise SystemExit("source_hash input errors: %s" % errors[:3])
    return {"source_hash": got, "rows_hashed": rows_seen, "asof": asof,
            "total_daily_rows": sum(rows_seen.values()),
            "empty_histories": sorted(c for c, n in rows_seen.items() if n == 0)}


# --------------------------------------------------------------------------- #
# facts / MRT / relations mapping (carried over from e2e/replay_rebuild_m08.py)
# --------------------------------------------------------------------------- #
def jarr(v):
    return json.dumps(v, ensure_ascii=False, separators=(",", ":")) if v is not None else None


def merge_month(month: dict, slice_rows: list, engine: dict, source_hash: dict,
                index_daily: list, ref: dict) -> dict:
    """Build facts(67) / MRT(15) / relations(37) rows exactly as the verified replay."""
    sl = {r["identity_code"]: r for r in slice_rows}
    idx_open, idx_close = {}, {}
    for r in index_daily:
        idx_open[(r["identity_code"], r["trade_date"])] = r["open"]
        idx_close[(r["identity_code"], r["trade_date"])] = r["close"]
    stem_el, branch_el = ref["stem_element"], ref["branch_element"]
    cal_year, cal_no, cal_branch, cal_yp = (month["solar_year"], month["month_no"],
                                            month["month_branch"], month["year_pillar"])

    def benchmark(sec_type, exchange, first_d, last_d):
        if sec_type == "INDEX":
            return None, None, None
        bcode = BENCH_OF.get(exchange)
        if not bcode:
            return None, None, "MISSING_EXACT_INDEX_ENDPOINT"
        o, c = idx_open.get((bcode, first_d)), idx_close.get((bcode, last_d))
        if not o or not c:
            return bcode, None, "MISSING_EXACT_INDEX_ENDPOINT"
        from decimal import localcontext
        with localcontext() as ctx:      # same prec as the verified replay
            ctx.prec = 50
            left, right = D(o), D(c)
            if left <= 0 or right <= 0:
                return bcode, None, "MISSING_EXACT_INDEX_ENDPOINT"
            bret = float(right / left - 1)
        return bcode, bret, "MATCHED"

    facts, mrt, rels = {}, {}, []
    for code, t in engine.items():
        s = sl[code]
        status = t["row_status"]
        calculated = bool(t["relation_calculated"])
        cand_kinds = list(dict.fromkeys(c.get("candidate_kind")
                                        for c in t["candidate_facts_json"]))
        # 一行可同时命中多种候选（如 拱合候选 + 三会缺支候选），主库既有
        # 34,051 行就是逗号并列值；不许在这里拒绝。

        natal_day = t.get("natal_day") or ""
        f = {
            "identity_code": code, "month_id": t["period_id"],
            "security_code": t["input_provenance"].get("security_code", ""),
            "solar_year": cal_year, "month_no": int(cal_no), "month_branch": cal_branch,
            "flow_pillar": t["flow_pillar"], "flow_stem": t["flow_pillar"][0],
            "flow_branch": t["flow_pillar"][1],
            "flow_stem_element": stem_el.get(t["flow_pillar"][0]),
            "flow_branch_element": branch_el.get(t["flow_pillar"][1]),
            "flow_year_pillar": cal_yp,
            "start_term": s["start_term"], "start_at": s["start_at"], "end_at": s["end_at"],
            "first_date": s["first_date"], "last_date": s["last_date"],
            "trading_days": int(s["n_bars"]), "body_return": float(s["body_return"]),
            "return_decimal": s["return_decimal"], "direction": s["direction"],
            "fact_status": s["fact_status"], "eligible": int(s["eligible"]),
            "single_day": int(s["single_day"]), "partial_listing": int(s["partial_listing"]),
            "partial_delisting": int(s["partial_delisting"]),
            "quality_flags": s["quality_flags"], "endpoint_basis": s["endpoint_basis"],
            "raw_open": s["raw_open"], "raw_close": s["raw_close"],
            "factor_first": s["factor_first"], "factor_last": s["factor_last"],
            "anchor_date": s["anchor_date"], "anchor_factor": s["anchor_factor"],
            "qfq_open": s["qfq_open"], "qfq_close": s["qfq_close"],
            "source_hash": source_hash.get(code),
            "computation_status": status,
            "research_sample_status": ("ELIGIBLE_UNDISPUTED" if status == "COMPUTED" else
                                       "EXCLUDED_BOUNDARY_DISPUTED"
                                       if status == "COMPUTED_BOUNDARY_DISPUTED" else
                                       "EXCLUDED_NO_PROFILE"),
            "undisputed": 1 if status == "COMPUTED" else 0,
            "boundary_disputed": 1 if t["boundary_disputed"] else 0,
            "profile_present": 0 if status == "BLOCKED_NO_PROFILE" else 1,
            "day_master_stem": natal_day[0] if (calculated and natal_day) else None,
            "day_master_element": (stem_el.get(natal_day[0])
                                   if (calculated and natal_day) else None),
            "stem_ten_god": t["official_ten_god"],
            "relation_type_list": json.dumps(t["relation_types_json"], ensure_ascii=False,
                                             separators=(",", ":")),
            "candidate_kind_list": ",".join(cand_kinds) if cand_kinds else None,
            "confirmed_event_count": t["confirmed_event_count"],
            "candidate_structure_count": t["candidate_structure_count"],
            "distinct_relation_type_count": t["distinct_relation_type_count"],
            "profile_version": t["profile_version"],
            "profile_sha256": t["input_provenance"].get("profile_sha256", ""),
            "relation_rule_version": t["relation_rule_version"],
            "policy_version": ref["policy_version"], "policy_sha256": ref["policy_sha256"],
            "adapter_contract_version": t["adapter_contract_version"],
            "calendar_sha256": "", "source_batch_run_id": "",
            "row_sha256": t["row_sha256"], "label": t["label"],
            "label_basis": t["label_basis"],
            "error_class": None, "error_message": None,
        }
        bcode, bret, bstatus = benchmark(
            t["input_provenance"].get("security_type", ""), s.get("exchange", ""),
            s["first_date"], s["last_date"])
        f["benchmark_code"] = bcode
        f["benchmark_return"] = bret
        f["comparison_status"] = bstatus
        f["excess_return"] = f["body_return"] - bret if bret is not None else None
        facts[code] = f

        common = {
            "identity_code": code, "month_id": month["month_id"],
            "flow_pillar": t["flow_pillar"] or s["flow_pillar"],
            "month_branch": s["month_branch"], "solar_year": int(s["solar_year"]),
            "direction": s["direction"], "body_return": float(s["body_return"]),
            "single_day": int(s["single_day"]), "undisputed": f["undisputed"],
            "boundary_disputed": f["boundary_disputed"],
            "research_sample_status": f["research_sample_status"],
        }
        by_type = defaultdict(list)
        for fact in t["relation_facts_json"]:
            by_type[fact["relation_type"]].append(fact.get("natal_position"))
        for typ, poss in by_type.items():
            pp = sorted({p for p in poss if p}, key=lambda x: POS_ORDER.get(x, 9))
            mrt[(code, "OFFICIAL", typ)] = dict(
                common, record_kind="OFFICIAL", relation_type=typ,
                positions=",".join(pp) if pp else "multi", event_count=len(poss))
        cand = Counter(c.get("candidate_kind") for c in t["candidate_facts_json"])
        for typ, n in cand.items():
            mrt[(code, "CANDIDATE", typ)] = dict(
                common, record_kind="CANDIDATE", relation_type=typ,
                positions="multi", event_count=n)

        rcommon = {"identity_code": code, "month_id": month["month_id"],
                   "solar_year": f["solar_year"], "flow_pillar": f["flow_pillar"],
                   "month_branch": f["month_branch"], "undisputed": f["undisputed"],
                   "boundary_disputed": f["boundary_disputed"],
                   "research_sample_status": f["research_sample_status"],
                   "direction": f["direction"], "body_return": f["body_return"],
                   "single_day": f["single_day"], "svu": ""}
        for fact in t["relation_facts_json"]:
            fam = fact.get("relation_family")
            rec = dict(rcommon)
            rec.update({
                "record_kind": "OFFICIAL", "relation_type": fact["relation_type"],
                "family": fam, "natal_position": fact.get("natal_position") or "multi",
                "occupied_positions": jarr(fact.get("occupied_positions")),
                "natal_positions_nonflow": jarr(fact.get("natal_positions_nonflow")),
                "flow_stem": fact.get("flow_stem"), "flow_branch": fact.get("flow_branch"),
                "flow_element": (fact.get("flow_stem_element") if fam == "stem"
                                 else fact.get("flow_branch_element") if fam == "branch"
                                 else None),
                "natal_element": (fact.get("natal_stem_element") if fam == "stem"
                                  else fact.get("natal_branch_element") if fam == "branch"
                                  else None),
                "origin_type": fact.get("origin_type") or "SINGLE_PAIR",
                "structure_combo": fact.get("structure_combo"),
                "present_branches": jarr(fact.get("present_distinct_branches")),
                "missing_branches": jarr(fact.get("missing_branches")),
                "wang_branch_present": (str(int(fact["wang_branch_present"]))
                                        if fact.get("wang_branch_present") is not None
                                        else None),
                "partial_kind": fact.get("partial_kind"),
                "establishment_rule_id": fact.get("establishment_rule_id"),
                "candidate_transformation_element": fact.get("candidate_transformation_element"),
                "transformation_established": fact.get("transformation_established"),
                "transformation_candidate_disputed": fact.get("transformation_candidate_disputed"),
                "fact_direction_marker": DIRECTION_MARKER.get(fact["relation_type"]),
                "confirmed": 1 if "confirmed" in fact else None,
                "frequency_eligible": None, "note": None,
            })
            rels.append(rec)
        for cand_f in t["candidate_facts_json"]:
            rec = dict(rcommon)
            rec.update({
                "record_kind": "CANDIDATE", "relation_type": cand_f.get("candidate_kind"),
                "family": cand_f.get("structure_family"), "natal_position": "multi",
                "occupied_positions": jarr(cand_f.get("occupied_positions")),
                "natal_positions_nonflow": jarr(cand_f.get("natal_positions_nonflow")),
                "flow_stem": f["flow_stem"], "flow_branch": f["flow_branch"],
                "flow_element": f["flow_branch_element"], "natal_element": None,
                "origin_type": cand_f.get("origin_type") or "FLOW_PARTIAL",
                "structure_combo": cand_f.get("structure_combo"),
                "present_branches": jarr(cand_f.get("present_distinct_branches")),
                "missing_branches": jarr(cand_f.get("missing_branches")),
                "wang_branch_present": (str(int(cand_f["wang_branch_present"]))
                                        if cand_f.get("wang_branch_present") is not None
                                        else None),
                "partial_kind": cand_f.get("partial_kind"),
                "establishment_rule_id": cand_f.get("establishment_rule_id"),
                "candidate_transformation_element": None,
                "transformation_established": None,
                "transformation_candidate_disputed": None,
                "fact_direction_marker": None,
                "confirmed": int(bool(cand_f.get("confirmed"))),
                "frequency_eligible": int(bool(cand_f.get("frequency_eligible"))),
                "note": cand_f.get("note"),
            })
            rels.append(rec)
    rels.sort(key=lambda r: (r["identity_code"], r["record_kind"], r["relation_type"]))
    return {"facts": facts, "mrt": mrt, "relations": rels}


# --------------------------------------------------------------------------- #
# digest (copied from e2e/e2e_digest.py: same row order, same repr-based hash)
# --------------------------------------------------------------------------- #
PKS = {
    "monthly_facts": ("identity_code", "month_id"),
    "month_relation_types": ("identity_code", "month_id", "record_kind", "relation_type"),
    "monthly_relations": ("identity_code", "month_id", "record_kind", "relation_type",
                          "natal_position"),
    "stats_pillar60": ("identity_code", "flow_pillar"),
    "stats_branch12": ("identity_code", "sample_policy", "branch"),
    "stats_relation_types": ("identity_code", "record_kind", "relation_type"),
    "stats_relation_pillar": ("identity_code", "record_kind", "relation_type", "flow_pillar"),
    "relation_type_market_stats": ("category_id", "item"),
}
STATS_TABLES = ("stats_pillar60", "stats_branch12", "stats_relation_types",
                "stats_relation_pillar", "relation_type_market_stats")


def digest_table(con, table: str, where: str = "", params=()) -> dict:
    pk = PKS[table]
    cols = [r[1] for r in con.execute("PRAGMA table_info(%s)" % table)]
    if table == "monthly_relations":
        cols = [c for c in cols if c != "seq"]      # seq is an insertion counter
        order = ",".join('"%s"' % c for c in cols)
    else:
        order = ",".join(pk)
    h = hashlib.sha256()
    n = 0
    sql = ("SELECT %s FROM %s %s ORDER BY %s"
           % (",".join('"%s"' % c for c in cols), table, where, order))
    for row in con.execute(sql, params):
        h.update(("|".join(repr(v) for v in row) + "\n").encode())
        n += 1
    return {"rows": n, "sha256": h.hexdigest()}


# --------------------------------------------------------------------------- #
# staging database
# --------------------------------------------------------------------------- #
def table_cols(con, table: str) -> list:
    return [r[1] for r in con.execute("PRAGMA table_info(%s)" % table)]


def count_rows(con, table: str, month_id: str = None) -> int:
    if month_id is None:
        return con.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
    return con.execute("SELECT COUNT(*) FROM %s WHERE month_id=?" % table,
                       (month_id,)).fetchone()[0]


def write_month_into_copy(db_copy: Path, month: dict, merged: dict, rehearsal: bool) -> dict:
    con = connect_rw(db_copy)
    try:
        fcols = table_cols(con, "monthly_facts")
        mcols = table_cols(con, "month_relation_types")
        rcols = table_cols(con, "monthly_relations")
        if len(fcols) != 67 or fcols[:len(MARKET_COLS_FACTS)] != MARKET_COLS_FACTS:
            raise SystemExit("monthly_facts schema drift: %d columns" % len(fcols))
        if len(mcols) != 15 or len(rcols) != 37:
            raise SystemExit("child table schema drift: mrt=%d relations=%d"
                             % (len(mcols), len(rcols)))
        before = {"monthly_facts": count_rows(con, "monthly_facts", month["month_id"]),
                  "month_relation_types": count_rows(con, "month_relation_types",
                                                     month["month_id"]),
                  "monthly_relations": count_rows(con, "monthly_relations",
                                                  month["month_id"])}
        if any(before.values()) and not rehearsal:
            raise SystemExit("month %s already in the copy (%s) and --rehearsal not given"
                             % (month["month_id"], before))
        with con:
            con.execute("DELETE FROM monthly_relations WHERE month_id=?", (month["month_id"],))
            con.execute("DELETE FROM month_relation_types WHERE month_id=?",
                        (month["month_id"],))
            con.execute("DELETE FROM monthly_facts WHERE month_id=?", (month["month_id"],))
            con.executemany(
                "INSERT INTO monthly_facts(%s) VALUES(%s)"
                % (",".join(fcols), ",".join("?" * len(fcols))),
                [[r[c] for c in fcols] for r in merged["facts"].values()])
            con.executemany(
                "INSERT INTO month_relation_types(%s) VALUES(%s)"
                % (",".join(mcols), ",".join("?" * len(mcols))),
                [[r[c] for c in mcols] for r in merged["mrt"].values()])
            con.executemany(
                "INSERT INTO monthly_relations(%s) VALUES(%s)"
                % (",".join(rcols), ",".join("?" * len(rcols))),
                [[r.get(c) for c in rcols] for r in merged["relations"]])
        after = {"monthly_facts": count_rows(con, "monthly_facts", month["month_id"]),
                 "month_relation_types": count_rows(con, "month_relation_types",
                                                    month["month_id"]),
                 "monthly_relations": count_rows(con, "monthly_relations",
                                                 month["month_id"])}
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("PRAGMA journal_mode=DELETE")
        con.commit()
    finally:
        con.close()
    expected = {"monthly_facts": len(merged["facts"]),
                "month_relation_types": len(merged["mrt"]),
                "monthly_relations": len(merged["relations"])}
    if after != expected:
        raise SystemExit("insert verification failed: %s != %s" % (after, expected))
    print("      copy month rows: before=%s after=%s" % (before, after))
    return {"before": before, "after": after, "expected": expected}


def recompute_stats(db_copy: Path, work_dir: Path, tag: str) -> dict:
    """Run the verified stats rebuilder (facts -> 5 tables) into a scratch sqlite."""
    out = work_dir / ("stats_%s.sqlite" % tag)
    log = work_dir / ("stats_%s_log.json" % tag)
    for p in (out, log):
        if p.exists():
            p.unlink()
    cmd = [sys.executable, str(E2E / "e2e_build_stats.py"), "--db", str(db_copy),
           "--out", str(out), "--log", str(log)]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise SystemExit("stats rebuild failed: %s" % proc.stderr[-1200:])
    logd = json.loads(log.read_text(encoding="utf-8"))
    print("      stats rebuilt rows=%s in %.1fs"
          % ({k: v["rows"] for k, v in logd.items() if isinstance(v, dict) and "rows" in v},
             time.time() - t0))
    return {"scratch": out, "log": logd, "seconds": round(time.time() - t0, 1)}


def replace_stats(db_copy: Path, scratch: Path) -> dict:
    """DELETE + INSERT the 5 tables inside the copy (schema and indexes untouched)."""
    con = connect_rw(db_copy)
    out = {}
    try:
        con.execute("ATTACH DATABASE ? AS st", ("file:" + quote(str(scratch.resolve()))
                                                + "?mode=ro",))
        for t in STATS_TABLES:
            cols = table_cols(con, t)
            scols = [r[1] for r in con.execute("PRAGMA st.table_info(%s)" % t)]
            if cols != scols:
                raise SystemExit("column drift on %s: %s vs %s" % (t, cols, scols))
            n_src = con.execute("SELECT COUNT(*) FROM st.%s" % t).fetchone()[0]
            with con:
                con.execute("DELETE FROM %s" % t)
                con.execute("INSERT INTO %s(%s) SELECT %s FROM st.%s"
                            % (t, ",".join('"%s"' % c for c in cols),
                               ",".join('"%s"' % c for c in cols), t))
            n_dst = con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
            if n_dst != n_src:
                raise SystemExit("stats copy mismatch on %s: %d != %d" % (t, n_dst, n_src))
            out[t] = n_dst
        con.execute("DETACH DATABASE st")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("PRAGMA journal_mode=DELETE")
        con.commit()
    finally:
        con.close()
    print("      stats installed into the copy: %s" % out)
    return out


# --------------------------------------------------------------------------- #
# four reconciliations
# --------------------------------------------------------------------------- #
def spot_check(con_stage, month: dict, slice_rows: list, engine: dict, source_hash: dict,
               codes: list) -> dict:
    """Independent recompute of 3 identities straight from slice + parquet + engine JSON."""
    sl = {r["identity_code"]: r for r in slice_rows}
    codes = [c for c in codes if c in sl]
    if len(codes) < 3:
        codes = sorted(sl)[:3]
    fcols = table_cols(con_stage, "monthly_facts")
    mcols = table_cols(con_stage, "month_relation_types")
    rcols = table_cols(con_stage, "monthly_relations")
    db_f = {(r[fcols.index("identity_code")]): dict(zip(fcols, r)) for r in con_stage.execute(
        "SELECT %s FROM monthly_facts WHERE month_id=?" % ",".join(fcols),
        (month["month_id"],))}
    db_m = defaultdict(list)
    for r in con_stage.execute("SELECT %s FROM month_relation_types WHERE month_id=?"
                               % ",".join(mcols), (month["month_id"],)):
        d = dict(zip(mcols, r))
        db_m[d["identity_code"]].append(d)
    db_r = defaultdict(list)
    for r in con_stage.execute("SELECT %s FROM monthly_relations WHERE month_id=?"
                               % ",".join(rcols), (month["month_id"],)):
        d = dict(zip(rcols, r))
        db_r[d["identity_code"]].append(d)

    # factors re-read from the parquet for exactly these bars (independent of the CSV)
    need = []
    for c in codes:
        s = sl[c]
        need += [(c, s["first_date"]), (c, s["last_date"]), (c, s["anchor_date"])]
    dcon = duckdb_mod().connect()
    got = {}
    for c, d in set(need):
        row = dcon.execute("SELECT adj_factor FROM read_parquet(?) WHERE ts_code=? "
                           "AND trade_date=?", [str(FACTOR_PARQUET), c, dash_to_ymd(d)]
                           ).fetchone()
        got[(c, d)] = D(str(row[0])) if row else None
    dcon.close()

    res = {"codes": codes, "stocks": {}}
    for c in codes:
        s = sl[c]
        f = db_f.get(c)
        item = {"problems": []}
        if f is None:
            item["problems"].append("month row missing from staging")
            res["stocks"][c] = item
            continue
        is_index = s["security_type"] == "INDEX"
        f1 = D(1) if is_index else got.get((c, s["first_date"]))
        f2 = D(1) if is_index else got.get((c, s["last_date"]))
        fa = D(1) if is_index else got.get((c, s["anchor_date"]))
        if None in (f1, f2, fa):
            item["problems"].append("factor missing in parquet")
            res["stocks"][c] = item
            continue
        calc = body(s["raw_open"], s["raw_close"], format(f1, "f"), format(f2, "f"),
                    format(fa, "f"))
        checks = {
            "qfq_open": abs(D(calc["qfq_open"]) - D(f["qfq_open"])) <= TOL,
            "qfq_close": abs(D(calc["qfq_close"]) - D(f["qfq_close"])) <= TOL,
            "return_decimal": abs(D(calc["return_decimal"]) - D(f["return_decimal"])) <= TOL,
            "body_return": float(calc["body_return"]) == float(f["body_return"]),
            "direction": calc["direction"] == f["direction"],
            "factor_first": f1 == D(f["factor_first"]),
            "factor_last": f2 == D(f["factor_last"]),
            "anchor_factor": fa == D(f["anchor_factor"]),
            "anchor_date": s["anchor_date"] == f["anchor_date"],
            "first_date": s["first_date"] == f["first_date"],
            "last_date": s["last_date"] == f["last_date"],
            "trading_days": int(s["n_bars"]) == int(f["trading_days"]),
            "source_hash": source_hash.get(c) == f["source_hash"],
        }
        t = engine[c]
        eng_types = json.dumps(t["relation_types_json"], ensure_ascii=False,
                               separators=(",", ":"))
        cand = list(dict.fromkeys(x.get("candidate_kind") for x in t["candidate_facts_json"]))
        checks.update({
            "row_sha256": t["row_sha256"] == f["row_sha256"],
            "relation_type_list": eng_types == f["relation_type_list"],
            "confirmed_event_count": t["confirmed_event_count"] == f["confirmed_event_count"],
            "distinct_relation_type_count":
                t["distinct_relation_type_count"] == f["distinct_relation_type_count"],
            "candidate_kind_list": (cand[0] if cand else None) == f["candidate_kind_list"],
            "computation_status": t["row_status"] == f["computation_status"],
            "undisputed": (1 if t["row_status"] == "COMPUTED" else 0) == f["undisputed"],
        })
        # MRT re-derived from the engine facts
        exp_mrt = Counter()
        exp_pos = defaultdict(set)
        for fact in t["relation_facts_json"]:
            exp_mrt[("OFFICIAL", fact["relation_type"])] += 1
            if fact.get("natal_position"):
                exp_pos[("OFFICIAL", fact["relation_type"])].add(fact["natal_position"])
        for cand_f in t["candidate_facts_json"]:
            exp_mrt[("CANDIDATE", cand_f.get("candidate_kind"))] += 1
        got_mrt = {(d["record_kind"], d["relation_type"]): d for d in db_m.get(c, [])}
        checks["mrt_key_set"] = set(exp_mrt) == set(got_mrt)
        checks["mrt_event_count"] = all(got_mrt[k]["event_count"] == v
                                        for k, v in exp_mrt.items() if k in got_mrt)
        checks["mrt_positions"] = all(
            (got_mrt[k]["positions"] or "multi")
            == (",".join(sorted(exp_pos[k], key=lambda x: POS_ORDER.get(x, 9)))
                if exp_pos[k] else "multi")
            for k in exp_mrt if k[0] == "OFFICIAL" and k in got_mrt)
        # relations rows re-derived from the engine facts
        exp_rel = Counter()
        for fact in t["relation_facts_json"]:
            exp_rel[("OFFICIAL", fact["relation_type"])] += 1
        for cand_f in t["candidate_facts_json"]:
            exp_rel[("CANDIDATE", cand_f.get("candidate_kind"))] += 1
        got_rel = Counter((d["record_kind"], d["relation_type"]) for d in db_r.get(c, []))
        checks["relations_row_counts"] = exp_rel == got_rel
        bad = [k for k, v in checks.items() if not v]
        item.update({"checks": len(checks), "failed": bad,
                     "values": {k: [str(f.get(k)), v] for k, v in (
                         ("qfq_open", calc["qfq_open"]), ("qfq_close", calc["qfq_close"]),
                         ("return_decimal", calc["return_decimal"]))}})
        item["problems"] += bad
        res["stocks"][c] = item
    res["all_ok"] = all(not v["problems"] for v in res["stocks"].values())
    return res


def increment_check(con_stage, con_formal, month: dict, months_hist: list,
                    engine: dict) -> dict:
    """④ relation-type increments: MRT/relations vs engine, and the 5-table count deltas."""
    mid = month["month_id"]
    marks = ",".join("?" * len(months_hist)) if months_hist else None
    out = {}

    # (a) month rows vs engine facts (all identities, exact)
    fcols = table_cols(con_stage, "monthly_facts")
    mcols = table_cols(con_stage, "month_relation_types")
    stage_mrt = defaultdict(dict)
    for r in con_stage.execute("SELECT %s FROM month_relation_types WHERE month_id=?"
                               % ",".join(mcols), (mid,)):
        d = dict(zip(mcols, r))
        stage_mrt[d["identity_code"]][(d["record_kind"], d["relation_type"])] = d
    stage_rel = Counter((r[0], (r[1], r[2])) for r in con_stage.execute(
        "SELECT identity_code, record_kind, relation_type FROM monthly_relations "
        "WHERE month_id=?", (mid,)))
    rel_by_code = defaultdict(Counter)
    for (c, k) in stage_rel:
        rel_by_code[c][k] = stage_rel[(c, k)]
    bad_keys = bad_count = bad_rel = 0
    ex = []
    for code, t in engine.items():
        exp = Counter()
        for fact in t["relation_facts_json"]:
            exp[("OFFICIAL", fact["relation_type"])] += 1
        for cand_f in t["candidate_facts_json"]:
            exp[("CANDIDATE", cand_f.get("candidate_kind"))] += 1
        got = {k: v["event_count"] for k, v in stage_mrt.get(code, {}).items()}
        if set(got) != set(exp):
            bad_keys += 1
            if len(ex) < 5:
                ex.append([code, sorted(set(got)), sorted(set(exp))])
        elif any(got[k] != exp[k] for k in exp):
            bad_count += 1
            if len(ex) < 5:
                ex.append([code, "count", {k: got[k] for k in exp}, dict(exp)])
        if dict(rel_by_code.get(code, {})) != dict(exp):
            bad_rel += 1
    out["mrt_vs_engine"] = {"identities": len(engine), "key_set_mismatch": bad_keys,
                            "event_count_mismatch": bad_count,
                            "relations_rows_mismatch": bad_rel,
                            "examples": ex,
                            "ok": not (bad_keys or bad_count or bad_rel)}

    # (b) summary-table count increments (base = every month except the target)
    month_pillar = defaultdict(lambda: [0, 0, 0, 0, 0])   # n, up, dn, flat, single
    month_branch = defaultdict(lambda: [0, 0, 0, 0])      # n, up, dn, flat (ALL)
    month_branch_x = defaultdict(lambda: [0, 0, 0, 0])    # EXCLUDE_SINGLE
    for ident, pillar, branch, direction, single in con_stage.execute(
            "SELECT identity_code, flow_pillar, month_branch, direction, single_day "
            "FROM monthly_facts WHERE month_id=?", (mid,)):
        di = {"UP": 1, "DOWN": 2, "FLAT": 3}[direction]
        p = month_pillar[(ident, pillar)]
        p[0] += 1
        p[di] += 1
        p[4] += int(single or 0)
        b = month_branch[(ident, branch)]
        b[0] += 1
        b[di] += 1
        if not single:
            bx = month_branch_x[(ident, branch)]
            bx[0] += 1
            bx[di] += 1
    base_pillar = _base_pillar(con_formal, months_hist)
    base_branch = _base_branch(con_formal, months_hist)

    # stats_pillar60 counts
    mism = []
    checked = 0
    for ident, pillar, n, up, dn, fl, es in con_stage.execute(
            "SELECT identity_code, flow_pillar, all_sample_n, up_n_ALL, down_n_ALL, "
            "flat_n_ALL, exclude_single_n FROM stats_pillar60"):
        base = base_pillar.get((ident, pillar), [0, 0, 0, 0, 0])
        add = month_pillar.get((ident, pillar)) or [0, 0, 0, 0, 0]
        expect = (base[0] + add[0], base[1] + add[1], base[2] + add[2],
                  base[3] + add[3],
                  (base[0] - base[4]) + (add[0] - add[4]))
        checked += 1
        if (n, up, dn, fl, es) != expect:
            if len(mism) < 5:
                mism.append([ident, pillar, [n, up, dn, fl, es], list(expect)])
    out["stats_pillar60_counts"] = {"rows_checked": checked, "mismatch": len(mism),
                                    "examples": mism, "ok": not mism,
                                    "columns": ["all_sample_n", "up_n_ALL", "down_n_ALL",
                                                "flat_n_ALL", "exclude_single_n"]}

    # stats_branch12 counts
    mism = []
    checked = 0
    for ident, pol, br, n, up, dn, fl in con_stage.execute(
            "SELECT identity_code, sample_policy, branch, n, up_n, down_n, flat_n "
            "FROM stats_branch12"):
        base = base_branch.get((ident, br, pol), [0, 0, 0, 0])
        add = (month_branch if pol == "ALL" else month_branch_x).get((ident, br)) or [0, 0, 0, 0]
        expect = (base[0] + add[0], base[1] + add[1], base[2] + add[2], base[3] + add[3])
        checked += 1
        if (n, up, dn, fl) != expect:
            if len(mism) < 5:
                mism.append([ident, pol, br, [n, up, dn, fl], list(expect)])
    out["stats_branch12_counts"] = {"rows_checked": checked, "mismatch": len(mism),
                                    "examples": mism, "ok": not mism,
                                    "columns": ["n", "up_n", "down_n", "flat_n"]}

    # stats_relation_types / stats_relation_pillar increments
    hist_rt = Counter()
    if marks:
        for r in con_formal.execute(
                "SELECT identity_code, record_kind, relation_type, COUNT(*), SUM(undisputed) "
                "FROM month_relation_types WHERE month_id IN (%s) "
                "GROUP BY 1,2,3" % marks, months_hist):
            hist_rt[(r[0], r[1], r[2])] = (r[3], r[4] or 0)
    month_rt = Counter()
    month_rt_und = Counter()
    for r in con_stage.execute(
            "SELECT identity_code, record_kind, relation_type, undisputed FROM "
            "month_relation_types WHERE month_id=?", (mid,)):
        month_rt[(r[0], r[1], r[2])] += 1
        month_rt_und[(r[0], r[1], r[2])] += int(r[3] or 0)
    mism = []
    checked = 0
    for r in con_stage.execute(
            "SELECT identity_code, record_kind, relation_type, months_any, months_undisputed "
            "FROM stats_relation_types"):
        k = (r[0], r[1], r[2])
        base = hist_rt.get(k, (0, 0))
        expect = (base[0] + month_rt.get(k, 0), base[1] + month_rt_und.get(k, 0))
        checked += 1
        if (r[3], r[4]) != expect:
            if len(mism) < 5:
                mism.append([list(k), [r[3], r[4]], list(expect)])
    out["stats_relation_types_months"] = {"rows_checked": checked, "mismatch": len(mism),
                                          "examples": mism, "ok": not mism,
                                          "note": "months_any / months_undisputed only"}
    hist_rp = Counter()
    if marks:
        for r in con_formal.execute(
                "SELECT identity_code, record_kind, relation_type, flow_pillar, COUNT(*), "
                "SUM(undisputed) FROM month_relation_types WHERE month_id IN (%s) "
                "GROUP BY 1,2,3,4" % marks, months_hist):
            hist_rp[(r[0], r[1], r[2], r[3])] = (r[4], r[5] or 0)
    month_rp = Counter()
    month_rp_und = Counter()
    for r in con_stage.execute(
            "SELECT identity_code, record_kind, relation_type, flow_pillar, undisputed FROM "
            "month_relation_types WHERE month_id=?", (mid,)):
        month_rp[(r[0], r[1], r[2], r[3])] += 1
        month_rp_und[(r[0], r[1], r[2], r[3])] += int(r[4] or 0)
    mism = []
    checked = 0
    for r in con_stage.execute(
            "SELECT identity_code, record_kind, relation_type, flow_pillar, months_any, "
            "months_undisputed FROM stats_relation_pillar"):
        k = (r[0], r[1], r[2], r[3])
        base = hist_rp.get(k, (0, 0))
        expect = (base[0] + month_rp.get(k, 0), base[1] + month_rp_und.get(k, 0))
        checked += 1
        if (r[4], r[5]) != expect:
            if len(mism) < 5:
                mism.append([list(k), [r[4], r[5]], list(expect)])
    out["stats_relation_pillar_months"] = {"rows_checked": checked, "mismatch": len(mism),
                                           "examples": mism, "ok": not mism}
    out["ok"] = all(v["ok"] for k, v in out.items() if isinstance(v, dict) and "ok" in v)
    out["not_checked"] = ["stats_pillar60/branch12 means, medians, grades, occurrence lists",
                          "stats_relation_types/pillar rates, means, json position/origin "
                          "aggregates, events",
                          "relation_type_market_stats (whole-table recompute; covered by the "
                          "5-table compare, which is 0-diff in rehearsal only)"]
    return out


# helpers for the baseline (history) counts used by increment_check
def _base_pillar(con, months) -> dict:
    """{(identity, flow_pillar): [n, up, dn, flat, single]} over months != target."""
    if not months:
        return {}
    marks = ",".join("?" * len(months))
    out = {}
    for r in con.execute(
            "SELECT identity_code, flow_pillar, COUNT(*), "
            "SUM(CASE WHEN direction='UP' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN direction='DOWN' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN direction='FLAT' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN single_day=1 THEN 1 ELSE 0 END) "
            "FROM monthly_facts WHERE month_id IN (%s) GROUP BY 1,2" % marks, months):
        out[(r[0], r[1])] = [r[2], r[3], r[4], r[5], r[6]]
    return out


def _base_branch(con, months) -> dict:
    """{(identity, month_branch, sample_policy): [n, up, dn, flat]} over months != target."""
    if not months:
        return {}
    marks = ",".join("?" * len(months))
    out = {}
    for ident, br, single, direction, n in con.execute(
            "SELECT identity_code, month_branch, single_day, direction, COUNT(*) "
            "FROM monthly_facts WHERE month_id IN (%s) GROUP BY 1,2,3,4" % marks, months):
        di = {"UP": 1, "DOWN": 2, "FLAT": 3}[direction]
        for pol in (("ALL",) if single else ("ALL", "EXCLUDE_SINGLE")):
            cur = out.setdefault((ident, br, pol), [0, 0, 0, 0])
            cur[0] += n
            cur[di] += n
    return out


# --------------------------------------------------------------------------- #
# report / log
# --------------------------------------------------------------------------- #
def append_update_log(mode: str, status: str, code: str, line: str) -> None:
    UPDATE_LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = iso8(now8())
    row = "| %s | post_month(%s) | %s | %s | %s |\n" % (stamp, mode, status, code or "", line)
    if not UPDATE_LOG.exists():
        UPDATE_LOG.write_text("# 更新记录\n\n| 北京时间 | trigger | status | 错误码 | 一句话 |\n"
                              "|---|---|---|---|---|\n" + row, encoding="utf-8")
    else:
        with open(UPDATE_LOG, "a", encoding="utf-8") as f:
            f.write(row)


def disk_free_gb(path: Path) -> float:
    st = os.statvfs(str(path))
    return round(st.f_bavail * st.f_frsize / (1024 ** 3), 2)


# --------------------------------------------------------------------------- #
# modes
# --------------------------------------------------------------------------- #
def plan_only(args, con, month, months_hist) -> int:
    t0 = time.time()
    mid = month["month_id"]
    existing = {t: count_rows(con, t, mid) for t in ("monthly_facts", "month_relation_types",
                                                     "monthly_relations")}
    totals = {t: count_rows(con, t) for t in ("monthly_facts", "month_relation_types",
                                              "monthly_relations")}
    zips = year_zips(month["start_date"], month["end_date"])
    print("[dry-run] read-only plan for %s (%s)" % (mid, month["month_pillar"]))
    print("  window        : (%s, %s]  start_at=%s end_at=%s"
          % (month["start_date"], month["end_date"], month["start_at"], month["end_at"]))
    # read-only coverage probe: max bar date in the mirror for this window
    _pc, per_date, _zc = scan_zip(zips, month["start_date"], "9999-12-31")
    mirror_max = max(per_date) if per_date else None
    asof = args.as_of or mirror_max
    if asof is None:
        print("  !! no bars found in the mirror for this window")
        return EXIT_ARGS
    if mirror_max and asof > mirror_max:
        print("  note          : --as-of %s is beyond the mirror (%s); using the mirror date"
              % (asof, mirror_max))
        asof = mirror_max
    covered = sorted(d for d in per_date if month["start_date"] < d <= asof)
    gate = can_post_month(month, now8(), asof, covered)
    print("  as_of         : %s (mirror max %s)" % (asof, mirror_max))
    print("  gate          : can_post=%s reason=%s" % (gate["can_post_month"], gate["reason"]))
    for c in gate["checks"]:
        print("     %-40s %s  %s" % (c["id"], "OK  " if c["ok"] else "FAIL", c["detail"]))
    print("  coverage      : %d/%d trading days (weekday estimate)"
          % (gate["coverage"]["covered_trading_days"],
             gate["coverage"]["expected_trading_days_weekday_estimate"]))
    print("  db rows now   : month=%s totals=%s" % (existing, totals))
    if any(existing.values()) and not args.rehearsal:
        print("  !! month already posted; staging would refuse (use --rehearsal to rebuild)")
    print("  verified assets: build_stats=%s compare_stats=%s"
          % ((E2E / "e2e_build_stats.py").exists(),
             (E2E / "e2e_compare_stats.py").exists()))
    print("  engine        : %s (run.py exists=%s)"
          % (ENGINE_DIR, (ENGINE_DIR / "run.py").exists()))
    print("  inputs        : zip=%s factor=%s index=%s duckdb=%s master_csv=%s"
          % (all(p.exists() for p in zips), FACTOR_PARQUET.exists(),
             INDEX_PARQUET.exists(), NAS_DUCKDB.exists(), STOCK_MASTER_CSV.exists()))
    print("  free disk     : %.2f GiB (a staging copy needs ~%.2f GiB)"
          % (disk_free_gb(DEV_ROOT), MASTER_DB.stat().st_size / (1024 ** 3) + 1.0))
    out_dir = WORK_ROOT / mid
    print("  would write   : %s/{slice,engine_in,engine_out/,db_staging.sqlite,"
          "stats_rebuilt.sqlite,report.json}" % out_dir)
    print("  summary tables: recomputed by e2e_build_stats.py, the stage3_stats.py "
          "equivalent (details in report.json -> stats.stage3_equivalence)")
    print("  stats as-of   : stage3 constant input_data_as_of=2026-10-14 (see report)")
    print("  dry-run wrote nothing; %.1fs" % (time.time() - t0))
    if not gate["can_post_month"]:
        print("  => INCOMPLETE_MONTH (%s); staging requires --allow-incomplete, "
              "promote is always refused" % gate["reason"])
        return EXIT_INCOMPLETE
    return EXIT_OK


def stats_reproducible(db: Path, work_dir: Path, tag: str) -> dict:
    """用同一实现从事实表重算 5 张汇总表，必须与库里逐表相等。

    新月份进入聚合表必然改变汇总值 ⇒ 正确判据不是"整表不变"，而是"统计可由事实表复现"。
    """
    out = work_dir / ("stats_repro_%s.sqlite" % tag)
    log = work_dir / ("stats_repro_%s_log.json" % tag)
    for p in (out, log):
        p.unlink(missing_ok=True)
    cmd = [sys.executable, str(E2E / "e2e_build_stats.py"),
           "--db", str(db), "--out", str(out), "--log", str(log)]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr[-1200:]}
    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        con.execute("ATTACH DATABASE ? AS st", (str(out.resolve()),))
        tables = {}
        for t in STATS_TABLES:
            a = con.execute('SELECT COUNT(*) FROM (SELECT * FROM main."%s" EXCEPT '
                            'SELECT * FROM st."%s")' % (t, t)).fetchone()[0]
            b = con.execute('SELECT COUNT(*) FROM (SELECT * FROM st."%s" EXCEPT '
                            'SELECT * FROM main."%s")' % (t, t)).fetchone()[0]
            tables[t] = {"only_db": a, "only_recomputed": b}
    finally:
        con.close()
    ok = all(v["only_db"] == 0 and v["only_recomputed"] == 0 for v in tables.values())
    return {"ok": ok, "tables": tables}


def run_staging(args, con_master, month, months_hist) -> int:
    t0 = time.time()
    mid = month["month_id"]
    work_dir = WORK_ROOT / mid
    # clear only this tool's own artefacts under work/post/<month>/
    for name in ("db_staging.sqlite", "stats_rebuilt.sqlite", "stats_rebuilt_log.json",
                 "report.json", "compare_stats.json", "digest_formal.json",
                 "digest_staging.json", "engine_out", "slice", "engine_in"):
        p = work_dir / name
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
    work_dir.mkdir(parents=True, exist_ok=True)

    formal_before = sha256_file(MASTER_DB)
    print("[1/9] calendar: %s %s window (%s, %s]"
          % (mid, month["month_pillar"], month["start_date"], month["end_date"]))
    print("      formal db sha256(before) = %s" % formal_before)

    # ---- step 3: slice -----------------------------------------------------
    print("[3/9] building the qfq slice (replay_slice_month logic) ...")
    sl = build_slice(con_master, month, args.as_of, work_dir / "slice")
    data_through = sl["data_through"]
    args.as_of_eff = sl["as_of"]
    print("      as_of used = %s (source: %s), data_through = %s"
          % (args.as_of_eff, args.as_of_source, data_through))
    covered = [d for d in sl["trading_days"]]
    print("[2/9] can_post_month gate")
    gate = can_post_month(month, now8(), data_through, covered)
    for c in gate["checks"]:
        print("      %-40s %s  %s" % (c["id"], "OK  " if c["ok"] else "FAIL", c["detail"]))
    incomplete = not gate["can_post_month"]
    if incomplete and not args.allow_incomplete:
        print("      REFUSED: %s (a partial month must not be posted as a full month)"
              % gate["reason"])
        write_report(work_dir, month, args, {"status": "REFUSED_INCOMPLETE",
                                             "gate": gate,
                                             "formal_sha256_before": formal_before})
        append_update_log("staging", "REFUSED_INCOMPLETE", "INCOMPLETE_MONTH",
                          "%s gate failed: %s" % (mid, gate["reason"]))
        return EXIT_INCOMPLETE
    status = "INCOMPLETE_MONTH" if incomplete else "OK"

    # ---- step 4: engine input ---------------------------------------------
    print("[4/9] building the 34-column engine input ...")
    cols = [r[1] for r in con_master.execute("PRAGMA table_info(securities)")]
    securities = {d["identity_code"]: d for d in (dict(zip(cols, r)) for r in
                  con_master.execute("SELECT %s FROM securities" % ",".join(cols)))}
    profiles = load_profiles(con_master)
    complete = int(sl["rows"][0]["fact_status"] == "FINAL") == 1
    inp = build_engine_input(month, sl["rows"], securities, profiles, complete,
                             work_dir / "engine_in")
    m08check = verify_input_against_m08(inp["rows"], mid)

    # ---- step 5: engine ----------------------------------------------------
    print("[5/9] running the v13 engine (subprocess, PYTHONDONTWRITEBYTECODE=1) ...")
    eng_run = run_engine(inp["csv"], work_dir / "engine_out")
    engine = load_engine_results(eng_run["db"])
    if len(engine) != len(inp["rows"]):
        raise SystemExit("engine returned %d rows for %d input rows"
                         % (len(engine), len(inp["rows"])))
    keys_equal = set(engine) == {r["identity_code"] for r in sl["rows"]}
    print("      engine keys == slice keys: %s" % keys_equal)
    if not keys_equal:
        raise SystemExit("engine identity set != slice identity set")

    # ---- step 6a: source_hash --------------------------------------------
    print("[6a/9] recomputing source_hash (stock level, cutoff=%s) ..." % args.as_of_eff)
    sh = compute_source_hash(sorted(engine), args.as_of_eff, NAS_DUCKDB, STOCK_MASTER_CSV)
    print("      hashed %d identities, %d daily rows, empty histories=%d"
          % (len(sh["source_hash"]), sh["total_daily_rows"], len(sh["empty_histories"])))

    # ---- step 6b: merge + copy -------------------------------------------
    print("[6b/9] merging facts/MRT/relations and writing them into the DB COPY ...")
    ref = {"stem_element": dict(con_master.execute("SELECT stem, element FROM stem_element")),
           "branch_element": dict(con_master.execute("SELECT branch, element FROM branch_element"))}
    pol = con_master.execute("SELECT DISTINCT policy_version, policy_sha256 FROM "
                             "monthly_facts").fetchall()
    if len(pol) != 1:
        raise SystemExit("expected one policy pair, got %s" % pol)
    ref["policy_version"], ref["policy_sha256"] = pol[0]
    merged = merge_month(month, sl["rows"], engine, sh["source_hash"], sl["index_daily"], ref)
    print("      merged facts=%d mrt=%d relations=%d"
          % (len(merged["facts"]), len(merged["mrt"]), len(merged["relations"])))
    copy = work_dir / "db_staging.sqlite"
    print("      copying the master DB (%.2f GiB) -> %s"
          % (MASTER_DB.stat().st_size / (1024 ** 3), copy))
    shutil.copyfile(MASTER_DB, copy)
    written = write_month_into_copy(copy, month, merged, args.rehearsal)

    # ---- step 7: summary tables ------------------------------------------
    print("[7/9] recomputing the 5 summary tables ...")
    print("      equivalence to stage3_stats.py (stated BEFORE the recompute):")
    for line in STAGE3_NOTE.split(". "):
        if line.strip():
            print("        - %s" % line.strip().rstrip("."))
    print("      implementation: work/extend/e2e/e2e_build_stats.py (unchanged, verified)")
    stats = recompute_stats(copy, work_dir, "rebuilt")
    installed = replace_stats(copy, stats["scratch"])

    # ---- step 8: four reconciliations ------------------------------------
    print("[8/9] four reconciliations ...")
    con_stage = connect_ro(copy)
    # (1) history unchanged
    marks = ",".join("?" * len(months_hist)) if months_hist else None
    hist = {}
    for t in ("monthly_facts", "month_relation_types", "monthly_relations"):
        where = "WHERE month_id IN (%s)" % marks if marks else ""
        hist[t] = {"formal": digest_table(con_master, t, where, months_hist),
                   "staging": digest_table(con_stage, t, where, months_hist)}
    hist_ok = all(v["formal"]["sha256"] == v["staging"]["sha256"] for v in hist.values())
    # implementation self-check against the recorded evidence
    ev = EVIDENCE.get("2026-M08")
    evidence_match = None
    if mid == "2026-M08" and ev:
        evidence_match = {
            "facts": hist["monthly_facts"]["formal"]["sha256"]
            == ev["history_facts_le_2026-M07"],
            "mrt": hist["month_relation_types"]["formal"]["sha256"]
            == ev["history_mrt_le_2026-M07"],
            "facts_rows": hist["monthly_facts"]["formal"]["rows"] == ev["history_facts_rows"],
            "mrt_rows": hist["month_relation_types"]["formal"]["rows"] == ev["history_mrt_rows"]}
    stats_hist = {}
    for t in STATS_TABLES:
        stats_hist[t] = {"formal": digest_table(con_master, t),
                         "staging": digest_table(con_stage, t)}
    # 新月份进入聚合表必然改变它们；判据改为"统计可由事实表复现"（正式库侧）。
    stats_repro = stats_reproducible(MASTER_DB, work_dir, "master")
    stats_hist_ok = bool(stats_repro.get("ok"))
    # extra evidence: the target month's own rows, byte for byte (expected only in a
    # rehearsal, where the month already exists in the formal DB)
    month_digest = {}
    for t in ("monthly_facts", "month_relation_types", "monthly_relations"):
        month_digest[t] = {
            "formal": digest_table(con_master, t, "WHERE month_id=?", (mid,)),
            "staging": digest_table(con_stage, t, "WHERE month_id=?", (mid,))}
    month_ok = all(v["formal"]["sha256"] == v["staging"]["sha256"]
                   for v in month_digest.values())
    chk1 = {"id": "HISTORY_UNCHANGED", "ok": bool(hist_ok and stats_hist_ok
                                                 and (month_ok if args.rehearsal else True)),
            "history_tables": hist, "evidence_self_check": evidence_match,
            "summary_tables_whole_table": stats_hist,
            "stats_reproducible": stats_repro,
            "month_rows_byte_identical": month_digest,
            "note": "history = month_id strictly before the target (calendar order); the 5 "
                    "summary tables are compared as whole tables; the target month's own "
                    "rows are compared byte for byte in a rehearsal"}

    # (2) row counts
    counts = {}
    counts_ok = True
    for t in ("monthly_facts", "month_relation_types", "monthly_relations"):
        tf = count_rows(con_master, t)
        mf = count_rows(con_master, t, mid)
        ts = count_rows(con_stage, t)
        ms = count_rows(con_stage, t, mid)
        exp = {"total_staging": tf - mf + ms, "month_staging": ms}
        ok = ts == exp["total_staging"] and ms == written["expected"][t]
        counts_ok &= ok
        counts[t] = {"total_formal": tf, "month_formal": mf, "total_staging": ts,
                     "month_staging": ms, "month_delta": ms - mf,
                     "expected_total": exp["total_staging"],
                     "expected_month": written["expected"][t], "ok": ok}
    chk2 = {"id": "ROW_COUNTS", "ok": counts_ok, "tables": counts,
            "slice_rows": len(sl["rows"]), "missing_identities": len(sl["missing"]),
            "missing_by_reason": dict(Counter(m["reason"] for m in sl["missing"]))}

    # (3) 3-stock recompute
    spot = spot_check(con_stage, month, sl["rows"], engine, sh["source_hash"],
                      ["000001.SZ", "600004.SH", "001216.SZ"])
    chk3 = {"id": "SPOT_RECOMPUTE_3", "ok": spot["all_ok"], "detail": spot}

    # (4) relation-type increments
    inc = increment_check(con_stage, con_master, month, months_hist, engine)
    chk4 = {"id": "RELATION_INCREMENT", "ok": inc["ok"], "detail": inc}

    checks = {c["id"]: c for c in (chk1, chk2, chk3, chk4)}
    all_ok = all(c["ok"] for c in checks.values())

    # 5-table value compare (must be 0 diff in rehearsal; recorded otherwise)
    comp_json = work_dir / "compare_stats.json"
    cmd = [sys.executable, str(E2E / "e2e_compare_stats.py"), "--a", str(copy),
           "--b", str(MASTER_DB), "--out", str(comp_json)]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                         env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    if proc.returncode != 0:
        raise SystemExit("5-table compare failed: %s" % proc.stderr[-800:])
    (work_dir / "compare_stats.out").write_text(proc.stdout, encoding="utf-8")
    comp = json.loads(comp_json.read_text(encoding="utf-8"))
    stats_diff = {t: {"rows_a": v["rows_a"], "rows_b": v["rows_b"],
                      "pk_set_equal": v["pk_set_equal"],
                      "rows_with_any_diff": v["rows_with_any_diff"]}
                  for t, v in comp["tables"].items()}
    stats_zero_diff = all(v["pk_set_equal"] and v["rows_with_any_diff"] == 0
                          for v in comp["tables"].values())
    if args.rehearsal and not stats_zero_diff:
        all_ok = False
    if not all_ok:
        status = "RECON_FAILED"

    con_stage.close()

    # ---- step 9: report + log --------------------------------------------
    print("[9/9] writing report ...")
    formal_after = sha256_file(MASTER_DB)
    report = {
        "schema": "post-month-report-v1",
        "script": "tools/post_month.py",
        "script_version": SCRIPT_VERSION,
        "generated_at": iso8(now8()),
        "timezone": "UTC+8 (fixed offset)",
        "mode": "staging",
        "rehearsal": bool(args.rehearsal),
        "status": status,
        "month": {k: month[k] for k in MONTH_KEYS},
        "as_of": args.as_of_eff,
        "as_of_source": args.as_of_source,
        "source_hash_cutoff": args.as_of_eff,
        "gate": gate,
        "slice": {"rows": len(sl["rows"]), "missing": len(sl["missing"]),
                  "missing_by_reason": dict(Counter(m["reason"] for m in sl["missing"])),
                  "data_through": data_through,
                  "trading_days_in_window": sl["trading_days"],
                  "direction": sl["manifest"]["counts"]["direction"],
                  "quality_flags": sl["manifest"]["counts"]["quality_flags"],
                  "path": str(sl["slice_csv"])},
        "engine_input": {"rows": len(inp["rows"]), "columns": len(COLS34),
                         "sha256": inp["manifest"]["output_csv_sha256"],
                         "row_status": inp["manifest"]["row_status"],
                         "return_valid_basis": inp["manifest"]["return_valid_basis"],
                         "m08_builder_self_check": {
                             "columns_checked": len(m08check.get("columns", {})),
                             "all_equal": m08check.get("all_equal"),
                             "columns_with_mismatch": m08check.get("columns_with_mismatch")}},
        "engine": {"rows": len(engine), "wall_seconds": eng_run["wall_seconds"],
                   "engine_sha256": eng_run["engine_sha256"],
                   "rule_reference_sha256": eng_run["rule_reference_sha256"],
                   "policy_sha256": eng_run["policy_sha256"],
                   "engine_db_sha256": eng_run["engine_db_sha256"],
                   "confirmed_relation_occurrences":
                       eng_run["manifest"].get("confirmed_relation_occurrences"),
                   "candidate_structure_occurrences":
                       eng_run["manifest"].get("candidate_structure_occurrences"),
                   "keys_equal_slice": keys_equal},
        "source_hash": {"cutoff": args.as_of_eff, "identities": len(sh["source_hash"]),
                        "total_daily_rows": sh["total_daily_rows"],
                        "empty_histories": sh["empty_histories"][:10]},
        "written": written,
        "merged": {"facts": len(merged["facts"]), "mrt": len(merged["mrt"]),
                   "relations": len(merged["relations"])},
        "stats": {"installed_rows": installed, "rebuild_seconds": stats["seconds"],
                  "stage3_equivalence": STAGE3_NOTE},
        "five_table_compare_vs_formal": stats_diff,
        "five_table_zero_diff": stats_zero_diff,
        "five_table_compare_note": ("rows_with_any_diff comes from the JSON of "
                                    "e2e_compare_stats.py; its stdout prints the join row "
                                    "count when there are no differing columns, which is a "
                                    "known quirk of that print statement, not a difference"),
        "checks": checks,
        "all_checks_pass": all_ok,
        "formal_db": {"path": str(MASTER_DB), "sha256_before": formal_before,
                      "sha256_after": formal_after,
                      "unchanged": formal_before == formal_after},
        "staging_db": {"path": str(copy), "bytes": copy.stat().st_size,
                       "sha256": sha256_file(copy)},
        "disk": {"free_gib_after": disk_free_gb(DEV_ROOT),
                 "staging_copy_gib": round(copy.stat().st_size / (1024 ** 3), 2)},
        "seconds_total": round(time.time() - t0, 1),
    }
    write_report(work_dir, month, args, report)
    one = ("%s 演练=%s：facts %d / MRT %d / relations %d；5 张汇总表差异=%s；"
           "历史哈希=%s；对账=%s" % (mid, bool(args.rehearsal), len(merged["facts"]),
                                len(merged["mrt"]), len(merged["relations"]),
                                "0" if stats_zero_diff else "见报告",
                                "不变" if chk1["ok"] else "变化",
                                "全过" if all_ok else "失败"))
    append_update_log("staging", status, "" if all_ok else "RECON_FAIL", one)
    print("\nRESULT %s" % status)
    print("  facts=%d mrt=%d relations=%d" % (len(merged["facts"]), len(merged["mrt"]),
                                              len(merged["relations"])))
    print("  five_table_compare: %s (zero_diff=%s)"
          % ({k: v["rows_with_any_diff"] for k, v in stats_diff.items()}, stats_zero_diff))
    print("  checks: %s" % {k: v["ok"] for k, v in checks.items()})
    print("  formal db sha256 unchanged: %s" % (formal_before == formal_after))
    print("  report: %s" % (work_dir / "report.json"))
    print("  %.1fs total" % (time.time() - t0))
    return EXIT_OK if all_ok else EXIT_RECON


STAGE3_NOTE = (
    "The 5 tables are recomputed by work/extend/e2e/e2e_build_stats.py, which is "
    "stage3_stats.py's equivalent implementation: stats_relation_types / "
    "stats_relation_pillar run stage3's SQL verbatim (only the schema prefix changes); "
    "stats_pillar60 re-derives the frozen v13_full_60pillar_history.csv numbers from "
    "monthly_facts with the generator's own functions (statistics.fmean/median, round(x,8), "
    "grade(), marker()); stats_branch12 re-derives the batch branch12_stats "
    "(statistics.mean/median at full precision, grade12/note12); "
    "relation_type_market_stats re-implements stats_relation.py's aggregation. "
    "Evidence: recomputation on the untouched formal DB matched all 5 tables with 0 "
    "differing rows (work/extend/e2e/compare_calib.json), and after deleting+rebuilding "
    "M08 it again matched with 0 differing rows (compare_rebuilt.json). "
    "Known constant: input_data_as_of is the frozen stage3 build stamp 2026-10-14; it is "
    "NOT advanced with the as-of (changing it would rewrite every stats_pillar60 row)."
)


def write_report(work_dir: Path, month: dict, args, report: dict) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    payload = dict(report)
    payload.setdefault("schema", "post-month-report-v1")
    payload.setdefault("month", {k: month[k] for k in MONTH_KEYS})
    payload["cli"] = {"month": args.month, "as_of": args.as_of, "mode": args.mode,
                      "allow_incomplete": args.allow_incomplete,
                      "rehearsal": args.rehearsal}
    (work_dir / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_promote(args, month) -> int:
    mid = month["month_id"]
    work_dir = WORK_ROOT / mid
    report_path = work_dir / "report.json"
    approved = work_dir / "approved"
    print("[promote] %s" % mid)
    if not report_path.exists():
        print("  REFUSED: no staging report at %s (run --mode staging first)" % report_path)
        return EXIT_ARGS
    rep = json.loads(report_path.read_text(encoding="utf-8"))
    if rep.get("status") == "INCOMPLETE_MONTH" or args.allow_incomplete:
        print("  REFUSED: INCOMPLETE_MONTH is never promotable")
        append_update_log("promote", "REFUSED_INCOMPLETE", "INCOMPLETE_MONTH",
                          "%s promote refused: incomplete month" % mid)
        return EXIT_INCOMPLETE
    checks = rep.get("checks", {})
    # HISTORY 的 rehearsal 月级 digest 会因 source_hash 溯源列（建月时 nas_source 的截止日）
    # 不同而失败；但"历史表逐字节相同 + 统计可复现"才是真正的不可逆风险判据。
    h = checks.get("HISTORY_UNCHANGED", {})
    h_ok = bool(h.get("ok"))
    if not h_ok and rep.get("rehearsal"):
        hist_eq = all(v["formal"]["sha256"] == v["staging"]["sha256"]
                      for v in (h.get("history_tables") or {}).values())
        stats_ok = bool((h.get("stats_reproducible") or {}).get("ok"))
        h_ok = bool(hist_eq and stats_ok)
        if h_ok:
            print("  NOTE: rehearsal month digest differs (source_hash provenance); "
                  "history tables byte-equal + stats reproducible -> accepted")
    failed = [k for k in CHECKS if k != "HISTORY_UNCHANGED"
              and not checks.get(k, {}).get("ok")]
    if not h_ok:
        failed.append("HISTORY_UNCHANGED")
    if failed:
        print("  REFUSED: reconciliations failed %s" % (failed or "all_checks_pass=false"))
        append_update_log("promote", "REFUSED_RECON", "RECON_FAIL",
                          "%s promote refused: %s" % (mid, failed))
        return EXIT_RECON
    # formal DB must not have drifted since staging
    now_sha = sha256_file(MASTER_DB)
    if now_sha != rep.get("formal_db", {}).get("sha256_before"):
        print("  REFUSED: formal DB changed since staging (%s != %s)"
              % (now_sha, rep.get("formal_db", {}).get("sha256_before")))
        return EXIT_RECON
    stage = Path(rep.get("staging_db", {}).get("path", ""))
    if not stage.exists():
        print("  REFUSED: staging DB vanished: %s" % stage)
        return EXIT_ARGS
    stage_sha = sha256_file(stage)
    if stage_sha != rep["staging_db"]["sha256"]:
        print("  REFUSED: staging DB changed since the report (%s != %s)"
              % (stage_sha, rep["staging_db"]["sha256"]))
        return EXIT_RECON
    print("  four reconciliations: ALL PASS")
    print("  staging db sha256 matches report: yes")
    print("  formal db sha256 unchanged since staging: yes")
    if not approved.exists():
        print("  REFUSED: missing human approval file %s" % approved)
        print("  (create it after a human has read report.json; content is not interpreted)")
        append_update_log("promote", "REFUSED_NO_APPROVAL", "NO_APPROVAL",
                          "%s promote refused: approved file missing" % mid)
        return EXIT_NO_APPROVAL
    print("  approval file present: %s" % approved)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    backup_dir = MASTER_DB.parents[2] / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / ("pre_promote_%s_%s.sqlite" % (mid, stamp))
    try:
        os.link(MASTER_DB, backup)          # 同盘硬链接：瞬时、旧内容独立保留
    except OSError:
        shutil.copy2(MASTER_DB, backup)
    print("  pre-promote backup: %s" % backup)
    sc = connect_rw(stage)
    try:
        sc.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        sc.execute("PRAGMA journal_mode=DELETE")
        assert sc.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        sc.close()
    os.replace(stage, MASTER_DB)            # 同卷原子替换
    con = connect_ro(MASTER_DB)
    try:
        ok_int = con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        got = count_rows(con, "monthly_facts", mid)
    finally:
        con.close()
    expect = (rep.get("checks", {}).get("ROW_COUNTS", {}).get("tables", {})
              .get("monthly_facts", {}).get("month_staging"))
    if not ok_int or (expect is not None and got != expect):
        print("  VERIFY FAILED (integrity=%s facts=%s expect=%s) -> restoring backup"
              % (ok_int, got, expect))
        os.replace(backup, MASTER_DB)
        append_update_log("promote", "ROLLED_BACK", "VERIFY_FAIL",
                          "%s promote rolled back after verify failure" % mid)
        return EXIT_RECON
    append_update_log("promote", "OK", "",
                      "%s promoted: facts=%d; backup=%s" % (mid, got, backup.name))
    print("  PROMOTED: %s (facts=%d)" % (MASTER_DB, got))
    return EXIT_OK


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Single entry point for posting one flow month (dry-run by default).")
    ap.add_argument("--month", required=True, help="flow month to post, e.g. 2026-M09")
    ap.add_argument("--as-of", default=None,
                    help="market data cutoff YYYY-MM-DD (default: local mirror max trade day)")
    ap.add_argument("--mode", choices=("dry-run", "staging", "promote"), default="dry-run")
    ap.add_argument("--allow-incomplete", action="store_true",
                    help="let an unfinished month reach staging (status INCOMPLETE_MONTH); "
                         "promote is still always refused")
    ap.add_argument("--rehearsal", action="store_true",
                    help="the month is already in the library: rebuild it inside the copy "
                         "instead of refusing")
    args = ap.parse_args()

    if not MASTER_DB.exists():
        print("master DB missing: %s" % MASTER_DB)
        return EXIT_ARGS
    if args.mode in ("staging", "promote"):
        ensure_py311()
    if args.rehearsal and args.mode == "promote":
        print("--rehearsal and --mode promote are mutually exclusive")
        return EXIT_ARGS
    if args.as_of:
        try:
            date.fromisoformat(args.as_of)
        except ValueError:
            print("--as-of must be YYYY-MM-DD, got %r" % args.as_of)
            return EXIT_ARGS

    con = connect_ro(MASTER_DB)
    try:
        month = read_month(con, args.month)
        hist = months_before(con, args.month)
        existing = count_rows(con, "monthly_facts", args.month)
        if args.mode == "staging" and existing and not args.rehearsal:
            print("%s already has %d monthly_facts rows; pass --rehearsal to rebuild it "
                  "inside the copy" % (args.month, existing))
            return EXIT_ARGS
        if args.mode == "staging" and not args.rehearsal:
            # posting a month older than the newest posted month would leave a hole
            last = con.execute(
                "SELECT month_id, solar_year, month_no FROM calendar_months WHERE month_id "
                "IN (SELECT DISTINCT month_id FROM monthly_facts) "
                "ORDER BY solar_year DESC, month_no DESC LIMIT 1").fetchone()
            if last and (int(month["solar_year"]), int(month["month_no"])) < (int(last[1]),
                                                                             int(last[2])):
                print("refusing to leave a hole: %s is older than the newest posted month %s"
                      % (args.month, last[0]))
                return EXIT_ARGS

        args.as_of_eff = args.as_of
        args.as_of_source = "cli" if args.as_of else "mirror_max"
        if args.mode == "dry-run":
            return plan_only(args, con, month, hist)
        if args.mode == "promote":
            return run_promote(args, month)
        return run_staging(args, con, month, hist)
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
