#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build the "next flow-month" market slice from the read-only 2026 yearly zip.

Scope (deliberately narrow):
  * READ-ONLY access to the master sqlite (URI ?mode=ro + PRAGMA query_only=ON).
  * READ-ONLY access to source_mirror/正式源/全A日K_合并版/2026.zip.
  * Writes ONLY new files under work/extend/<month_id>/ (refuses to overwrite).

Month window convention is taken from the existing project (verified against
monthly_facts for 2026-M05..M08): a daily bar dated D belongs to the flow month
iff  start_date < D <= end_date, where start_date/end_date are the *date* parts
of calendar_months.start_at / end_at (both UTC+8). i.e. the bar on the solar-term
day itself belongs to the PREVIOUS month, the bar on the closing day belongs to
THIS month. monthly_facts.first_date/last_date reproduce exactly under this rule.

Return basis:
  body_return = month_last_close / month_first_open - 1  on RAW (unadjusted)
  prices, exactly as requested. NOTE: the master DB's monthly_facts.body_return
  is computed on qfq (forward-adjusted) prices, so for stocks with an
  ex-dividend/split inside the window the two differ. This script records
  return_basis=RAW_FIRST_OPEN_LAST_CLOSE and keeps prices raw.

No pandas / duckdb / pyarrow: stdlib only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import sqlite3
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, getcontext
from pathlib import Path

getcontext().prec = 34

TZ8 = timezone(timedelta(hours=8))

DEV_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path(
    "/Volumes/workSSD/software development/AStock_History_Scan/"
    "bazi_month_relation_local_v1/database/bazi_month_relation_local_v1.sqlite"
)
DOCS_ROOT = Path(os.environ.get("BAZI_DOCS_ROOT", str(DEV_ROOT.parent)))
DEFAULT_ZIP = DOCS_ROOT / "source_mirror/正式源/全A日K_合并版/2026.zip"
DEFAULT_OUT_ROOT = DEV_ROOT / "work" / "extend"

SCRIPT_VERSION = "1.0.0"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def now8() -> datetime:
    return datetime.now(TZ8)


def iso8(dt: datetime) -> str:
    return dt.astimezone(TZ8).isoformat(timespec="seconds")


def parse_aware(s: str) -> datetime:
    """Parse an ISO timestamp that must carry an offset; normalise to UTC+8."""
    d = datetime.fromisoformat(s)
    if d.tzinfo is None:
        raise ValueError("naive timestamp rejected: %r" % s)
    return d.astimezone(TZ8)


def connect_ro(db_path: Path) -> sqlite3.Connection:
    uri = db_path.absolute().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA temp_store=FILE")  # never spill into the master DB
    return con


def dec_str(d: Decimal) -> str:
    """Fixed 12-dp decimal string, no exponent, no float noise."""
    q = d.quantize(Decimal("1e-12"), rounding=ROUND_HALF_UP)
    return format(q, "f")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# month resolution
# --------------------------------------------------------------------------- #
def resolve_target_month(con: sqlite3.Connection) -> dict:
    cur = con.cursor()
    last_fact_month = cur.execute("SELECT max(month_id) FROM monthly_facts").fetchone()[0]
    row = cur.execute(
        """
        SELECT month_id, solar_year, month_no, year_pillar, month_pillar, month_branch,
               start_term, start_at, end_at
          FROM calendar_months
         WHERE month_id > ?
         ORDER BY month_id
         LIMIT 1
        """,
        (last_fact_month,),
    ).fetchone()
    if row is None:
        raise SystemExit("no calendar month after %s" % last_fact_month)
    keys = ["month_id", "solar_year", "month_no", "year_pillar", "month_pillar",
            "month_branch", "start_term", "start_at", "end_at"]
    m = dict(zip(keys, row))
    m["previous_month_with_facts"] = last_fact_month
    m["facts_rows_in_target"] = cur.execute(
        "SELECT count(*) FROM monthly_facts WHERE month_id=?", (m["month_id"],)
    ).fetchone()[0]
    m["relation_rows_in_target"] = cur.execute(
        "SELECT count(*) FROM month_relation_types WHERE month_id=?", (m["month_id"],)
    ).fetchone()[0]
    m["_start"] = parse_aware(m["start_at"])
    m["_end"] = parse_aware(m["end_at"])
    m["start_date"] = m["_start"].date().isoformat()
    m["end_date"] = m["_end"].date().isoformat()
    return m


