#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M08 connectivity replay on a COPY of the master DB.

Sequence (all writes go to the copy only):
  1. export the still-intact target month from the master DB as the baseline
     (read-only SELECT; evidence only, never an input to the rebuild);
  2. copy the master DB to <out>/db_replay.sqlite (refuses to overwrite);
  3. DELETE the target month from the copy, recording counts before/after;
  4. rebuild it through the incremental path:
       - month columns            <- <month>_slice.csv (zip + factors + index mirror)
       - chart / relation columns <- latest SAME-PILLAR history row of that identity
                                     (invariant: relation sets are constant per identity+pillar)
       - relation rows            <- same-pillar MRT rows, month-level fields refreshed
     identities with no same-pillar history are left NULL and counted as uncovered;
     nothing is fabricated, no other pillar is substituted;
  5. compare rebuilt rows against the baseline column by column.

Never opens the master DB read-write. Never runs the relation engine.
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D, localcontext
from pathlib import Path

DEV_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path(
    "/Volumes/workSSD/software development/AStock_History_Scan/"
    "bazi_month_relation_local_v1/database/bazi_month_relation_local_v1.sqlite")
TZ8 = timezone(timedelta(hours=8))
SCRIPT_VERSION = "1.0.0"

# columns the incremental path takes from the same-pillar history template
CARRY_COLS = [
    "computation_status", "research_sample_status", "undisputed", "boundary_disputed",
    "profile_present", "day_master_stem", "day_master_element", "stem_ten_god",
    "relation_type_list", "candidate_kind_list", "confirmed_event_count",
    "candidate_structure_count", "distinct_relation_type_count",
    "profile_version", "profile_sha256", "label_basis",
]
MRT_CARRY_COLS = ["positions", "event_count", "undisputed", "boundary_disputed",
                  "research_sample_status"]
CONST = {
    "relation_rule_version": "bazi-relation-research-v1.3.0-provisional",
    "policy_version": "bazi-relation-research-v1.3.0-provisional",
    "policy_sha256": "0d9e27574f91222ae8c50731e545acad3ca47b450fad2068602dbbc6d55a8af7",
    "adapter_contract_version": "v13-integration-adapter-1.0",
    "calendar_sha256": "",
    "source_batch_run_id": "",
    "label": "未知",
}


def iso8(dt: datetime) -> str:
    return dt.astimezone(TZ8).replace(microsecond=0).isoformat()


def connect_ro(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path.absolute().as_uri() + "?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA temp_store=FILE")
    return con


def table_cols(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute("PRAGMA table_info(%s)" % table)]


def export_baseline(con: sqlite3.Connection, month: str, out_dir: Path) -> dict:
    facts_cols = table_cols(con, "monthly_facts")
    mrt_cols = table_cols(con, "month_relation_types")
    facts = [dict(zip(facts_cols, r)) for r in con.execute(
        "SELECT %s FROM monthly_facts WHERE month_id=? ORDER BY identity_code"
        % ",".join(facts_cols), (month,))]
    mrt = [dict(zip(mrt_cols, r)) for r in con.execute(
        "SELECT %s FROM month_relation_types WHERE month_id=? "
        "ORDER BY identity_code,record_kind,relation_type"
        % ",".join(mrt_cols), (month,))]
    (out_dir / ("%s_baseline_facts.json" % month)).write_text(
        json.dumps(facts, ensure_ascii=False), encoding="utf-8")
    (out_dir / ("%s_baseline_mrt.json" % month)).write_text(
        json.dumps(mrt, ensure_ascii=False), encoding="utf-8")
    return {"facts": facts, "facts_cols": facts_cols,
            "mrt": mrt, "mrt_cols": mrt_cols}


def load_templates(con: sqlite3.Connection, month: str, pillar: str) -> dict:
    """Latest same-pillar fact row per identity strictly before `month`."""
    cols = table_cols(con, "monthly_facts")
    out = {}
    for r in con.execute(
            "SELECT %s FROM monthly_facts WHERE flow_pillar=? AND month_id<? "
            "ORDER BY identity_code, month_id" % ",".join(cols), (pillar, month)):
        d = dict(zip(cols, r))
        out[d["identity_code"]] = d
    return out


