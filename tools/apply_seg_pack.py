#!/usr/bin/env python3
"""客户机应用「100%段 月度增量包」：只替换 slot88_full_market_v1/ 下的文件。

只用标准库；每个文件校验字节数与 sha256；替换前备份到 _update_backup/seg_<日期>/；
Windows 上文件被页面占用时逐秒重试（最多 60 秒）。失败不动任何文件。

用法：
  py -3 apply_seg_pack.py --pack seg_pack_2026-M09.zip --dry-run
  py -3 apply_seg_pack.py --pack seg_pack_2026-M09.zip
  py -3 apply_seg_pack.py --pack ... --root D:\查询包根目录
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

BJT = timezone(timedelta(hours=8))
HERE = Path(__file__).resolve().parent
DEFAULT_ROOT = HERE.parents[1]          # 查询包根（能看到 bazi_month_relation_tengod_dev_v1 的那层）
ALLOWED_PREFIX = "slot88_full_market_v1/"


def now_bjt() -> datetime:
    return datetime.now(BJT)


def log(msg: str) -> None:
    print(f"[{now_bjt().strftime('%F %T')}] {msg}", flush=True)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve(root: Path, name: str) -> Path:
    rel = name.replace("\\", "/").lstrip("/")
    if not rel or ".." in Path(rel).parts:
        raise SystemExit(f"包内路径非法：{name!r}")
    if not rel.startswith(ALLOWED_PREFIX):
        raise SystemExit(f"包内路径不在允许范围（只允许 {ALLOWED_PREFIX}）：{name!r}")
    target = (root / rel).resolve()
    if not target.is_relative_to(root.resolve()):
        raise SystemExit(f"包内路径越界：{name!r}")
    return target


def replace(root: Path, target: Path, data: bytes, stamp: str) -> None:
    rel = target.relative_to(root.resolve())
    backup = root / "_update_backup" / f"seg_{stamp}" / rel
    if target.exists() and not backup.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(target.read_bytes())
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".new")
    tmp.write_bytes(data)
    err = None
    for _ in range(60):
        try:
            os.replace(tmp, target)
            return
        except PermissionError as exc:
            err = exc
            time.sleep(1)
    tmp.unlink(missing_ok=True)
    raise SystemExit(f"替换失败（文件被占用）：{rel} {err}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", required=True)
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    pack, root = Path(args.pack), Path(args.root)
    if not pack.is_file():
        raise SystemExit(f"包不存在：{pack}")
    stamp = now_bjt().strftime("%Y%m%d_%H%M%S")
    with zipfile.ZipFile(pack) as zf:
        try:
            man = json.loads(zf.read("manifest.json").decode("utf-8"))
        except KeyError:
            raise SystemExit("包内缺少 manifest.json")
        if man.get("schema") != "astock_seg_pack_v1":
            raise SystemExit(f"未知包格式：{man.get('schema')}")
        files = man.get("files") or []
        if not files:
            raise SystemExit("包内没有文件清单")
        log(f"包 {pack.name} 月份={man.get('month')} 构建于 {man.get('built_at_bjt')}，"
            f"{len(files)} 个文件")
        if args.dry_run:
            for f in files:
                t = resolve(root, f["path"])
                old = t.stat().st_size if t.is_file() else None
                log(f"  {f['path']}  {f['bytes']:,} 字节（现有 {old if old is not None else '—'}）")
            return 0
        done = 0
        for f in files:
            data = zf.read(f["path"])
            if len(data) != int(f["bytes"]):
                raise SystemExit(f"字节数不符：{f['path']}")
            if sha256_bytes(data) != f["sha256"]:
                raise SystemExit(f"sha256 不符：{f['path']}")
            target = resolve(root, f["path"])
            replace(root, target, data, stamp)
            done += 1
            log(f"  [{done}/{len(files)}] {f['path']}  {len(data):,} 字节")
    meta_path = root / "slot88_full_market_v1/data/seg_meta.json"
    delta_dir = root / "slot88_full_market_v1/data/seg_delta"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        dman = json.loads((delta_dir / "manifest.json").read_text(encoding="utf-8"))
        for name in (dman.get("pillars") or {}):
            if not (delta_dir / f"{name}.parquet").is_file():
                raise SystemExit(f"安装后缺增量分片：{name}.parquet")
        log(f"完成：库边界 {meta.get('library_last_month')}，"
            f"替换柱 {', '.join(sorted((dman.get('pillars') or {})))}")
    except (OSError, ValueError) as exc:
        raise SystemExit(f"安装后自检失败：{exc}")
    log("页面刷新后，100%段页的条数与抽屉历史线应立刻变化（换文件不用重启服务）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