# --------------------------------------------------------------------------- #
# market scan
# --------------------------------------------------------------------------- #
def scan_zip_window(zip_path: Path, lo: str, hi: str, wanted: set | None):
    """Return (per_code, per_date_all_codes, zip_codes, dup_pairs, max_date_in_window).

    per_code[code] = list[(date_str, open_str, close_str)] sorted by date,
    only for codes that have >=1 bar in (lo, hi].  Dates are ISO strings so
    lexicographic comparison is chronological.
    """
    per_code: dict[str, list[tuple[str, str, str]]] = {}
    per_date: Counter = Counter()
    zip_codes: set[str] = set()
    dup_pairs = 0
    max_d: str | None = None

    with zipfile.ZipFile(zip_path) as zf:
        for name in sorted(zf.namelist()):
            if not name.endswith(".csv"):
                continue
            code = name.rsplit("/", 1)[-1][:-4]
            zip_codes.add(code)
            if wanted is not None and code not in wanted:
                continue
            with zf.open(name) as fh:
                rd = csv.reader(io.TextIOWrapper(fh, encoding="utf-8", newline=""))
                header = next(rd)
                if header[:6] != ["code", "datetime", "open", "high", "low", "close"]:
                    raise SystemExit("unexpected header in %s: %r" % (name, header[:6]))
                bars = []
                seen = set()
                for r in rd:
                    d = r[1]
                    if lo < d <= hi:
                        if d in seen:
                            dup_pairs += 1
                        seen.add(d)
                        bars.append((d, r[2], r[5]))
                        per_date[d] += 1
                        if max_d is None or d > max_d:
                            max_d = d
            if bars:
                bars.sort()
                per_code[code] = bars
    return per_code, per_date, zip_codes, dup_pairs, max_d


def aggregate(per_code, month_id, month_pillar, complete, market_days, data_through):
    """Fold raw bars into one row per code. Flat is decided with Decimal, not float."""
    out = {}
    for code, bars in per_code.items():
        first_d, first_o, _ = bars[0]
        last_d, _, last_c = bars[-1]
        fo, lc = Decimal(first_o), Decimal(last_c)
        if lc > fo:
            state = "UP"
        elif lc < fo:
            state = "DOWN"
        else:
            state = "FLAT"
        ret = (lc / fo) - 1 if fo != 0 else Decimal(0)
        out[code] = {
            "month_id": month_id,
            "month_pillar": month_pillar,
            "month_first_open": first_o,
            "month_last_close": last_c,
            "body_return": dec_str(ret),
            "up_down_flat": state,
            "n_bars": len(bars),
            "first_bar_date": first_d,
            "last_bar_date": last_d,
            "complete": complete,
            "market_trading_days_in_window": market_days,
            "market_data_through": data_through,
        }
    return out


