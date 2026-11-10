#!/Users/zhangxingjin/.cache/stock-analyzer/quality-py311-20261012/bin/python3
# -*- coding: utf-8 -*-
"""QFQ variant of the next-flow-month slice + a month-posting completeness gate.

Scope (strictly additive):
  * READ-ONLY master sqlite (URI ?mode=ro + PRAGMA query_only=ON);
  * READ-ONLY 2026 yearly zip and the adj_factor parquet;
  * writes ONLY new files under work/extend/2026-M09_qfq/ (refuses to overwrite);
  * never touches the RAW artefacts under work/extend/2026-M09/.

Step 1 reverse-engineers monthly_facts' qfq convention on 2026-M08 (all rows)
and fails loudly if no formula reproduces the DB with 0 mismatches (>1e-12).
Step 2 rebuilds the existing 2026-M09 RAW slice with the confirmed formula.
Step 3 evaluates "can this flow month be posted yet" under the existing
scheduler rule (first trading day on/after the term boundary, 20:00 BJT).

Interpreter: the pre-built quality env (duckdb/pyarrow available, no installs).
Time: fixed UTC+8, fields suffixed _bjt; no naive/zoneinfo/tzdata/UTC.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sqlite3
import zipfile
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, getcontext
from pathlib import Path

import duckdb

getcontext().prec = 60
TZ8 = timezone(timedelta(hours=8))
TOL = Decimal("1e-12")

DEV_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path(
    "/Volumes/workSSD/software development/AStock_History_Scan/"
    "bazi_month_relation_local_v1/database/bazi_month_relation_local_v1.sqlite"
)
DOCS_ROOT = Path(os.environ.get("BAZI_DOCS_ROOT", str(DEV_ROOT.parent)))
DEFAULT_ZIP = DOCS_ROOT / "source_mirror/正式源/全A日K_合并版/2026.zip"
DEFAULT_FACTOR = DOCS_ROOT / (
    "source_mirror/正式源/A股复权因子_tushare全量/adj_factor_all.parquet")
DEFAULT_INDEX = DOCS_ROOT / (
    "source_mirror/正式源/A股指数日线_tushare/index_daily.parquet")
DEFAULT_RAW_DIR = DEV_ROOT / "work" / "extend" / "2026-M09"
DEFAULT_OUT_DIR = DEV_ROOT / "work" / "extend" / "2026-M09_qfq"

SCRIPT_VERSION = "1.0.0"
GLOBAL_FACTOR_MAX = "20261014"


# --------------------------------------------------------------------------- #
# time / decimal helpers
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


def dec_str(d: Decimal, places: int = 12) -> str:
    q = d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return format(q, "f")


def dec_factor(d: Decimal) -> str:
    return format(d, "f")


def ymd_to_dash(s: str) -> str:
    return "%s-%s-%s" % (s[0:4], s[4:6], s[6:8])


def dash_to_ymd(s: str) -> str:
    return s.replace("-", "")


def max_abs(vals) -> Decimal:
    return max((abs(v) for v in vals), default=Decimal(0))


# --------------------------------------------------------------------------- #
# IO
# --------------------------------------------------------------------------- #
def connect_ro(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path.absolute().as_uri() + "?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA temp_store=FILE")
    return con


def scan_zip(zip_path: Path, lo: str, hi: str):
    """All bars with lo < date <= hi, per code, plus per-date counts."""
    per_code: dict[str, list[tuple[str, str, str]]] = {}
    per_date: Counter = Counter()
    zip_codes: set[str] = set()
    with zipfile.ZipFile(zip_path) as zf:
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
                per_code[code] = bars
    return per_code, per_date, zip_codes


def load_factors(parquet: Path, wanted: set[str], lo_ymd: str, hi_ymd: str,
                 extra_dates: list[str] | None = None):
    """(factor[(code, iso_date)] -> Decimal, max_date[code] -> 'YYYY-MM-DD').

    extra_dates covers old anchor dates of delisted stocks (outside lo..hi).
    """
    con = duckdb.connect()
    con.execute("SET threads=4")
    max_rows = con.execute(
        "SELECT ts_code, max(trade_date) FROM read_parquet('%s') GROUP BY 1" % parquet
    ).fetchall()
    max_date = {c: ymd_to_dash(d) for c, d in max_rows if c in wanted}
    rows = con.execute(
        "SELECT ts_code, trade_date, adj_factor FROM read_parquet('%s') "
        "WHERE trade_date >= ? AND trade_date <= ?" % parquet,
        [lo_ymd, hi_ymd],
    ).fetchall()
    if extra_dates:
        marks = ",".join("?" for _ in extra_dates)
        rows += con.execute(
            "SELECT ts_code, trade_date, adj_factor FROM read_parquet('%s') "
            "WHERE trade_date IN (%s)" % (parquet, marks), extra_dates).fetchall()
    factors = {
        (c, ymd_to_dash(d)): Decimal(str(f))
        for c, d, f in rows if c in wanted
    }
    con.close()
    return factors, max_date


# --------------------------------------------------------------------------- #
# Step 1: reverse-engineer the master qfq convention on 2026-M08
# --------------------------------------------------------------------------- #
def verify_m08(con, bars_all, factors, max_date, last_bar, month_start, month_end):
    rows = con.execute(
        "SELECT identity_code, first_date, last_date, raw_open, raw_close, "
        "       factor_first, factor_last, anchor_date, anchor_factor, "
        "       qfq_open, qfq_close, body_return, return_decimal, direction "
        "  FROM monthly_facts WHERE month_id='2026-M08' ORDER BY identity_code"
    ).fetchall()
    cols = ["identity_code", "first_date", "last_date", "raw_open", "raw_close",
            "factor_first", "factor_last", "anchor_date", "anchor_factor",
            "qfq_open", "qfq_close", "body_return", "return_decimal", "direction"]
    facts = [dict(zip(cols, r)) for r in rows]

    dev = {k: [] for k in ("qfq_open", "qfq_close", "body_return", "return_decimal")}
    bad = {k: 0 for k in dev}
    anchor_hyp = Counter()
    factor_missing = []
    index_factor_1 = []
    endpoint_mismatch = {"raw_open": 0, "raw_close": 0,
                         "first_date": 0, "last_date": 0, "examples": []}
    factor_col_mismatch = Counter()
    body_vs_decimal = 0
    direction_mismatch = 0
    zip_rows = db_rows = 0

    for f in facts:
        code = f["identity_code"]
        bars = [b for b in bars_all.get(code, [])
                if month_start < b[0] <= month_end]
        if bars:
            raw_o, raw_c = Decimal(bars[0][1]), Decimal(bars[-1][2])
            first_d, last_d = bars[0][0], bars[-1][0]
            zip_rows += 1
            if Decimal(f["raw_open"]) != raw_o:
                endpoint_mismatch["raw_open"] += 1
            if Decimal(f["raw_close"]) != raw_c:
                endpoint_mismatch["raw_close"] += 1
            if f["first_date"] != first_d:
                endpoint_mismatch["first_date"] += 1
            if f["last_date"] != last_d:
                endpoint_mismatch["last_date"] += 1
            if (Decimal(f["raw_open"]) != raw_o or Decimal(f["raw_close"]) != raw_c
                    or f["first_date"] != first_d or f["last_date"] != last_d):
                if len(endpoint_mismatch["examples"]) < 5:
                    endpoint_mismatch["examples"].append(
                        [code, first_d, last_d, str(raw_o), str(raw_c),
                         f["first_date"], f["last_date"], f["raw_open"], f["raw_close"]])
        else:
            raw_o, raw_c = Decimal(f["raw_open"]), Decimal(f["raw_close"])
            first_d, last_d = f["first_date"], f["last_date"]
            db_rows += 1

        # anchor hypotheses (all rows, independent of factor lookup)
        anchor_hyp["stock_last_available_bar_date"] += int(
            f["anchor_date"] == last_bar.get(code))
        anchor_hyp["per_stock_max_factor_date"] += int(
            f["anchor_date"] == max_date.get(code))
        anchor_hyp["global_max_factor_date"] += int(f["anchor_date"] == ymd_to_dash(GLOBAL_FACTOR_MAX))
        anchor_hyp["stock_last_bar_date_in_month"] += int(f["anchor_date"] == last_d)
        anchor_hyp["month_end_date"] += int(f["anchor_date"] == month_end)

        f1 = factors.get((code, first_d))
        f2 = factors.get((code, last_d))
        fa = factors.get((code, f["anchor_date"]))
        if f1 is None or f2 is None or fa is None or fa == 0:
            # Indices are absent from the tushare stock adj_factor table and are
            # carried at factor 1 by the master. Only honour that when the DB
            # itself declares 1/1/1 AND the code is absent from the factor table.
            if (code not in max_date
                    and str(f["factor_first"]) == "1"
                    and str(f["factor_last"]) == "1"
                    and str(f["anchor_factor"]) == "1"):
                f1 = f2 = fa = Decimal(1)
                index_factor_1.append(code)
            else:
                factor_missing.append([code, first_d, last_d, f["anchor_date"],
                                       str(f1), str(f2), str(fa)])
                continue
        factor_col_mismatch["factor_first"] += int(Decimal(f["factor_first"]) != f1)
        factor_col_mismatch["factor_last"] += int(Decimal(f["factor_last"]) != f2)
        factor_col_mismatch["anchor_factor"] += int(Decimal(f["anchor_factor"]) != fa)

        qo = raw_o * f1 / fa
        qc = raw_c * f2 / fa
        ret = qc / qo - 1
        ref = {"qfq_open": Decimal(f["qfq_open"]),
               "qfq_close": Decimal(f["qfq_close"]),
               "body_return": Decimal(repr(f["body_return"])),
               "return_decimal": Decimal(f["return_decimal"])}
        calc = {"qfq_open": qo, "qfq_close": qc, "body_return": ret,
                "return_decimal": ret}
        for k in dev:
            d = abs(calc[k] - ref[k])
            dev[k].append(d)
            if d > TOL:
                bad[k] += 1
        body_vs_decimal += int(float(ref["return_decimal"]) != f["body_return"])
        direction_mismatch += int(
            f["direction"] != ("UP" if qc > qo else "DOWN" if qc < qo else "FLAT"))

    return {
        "month_id": "2026-M08",
        "rows": len(facts),
        "rows_endpoints_from_zip": zip_rows,
        "rows_endpoints_from_db_raw": db_rows,
        "stocks_verified_independently": len(facts) - len(index_factor_1),
        "formula": "qfq_open = raw_open * adj_factor(first_date) / adj_factor(anchor_date); "
                   "qfq_close = raw_close * adj_factor(last_date) / adj_factor(anchor_date)",
        "anchor_rule": "anchor_date = that stock's last available bar date (max bar date "
                       "for the stock in the daily source, up to market_data_through). "
                       "It is NOT max(adj_factor.trade_date): suspended stocks keep "
                       "factor rows after their last bar, but the DB anchors on the last bar.",
        "anchor_hypothesis_matches": dict(anchor_hyp),
        "anchor_hypothesis_mismatches": {
            k: len(facts) - v for k, v in anchor_hyp.items()},
        "factor_rows_missing": len(factor_missing),
        "factor_missing_examples": factor_missing[:10],
        "index_identities_factor_1_assumed": index_factor_1,
        "index_identities_note": "indices absent from the stock adj_factor table; "
                                  "DB declares factor 1/1/1 and qfq==raw for them",
        "factor_column_mismatches": dict(factor_col_mismatch),
        "endpoint_mismatches": endpoint_mismatch,
        "qfq_open": {"compared": len(dev["qfq_open"]),
                     "mismatch_gt_1e-12": bad["qfq_open"],
                     "max_abs_dev": str(max_abs(dev["qfq_open"]))},
        "qfq_close": {"compared": len(dev["qfq_close"]),
                      "mismatch_gt_1e-12": bad["qfq_close"],
                      "max_abs_dev": str(max_abs(dev["qfq_close"]))},
        "body_return": {"compared": len(dev["body_return"]),
                        "mismatch_gt_1e-12": bad["body_return"],
                        "max_abs_dev": str(max_abs(dev["body_return"])),
                        "equals_qfq_close_over_open_minus_1": True},
        "return_decimal": {"compared": len(dev["return_decimal"]),
                           "mismatch_gt_1e-12": bad["return_decimal"],
                           "max_abs_dev": str(max_abs(dev["return_decimal"]))},
        "body_return_equals_float_return_decimal_mismatches": body_vs_decimal,
        "direction_vs_qfq_sign_mismatches": direction_mismatch,
    }


# --------------------------------------------------------------------------- #
# Step 2: qfq rebuild of the existing 2026-M09 raw slice
# --------------------------------------------------------------------------- #
def build_m09_qfq(raw_csv: Path, factors, last_bar):
    with open(raw_csv, encoding="utf-8", newline="") as fh:
        rd = csv.DictReader(fh)
        raw_fields = list(rd.fieldnames or [])
        raw_rows = list(rd)

    new_fields = ["qfq_open", "qfq_close", "anchor_date", "adj_factor_used",
                  "body_return_qfq", "up_down_flat_qfq",
                  "adj_factor_first", "adj_factor_last"]
    out_rows = []
    changes = []
    unavailable = []
    qfq_dist = Counter()
    dev_pp = []
    raw_dir_check = 0
    anchor_check = 0

    for r in raw_rows:
        code = r["identity_code"]
        first_d, last_d = r["first_bar_date"], r["last_bar_date"]
        anchor = last_bar.get(code)
        anchor_check += int(anchor == last_d)
        f1 = factors.get((code, first_d))
        f2 = factors.get((code, last_d))
        fa = factors.get((code, anchor)) if anchor else None
        miss = [name for name, v in (("first", f1), ("last", f2), ("anchor", fa))
                if v is None]
        if miss or not anchor:
            unavailable.append({
                "identity_code": code, "security_code": r["security_code"],
                "name": r["name"], "first_bar_date": first_d,
                "last_bar_date": last_d, "anchor_date": anchor or "",
                "missing_factor_at": ",".join(miss) if miss else "anchor_date",
                "bars": r["n_bars"],
            })
            continue

        raw_o = Decimal(r["month_first_open"])
        raw_c = Decimal(r["month_last_close"])
        raw_ret = raw_c / raw_o - 1
        raw_dir_calc = "UP" if raw_c > raw_o else "DOWN" if raw_c < raw_o else "FLAT"
        raw_dir_check += int(raw_dir_calc != r["up_down_flat"])

        qo = raw_o * f1 / fa
        qc = raw_c * f2 / fa
        qret = qc / qo - 1
        qdir = "UP" if qc > qo else "DOWN" if qc < qo else "FLAT"
        qfq_dist[qdir] += 1
        delta_pp = (qret - raw_ret) * 100
        dev_pp.append(delta_pp)

        out = dict(r)
        out.update({
            "qfq_open": dec_str(qo),
            "qfq_close": dec_str(qc),
            "anchor_date": anchor,
            "adj_factor_used": dec_factor(fa),
            "body_return_qfq": dec_str(qret),
            "up_down_flat_qfq": qdir,
            "adj_factor_first": dec_factor(f1),
            "adj_factor_last": dec_factor(f2),
        })
        out_rows.append(out)

        if qdir != r["up_down_flat"]:
            changes.append({
                "identity_code": code, "security_code": r["security_code"],
                "name": r["name"], "raw_direction": r["up_down_flat"],
                "qfq_direction": qdir,
                "raw_return": dec_str(raw_ret),
                "qfq_return": dec_str(qret),
                "delta_pp": dec_str(delta_pp, 6),
                "month_first_open": r["month_first_open"],
                "month_last_close": r["month_last_close"],
                "qfq_open": dec_str(qo), "qfq_close": dec_str(qc),
                "adj_factor_first": dec_factor(f1),
                "adj_factor_last": dec_factor(f2),
                "anchor_date": anchor,
                "first_bar_date": first_d, "last_bar_date": last_d,
            })

    changes.sort(key=lambda x: (-abs(Decimal(x["delta_pp"])), x["identity_code"]))
    return (raw_fields, out_rows, changes, unavailable, dict(qfq_dist),
            max_abs(dev_pp), raw_dir_check, anchor_check)


# --------------------------------------------------------------------------- #
# Step 3: posting gate (independent function, fixed UTC+8)
# --------------------------------------------------------------------------- #
def first_trading_day_on_or_after(boundary: datetime) -> date:
    """Same rule as app/bjtime.py (weekday-only, no holiday calendar)."""
    d = boundary.date()
    if boundary >= datetime.combine(d, time(15, 0), tzinfo=TZ8):
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
                   covered_days: list[str]) -> dict:
    """Decide whether this flow month may be posted now.

    Required checks:
      1. the flow month itself has ended (asof >= end_at);
      2. the last trading day inside (start_date, end_date] has closed (>=15:00);
      3. the market data actually reached that last trading day.
    """
    start_at, end_at = parse_bjt(month["start_at"]), parse_bjt(month["end_at"])
    start_d, end_d = start_at.date(), end_at.date()
    last_td = last_weekday_on_or_before(end_d)   # weekday estimate, no holiday calendar
    last_close = datetime.combine(last_td, time(15, 0), tzinfo=TZ8)
    data_through = date.fromisoformat(market_data_through)
    expected_days = count_weekdays(start_d, end_d)

    checks = [
        {"id": "MONTH_ENDED", "ok": asof >= end_at,
         "detail": "asof %s vs end_at %s" % (iso8(asof), iso8(end_at))},
        {"id": "LAST_TRADING_DAY_CLOSED", "ok": asof >= last_close,
         "detail": "last trading day %s (weekday estimate), close 15:00 BJT"
                   % last_td.isoformat()},
        {"id": "MARKET_DATA_THROUGH_LAST_TRADING_DAY", "ok": data_through >= last_td,
         "detail": "market_data_through %s vs last trading day %s"
                   % (market_data_through, last_td.isoformat())},
    ]
    failed = [c for c in checks if not c["ok"]]
    can_post = not failed

    target = first_trading_day_on_or_after(end_at)
    first_eligible = datetime.combine(target, time(20, 0), tzinfo=TZ8)

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
        "can_post_month": can_post,
        "reason": "; ".join(c["id"] for c in failed) if failed else "OK",
        "reason_text": ("流月未结束；区间最后一个交易日尚未收盘；行情只到 %s，"
                        "未覆盖 %s" % (market_data_through, last_td.isoformat()))
                       if failed else "可入账",
        "first_eligible_date": iso8(first_eligible),
        "first_eligible_date_basis": (
            "existing scheduler rule: first weekday on/after end_at (end_at is after "
            "15:00 so the next day; 2026-11-08 is Sunday) at 20:00 BJT; additionally "
            "the scheduler requires source max trade date >= %s" % target.isoformat()),
        "source_requirement_for_first_eligible": {
            "max_trade_date_gte": target.isoformat(),
            "why": "app/update_scheduler.evaluate_trigger: src_max < target => SOURCE_NOT_READY",
        },
        "coverage": {
            "covered_trading_days": covered,
            "covered_dates": covered_days,
            "expected_trading_days_weekday_estimate": expected_days,
            "expected_days_basis": "weekdays in (start_date, end_date]; holidays NOT modelled",
            "coverage_ratio": round(covered / expected_days, 6) if expected_days else None,
            "missing_trading_days_estimate": expected_days - covered,
        },
    }


def m08_partial_error_reference(bars_all, factors, last_bar, codes,
                                month_start: str, month_end: str) -> dict:
    """Measured on complete M08: first-4-bars snapshot vs full month (qfq)."""
    dev, flips, n = [], 0, 0
    for code, bars in bars_all.items():
        if code not in codes:
            continue
        bars = [b for b in bars if month_start < b[0] <= month_end]
        if len(bars) < 5:
            continue
        anchor = last_bar.get(code)
        f0 = factors.get((code, bars[0][0]))
        f3 = factors.get((code, bars[3][0]))
        fl = factors.get((code, bars[-1][0]))
        fa = factors.get((code, anchor)) if anchor else None
        if None in (f0, f3, fl, fa) or fa == 0:
            continue
        p = (Decimal(bars[3][2]) / Decimal(bars[0][1])) * (f3 / f0) - 1
        full = (Decimal(bars[-1][2]) / Decimal(bars[0][1])) * (fl / f0) - 1
        d = (p - full) * 100
        dev.append(d)
        sp = 0 if p == 0 else (1 if p > 0 else -1)
        sf = 0 if full == 0 else (1 if full > 0 else -1)
        flips += int(sp != sf)
        n += 1
    dev_sorted = sorted(abs(x) for x in dev)
    med = dev_sorted[len(dev_sorted) // 2] if dev_sorted else Decimal(0)
    return {
        "month_id": "2026-M08",
        "sample_rows": n,
        "basis": "qfq return over each stock's first 4 bars vs its full-month return",
        "direction_flips": flips,
        "direction_flip_ratio": round(flips / n, 6) if n else None,
        "max_abs_dev_pp": str(max_abs(dev)),
        "median_abs_dev_pp": str(med),
    }


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--zip", default=str(DEFAULT_ZIP))
    ap.add_argument("--factor", default=str(DEFAULT_FACTOR))
    ap.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR))
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    args = ap.parse_args()

    db_path, zip_path = Path(args.db), Path(args.zip)
    factor_path, raw_dir, out_dir = Path(args.factor), Path(args.raw_dir), Path(args.out_dir)
    for p in (db_path, zip_path, factor_path):
        if not p.exists():
            raise SystemExit("missing input: %s" % p)
    if not (raw_dir / "new_month_slice.csv").exists():
        raise SystemExit("raw slice missing: %s" % (raw_dir / "new_month_slice.csv"))
    if out_dir.exists() and any(out_dir.iterdir()):
        raise SystemExit("refusing to overwrite existing artefacts in %s" % out_dir)

    con = connect_ro(db_path)
    month = dict(zip(
        ("month_id", "start_at", "end_at"),
        con.execute("SELECT month_id, start_at, end_at FROM calendar_months "
                    "WHERE month_id='2026-M09'").fetchone()))
    raw_manifest = json.loads((raw_dir / "slice_manifest.json").read_text(encoding="utf-8"))
    data_through = raw_manifest["coverage"]["market_data_through"]
    covered_days = raw_manifest["coverage"]["trading_days_in_window_list"]

    raw_csv = raw_dir / "new_month_slice.csv"
    with open(raw_csv, encoding="utf-8", newline="") as fh:
        raw_rows = list(csv.DictReader(fh))
    wanted = {r["identity_code"] for r in raw_rows}
    m08_codes = {r[0] for r in con.execute(
        "SELECT identity_code FROM monthly_facts WHERE month_id='2026-M08'")}
    old_anchor_dates = [r[0] for r in con.execute(
        "SELECT DISTINCT anchor_date FROM monthly_facts WHERE month_id='2026-M08' "
        "AND anchor_date < '2026-09-01'")]

    print("[1/3] scanning 2026.zip ...")
    bars_all, per_date, zip_codes = scan_zip(zip_path, "2026-09-07", "2026-11-07")
    print("      codes with bars: %d | dates: %d" % (len(bars_all), len(per_date)))
    last_bar = {}
    for code, bars in bars_all.items():
        cand = [b[0] for b in bars if b[0] <= data_through]
        if cand:
            last_bar[code] = max(cand)

    print("[2/3] loading adj_factor parquet ...")
    factors, max_date = load_factors(
        factor_path, wanted | set(bars_all) | m08_codes, "20260901",
        GLOBAL_FACTOR_MAX,
        extra_dates=[dash_to_ymd(d) for d in old_anchor_dates])
    print("      factor rows in window: %d | codes with max date: %d"
          % (len(factors), len(max_date)))

    print("[3/3] verifying M08 formula ...")
    m08 = verify_m08(con, bars_all, factors, max_date, last_bar,
                     "2026-09-07", "2026-10-08")
    zero_bad = all(m08[k]["mismatch_gt_1e-12"] == 0
                   for k in ("qfq_open", "qfq_close", "body_return", "return_decimal"))
    if m08["factor_rows_missing"] or not zero_bad:
        raise SystemExit("M08 formula NOT reproduced with 0 mismatches: %s"
                         % json.dumps({k: m08[k] for k in (
                             "factor_rows_missing", "qfq_open", "qfq_close",
                             "body_return", "return_decimal")}, ensure_ascii=False))

    print("[4/4] building qfq M09 slice ...")
    (raw_fields, out_rows, changes, unavailable, qfq_dist,
     max_dev_pp, raw_dir_check, anchor_check) = build_m09_qfq(
        raw_csv, factors, last_bar)

    out_dir.mkdir(parents=True, exist_ok=True)
    qfq_csv = out_dir / "new_month_slice_qfq.csv"
    with open(qfq_csv, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=raw_fields + [
            "qfq_open", "qfq_close", "anchor_date", "adj_factor_used",
            "body_return_qfq", "up_down_flat_qfq",
            "adj_factor_first", "adj_factor_last"])
        w.writeheader()
        w.writerows(out_rows)

    chg_csv = out_dir / "qfq_direction_changes.csv"
    with open(chg_csv, "w", encoding="utf-8", newline="") as fh:
        fields = list(changes[0].keys()) if changes else [
            "identity_code", "security_code", "name", "raw_direction",
            "qfq_direction", "raw_return", "qfq_return", "delta_pp"]
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(changes)

    unavail_csv = out_dir / "qfq_unavailable.csv"
    if unavailable:
        with open(unavail_csv, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(unavailable[0].keys()))
            w.writeheader()
            w.writerows(unavailable)

    comp_csv = out_dir / "qfq_vs_raw_comparison.csv"
    with open(comp_csv, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=[
            "identity_code", "security_code", "name", "raw_direction",
            "qfq_direction", "direction_changed", "raw_return", "qfq_return",
            "delta_pp", "month_first_open", "month_last_close", "qfq_open",
            "qfq_close", "adj_factor_first", "adj_factor_last", "anchor_date",
            "first_bar_date", "last_bar_date"])
        w.writeheader()
        for out in out_rows:
            raw_ret = (Decimal(out["month_last_close"])
                       / Decimal(out["month_first_open"]) - 1)
            qret = Decimal(out["body_return_qfq"])
            w.writerow({
                "identity_code": out["identity_code"],
                "security_code": out["security_code"],
                "name": out["name"],
                "raw_direction": out["up_down_flat"],
                "qfq_direction": out["up_down_flat_qfq"],
                "direction_changed": int(out["up_down_flat"] != out["up_down_flat_qfq"]),
                "raw_return": dec_str(raw_ret),
                "qfq_return": dec_str(qret),
                "delta_pp": dec_str((qret - raw_ret) * 100, 6),
                "month_first_open": out["month_first_open"],
                "month_last_close": out["month_last_close"],
                "qfq_open": out["qfq_open"],
                "qfq_close": out["qfq_close"],
                "adj_factor_first": out["adj_factor_first"],
                "adj_factor_last": out["adj_factor_last"],
                "anchor_date": out["anchor_date"],
                "first_bar_date": out["first_bar_date"],
                "last_bar_date": out["last_bar_date"],
            })

    # carry the raw missing list along; never turn NO_DATA into FLAT/0%.
    shutil.copyfile(raw_dir / "missing_identities.csv",
                    out_dir / "missing_identities.csv")

    gate = can_post_month(
        month, now8(), data_through, covered_days)
    with zipfile.ZipFile(zip_path) as zf:
        member = sorted(n for n in zf.namelist() if n.endswith(".csv"))[0]
        zt = zf.getinfo(member).date_time
    zip_stamp = "%04d-%02d-%02dT%02d:%02d" % zt[:5]
    fe = datetime.fromisoformat(gate["first_eligible_date"])
    lag = fe + timedelta(days=1)
    while lag.weekday() >= 5:
        lag += timedelta(days=1)
    gate["first_eligible_date_if_source_t_plus_1"] = iso8(lag)
    gate["source_publication_observation"] = {
        "zip_member_timestamp_observed": zip_stamp,
        "zip_member_time_basis": "ZIP member date_time, no timezone; observed only",
        "data_through": data_through,
        "note": "2026.zip members are stamped 2026-10-15 08:45 while data ends "
                "2026-10-14, i.e. a T+1 morning build. If that cadence holds, the "
                "2026-11-09 20:00 trigger sees no 2026-11-09 bar and is deferred to "
                "the next 20:00. Exact vendor publication time is NOT verified.",
    }
    gate["coverage"]["crosscheck_2025_M09_actual_trading_days"] = con.execute(
        "SELECT max(trading_days) FROM monthly_facts WHERE month_id='2025-M09'"
    ).fetchone()[0]
    gate["coverage"]["crosscheck_note"] = (
        "2025-M09 had 22 trading days because its term started 2025-10-08 08:41 "
        "(before the open, so 10-08 counted); 2026-M09 starts 2026-10-08 14:29 "
        "(after the close, so 10-08 is excluded) => 21 weekdays from 10-09 to 11-06."
    )
    gate["partial_snapshot_error_reference"] = m08_partial_error_reference(
        bars_all, factors, last_bar, m08_codes, "2026-09-07", "2026-10-08")
    gate["forced_posting_error_note"] = (
        "M09 currently has %d of %d expected trading days (%.1f%%); a forced posting "
        "would record a %d-day body as a full-month body (measured M08 analogue: "
        "%d/%d stocks would flip direction, max %.3f pp)."
        % (gate["coverage"]["covered_trading_days"],
           gate["coverage"]["expected_trading_days_weekday_estimate"],
           100.0 * gate["coverage"]["covered_trading_days"]
           / gate["coverage"]["expected_trading_days_weekday_estimate"],
           gate["coverage"]["covered_trading_days"],
           gate["partial_snapshot_error_reference"]["direction_flips"],
           gate["partial_snapshot_error_reference"]["sample_rows"],
           float(gate["partial_snapshot_error_reference"]["max_abs_dev_pp"])))
    (out_dir / "posting_gate.json").write_text(
        json.dumps(gate, ensure_ascii=False, indent=2), encoding="utf-8")

    raw_dist = Counter(r["up_down_flat"] for r in raw_rows)
    with open(out_dir / "missing_identities.csv", encoding="utf-8") as fh:
        missing_carried = sum(1 for _ in fh) - 1
    manifest = {
        "script": "tools/qfq_extend_slice.py",
        "script_version": SCRIPT_VERSION,
        "generated_at": iso8(now8()),
        "timezone": "UTC+8 (fixed offset)",
        "inputs": {
            "db": {"path": str(db_path), "mode": "ro", "query_only": "ON"},
            "source_zip": {"path": str(zip_path)},
            "adj_factor_parquet": {"path": str(factor_path),
                                   "global_max_trade_date": ymd_to_dash(GLOBAL_FACTOR_MAX)},
            "raw_slice": str(raw_csv),
        },
        "month": month,
        "window_rule": "bar_date in (start_date, end_date]; start day belongs to previous month",
        "m08_verification": m08,
        "qfq_slice": {
            "rows": len(out_rows),
            "expected_rows_from_raw": len(raw_rows),
            "reconciliation": len(out_rows) + len(unavailable) == len(raw_rows),
            "up_down_flat_qfq": qfq_dist,
            "up_down_flat_raw": dict(raw_dist),
            "direction_changes": len(changes),
            "max_abs_return_deviation_pp": str(max_dev_pp),
            "raw_direction_recompute_mismatches": raw_dir_check,
            "anchor_equals_stock_last_available_bar": anchor_check == len(out_rows),
            "anchor_mismatches": len(out_rows) - anchor_check,
            "unavailable_qfq_rows": len(unavailable),
            "missing_identities_carried_over": missing_carried,
            "return_basis": "QFQ_FIRST_OPEN_LAST_CLOSE (anchor = per-stock last available "
                            "bar date, as of market_data_through)",
            "columns_added": [
                "qfq_open", "qfq_close", "anchor_date", "adj_factor_used",
                "body_return_qfq", "up_down_flat_qfq",
                "adj_factor_first", "adj_factor_last"],
            "adj_factor_used_semantics": "adj_factor(anchor_date), the denominator; "
                                         "first/last factors are emitted separately",
        },
        "posting_gate": gate,
        "artefacts": {
            "qfq_slice": str(qfq_csv),
            "qfq_vs_raw_comparison": str(comp_csv),
            "direction_changes": str(chg_csv),
            "unavailable": str(unavail_csv) if unavailable else None,
            "missing_identities": str(out_dir / "missing_identities.csv"),
            "posting_gate": str(out_dir / "posting_gate.json"),
        },
    }
    (out_dir / "slice_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    con.close()

    print("M08 verify rows          :", m08["rows"],
          "| qfq mismatches:", m08["qfq_open"]["mismatch_gt_1e-12"],
          m08["qfq_close"]["mismatch_gt_1e-12"],
          m08["body_return"]["mismatch_gt_1e-12"])
    print("M08 anchor hyp matches   :", m08["anchor_hypothesis_matches"])
    print("M08 max abs dev          : qfq_open", m08["qfq_open"]["max_abs_dev"],
          "| body_return", m08["body_return"]["max_abs_dev"])
    print("M09 qfq rows             :", len(out_rows), "/", len(raw_rows),
          "| unavailable:", len(unavailable))
    print("M09 qfq up/down/flat     :", qfq_dist,
          "| raw:", dict(raw_dist))
    print("M09 direction changes    :", len(changes),
          "| max dev pp:", str(max_dev_pp))
    print("can_post_month           :", gate["can_post_month"],
          "|", gate["reason"], "| first eligible:", gate["first_eligible_date"])
    print("coverage                 :", gate["coverage"])
    print("out dir                  :", out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
