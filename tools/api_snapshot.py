"""P6 预检：抓取升级前 8737 的原始 API 响应快照。

用途：升级后不能再拿同一个 8737 当 OLD，因此必须在升级前把旧响应落盘，
升级后用 api_compat_check.py 对快照逐字段比对。

用法：
  python3 tools/api_snapshot.py --base http://127.0.0.1:8737 \
      --out evidence/api_baseline_8737_pre_p6.json --label pre-p6
"""
from __future__ import annotations

import argparse
import hashlib
import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL_APP = Path("/Volumes/workSSD/software development/AStock_History_Scan"
                    "/bazi_month_relation_local_v1/app")
ASHARE_DB = Path("/Volumes/workSSD/software development/AStock_History_Scan"
                 "/bazi_month_relation_local_v1/database/bazi_month_relation_local_v1.sqlite")
ETF_DB = Path("/Volumes/workSSD/software development/AStock_History_Scan"
              "/bazi_month_relation_etf_v1/database/bazi_month_relation_etf_v1.sqlite")

# 覆盖旧系统全部只读查询入口 + 单交易日两种口径；参数与线上页面一致
ENDPOINTS = [
    ("/api/stock", {"id": "510050.SH", "dataset": "etf"}),
    ("/api/stock", {"id": "000001.SH", "dataset": "ashare"}),
    ("/api/stock", {"id": "000003.SZ", "dataset": "ashare"}),
    ("/api/branch12", {"id": "510050.SH", "dataset": "etf", "policy": "ALL"}),
    ("/api/branch12", {"id": "510050.SH", "dataset": "etf", "policy": "EXCLUDE_SINGLE"}),
    ("/api/branch12", {"id": "000001.SH", "dataset": "ashare", "policy": "ALL"}),
    ("/api/branch12", {"id": "000536.SZ", "dataset": "ashare", "policy": "ALL"}),
    ("/api/branch12", {"id": "000536.SZ", "dataset": "ashare",
                       "policy": "EXCLUDE_SINGLE"}),
    ("/api/branch_detail", {"id": "510050.SH", "branch": "亥", "dataset": "etf",
                            "policy": "ALL"}),
    ("/api/branch_detail", {"id": "000536.SZ", "branch": "申", "dataset": "ashare",
                            "policy": "ALL"}),
    ("/api/pillar60", {"id": "510050.SH", "dataset": "etf"}),
    ("/api/pillar60", {"id": "000001.SH", "dataset": "ashare"}),
    ("/api/pillar60", {"id": "000003.SZ", "dataset": "ashare"}),
    ("/api/pillar_detail", {"id": "510050.SH", "pillar": "己亥", "dataset": "etf"}),
    ("/api/pillar_detail", {"id": "000001.SH", "pillar": "壬寅", "dataset": "ashare"}),
    ("/api/relation_types", {"id": "510050.SH", "dataset": "etf",
                             "record_kind": "OFFICIAL"}),
    ("/api/relation_types", {"id": "000001.SH", "dataset": "ashare",
                             "record_kind": "OFFICIAL"}),
    ("/api/relation_pillars", {"id": "510050.SH", "dataset": "etf",
                               "record_kind": "OFFICIAL", "type": "六合"}),
    ("/api/relation_months", {"id": "510050.SH", "dataset": "etf",
                              "record_kind": "OFFICIAL", "type": "六合",
                              "pillar": "己亥"}),
    ("/api/month_detail", {"id": "510050.SH", "month": "2006-M10", "dataset": "etf"}),
    ("/api/month_detail", {"id": "000536.SZ", "month": "2002-M01", "dataset": "ashare"}),
    ("/api/calendar_now", {"dataset": "etf"}),
    ("/api/calendar_now", {"dataset": "ashare"}),
    ("/api/meta", {"dataset": "etf"}),
    ("/api/meta", {"dataset": "ashare"}),
    ("/api/catalog", {"dataset": "etf"}),
    ("/api/sources", {"dataset": "etf"}),
    ("/api/pillar_options", {"dataset": "etf"}),
    ("/api/search", {"q": "茅台", "dataset": "ashare", "limit": "20"}),
]

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(base: str, path: str, params: dict) -> dict:
    url = base + path + "?" + urllib.parse.urlencode(params)
    with _opener.open(url, timeout=120) as r:
        body = r.read().decode("utf-8")
        return {"url": url, "http_status": r.status,
                "body": json.loads(body), "bytes": len(body.encode("utf-8"))}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8737")
    ap.add_argument("--out", default=str(ROOT / "evidence/api_baseline_8737_pre_p6.json"))
    ap.add_argument("--label", default="pre-p6")
    args = ap.parse_args()

    endpoints = []
    for path, params in ENDPOINTS:
        out = fetch(args.base, path, params)
        out["path"] = path
        out["params"] = params
        endpoints.append(out)
        print(f"{out['http_status']} {path} {json.dumps(params, ensure_ascii=False)} "
              f"{out['bytes']}B")

    code = {}
    for name in ("api.py", "server.py", "static/index.html", "static/app.js", "static/style.css"):
        p = ORIGINAL_APP / name
        if p.is_file():
            code[name] = {"path": str(p), "sha256": sha256_file(p), "size": p.stat().st_size}

    dst = Path(args.out)
    dst.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "snapshot_version": "api-baseline-v1",
        "label": args.label,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "captured_at_cst": datetime.now(timezone(timedelta(hours=8))).isoformat(
            timespec="seconds"),
        "base_url": args.base,
        "code": code,
        "databases": [
            {"path": str(p), "size": p.stat().st_size,
             "mtime_epoch": int(p.stat().st_mtime)}
            for p in (ASHARE_DB, ETF_DB) if p.is_file()],
        "endpoints": endpoints,
    }
    dst.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSNAPSHOT {len(endpoints)} endpoints -> {dst}")
    print(f"SHA256 {sha256_file(dst)}")


if __name__ == "__main__":
    main()