def load_latest_any(con: sqlite3.Connection, month: str) -> dict:
    """Latest row per identity in ANY pillar (used only for stock-level source_hash)."""
    out = {}
    for code, sh, mid in con.execute(
            "SELECT identity_code, source_hash, month_id FROM monthly_facts "
            "WHERE month_id<? ORDER BY identity_code, month_id", (month,)):
        out[code] = sh
    return out


def load_mrt_templates(con: sqlite3.Connection, month: str, pillar: str) -> dict:
    """Same-pillar MRT rows from the LATEST earlier month that has this pillar."""
    cols = table_cols(con, "month_relation_types")
    out = defaultdict(list)
    cur_month: dict[str, str] = {}
    for r in con.execute(
            "SELECT %s FROM month_relation_types WHERE flow_pillar=? AND month_id<? "
            "ORDER BY identity_code, month_id, record_kind, relation_type"
            % ",".join(cols), (pillar, month)):
        d = dict(zip(cols, r))
        code, mid = d["identity_code"], d["month_id"]
        if cur_month.get(code) != mid:
            cur_month[code] = mid
            out[code] = []
        out[code].append(d)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--month", default="2026-M08")
    ap.add_argument("--slice", default=str(
        DEV_ROOT / "work" / "extend" / "replay" / "2026-M08_slice.csv"))
    ap.add_argument("--out-dir", default=str(DEV_ROOT / "work" / "extend" / "replay"))
    ap.add_argument("--index-daily", default=None,
                    help="index daily bars emitted by replay_slice_month.py; used to "
                         "compute the benchmark over each stock's OWN endpoints "
                         "(scanner compare_index rule)")
    args = ap.parse_args()

    db_path, slice_path = Path(args.db), Path(args.slice)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    month = args.month
    replay_path = out_dir / "db_replay.sqlite"

    src = db_path.stat()
    print("[1/6] exporting baseline %s from the master (read-only) ..." % month)
    ro = connect_ro(db_path)
    cal = ro.execute("SELECT month_pillar, month_branch, solar_year, start_at, end_at, "
                     "start_term, year_pillar FROM calendar_months WHERE month_id=?",
                     (month,)).fetchone()
    if cal is None:
        raise SystemExit("month not in calendar_months")
    pillar, branch, solar_year, start_at, end_at, start_term, year_pillar = cal
    stem_el = dict(ro.execute("SELECT stem, element FROM stem_element"))
    branch_el = dict(ro.execute("SELECT branch, element FROM branch_element"))
    base = export_baseline(ro, month, out_dir)
    securities = {r[0]: {"security_code": r[1], "name": r[2], "exchange": r[3],
                         "security_type": r[4], "listed_date": r[5], "delisted_date": r[6]}
                  for r in ro.execute(
                      "SELECT identity_code,security_code,name,exchange,security_type,"
                      "listed_date,delisted_date FROM securities")}
    blank_profile = ro.execute(
        "SELECT count(*) FROM monthly_facts WHERE month_id=? AND profile_present=0",
        (month,)).fetchone()[0]
    ro.close()
    print("      baseline facts rows: %d | MRT rows: %d | profile_present=0: %d"
          % (len(base["facts"]), len(base["mrt"]), blank_profile))

    print("[2/6] copying master DB -> %s" % replay_path)
    if replay_path.exists():
        raise SystemExit("refusing to overwrite existing %s" % replay_path)
    shutil.copy2(db_path, replay_path)
    after = replay_path.stat()
    quick = sqlite3.connect(replay_path)
    qc = quick.execute("PRAGMA quick_check").fetchone()[0]
    print("      copy %.0f MB | src mtime %s size %d | copy mtime %s size %d | quick_check=%s"
          % (after.st_size / 1e6, src.st_mtime, src.st_size, after.st_mtime,
             after.st_size, qc))
    if qc != "ok":
        raise SystemExit("copy failed quick_check: %s" % qc)

    print("[3/6] deleting %s from the copy ..." % month)
    before_f = quick.execute("SELECT count(*) FROM monthly_facts WHERE month_id=?",
                             (month,)).fetchone()[0]
    before_m = quick.execute("SELECT count(*) FROM month_relation_types WHERE month_id=?",
                             (month,)).fetchone()[0]
    print("      BEFORE delete: monthly_facts=%d  month_relation_types=%d"
          % (before_f, before_m))
    with quick:
        quick.execute("DELETE FROM monthly_facts WHERE month_id=?", (month,))
        quick.execute("DELETE FROM month_relation_types WHERE month_id=?", (month,))
    after_f = quick.execute("SELECT count(*) FROM monthly_facts WHERE month_id=?",
                            (month,)).fetchone()[0]
    after_m = quick.execute("SELECT count(*) FROM month_relation_types WHERE month_id=?",
                            (month,)).fetchone()[0]
    print("      AFTER  delete: monthly_facts=%d  month_relation_types=%d"
          % (after_f, after_m))
    if after_f or after_m:
        raise SystemExit("delete did not clear the month")

    print("[4/6] loading same-pillar templates from the copy (month_id < %s) ..." % month)
    tpl = load_templates(quick, month, pillar)
    tpl_mrt = load_mrt_templates(quick, month, pillar)
    latest_any = load_latest_any(quick, month)
    print("      identities with a %s template: %d | with MRT template: %d | any history: %d"
          % (pillar, len(tpl), len(tpl_mrt), len(latest_any)))

    with open(slice_path, encoding="utf-8", newline="") as fh:
        slice_rows = list(csv.DictReader(fh))
    idx_daily_path = Path(args.index_daily) if args.index_daily else (
        out_dir / ("%s_index_daily.csv" % month))
    idx_open, idx_close = {}, {}
    if idx_daily_path.exists():
        with open(idx_daily_path, encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                idx_open[(r["identity_code"], r["trade_date"])] = r["open"]
                idx_close[(r["identity_code"], r["trade_date"])] = r["close"]
    print("      index daily bars loaded: %d dates x codes (file %s)"
          % (len(idx_open), idx_daily_path.name))

    def index_return(code: str, first_d: str, last_d: str):
        """Scanner compare_index: body(index_open@first, index_close@last, 1, 1)."""
        o, c = idx_open.get((code, first_d)), idx_close.get((code, last_d))
        if not o or not c:
            return None
        with localcontext() as ctx:
            ctx.prec = 50
            left, right = D(o), D(c)
            if left <= 0 or right <= 0:
                return None
            return float(right / left - 1)

    bench_of = {"SH": "000001.SH", "SZ": "399106.SZ"}

    print("[5/6] rebuilding monthly_facts + month_relation_types ...")
    fact_cols = base["facts_cols"]
    mrt_cols = base["mrt_cols"]
    facts_out, mrt_out, uncovered = [], [], []
    fill_stats = Counter()
    for r in slice_rows:
        code = r["identity_code"]
        sec = securities[code]
        t = tpl.get(code)
        f = {c: None for c in fact_cols}
        # --- month columns: slice only
        f.update({
            "identity_code": code, "month_id": month,
            "security_code": sec["security_code"],
            "solar_year": solar_year, "month_no": int(r["month_no"]),
            "month_branch": branch, "flow_pillar": pillar,
            "flow_stem": pillar[0], "flow_branch": pillar[1],
            "start_term": start_term, "start_at": start_at, "end_at": end_at,
            "first_date": r["first_date"], "last_date": r["last_date"],
            "trading_days": int(r["n_bars"]),
            "body_return": float(r["body_return"]),
            "return_decimal": r["return_decimal"], "direction": r["direction"],
            "fact_status": r["fact_status"], "eligible": int(r["eligible"]),
            "single_day": int(r["single_day"]),
            "partial_listing": int(r["partial_listing"]),
            "partial_delisting": int(r["partial_delisting"]),
            "quality_flags": r["quality_flags"], "endpoint_basis": r["endpoint_basis"],
            "raw_open": r["raw_open"], "raw_close": r["raw_close"],
            "factor_first": r["factor_first"], "factor_last": r["factor_last"],
            "anchor_date": r["anchor_date"], "anchor_factor": r["anchor_factor"],
            "qfq_open": r["qfq_open"], "qfq_close": r["qfq_close"],
        })
        f["flow_stem_element"] = stem_el.get(pillar[0])
        f["flow_branch_element"] = branch_el.get(pillar[1])
        f["flow_year_pillar"] = year_pillar
        # --- chart / relation columns: same-pillar template only
        if t:
            for c in CARRY_COLS:
                f[c] = t[c]
            f["source_hash"] = t["source_hash"]
            fill_stats["same_pillar"] += 1
        else:
            # No same-pillar history => several chart columns are NOT NULL in the
            # schema, so the row cannot be written without inventing them.
            # Do NOT insert, do NOT substitute another pillar, do NOT use 0/FLAT.
            fill_stats["not_rebuilt_no_same_pillar_template"] += 1
            uncovered.append({"identity_code": code, "name": sec["name"],
                              "security_type": sec["security_type"],
                              "has_any_history": int(code in latest_any),
                              "source_hash_available_stock_level":
                                  int(latest_any.get(code) is not None),
                              "reason": "NO_SAME_PILLAR_HISTORY" if code in latest_any
                                        else "NO_HISTORY_AT_ALL"})
            continue
        for c, v in CONST.items():
            f[c] = v
        # --- benchmark columns: index rows in the same slice
        if sec["security_type"] == "INDEX":
            f["benchmark_code"] = None
            f["benchmark_return"] = None
            f["excess_return"] = None
            f["comparison_status"] = None
        else:
            bcode = bench_of.get(sec["exchange"])
            f["benchmark_code"] = bcode
            bret = index_return(bcode, r["first_date"], r["last_date"]) if bcode else None
            if bret is not None:
                f["benchmark_return"] = bret
                f["excess_return"] = float(f["body_return"]) - bret
                f["comparison_status"] = "MATCHED"
            else:
                f["comparison_status"] = "MISSING_EXACT_INDEX_ENDPOINT"
        # row_sha256 deliberately left NULL: it hashes the engine result JSON
        # (which embeds period_id), it is not derivable from history rows.
        f["row_sha256"] = None
        facts_out.append(f)

        for m in tpl_mrt.get(code, []):
            mrt_out.append({
                "identity_code": code, "month_id": month,
                "record_kind": m["record_kind"], "relation_type": m["relation_type"],
                "flow_pillar": pillar, "month_branch": branch,
                "solar_year": solar_year, "positions": m["positions"],
                "event_count": m["event_count"],
                "direction": r["direction"], "body_return": float(r["body_return"]),
                "single_day": int(r["single_day"]),
                "undisputed": m["undisputed"],
                "boundary_disputed": m["boundary_disputed"],
                "research_sample_status": m["research_sample_status"],
            })

    ins_f = "INSERT INTO monthly_facts(%s) VALUES(%s)" % (
        ",".join(fact_cols), ",".join("?" * len(fact_cols)))
    ins_m = "INSERT INTO month_relation_types(%s) VALUES(%s)" % (
        ",".join(mrt_cols), ",".join("?" * len(mrt_cols)))
    with quick:
        quick.executemany(ins_f, [[f[c] for c in fact_cols] for f in facts_out])
        quick.executemany(ins_m, [[m[c] for c in mrt_cols] for m in mrt_out])
    reb_f = quick.execute("SELECT count(*) FROM monthly_facts WHERE month_id=?",
                          (month,)).fetchone()[0]
    reb_m = quick.execute("SELECT count(*) FROM month_relation_types WHERE month_id=?",
                          (month,)).fetchone()[0]
    print("      rebuilt: monthly_facts=%d  month_relation_types=%d" % (reb_f, reb_m))
    print("      fill: %s" % dict(fill_stats))

    print("[6/6] comparing rebuilt rows against the baseline ...")
    rebuilt_facts = [dict(zip(fact_cols, r)) for r in quick.execute(
        "SELECT %s FROM monthly_facts WHERE month_id=? ORDER BY identity_code"
        % ",".join(fact_cols), (month,))]
    rebuilt_mrt = [dict(zip(mrt_cols, r)) for r in quick.execute(
        "SELECT %s FROM month_relation_types WHERE month_id=? "
        "ORDER BY identity_code,record_kind,relation_type" % ",".join(mrt_cols), (month,))]
    quick.close()

    old = {r["identity_code"]: r for r in base["facts"]}
    new = {r["identity_code"]: r for r in rebuilt_facts}
    missing_rows = sorted(set(old) - set(new))
    extra_rows = sorted(set(new) - set(old))
    keys = sorted(set(old) & set(new))
    per_col = []
    for c in fact_cols:
        eq = both_null = mism_uncovered = mism_value = new_only = old_only = 0
        max_diff = 0.0
        example = None
        for k in keys:
            ov, nv = old[k].get(c), new.get(k, {}).get(c)
            if ov is None and nv is None:
                both_null += 1
            elif ov is None and nv is not None:
                new_only += 1
                example = example or [k, None, nv]
            elif ov is not None and nv is None:
                mism_uncovered += 1
                example = example or [k, ov, None]
            elif isinstance(ov, float) or isinstance(nv, float):
                if float(ov) == float(nv):
                    eq += 1
                else:
                    mism_value += 1
                    max_diff = max(max_diff, abs(float(ov) - float(nv)))
                    example = example or [k, ov, nv]
            else:
                if str(ov) == str(nv):
                    eq += 1
                else:
                    mism_value += 1
                    example = example or [k, ov, nv]
        per_col.append({"column": c, "compared": len(keys), "equal_nonnull": eq,
                        "both_null": both_null, "mismatch_value": mism_value,
                        "uncovered_new_null": mism_uncovered,
                        "new_filled_old_null": new_only,
                        "max_abs_diff": max_diff if max_diff else None,
                        "first_mismatch": example})
    row_all_equal = sum(1 for k in keys
                        if all(old[k].get(c) == new[k].get(c) for c in fact_cols))
    row_equal_ex_unhash = sum(
        1 for k in keys
        if all(old[k].get(c) == new[k].get(c) for c in fact_cols if c != "row_sha256"))

    # --- MRT comparison
    def mkey(r):
        return (r["identity_code"], r["record_kind"], r["relation_type"])
    om = {mkey(r): r for r in base["mrt"]}
    nm = {mkey(r): r for r in rebuilt_mrt}
    mrt_per_col = []
    for c in mrt_cols:
        if c == "month_id":
            continue
        eq = both_null = mism_uncovered = mism_value = new_only = old_only = 0
        example = None
        for k in sorted(set(om) | set(nm)):
            ov, nv = om.get(k, {}).get(c), nm.get(k, {}).get(c)
            if ov is None and nv is None:
                both_null += 1
            elif ov is None and nv is not None:
                new_only += 1
                example = example or [list(k), None, nv]
            elif ov is not None and nv is None:
                mism_uncovered += 1
                example = example or [list(k), ov, None]
            elif isinstance(ov, float) or isinstance(nv, float):
                if float(ov) == float(nv):
                    eq += 1
                else:
                    mism_value += 1
                    example = example or [list(k), ov, nv]
            else:
                if str(ov) == str(nv):
                    eq += 1
                else:
                    mism_value += 1
                    example = example or [list(k), ov, nv]
        mrt_per_col.append({"column": c, "equal_nonnull": eq, "both_null": both_null,
                            "mismatch_value": mism_value,
                            "uncovered_new_null": mism_uncovered,
                            "new_filled_old_null": new_only,
                            "first_mismatch": example})
    mrt_missing = sorted(set(om) - set(nm))
    mrt_extra = sorted(set(nm) - set(om))
    uncovered_codes = {u["identity_code"] for u in uncovered}
    mrt_missing_on_uncovered = sum(1 for k in mrt_missing if k[0] in uncovered_codes)
    mrt_missing_on_covered = len(mrt_missing) - mrt_missing_on_uncovered
    mrt_rows_equal = sum(1 for k in set(om) & set(nm)
                         if all(om[k][c] == nm[k][c] for c in mrt_cols))

    report = {
        "script": "tools/replay_rebuild_month.py",
        "script_version": SCRIPT_VERSION,
        "generated_at": iso8(datetime.now(TZ8)),
        "month": month,
        "pillar": pillar,
        "source_db": str(db_path),
        "replay_db": str(replay_path),
        "replay_db_size_bytes": replay_path.stat().st_size,
        "safety": {"master_db_opened": "read-only (uri ?mode=ro + PRAGMA query_only=ON)",
                   "writes": "replay copy only", "relation_engine": "not run",
                   "orchestrator": "not run"},
        "delete": {"before": {"monthly_facts": before_f, "month_relation_types": before_m},
                   "after": {"monthly_facts": after_f, "month_relation_types": after_m}},
        "rebuild": {"slice_rows": len(slice_rows),
                    "monthly_facts": reb_f, "month_relation_types": reb_m,
                    "fill_stats": dict(fill_stats),
                    "uncovered": uncovered},
        "facts_comparison": {
            "rows_baseline": len(old), "rows_rebuilt": len(new),
            "aligned_rows": len(keys),
            "missing_in_rebuild": len(missing_rows),
            "extra_in_rebuild": len(extra_rows),
            "missing_in_rebuild_examples": missing_rows[:10],
            "extra_in_rebuild_examples": extra_rows[:10],
            "keys_equal": set(old) == set(new),
            "rows_all_67_columns_equal": row_all_equal,
            "rows_equal_excluding_row_sha256": row_equal_ex_unhash,
            "per_column": per_col,
        },
        "mrt_comparison": {
            "rows_baseline": len(om), "rows_rebuilt": len(nm),
            "keys_missing_in_rebuild": len(mrt_missing),
            "keys_extra_in_rebuild": len(mrt_extra),
            "missing_rows_on_uncovered_identities": mrt_missing_on_uncovered,
            "missing_rows_on_rebuilt_identities": mrt_missing_on_covered,
            "missing_examples": [list(k) for k in mrt_missing[:10]],
            "extra_examples": [list(k) for k in mrt_extra[:10]],
            "rows_all_columns_equal": mrt_rows_equal,
            "per_column": mrt_per_col,
        },
    }
    (out_dir / ("%s_replay_report.json" % month)).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(out_dir / ("%s_column_comparison.csv" % month), "w",
              encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["table", "column", "equal_nonnull", "both_null", "mismatch_value",
                    "uncovered_new_null", "new_filled_old_null", "max_abs_diff",
                    "first_mismatch_old", "first_mismatch_new"])
        for p in per_col:
            ex = p["first_mismatch"] or [None, None, None]
            w.writerow(["monthly_facts", p["column"], p["equal_nonnull"], p["both_null"],
                        p["mismatch_value"], p["uncovered_new_null"],
                        p["new_filled_old_null"], p["max_abs_diff"], ex[1], ex[2]])
        for p in mrt_per_col:
            ex = p["first_mismatch"] or [None, None, None]
            w.writerow(["month_relation_types", p["column"], p["equal_nonnull"],
                        p["both_null"], p["mismatch_value"], p["uncovered_new_null"],
                        p["new_filled_old_null"], "", ex[1], ex[2]])

    print("facts rows baseline/rebuilt:", len(old), "/", len(new),
          "| aligned:", len(keys), "| missing in rebuild:", len(missing_rows),
          "| extra:", len(extra_rows))
    print("facts rows equal on all 67 columns:", row_all_equal,
          "| excluding row_sha256:", row_equal_ex_unhash)
    for p in per_col:
        if p["mismatch_value"] or p["uncovered_new_null"] or p["new_filled_old_null"]:
            print("  MISMATCH %-30s eq=%-5d value=%-5d uncovered=%-5d new_only=%-5d ex=%s"
                  % (p["column"], p["equal_nonnull"], p["mismatch_value"],
                     p["uncovered_new_null"], p["new_filled_old_null"],
                     p["first_mismatch"]))
    print("MRT rows baseline/rebuilt:", len(om), "/", len(nm),
          "| missing:", len(mrt_missing), "(on uncovered ids:", mrt_missing_on_uncovered,
          ", on rebuilt ids:", mrt_missing_on_covered, ") | extra:", len(mrt_extra),
          "| rows equal:", mrt_rows_equal)
    for p in mrt_per_col:
        if p["mismatch_value"] or p["uncovered_new_null"] or p["new_filled_old_null"]:
            print("  MRT %-28s eq=%-6d value=%-4d uncovered=%-5d new_only=%-4d ex=%s"
                  % (p["column"], p["equal_nonnull"], p["mismatch_value"],
                     p["uncovered_new_null"], p["new_filled_old_null"],
                     p["first_mismatch"]))
    print("report:", out_dir / ("%s_replay_report.json" % month))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
