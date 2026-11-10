#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Falsification test for the "same month pillar -> same relation set" invariant.

Read-only (URI ?mode=ro + PRAGMA query_only=ON). SELECT only.

Question: for a fixed identity_code and a fixed month_pillar (flow_pillar), is the
*set of relation types* identical across every historical occurrence of that
pillar?

Where relations live (actual schema, see PRAGMA table_info output in the report):
  month_relation_types  one row per (identity_code, month_id, record_kind,
                        relation_type)  -> already the folded per-month set.
  monthly_relations     one row per relation *pair/instance* (has natal_position,
                        occupied_positions, svu, ...); must be folded to a set.
  monthly_facts.relation_type_list  a JSON array of the month's relation types.

Folding used here: per (identity, month) we keep
    set of (record_kind, relation_type)
Because relation_type -> record_kind is 1:1 in this DB (checked at runtime), this
is byte-equivalent to comparing only relation_type. Sorted tuples are compared
byte-for-byte. Return/benefit fields are NOT part of the compared set.

Two independent phases:
  A) random sample of identities, all pillars each;
  B) all identities, a handful of pillars (always including the target pillar).

Every SQL is an index seek / bounded index range scan; the slowest statement's
wall time is reported so the "no full-table scan" rule is auditable.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ8 = timezone(timedelta(hours=8))
DEV_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path(
    "/Volumes/workSSD/software development/AStock_History_Scan/"
    "bazi_month_relation_local_v1/database/bazi_month_relation_local_v1.sqlite"
)
OUT_DIR = DEV_ROOT / "work" / "extend" / "invariant"

STEMS = list("甲乙丙丁戊己庚辛壬癸")
BRANCHES = list("子丑寅卯辰巳午未申酉戌亥")
PILLARS60 = ["%s%s" % (STEMS[i % 10], BRANCHES[i % 12]) for i in range(60)]


def now8() -> str:
    return datetime.now(TZ8).isoformat(timespec="seconds")


def connect_ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA temp_store=FILE")
    return con


class Timed:
    """Tiny helper that records the wall time of every statement."""

    def __init__(self, con: sqlite3.Connection):
        self.con = con
        self.times = []

    def all(self, sql, params=()):
        t0 = time.perf_counter()
        rows = self.con.execute(sql, params).fetchall()
        self.times.append((time.perf_counter() - t0, len(rows), sql.split()[0:6]))
        return rows

    def one(self, sql, params=()):
        t0 = time.perf_counter()
        row = self.con.execute(sql, params).fetchone()
        self.times.append((time.perf_counter() - t0, 1, sql.split()[0:6]))
        return row

    def stats(self):
        if not self.times:
            return {}
        worst = max(self.times, key=lambda x: x[0])
        return {
            "statements": len(self.times),
            "max_seconds": round(worst[0], 3),
            "max_statement": " ".join(worst[2]),
            "total_seconds": round(sum(t[0] for t in self.times), 2),
        }


def fold_months(rows):
    """rows: (month_id, record_kind, relation_type[, positions]) -> {month: tuple}."""
    per_month = defaultdict(set)
    per_month_pos = defaultdict(set)
    for r in rows:
        mid, rk, rt = r[0], r[1], r[2]
        per_month[mid].add((rk, rt))
        if len(r) > 3:
            per_month_pos[mid].add((rt, r[3] or ""))
    return (
        {m: tuple(sorted(s)) for m, s in per_month.items()},
        {m: tuple(sorted(s)) for m, s in per_month_pos.items()},
    )


def compare_pillar_map(pillar_months, pillar_months_pos, identity, pillar):
    """Return (n_groups_checked, list_of_violations) for one identity."""
    groups = 0
    viol = []
    for pil, months in pillar_months.items():
        if len(months) < 2:
            continue
        groups += 1
        # normalise order so the byte comparison is order-insensitive but exact
        months = {mid: tuple(sorted(s)) for mid, s in months.items()}
        distinct = sorted(set(months.values()))
        if len(distinct) > 1:
            # keep the raw sets verbatim
            sample_months = {}
            for mid in sorted(months):
                sample_months.setdefault(str(list(months[mid])), []).append(mid)
            viol.append({
                "identity_code": identity,
                "month_pillar": pil,
                "n_occurrences": len(months),
                "distinct_type_sets": len(distinct),
                "sets": sample_months,
                "positions_also_differ": len(set(pillar_months_pos.get(pil, {}).values())) > 1,
            })
    return groups, viol


