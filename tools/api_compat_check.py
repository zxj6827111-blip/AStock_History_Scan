"""P6 升级前后 API 对照：以“升级前快照”为 OLD，不再拿同一个 8737 当 OLD 和 NEW。

流程：
  1) 升级前：tools/api_snapshot.py 从 8737 抓取原始响应；
  2) 升级后：本脚本把目标服务（8737 或任意候选端口）与快照逐字段比较；
  3) 只要求旧字段子集完全一致，允许新增字段（十神增量）。

用法：
  python3 tools/api_compat_check.py \
      --baseline evidence/api_baseline_8737_pre_p6.json \
      --target http://127.0.0.1:8848 \
      --out evidence/api_compat_report.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / "evidence/api_baseline_8737_pre_p6.json"
DEFAULT_OUT = ROOT / "evidence/api_compat_report.json"
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch(base: str, path: str, params: dict) -> dict:
    url = base + path + "?" + urllib.parse.urlencode(params)
    with _opener.open(url, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def compare(old, new, path="$"):
    """旧字段子集比较：旧键必须存在且值相等；允许新键。"""
    diffs = []
    if isinstance(old, dict):
        if not isinstance(new, dict):
            return [f"{path}: 类型不一致 old=dict new={type(new).__name__}"]
        for k, v in old.items():
            if k not in new:
                diffs.append(f"{path}.{k}: 新响应缺失该旧字段")
            else:
                diffs.extend(compare(v, new[k], f"{path}.{k}"))
        return diffs
    if isinstance(old, list):
        if not isinstance(new, list):
            return [f"{path}: 类型不一致 old=list new={type(new).__name__}"]
        if len(old) != len(new):
            return [f"{path}: 列表长度不一致 old={len(old)} new={len(new)}"]
        for i, (a, b) in enumerate(zip(old, new)):
            diffs.extend(compare(a, b, f"{path}[{i}]"))
        return diffs
    if old != new:
        return [f"{path}: 值变化 old={old!r} new={new!r}"]
    return diffs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    ap.add_argument("--target", default="http://127.0.0.1:8848")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    baseline_path = Path(args.baseline)
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    results, total = [], 0
    for item in baseline["endpoints"]:
        try:
            new = fetch(args.target, item["path"], item["params"])
            diffs = compare(item["body"], new)
        except Exception as exc:  # noqa: BLE001
            diffs = [f"请求失败: {type(exc).__name__}: {exc}"]
        total += len(diffs)
        results.append({"path": item["path"], "params": item["params"],
                        "diff_count": len(diffs), "diffs": diffs[:20]})
        flag = "PASS" if not diffs else "FAIL"
        print(f"{flag} {item['path']} {json.dumps(item['params'], ensure_ascii=False)}"
              + ("" if not diffs else f"  diffs={len(diffs)}"))
        for d in diffs[:5]:
            print("   ", d)

    report = {
        "comparison_version": "api-compat-v2",
        "compared_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "baseline": {
            "path": str(baseline_path),
            "sha256": hashlib.sha256(baseline_path.read_bytes()).hexdigest(),
            "label": baseline.get("label"),
            "captured_at_utc": baseline.get("captured_at_utc"),
            "base_url": baseline.get("base_url"),
            "endpoint_count": len(baseline["endpoints"]),
        },
        "target": args.target,
        "total_diffs": total,
        "endpoints": len(results),
        "results": results,
    }
    out = Path(args.out)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nENDPOINTS={len(results)} TOTAL_DIFFS={total}")
    print(f"BASELINE_SHA256={report['baseline']['sha256']}")
    print(f"REPORT -> {out}")
    return 0 if total == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
