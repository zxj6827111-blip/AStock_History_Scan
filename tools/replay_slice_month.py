#!/Users/zhangxingjin/.cache/stock-analyzer/quality-py311-20261012/bin/python3
# -*- coding: utf-8 -*-
"""Build one already-posted flow month's slice for replay (read-only inputs).

Deliberately reuses tools/qfq_extend_slice.py for: master sqlite read-only connect,
2026 zip scan, adj_factor parquet load, ISO/Decimal helpers. It does NOT modify
that script and does NOT write anywhere except its own --out-dir.

The month columns are recomputed exactly the way the scanner does it
(AStock_Bazi_Scanner.../engine.py `body()`: Decimal prec=50 on
left = raw_open*factor_first, right = raw_close*factor_last,
qfq = left|right / anchor_factor, ret = right/left-1).
Status/quality columns follow the scanner's aggregate_period rules under the
COMPLETE_TRADE_ROWS absence policy.

Nothing here reads monthly_facts of the target month (except the optional
--verify-db cross-check, which is evidence only and never feeds the output).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D, localcontext
from pathlib import Path

DEV_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import qfq_extend_slice as q  # noqa: E402  (reuse only, never modified)

TZ8 = timezone(timedelta(hours=8))
SCRIPT_VERSION = "1.0.0"


def iso8(dt: datetime) -> str:
    return dt.astimezone(TZ8).replace(microsecond=0).isoformat()


def body(o: str, c: str, f1: str, f2: str, anchor: str) -> dict:
    """Exact replica of scanner engine.body() (prec 50)."""
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(q.DEFAULT_DB))
    ap.add_argument("--zip", default=str(q.DEFAULT_ZIP))
    ap.add_argument("--factor", default=str(q.DEFAULT_FACTOR))
    ap.add_argument("--month", default="2026-M08")
    ap.add_argument("--scan-hi", default="2026-11-07",
                    help="upper bound of the single zip pass; data cursor is the max bar date")
    ap.add_argument("--out-dir", default=str(DEV_ROOT / "work" / "extend" / "replay"))
    ap.add_argument("--index-parquet", default=str(q.DEFAULT_INDEX),
        help="index daily bars (the stock zip has no index members); used only for "
             "securities with security_type=INDEX")
    ap.add_argument("--verify-db", action="store_true",
                    help="cross-check the slice against monthly_facts of the same month "
                         "(read-only evidence only; must stay OFF for the rebuild input)")
    args = ap.parse_args()

    db_path, zip_path = Path(args.db), Path(args.zip)
    factor_path, out_dir = Path(args.factor), Path(args.out_dir)
    for p in (db_path, zip_path, factor_path):
        if not p.exists():
            raise SystemExit("missing input: %s" % p)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_csv = out_dir / ("%s_slice.csv" % args.month)
    if out_csv.exists():
        raise SystemExit("refusing to overwrite %s" % out_csv)

    con = q.connect_ro(db_path)
    row = con.execute(
        "SELECT month_id, solar_year, month_no, year_pillar, month_pillar, month_branch, "
        "start_term, start_at, end_at FROM calendar_months WHERE month_id=?",
        (args.month,)).fetchone()
    if row is None:
        raise SystemExit("month not in calendar_months: %s" % args.month)
    mk = ["month_id", "solar_year", "month_no", "year_pillar", "month_pillar",
          "month_branch", "start_term", "start_at", "end_at"]
    month = dict(zip(mk, row))
    start_at = q.parse_bjt(month["start_at"])
    end_at = q.parse_bjt(month["end_at"])
    start_date, end_date = start_at.date().isoformat(), end_at.date().isoformat()

    securities = {
        r[0]: {"security_code": r[1], "name": r[2], "exchange": r[3],
               "security_type": r[4], "listed_date": r[5], "delisted_date": r[6],
               "first_month_id": r[7], "last_month_id": r[8]}
        for r in con.execute(
            "SELECT identity_code, security_code, name, exchange, security_type, "
            "listed_date, delisted_date, first_month_id, last_month_id FROM securities")
    }

    print("[1/3] scanning %s in (%s, %s] ..." % (zip_path.name, start_date, args.scan_hi))
    per_code, per_date, zip_codes = q.scan_zip(zip_path, start_date, args.scan_hi)
    data_through = max(per_date)
    mkt_days_window = sorted(d for d in per_date if start_date < d <= end_date)
    mkt_days_set = set(mkt_days_window)
    print("      codes with bars: %d | market dates: %d | market_data_through: %s"
          % (len(per_code), len(per_date), data_through))
    print("      trading days in (%s, %s]: %d" % (start_date, end_date, len(mkt_days_window)))

    wanted = set(securities) | set(per_code)
    print("[2/3] loading adj_factor parquet ...")
    factors, max_date = q.load_factors(factor_path, wanted, "20260901", "20261014")
    print("      factor rows: %d | codes with a factor max date: %d"
          % (len(factors), len(max_date)))

    print("[3/3] aggregating %s ..." % args.month)
    # indices are absent from the stock zip; take them from the index daily mirror.
    index_bars: dict[str, list[tuple[str, str, str]]] = {}
    idx_daily: list[dict] = []
    idx_path = Path(args.index_parquet)
    idx_meta = {"path": str(idx_path), "present": idx_path.exists(), "codes": {}}
    if idx_path.exists():
        import duckdb
        dcon = duckdb.connect()
        idx_codes = sorted(c for c, t in
                           ((r[0], r[1]) for r in con.execute(
                               "SELECT identity_code, security_type FROM securities"))
                           if t == "INDEX")
        for code in idx_codes:
            b = dcon.execute(
                "SELECT trade_date, open, close FROM read_parquet(?) WHERE ts_code=? "
                "AND trade_date > ? AND trade_date <= ? ORDER BY trade_date",
                [str(idx_path), code, start_date.replace("-", ""),
                 args.scan_hi.replace("-", "")]).fetchall()
            bb = [(q.ymd_to_dash(d), format(D(str(o)), "f"), format(D(str(c)), "f"))
                  for d, o, c in b if o is not None and c is not None]
            idx_meta["codes"][code] = len(bb)
            for d, o, c in bb:
                idx_daily.append({"identity_code": code, "trade_date": d,
                                  "open": o, "close": c})
            if bb:
                index_bars[code] = bb
        dcon.close()
        print("      index bars from mirror: %s" % idx_meta["codes"])

    rows, missing, unavailable = [], [], []
    flag_dist = Counter()
    for code in sorted(securities):
        s = securities[code]
        bars = per_code.get(code, [])
        from_index = False
        if not bars and code in index_bars:
            bars = index_bars[code]
            from_index = True
        wbars = [b for b in bars if start_date < b[0] <= end_date]
        if not wbars:
            if code not in zip_codes and code not in index_bars:
                reason = "NOT_IN_2026_STOCK_ZIP"
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
                if code not in max_date:  # absent from the stock factor table (indices)
                    f1 = f2 = fa = D(1)
                    fsrc = "ABSENT_FROM_FACTOR_TABLE_DEFAULT_1"
                else:
                    unavailable.append({"identity_code": code, "first_date": first_d,
                                        "last_date": last_d, "anchor_date": anchor,
                                        "missing": ",".join(n for n, v in
                                                            (("first", f1), ("last", f2),
                                                             ("anchor", fa)) if v is None)})
                    continue
        b = body(raw_o, raw_c, format(f1, "f"), format(f2, "f"), format(fa, "f"))
        n = len(wbars)

        # --- status / quality columns, scanner aggregate_period under COMPLETE_TRADE_ROWS
        # asof = close of the data cursor day (the batch is only allowed to run after it)
        asof = datetime.fromisoformat(data_through + "T15:00:00+08:00")
        complete = end_at <= asof
        listed = s["listed_date"] or ""
        delisted = s["delisted_date"] or ""
        partial_listing = int(bool(listed) and listed > start_date)
        # at_close(delisted) < end_at  <=>  delisted_date < end_date (end_at is 14:29)
        partial_delisting = int(bool(delisted) and (
            delisted < end_date or (delisted == end_date and end_at.hour * 60 + end_at.minute > 15 * 60)))
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

    fields = list(rows[0].keys())
    with open(out_csv, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    miss_csv = out_dir / ("%s_slice_missing.csv" % args.month)
    with open(miss_csv, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(missing[0].keys()))
        w.writeheader()
        w.writerows(missing)

    idx_daily_csv = out_dir / ("%s_index_daily.csv" % args.month)
    with open(idx_daily_csv, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["identity_code", "trade_date", "open", "close"])
        w.writeheader()
        w.writerows(idx_daily)

    if unavailable:
        un_csv = out_dir / ("%s_slice_unavailable.csv" % args.month)
        with open(un_csv, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(unavailable[0].keys()))
            w.writeheader()
            w.writerows(unavailable)

    verify = None
    if args.verify_db:
        def dec_close(a, b):
            try:
                return abs(D(a) - D(b)) <= D("1e-40")
            except Exception:  # noqa: BLE001
                return False
        cols = ["first_date", "last_date", "trading_days", "raw_open", "raw_close",
                "factor_first", "factor_last", "anchor_date", "anchor_factor",
                "qfq_open", "qfq_close", "body_return", "return_decimal", "direction",
                "fact_status", "eligible", "single_day", "partial_listing",
                "partial_delisting", "quality_flags", "endpoint_basis", "source_hash"]
        db = {r[0]: dict(zip(cols, r[1:])) for r in con.execute(
            "SELECT identity_code, %s FROM monthly_facts WHERE month_id=?"
            % ",".join(cols), (args.month,))}
        sl = {r["identity_code"]: dict(r, trading_days=r["n_bars"]) for r in rows}
        per_col = {}
        for c in cols:
            if c == "source_hash":
                continue
            mism = []
            for k in set(db) & set(sl):
                dv, sv = db[k][c], sl[k][c]
                if c in ("qfq_open", "qfq_close", "return_decimal", "factor_first",
                         "factor_last", "anchor_factor", "raw_open", "raw_close"):
                    ok = dec_close(dv, sv)
                elif c == "body_return":
                    ok = float(dv) == float(sv)
                else:
                    ok = str(dv) == str(sv)
                if not ok:
                    mism.append([k, dv, sv])
            per_col[c] = {"compared": len(set(db) & set(sl)), "mismatch": len(mism),
                          "examples": mism[:3]}
        verify = {"rows_db": len(db), "rows_slice": len(sl),
                  "keys_equal": set(db) == set(sl),
                  "db_not_in_slice": sorted(set(db) - set(sl))[:10],
                  "slice_not_in_db": sorted(set(sl) - set(db))[:10],
                  "per_column": per_col}

    manifest = {
        "script": "tools/replay_slice_month.py",
        "script_version": SCRIPT_VERSION,
        "generated_at": iso8(datetime.now(TZ8)),
        "month": month,
        "window_rule": "bar_date in (start_date, end_date]; the solar-term day belongs "
                       "to the previous flow month",
        "data_cursor": {"market_data_through": data_through,
                        "basis": "max bar date in one pass over the yearly zip, "
                                 "bounded by --scan-hi",
                        "trading_days_in_window": mkt_days_window},
        "anchors": {"rule": "per-identity last available bar date <= market_data_through",
                    "default_factor_1": sorted(
                        r["identity_code"] for r in rows
                        if r["factor_source"] != "PARQUET")},
        "index_source": idx_meta,
        "outputs": {"slice": str(out_csv), "missing": str(miss_csv),
                    "index_daily": str(idx_daily_csv),
                    "unavailable": str(out_dir / ("%s_slice_unavailable.csv" % args.month))
                    if unavailable else None},
        "counts": {"rows": len(rows), "missing": len(missing),
                   "unavailable_factors": len(unavailable),
                   "missing_by_reason": dict(Counter(m["reason"] for m in missing)),
                   "quality_flags": dict(flag_dist),
                   "direction": dict(Counter(r["direction"] for r in rows))},
        "verification_against_db": verify,
    }
    (out_dir / ("%s_slice_manifest.json" % args.month)).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    con.close()

    print("rows                    :", len(rows))
    print("missing identities      :", len(missing), dict(Counter(m["reason"] for m in missing)))
    print("unavailable factors     :", len(unavailable))
    print("quality flags           :", dict(flag_dist))
    print("direction               :", dict(Counter(r["direction"] for r in rows)))
    if verify:
        print("verify vs DB rows       :", verify["rows_db"], "keys_equal:", verify["keys_equal"])
        print("verify mismatches       :", {k: v["mismatch"] for k, v in verify["per_column"].items()})
    print("out                     :", out_csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