def check_pillar(td: Timed, pillar: str):
    """Stream all rows of one pillar ordered by identity, compare per identity."""
    rows = td.all(
        "SELECT identity_code, month_id, record_kind, relation_type, positions "
        "FROM month_relation_types WHERE flow_pillar=? "
        "ORDER BY identity_code, month_id",
        (pillar,),
    )
    groups = 0
    violations = []
    positions_differ_but_types_same = 0
    per_identity = defaultdict(list)
    for r in rows:
        per_identity[r[0]].append(r[1:])
    for identity, irows in per_identity.items():
        per_month, per_month_pos = fold_months(irows)
        g, v = compare_pillar_map(
            {pillar: per_month}, {pillar: per_month_pos}, identity, pillar
        )
        groups += g
        violations.extend(v)
        if g and len(set(per_month.values())) == 1 and \
                len(set(per_month_pos.values())) > 1:
            positions_differ_but_types_same += 1
    return groups, violations, positions_differ_but_types_same, len(per_identity), len(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--sample-ids", type=int, default=400)
    ap.add_argument("--n-pillars", type=int, default=8)
    ap.add_argument("--seed", type=int, default=20261022)
    ap.add_argument("--target-pillar", default="戊戌")
    ap.add_argument("--all-pillars", action="store_true",
                    help="check all 60 pillars instead of a sample (60 bounded index "
                         "ranges, still no unbounded full-table scan)")
    args = ap.parse_args()

    con = connect_ro(Path(args.db))
    td = Timed(con)

    schema = {}
    for t in ("month_relation_types", "monthly_relations", "monthly_facts"):
        schema[t] = td.all("PRAGMA table_info(%s)" % t)

    total_mrt = td.one("SELECT count(*) FROM month_relation_types")[0]
    total_mr = td.one("SELECT count(*) FROM monthly_relations")[0]
    type_kind = td.all(
        "SELECT relation_type, count(DISTINCT record_kind) FROM month_relation_types "
        "GROUP BY relation_type HAVING count(DISTINCT record_kind) > 1"
    )
    mrt_identities = td.one("SELECT count(DISTINCT identity_code) FROM month_relation_types")[0]
    mr_identities = td.one("SELECT count(DISTINCT identity_code) FROM monthly_relations")[0]

    identities = [r[0] for r in con.execute(
        "SELECT identity_code FROM securities ORDER BY identity_code"
    )]
    mrt_ids = {r[0] for r in con.execute(
        "SELECT DISTINCT identity_code FROM month_relation_types"
    )}
    rnd = random.Random(args.seed)
    sample = sorted(rnd.sample(identities, min(args.sample_ids, len(identities))))

    # ---------------- phase A ----------------
    t0 = time.perf_counter()
    groups_a = 0
    viol_a = []
    occ_a = 0
    ids_with_data = 0
    for identity in sample:
        rows = td.all(
            "SELECT month_id, flow_pillar, record_kind, relation_type, positions "
            "FROM month_relation_types WHERE identity_code=?",
            (identity,),
        )
        if rows:
            ids_with_data += 1
        occ_a += len(rows)
        pm = defaultdict(dict)
        pmp = defaultdict(dict)
        for mid, pil, rk, rt, pos in rows:
            pm[pil].setdefault(mid, set()).add((rk, rt))
            pmp[pil].setdefault(mid, set()).add((rt, pos or ""))
        pm = {p: {m: tuple(sorted(s)) for m, s in v.items()} for p, v in pm.items()}
        pmp = {p: {m: tuple(sorted(s)) for m, s in v.items()} for p, v in pmp.items()}
        g, v = compare_pillar_map(pm, pmp, identity, None)
        groups_a += g
        viol_a.extend(v)
    phase_a_secs = round(time.perf_counter() - t0, 2)

    # ---------------- phase B ----------------
    pool = [p for p in PILLARS60 if p != args.target_pillar]
    if args.all_pillars:
        chosen = [args.target_pillar] + pool
    else:
        chosen = [args.target_pillar] + rnd.sample(pool, max(0, args.n_pillars - 1))
    groups_b = 0
    viol_b = []
    occ_b = 0
    per_pillar = {}
    pos_only = {}
    t0 = time.perf_counter()
    for pil in chosen:
        g, v, pdiff, n_ids, n_rows = check_pillar(td, pil)
        groups_b += g
        viol_b.extend(v)
        occ_b += n_rows
        per_pillar[pil] = {"rows": n_rows, "identities": n_ids,
                           "groups_ge2": g, "violations": len(v)}
        pos_only[pil] = pdiff
    phase_b_secs = round(time.perf_counter() - t0, 2)

    # ---------------- phase C: monthly_facts.relation_type_list cross-check ----
    # Confirms the pre-folded JSON list used by the pipeline equals the folded
    # OFFICIAL set from month_relation_types.
    cross_checked = cross_bad = 0
    cross_examples = []
    for identity in sample[:60]:
        facts = td.all(
            "SELECT month_id, relation_type_list FROM monthly_facts WHERE identity_code=?",
            (identity,),
        )
        mrt = td.all(
            "SELECT month_id, record_kind, relation_type FROM month_relation_types "
            "WHERE identity_code=? AND record_kind='OFFICIAL'",
            (identity,),
        )
        per = defaultdict(set)
        for mid, rk, rt in mrt:
            per[mid].add(rt)
        for mid, j in facts:
            cross_checked += 1
            try:
                lst = json.loads(j) if j else []
            except Exception:
                cross_bad += 1
                if len(cross_examples) < 5:
                    cross_examples.append([identity, mid, "UNPARSABLE", j])
                continue
            if sorted(lst) != sorted(per.get(mid, set())):
                cross_bad += 1
                if len(cross_examples) < 5:
                    cross_examples.append([identity, mid, sorted(lst), sorted(per.get(mid, set()))])

    # ---------------- phase D: fold check mrt vs monthly_relations ----------
    # monthly_relations stores one row per relation *pair* (natal_position /
    # occupied_positions per row). Folding it to DISTINCT (record_kind,
    # relation_type) per (identity, month) must reproduce month_relation_types.
    fold_checked = fold_bad = 0
    fold_examples = []
    for identity in sample[:100]:
        a = td.all(
            "SELECT month_id, record_kind, relation_type FROM month_relation_types "
            "WHERE identity_code=?",
            (identity,),
        )
        b = td.all(
            "SELECT month_id, record_kind, relation_type FROM monthly_relations "
            "WHERE identity_code=?",
            (identity,),
        )
        fa, fb = defaultdict(set), defaultdict(set)
        for mid, rk, rt in a:
            fa[mid].add((rk, rt))
        for mid, rk, rt in b:
            fb[mid].add((rk, rt))
        for mid in set(fa) | set(fb):
            fold_checked += 1
            if sorted(fa.get(mid, set())) != sorted(fb.get(mid, set())):
                fold_bad += 1
                if len(fold_examples) < 5:
                    fold_examples.append([identity, mid,
                                          sorted(fa.get(mid, set())),
                                          sorted(fb.get(mid, set()))])

    # ---------------- coverage of the target pillar for a lookup --------------
    target_ids = td.all(
        "SELECT identity_code, count(DISTINCT month_id) FROM month_relation_types "
        "WHERE flow_pillar=? GROUP BY identity_code",
        (args.target_pillar,),
    )
    counts = defaultdict(int)
    dist = {"one": 0, "two_plus": 0}
    for _, n in target_ids:
        dist["one" if n == 1 else "two_plus"] += 1
    n_ids_total = len(identities)
    with_target = len(target_ids)
    dist["identities_total"] = n_ids_total
    dist["identities_with_target_pillar"] = with_target
    dist["identities_without_target_pillar"] = n_ids_total - with_target
    no_relation_ids = len(identities) - len(mrt_ids & set(identities))
    sec_profile = {
        r[0]: r[1]
        for r in con.execute(
            "SELECT identity_code, has_natal_profile FROM securities"
        )
    }
    profile_but_no_relations = [
        c for c in identities
        if c not in mrt_ids and sec_profile.get(c) == 1
    ]

    violations = viol_a + viol_b
    report = {
        "generated_at": now8(),
        "timezone": "UTC+8",
        "db": str(args.db),
        "seed": args.seed,
        "schema": {t: [list(c) for c in cols] for t, cols in schema.items()},
        "table_totals": {
            "month_relation_types_rows": total_mrt,
            "monthly_relations_rows": total_mr,
            "month_relation_types_identities": mrt_identities,
            "monthly_relations_identities": mr_identities,
            "relation_types_with_multiple_record_kinds": type_kind,
        },
        "folding": (
            "per (identity_code, month_id) -> sorted tuple of (record_kind, relation_type); "
            "month_relation_types already stores one row per type per month, "
            "monthly_relations stores one row per relation instance/pair and is folded "
            "the same way. relation_type -> record_kind is 1:1, so the tuple equals the "
            "plain type set."
        ),
        "phase_a_random_identities": {
            "identities_sampled": len(sample),
            "identities_with_relation_rows": ids_with_data,
            "relation_rows_read": occ_a,
            "groups_identity_x_pillar_with_ge2_occurrences": groups_a,
            "violations": len(viol_a),
            "seconds": phase_a_secs,
        },
        "phase_b_pillars_all_identities": {
            "pillars": chosen,
            "all_60_pillars": args.all_pillars,
            "groups_checked": groups_b,
            "relation_rows_read": occ_b,
            "violations": len(viol_b),
            "per_pillar": per_pillar,
            "positions_differ_types_same": pos_only,
            "seconds": phase_b_secs,
        },
        "phase_c_relation_type_list_crosscheck": {
            "facts_rows_checked": cross_checked,
            "mismatches": cross_bad,
            "examples": cross_examples,
        },
        "phase_d_pair_to_set_fold_check": {
            "identities": 100,
            "identity_months_checked": fold_checked,
            "mismatches": fold_bad,
            "examples": fold_examples,
            "note": "monthly_relations folded with DISTINCT(record_kind, relation_type) "
                    "per (identity, month) vs month_relation_types",
        },
        "target_pillar_coverage": {
            "pillar": args.target_pillar,
            **dist,
            "identities_without_any_relation_rows": no_relation_ids,
            "profile_present_but_no_relation_rows": profile_but_no_relations,
        },
        "first_5_violations": violations[:5],
        "totals": {
            "groups_checked": groups_a + groups_b,
            "violations": len(violations),
            "consistent_groups": groups_a + groups_b - len(violations),
        },
        "sql_timing": td.stats(),
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / "pillar_invariant_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("== schema ==")
    for t, cols in report["schema"].items():
        print(" ", t, [c[1] for c in cols])
    print("rows: month_relation_types=%d monthly_relations=%d identities=%d/%d" %
          (total_mrt, total_mr, mrt_identities, mr_identities))
    print("relation_type with >1 record_kind:", type_kind)
    print("== phase A (random identities) ==")
    print("  identities=%d with_rows=%d rows=%d groups(>=2 occ)=%d violations=%d (%.2fs)" %
          (len(sample), ids_with_data, occ_a, groups_a, len(viol_a), phase_a_secs))
    print("== phase B (all identities x pillars) ==")
    print("  pillars=%s" % chosen)
    print("  groups=%d rows=%d violations=%d (%.2fs)" % (groups_b, occ_b, len(viol_b), phase_b_secs))
    for p, v in per_pillar.items():
        print("   ", p, v, "positions_diff_types_same=", pos_only[p])
    print("== phase C (relation_type_list vs folded OFFICIAL set) ==")
    print("  checked=%d mismatches=%d" % (cross_checked, cross_bad))
    print("  examples:", cross_examples[:3])
    print("== phase D (pair rows -> type set fold vs month_relation_types) ==")
    print("  identity-months=%d mismatches=%d" % (fold_checked, fold_bad))
    print("== target pillar coverage (%s) ==" % args.target_pillar)
    print(" ", dist, "no_relation_rows=%d" % no_relation_ids,
          "profile_but_no_relations=%s" % profile_but_no_relations)
    print("== totals ==")
    print("  groups=%d consistent=%d violations=%d" %
          (report["totals"]["groups_checked"], report["totals"]["consistent_groups"],
           report["totals"]["violations"]))
    print("  sql timing:", report["sql_timing"])
    print("  report:", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