# --------------------------------------------------------------------------- #
# self-check against the last fully-loaded month
# --------------------------------------------------------------------------- #
def self_check_previous_month(con, zip_path, prev_month_id, target, wanted, per_date_all):
    """Reproduce the previous month's endpoints from the zip and diff vs monthly_facts."""
    cur = con.cursor()
    row = cur.execute(
        "SELECT month_id, month_pillar, start_at, end_at FROM calendar_months WHERE month_id=?",
        (prev_month_id,),
    ).fetchone()
    if row is None:
        return {"status": "SKIPPED", "reason": "previous month not in calendar"}
    _, pillar, start_at, end_at = row
    lo = parse_aware(start_at).date().isoformat()
    hi = parse_aware(end_at).date().isoformat()
    per_code, per_date, _, dups, _ = scan_zip_window(zip_path, lo, hi, wanted)
    facts = {
        r[0]: r[1:]
        for r in cur.execute(
            "SELECT identity_code, first_date, last_date, raw_open, raw_close, trading_days "
            "FROM monthly_facts WHERE month_id=?",
            (prev_month_id,),
        )
    }
    market_days = len(per_date)
    missing_in_zip = sorted(set(facts) - set(per_code))
    extra_in_zip = sorted(set(per_code) - set(facts))
    bad_open = bad_close = bad_first = bad_last = bad_n = 0
    examples = []
    for code, bars in per_code.items():
        if code not in facts:
            continue
        fd, ld, ro, rc, n = facts[code]
        fo = bars[0][0]
        lc = bars[-1][0]
        if str(fo) != str(fd):
            bad_first += 1
        if str(lc) != str(ld):
            bad_last += 1
        if ro is not None and Decimal(ro) != Decimal(bars[0][1]):
            bad_open += 1
            if len(examples) < 5:
                examples.append([code, "open", bars[0][1], ro])
        if rc is not None and Decimal(rc) != Decimal(bars[-1][2]):
            bad_close += 1
            if len(examples) < 5:
                examples.append([code, "close", bars[-1][2], rc])
        if int(n or 0) != len(bars):
            bad_n += 1
    return {
        "status": "OK",
        "month_id": prev_month_id,
        "window": [lo, hi],
        "market_trading_days_in_window": market_days,
        "zip_codes_with_bars": len(per_code),
        "facts_rows": len(facts),
        "facts_rows_missing_from_zip": len(missing_in_zip),
        "facts_rows_missing_examples": missing_in_zip[:10],
        "zip_codes_not_in_facts": len(extra_in_zip),
        "zip_codes_not_in_facts_examples": extra_in_zip[:10],
        "mismatch_first_bar_date": bad_first,
        "mismatch_last_bar_date": bad_last,
        "mismatch_raw_open": bad_open,
        "mismatch_raw_close": bad_close,
        "mismatch_bar_count": bad_n,
        "mismatch_examples": examples,
        "duplicate_code_date_pairs": dups,
        "note": "body_return deliberately NOT compared: DB uses qfq, slice uses raw.",
    }


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--zip", default=str(DEFAULT_ZIP))
    ap.add_argument("--out-root", default=str(DEFAULT_OUT_ROOT))
    ap.add_argument("--skip-self-check", action="store_true")
    args = ap.parse_args()

    db_path, zip_path = Path(args.db), Path(args.zip)
    if not db_path.exists():
        raise SystemExit("db not found: %s" % db_path)
    if not zip_path.exists():
        raise SystemExit("zip not found: %s" % zip_path)

    con = connect_ro(db_path)
    cur = con.cursor()

    m = resolve_target_month(con)
    month_id = m["month_id"]
    out_dir = Path(args.out_root) / month_id
    if out_dir.exists() and any(out_dir.iterdir()):
        raise SystemExit("refusing to overwrite existing artefacts in %s" % out_dir)

    securities = {
        r[0]: {
            "security_code": r[1], "name": r[2], "exchange": r[3],
            "security_type": r[4], "listed_date": r[5], "delisted_date": r[6],
            "first_month_id": r[7], "last_month_id": r[8],
        }
        for r in cur.execute(
            "SELECT identity_code, security_code, name, exchange, security_type, "
            "listed_date, delisted_date, first_month_id, last_month_id FROM securities"
        )
    }

    lo, hi = m["start_date"], m["end_date"]
    per_code_all, per_date_all, zip_codes, dups, max_d = scan_zip_window(
        zip_path, lo, hi, None
    )

    # slice is keyed on DB identities only (identity_code == security_code here)
    per_code = {c: b for c, b in per_code_all.items() if c in securities}
    market_days = len(per_date_all)
    max_date_all_codes = max_d

    complete = 1 if (max_date_all_codes == m["end_date"] and max_date_all_codes is not None) else 0
    agg = aggregate(per_code, month_id, m["month_pillar"], complete, market_days, max_date_all_codes)

    out_dir.mkdir(parents=True, exist_ok=True)

    slice_path = out_dir / "new_month_slice.csv"
    fields = [
        "identity_code", "month_id", "month_pillar",
        "month_first_open", "month_last_close", "body_return", "up_down_flat",
        "n_bars", "first_bar_date", "last_bar_date", "month_end_date", "complete",
        "month_start_date", "market_trading_days_in_window", "market_data_through",
        "security_code", "name", "security_type", "return_basis",
    ]
    with open(slice_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for code in sorted(agg):
            a = agg[code]
            s = securities[code]
            w.writerow({
                **{k: a[k] for k in (
                    "month_id", "month_pillar", "month_first_open", "month_last_close",
                    "body_return", "up_down_flat", "n_bars", "first_bar_date",
                    "last_bar_date", "complete", "market_trading_days_in_window",
                    "market_data_through")},
                "identity_code": code,
                "month_end_date": m["end_date"],
                "month_start_date": m["start_date"],
                "security_code": s["security_code"],
                "name": s["name"],
                "security_type": s["security_type"],
                "return_basis": "RAW_FIRST_OPEN_LAST_CLOSE",
            })

    # ---- identities with no bar in the window (never counted as FLAT) ----
    missing = []
    for code, s in securities.items():
        if code in agg:
            continue
        if s["security_type"] != "A_SHARE":
            reason = "NOT_COVERED_BY_STOCK_ZIP"      # index identities
        elif code not in zip_codes:
            reason = "NOT_IN_2026_STOCK_ZIP"
        elif s["delisted_date"] and s["delisted_date"] < m["start_date"]:
            reason = "DELISTED_BEFORE_WINDOW"
        elif s["listed_date"] and s["listed_date"] > m["end_date"]:
            reason = "LISTED_AFTER_WINDOW"
        else:
            reason = "NO_BAR_IN_WINDOW"
        missing.append({
            "identity_code": code,
            "security_code": s["security_code"],
            "name": s["name"],
            "security_type": s["security_type"],
            "listed_date": s["listed_date"] or "",
            "delisted_date": s["delisted_date"] or "",
            "first_month_id": s["first_month_id"] or "",
            "last_month_id": s["last_month_id"] or "",
            "in_2026_zip": 1 if code in zip_codes else 0,
            "bars_in_window": 0,
            "reason": reason,
        })
    missing.sort(key=lambda r: (r["reason"], r["identity_code"]))

    missing_path = out_dir / "missing_identities.csv"
    if missing:
        with open(missing_path, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(missing[0].keys()))
            w.writeheader()
            w.writerows(missing)

    # ---- join-complete view: exactly one row per identity, NO_DATA is explicit ----
    all_path = out_dir / "new_month_slice_all_identities.csv"
    missing_by_code = {r["identity_code"]: r for r in missing}
    with open(all_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields + ["data_status"])
        w.writeheader()
        for code in sorted(securities):
            s = securities[code]
            row = {
                "identity_code": code, "month_id": month_id,
                "month_pillar": m["month_pillar"],
                "month_first_open": "", "month_last_close": "", "body_return": "",
                "up_down_flat": "NO_DATA", "n_bars": 0,
                "first_bar_date": "", "last_bar_date": "",
                "month_end_date": m["end_date"], "complete": 0,
                "month_start_date": m["start_date"],
                "market_trading_days_in_window": market_days,
                "market_data_through": max_date_all_codes,
                "security_code": s["security_code"], "name": s["name"],
                "security_type": s["security_type"],
                "return_basis": "RAW_FIRST_OPEN_LAST_CLOSE",
                "data_status": missing_by_code.get(code, {}).get("reason", ""),
            }
            if code in agg:
                a = agg[code]
                for k in ("month_first_open", "month_last_close", "body_return",
                          "up_down_flat", "n_bars", "first_bar_date", "last_bar_date",
                          "complete"):
                    row[k] = a[k]
                row["data_status"] = "HAS_BARS"
            w.writerow(row)

    # ---- market codes in the zip but absent from securities ----
    unmatched = [c for c in per_code_all if c not in securities]
    unmatched.sort()
    unmatched_path = out_dir / "market_codes_without_identity.csv"
    if unmatched:
        with open(unmatched_path, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["market_code", "bars_in_window", "first_bar_date",
                        "last_bar_date", "suffix", "in_securities"])
            for c in unmatched:
                bars = per_code_all[c]
                w.writerow([c, len(bars), bars[0][0], bars[-1][0],
                            c.rsplit(".", 1)[-1], 0])

    # ---- distribution ----
    dist = Counter(a["up_down_flat"] for a in agg.values())
    reason_dist = Counter(r["reason"] for r in missing)
    nbars_dist = Counter(a["n_bars"] for a in agg.values())

    self_check = None
    if not args.skip_self_check and m["previous_month_with_facts"] == "2026-M08":
        self_check = self_check_previous_month(
            con, zip_path, m["previous_month_with_facts"], m,
            set(securities), per_date_all,
        )

    manifest = {
        "script": "tools/extend_slice.py",
        "script_version": SCRIPT_VERSION,
        "generated_at": iso8(now8()),
        "timezone": "UTC+8 (fixed offset)",
        "db": {"path": str(db_path), "mode": "ro", "query_only": "ON",
               "sha256": None, "size_bytes": db_path.stat().st_size},
        "source_zip": {"path": str(zip_path), "size_bytes": zip_path.stat().st_size,
                       "sha256": sha256_file(zip_path)},
        "month": {
            "month_id": month_id,
            "solar_year": m["solar_year"],
            "month_no": m["month_no"],
            "year_pillar": m["year_pillar"],
            "month_pillar": m["month_pillar"],
            "month_branch": m["month_branch"],
            "start_term": m["start_term"],
            "start_at": iso8(m["_start"]),
            "end_at": iso8(m["_end"]),
            "start_date": m["start_date"],
            "end_date": m["end_date"],
            "previous_month_with_facts": m["previous_month_with_facts"],
            "facts_rows_in_target_month": m["facts_rows_in_target"],
            "relation_rows_in_target_month": m["relation_rows_in_target"],
        },
        "window_rule": "bar_date in (start_date, end_date]; start day belongs to previous month",
        "coverage": {
            "complete": complete,
            "market_data_through": max_date_all_codes,
            "month_end_date": m["end_date"],
            "trading_days_in_window": market_days,
            "trading_days_in_window_list": sorted(per_date_all),
            "bars_per_day_all_market_codes": dict(sorted(per_date_all.items())),
            "note": (
                "INCOMPLETE: source mirror stops at %s, month ends %s. Slice covers only "
                "%d trading day(s); do not treat body_return as a full-month return."
                % (max_date_all_codes, m["end_date"], market_days)
            ),
        },
        "counts": {
            "securities_identities": len(securities),
            "slice_rows_with_bars": len(agg),
            "all_identities_rows": len(agg) + len(missing),
            "missing_identities": len(missing),
            "missing_by_reason": dict(reason_dist),
            "market_codes_without_identity": len(unmatched),
            "zip_codes_total": len(zip_codes),
            "zip_codes_with_bars_in_window": len(per_code_all),
            "duplicate_code_date_pairs": dups,
            "up_down_flat": dict(dist),
            "n_bars_histogram": {str(k): v for k, v in sorted(nbars_dist.items())},
            "reconciliation": (
                len(agg) + len(missing) == len(securities)
            ),
        },
        "return_basis": {
            "body_return": "RAW month_last_close / month_first_open - 1",
            "flat_rule": "Decimal(last_close) == Decimal(first_open) -> FLAT (counted in denominator)",
            "db_difference": (
                "monthly_facts.body_return is qfq-adjusted; raw-based values differ for "
                "stocks with ex-dividend/split inside the window (M08 reference: 387/5224)."
            ),
        },
        "self_check_previous_month": self_check,
        "artefacts": {
            "slice": str(slice_path),
            "all_identities": str(all_path),
            "missing": str(missing_path) if missing else None,
            "market_codes_without_identity": str(unmatched_path) if unmatched else None,
        },
    }
    manifest_path = out_dir / "slice_manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    # ---- console summary ----
    print("month_id            :", month_id, "pillar", m["month_pillar"],
          "(year %s)" % m["year_pillar"])
    print("window              :", lo, "exclusive ->", hi, "inclusive")
    print("start_at / end_at   :", iso8(m["_start"]), "/", iso8(m["_end"]))
    print("facts rows in month :", m["facts_rows_in_target"],
          "| relation rows in month:", m["relation_rows_in_target"])
    print("market data through :", max_date_all_codes,
          "| trading days in window:", market_days, sorted(per_date_all))
    print("COMPLETE            :", complete, "(0 = partial month)")
    print("slice rows          :", len(agg))
    print("up / down / flat    :", dist.get("UP", 0), "/", dist.get("DOWN", 0),
          "/", dist.get("FLAT", 0))
    print("missing identities  :", len(missing), dict(reason_dist))
    print("identity reconciles :", len(agg) + len(missing) == len(securities),
          "(%d + %d = %d)" % (len(agg), len(missing), len(securities)))
    print("zip codes w/o identity:", len(unmatched))
    print("duplicate (code,date):", dups)
    if self_check:
        print("self-check M08      :", json.dumps(
            {k: v for k, v in self_check.items() if k != "facts_rows_missing_examples"},
            ensure_ascii=False))
    print("out dir             :", out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
