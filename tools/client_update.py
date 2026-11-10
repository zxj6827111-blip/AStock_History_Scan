#!/usr/bin/env python3
"""客户机数据更新器（只用标准库，Python 3.9+，不需要 pip 装任何东西）。

它做的事：
  1. 读查询包根目录的 update_client.json（服务器地址 + 更新口令）
  2. 向 /api/update/manifest 查最新包日期，比本地 update_state.json 新才继续
  3. 下载 → 校验 sha256 与字节数 → 逐文件备份到 _update_backup/<日期>/ → 原子替换
  4. 写 update_state.json，页面下次请求就读到新数据（换数据不用重启页面服务）

失败一律保留旧数据：服务器不在（CF 530/1033/502）、口令不对（401）、哈希不符、
文件被占用（Windows 正在读）都只记日志，下个周期再试，绝不让页面打不开。

运行方式（2026-11-10 起由查询程序自己拉起，不再依赖根目录的旧脚本）：
    python tools/client_update.py --loop      # 常驻：启动即查，19:00-22:59 每 30 分钟，其余每 3 小时
    python tools/client_update.py --once      # 只查一次（页面「日更」按钮走这条）
    python tools/client_update.py --once --verbose

位置约定：
  * 脚本在 `bazi_month_relation_tengod_dev_v1/tools/`（跟 git 走）；
  * 配置与日志在**查询包根目录**（`update_client.json`、`update.log`、`update_state.json`），
    与 `Start_Windows.bat` 并排；配置不进仓库，git pull 不会动它。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

BJT = timezone(timedelta(hours=8))
HERE = Path(__file__).resolve().parent          # .../bazi_month_relation_tengod_dev_v1/tools
PKG_DIR = HERE.parent                            # .../bazi_month_relation_tengod_dev_v1
QUERY_ROOT = HERE.parents[1]                     # 查询包根（客户机上能看到 Start_Windows.bat 的那层）
DEFAULT_CFG = QUERY_ROOT / "update_client.json"
STATE_NAME = "update_state.json"
LOG_NAME = "update.log"

# 只允许更新页面数据所在的目录，包外的任何路径一律拒绝（防恶意/损坏包写到系统目录）
ALLOWED_PREFIXES = (
    "source_input/live/",
    "bazi_month_relation_tengod_dev_v1/",
    "slot88_full_market_v1/",
    "today_watch_v1/ledger/",
    "today_watch_v1/data/",
)

DAY_WINDOW = (19 * 60, 22 * 60 + 59)   # 19:00–22:59 每 30 分钟查
DAY_INTERVAL = 30 * 60
NIGHT_INTERVAL = 180 * 60
_auth_printed = False


def now_bjt() -> datetime:
    return datetime.now(BJT)


def _log(root: Path, msg: str, also_print: bool = True) -> None:
    line = f"[{now_bjt().strftime('%F %T')}] {msg}\n"
    try:
        p = root / LOG_NAME
        if p.is_file() and p.stat().st_size > 2_000_000:
            p.replace(p.with_suffix(".log.1"))
        with p.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass
    if also_print:
        try:
            print(line.rstrip(), flush=True)
        except OSError:
            pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_json(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def interval_for(now: datetime) -> int:
    mins = now.hour * 60 + now.minute
    if now.weekday() < 5 and DAY_WINDOW[0] <= mins <= DAY_WINDOW[1]:
        return DAY_INTERVAL
    return NIGHT_INTERVAL


def due(now: datetime, last_ts: float) -> bool:
    return (now.timestamp() - last_ts) >= interval_for(now)


class Offline(Exception):
    """服务器不可达/通道没开——保留旧数据，下个周期再试。"""


class AuthBad(Exception):
    """口令不对——重试也没用，需要重新安装配置。"""


class NoPackage(Exception):
    """服务器还没有任何包——等下一档。"""


def _request(url: str, token: str, timeout: float):
    req = urllib.request.Request(url, headers={
        "X-Update-Token": token,
        "User-Agent": "astock-updater/1.0",
    })
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_manifest(base: str, token: str) -> dict:
    url = base.rstrip("/") + "/api/update/manifest"
    last = None
    for attempt in range(3):
        try:
            with _request(url, token, 20) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise AuthBad("更新口令不正确（401），请重新安装更新器配置") from exc
            if exc.code in (404, 503):
                raise NoPackage(f"服务器还没有更新包（HTTP {exc.code}）") from exc
            last = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(2 + attempt * 2)
    raise Offline(f"取清单失败：{last}")


def download(base: str, token: str, dest: Path, timeout: float = 300,
             path: str = "/api/update/package") -> None:
    url = base.rstrip("/") + (path if path.startswith("/") else "/" + path)
    last = None
    for attempt in range(3):
        try:
            with _request(url, token, timeout) as r, dest.open("wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
            return
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise AuthBad("下载被拒（401）：口令不正确") from exc
            if exc.code in (404, 503):
                raise NoPackage(f"包暂不可下载（HTTP {exc.code}）") from exc
            last = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        dest.unlink(missing_ok=True)
        time.sleep(3 + attempt * 3)
    raise Offline(f"下载失败：{last}")


def _resolve_member(root: Path, name: str) -> Path:
    rel = name.replace("\\", "/").lstrip("/")
    if not rel or ".." in Path(rel).parts:
        raise ValueError(f"包内路径非法：{name!r}")
    if not any(rel.startswith(p) for p in ALLOWED_PREFIXES):
        raise ValueError(f"包内路径不在允许范围：{name!r}")
    target = (root / rel).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError(f"包内路径越界：{name!r}")
    return target


def _replace_file(root: Path, rel: str, data: bytes, stamp: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = root / "_update_backup" / stamp / rel
    if target.exists() and not backup.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        try:
            backup.write_bytes(target.read_bytes())
        except OSError:
            pass
    tmp = target.with_name(target.name + ".new")
    tmp.write_bytes(data)
    err = None
    # Windows 下文件可能正被查询请求打开；逐秒重试，最多 60 秒
    for _ in range(60):
        try:
            os.replace(tmp, target)
            return
        except PermissionError as exc:
            err = exc
            time.sleep(1)
    tmp.unlink(missing_ok=True)
    raise PermissionError(f"替换失败（文件被占用）：{rel} {err}")


def apply_package(root: Path, zpath: Path, date: str) -> int:
    stamp = date.replace("-", "")
    count = 0
    with zipfile.ZipFile(zpath) as zf:
        names = [i.filename for i in zf.infolist() if not i.is_dir()]
        for name in names:
            target = _resolve_member(root, name)
            rel = str(target.relative_to(root.resolve())).replace(os.sep, "/")
            _replace_file(root, rel, zf.read(name), stamp)
            count += 1
    _prune_backups(root, keep=3)
    return count


def _prune_backups(root: Path, keep: int = 3) -> None:
    base = root / "_update_backup"
    if not base.is_dir():
        return
    dirs = sorted([d for d in base.iterdir() if d.is_dir()], key=lambda d: d.name,
                  reverse=True)
    for d in dirs[keep:]:
        for p in sorted(d.rglob("*"), reverse=True):
            try:
                p.unlink() if p.is_file() else p.rmdir()
            except OSError:
                pass
        try:
            d.rmdir()
        except OSError:
            pass


def run_cycle(root: Path, cfg: dict, quiet: bool = False, verbose: bool = False) -> str:
    """一轮更新：先拿本轮互斥锁，再交给 `_run_cycle_body`。

    锁分成两把是 2026-11-10 的改动：
      * `update_loop.lock` —— 只证明「已有一个 --loop 常驻在跑」，拿不到就退出；
      * `update.lock`      —— 每次真正下载/替换时占用，跑完即放。
    以前 --loop 一启动就把唯一的锁攥到进程结束，页面的「日更」按钮永远被判成
    「已有另一个更新器在运行」，等于按钮不存在。拆开后，后台巡检与手动点一下
    互斥但都可以真正执行：谁先拿到谁跑，另一个明确回 busy。
    """
    lock = _acquire_lock(root / "update.lock")
    if lock is None:
        _log(root, "已有另一个更新器正在更新，本次跳过", also_print=not quiet)
        return "busy"
    try:
        return _run_cycle_body(root, cfg, quiet=quiet, verbose=verbose)
    finally:
        try:
            lock.close()
        except OSError:
            pass


def _local_max_month(root: Path) -> str | None:
    db = root / "bazi_month_relation_local_v1/database/bazi_month_relation_local_v1.sqlite"
    if not db.is_file():
        return None
    try:
        con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            return con.execute("SELECT MAX(month_id) FROM monthly_facts").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return None


def _month_pack_matches(root: Path, pm: dict) -> tuple[bool, str]:
    """应用前先核对每一库的基版指纹；任何一库不符就整包不装，避免半更新。"""
    try:
        import apply_month as AM
    except Exception as exc:  # noqa: BLE001
        return False, f"无法导入 apply_month：{exc}"
    for dbname, rel in AM.TARGETS.items():
        fi = (pm.get("databases") or {}).get(dbname)
        if not fi:
            continue
        base_fp = (fi.get("fingerprints") or {}).get("base")
        target = root / rel
        if not base_fp or not target.is_file():
            continue
        try:
            con = sqlite3.connect(target.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                got = AM.fingerprint(con, AM.FP_TABLE[dbname])
            finally:
                con.close()
        except sqlite3.Error as exc:
            return False, f"{dbname} 读取失败：{exc}"
        diff = {k: (base_fp.get(k), got.get(k)) for k in base_fp
                if base_fp.get(k) != got.get(k)}
        if diff:
            return False, f"{dbname} 指纹不符 {diff}"
    return True, ""


def _apply_daily(root: Path, base: str, token: str, man: dict, state: dict,
                 state_path: Path, say: bool) -> str:
    date = str(man.get("date") or "")
    if len(date) != 10:
        raise ValueError(f"清单日期非法：{date!r}")
    if state.get("date") and date <= str(state["date"]):
        _log(root, f"日更已是最新（{state['date']}）", also_print=say)
        return "current"
    name = str(man.get("file") or "")
    if not name.startswith("update_") or not name.endswith(".zip"):
        raise ValueError(f"清单文件名非法：{name!r}")
    _log(root, f"发现日更包 {date}（本地 {state.get('date') or '—'}），开始下载")
    tmp = root / "_update_download.zip"
    tmp.unlink(missing_ok=True)
    download(base, token, tmp)
    size = tmp.stat().st_size
    if int(man.get("bytes") or -1) != size:
        tmp.unlink(missing_ok=True)
        raise ValueError(f"字节数不符：清单 {man.get('bytes')} 实际 {size}")
    digest = sha256_file(tmp)
    if digest != str(man.get("sha256") or ""):
        tmp.unlink(missing_ok=True)
        raise ValueError(f"sha256 不符：清单 {str(man.get('sha256'))[:12]} 实际 {digest[:12]}")
    count = apply_package(root, tmp, date)
    tmp.unlink(missing_ok=True)
    state.setdefault("schema", "astock_client_update_v1")
    state.update({
        "date": date, "file": name, "sha256": digest, "entries": count,
        "applied_at_bjt": now_bjt().strftime("%Y-%m-%dT%H:%M:%S+08:00"),
    })
    save_json(state_path, state)
    _log(root, f"日更完成：数据已到 {date}（{count} 个文件）")
    return "updated"


def _apply_month_packs(root: Path, base: str, token: str, man: dict, state: dict,
                       state_path: Path, say: bool) -> str:
    packs = man.get("month_packs") or []
    if not packs:
        return "skipped"
    latest = max(str(p.get("month")) for p in packs)
    if state.get("month") and str(state["month"]) >= latest:
        return "current"
    local = _local_max_month(root)
    if not local:
        _log(root, "主库不存在或读不到月份，跳过月包")
        return "skipped"
    updated = False
    for _ in range(6):
        if str(local) >= latest:
            break
        pack = next((p for p in packs if str(p.get("base_max_month")) == str(local)), None)
        if not pack:
            _log(root, f"[需全量] 主库在 {local}，没有以它为基版的月包（最新 {latest}）")
            return "gap"
        name = str(pack.get("file") or "")
        if not name.startswith("month_pack_") or not name.endswith(".zip"):
            raise ValueError(f"月包文件名非法：{name!r}")
        _log(root, f"月包 {pack.get('month')}（{pack.get('base_max_month')}→"
                   f"{pack.get('target_max_month')}）开始下载", also_print=say)
        tmp = root / "_update_month.zip"
        tmp.unlink(missing_ok=True)
        download(base, token, tmp, timeout=900,
                 path=f"/api/update/month_package?month={pack.get('month')}")
        size = tmp.stat().st_size
        if int(pack.get("bytes") or -1) != size:
            tmp.unlink(missing_ok=True)
            raise ValueError(f"月包字节数不符：清单 {pack.get('bytes')} 实际 {size}")
        digest = sha256_file(tmp)
        if digest != str(pack.get("sha256") or ""):
            tmp.unlink(missing_ok=True)
            raise ValueError(f"月包 sha256 不符：{str(pack.get('sha256'))[:12]} vs {digest[:12]}")
        with zipfile.ZipFile(tmp) as zf:
            pm = json.loads(zf.read("manifest.json").decode("utf-8"))
        ok, why = _month_pack_matches(root, pm)
        if not ok:
            tmp.unlink(missing_ok=True)
            _log(root, f"[需全量] 月包 {pack.get('month')} 无法套用：{why}")
            return "gap"
        rc = subprocess.run(
            [sys.executable, str(HERE / "apply_month.py"),
             "--pack", str(tmp), "--root", str(root)],
            capture_output=True, text=True)
        tmp.unlink(missing_ok=True)
        for line in (rc.stdout or "").strip().splitlines()[-4:]:
            _log(root, "  " + line, also_print=say)
        if rc.returncode != 0:
            _log(root, f"[失败] 月包应用失败：{(rc.stdout or '')[-300:]}{(rc.stderr or '')[-300:]}")
            return "failed"
        local = str(pack.get("target_max_month") or pack.get("month"))
        state.update({"month": str(pack.get("month")), "month_file": name,
                      "month_applied_at_bjt": now_bjt().strftime("%Y-%m-%dT%H:%M:%S+08:00")})
        save_json(state_path, state)
        updated = True
    return "updated" if updated else "current"


def _apply_seg_pack(root: Path, base: str, token: str, man: dict, state: dict,
                    state_path: Path, say: bool) -> str:
    seg = man.get("seg_pack")
    if not seg or not seg.get("month"):
        return "skipped"
    month = str(seg.get("month"))
    if state.get("seg") and str(state["seg"]) >= month:
        return "current"
    if not (root / "slot88_full_market_v1/data/parquet_n3").is_dir():
        _log(root, "[需全量] 缺少 100%段基础库（parquet_n3），无法只装增量")
        return "gap"
    name = str(seg.get("file") or "")
    if not name.startswith("seg_pack_") or not name.endswith(".zip"):
        raise ValueError(f"增量包文件名非法：{name!r}")
    _log(root, f"100%段增量 {month} 开始下载", also_print=say)
    tmp = root / "_update_seg.zip"
    tmp.unlink(missing_ok=True)
    download(base, token, tmp, timeout=900, path="/api/update/seg_package")
    size = tmp.stat().st_size
    if int(seg.get("bytes") or -1) != size:
        tmp.unlink(missing_ok=True)
        raise ValueError(f"增量包字节数不符：清单 {seg.get('bytes')} 实际 {size}")
    digest = sha256_file(tmp)
    if digest != str(seg.get("sha256") or ""):
        tmp.unlink(missing_ok=True)
        raise ValueError(f"增量包 sha256 不符：{str(seg.get('sha256'))[:12]} vs {digest[:12]}")
    stamp = "seg_" + month.replace("-", "")
    count = 0
    with zipfile.ZipFile(tmp) as zf:
        try:
            pm = json.loads(zf.read("manifest.json").decode("utf-8"))
        except KeyError:
            tmp.unlink(missing_ok=True)
            raise ValueError("增量包缺少 manifest.json")
        if pm.get("schema") != "astock_seg_pack_v1":
            tmp.unlink(missing_ok=True)
            raise ValueError(f"未知增量包格式：{pm.get('schema')}")
        for f in pm.get("files") or []:
            data = zf.read(f["path"])
            if len(data) != int(f.get("bytes", -1)):
                tmp.unlink(missing_ok=True)
                raise ValueError(f"增量文件字节不符：{f['path']}")
            if sha256_bytes(data) != f.get("sha256"):
                tmp.unlink(missing_ok=True)
                raise ValueError(f"增量文件 sha256 不符：{f['path']}")
            target = _resolve_member(root, f["path"])
            rel = str(target.relative_to(root.resolve())).replace(os.sep, "/")
            _replace_file(root, rel, data, stamp)
            count += 1
    tmp.unlink(missing_ok=True)
    _prune_backups(root, keep=5)
    state.update({"seg": month, "seg_file": name, "seg_entries": count,
                  "seg_applied_at_bjt": now_bjt().strftime("%Y-%m-%dT%H:%M:%S+08:00")})
    save_json(state_path, state)
    _log(root, f"100%段增量已到 {month}（{count} 个文件）")
    return "updated"


def _run_cycle_body(root: Path, cfg: dict, quiet: bool = False,
                    verbose: bool = False) -> str:
    say = verbose or not quiet
    state_path = root / STATE_NAME
    state = load_json(state_path)
    base = str(cfg.get("server") or "").strip()
    token = str(cfg.get("token") or "").strip()
    if not base or not token:
        _log(root, "配置不完整：update_client.json 缺 server 或 token")
        return "no_config"
    try:
        man = fetch_manifest(base, token)
        results = {}
        results["daily"] = _apply_daily(root, base, token, man, state, state_path, say)
        results["month"] = _apply_month_packs(root, base, token, man, state, state_path, say)
        results["seg"] = _apply_seg_pack(root, base, token, man, state, state_path, say)
        labels = {"daily": "日更", "month": "主库月包", "seg": "100%段"}
        _log(root, "本轮结果：" + "，".join(f"{labels.get(k, k)}={v}"
                                            for k, v in results.items()),
             also_print=say)
        if "gap" in results.values():
            return "gap"
        if "failed" in results.values():
            return "failed"
        if "updated" in results.values():
            return "updated"
        return "current"
    except AuthBad as exc:
        global _auth_printed
        if not _auth_printed:
            _auth_printed = True
            print(f"[需处理] {exc}", flush=True)
        _log(root, f"[需处理] {exc}", also_print=False)
        return "auth"
    except NoPackage as exc:
        _log(root, str(exc), also_print=say)
        return "no_package"
    except Offline as exc:
        _log(root, str(exc), also_print=say)
        return "offline"
    except (ValueError, zipfile.BadZipFile, PermissionError, OSError) as exc:
        _log(root, f"[失败] 更新未应用，保留旧数据：{type(exc).__name__}: {exc}")
        return "failed"


def _acquire_lock(path: Path):
    """互斥锁：--loop 用 update_loop.lock 防重复常驻，每轮更新用 update.lock 防并发写。
    句柄要一直被引用，否则锁会被 GC 释放。"""
    try:
        fh = path.open("w")
    except OSError:
        return None
    try:
        try:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        return fh
    except OSError:
        fh.close()
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help="常驻，按时间窗口重试")
    ap.add_argument("--once", action="store_true", help="只查一次")
    ap.add_argument("--config", default=str(DEFAULT_CFG))
    ap.add_argument("--root", default="", help="查询包根目录（默认取脚本向上两层）")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    cfg = load_json(Path(a.config))
    root = Path(a.root).resolve() if a.root else Path(cfg.get("root") or QUERY_ROOT).resolve()
    if not root.is_dir():
        print(f"[FAIL] 根目录不存在：{root}", file=sys.stderr)
        return 2
    quiet = bool(cfg.get("quiet")) and not a.verbose
    if a.once or not a.loop:
        st = run_cycle(root, cfg, verbose=True)
        return 0 if st in ("updated", "current", "busy") else 1

    lock = _acquire_lock(root / "update_loop.lock")
    if lock is None:
        print("已有另一个常驻更新器在运行，本次退出。", flush=True)
        return 0
    _log(root, "更新器启动：启动即查；工作日 19:00-22:59 每 30 分钟，其余每 3 小时")
    last_ts = 0.0
    while True:
        try:
            now = now_bjt()
            if last_ts == 0.0 or due(now, last_ts):
                run_cycle(root, cfg, quiet=quiet)
                last_ts = time.time()
        except Exception as exc:  # noqa: BLE001  任何异常都不能让更新器退出
            try:
                _log(root, f"[异常] {type(exc).__name__}: {exc}")
            except Exception:  # noqa: BLE001
                pass
            last_ts = time.time()
        time.sleep(60)


if __name__ == "__main__":
    raise SystemExit(main())
