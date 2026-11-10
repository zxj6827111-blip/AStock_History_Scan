"""客户机「日更」按钮：拉起已有的更新器，并把结果翻译成一句人话。

边界（红线）：
  * 本模块**不下载、不校验、不替换任何文件**——那些仍由查询包根目录下的
    `client_update.py` 负责；这里只拉起它、读它的输出；
  * 不联网、不碰任何 `.sqlite`；
  * 客户机没装更新器时如实回 `installed=false`，绝不假装更新过。

为什么用子进程而不是 import：更新器要长期演进（锁、下载、备份都在它那边），
服务与它保持一条干净的命令行契约 `client_update.py --once`；装的是旧版时，
这里能通过源码里的版本标记认出「旧版」，而不是让按钮静默失效。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config

BJT = timezone(timedelta(hours=8))
#: 查询包根（客户机 = 能看到 client_update.py 的那一层；开发机 = 项目根）。
#: 测试用 set_root() 指到临时目录，绝不碰生产 `client_update.json`/日志。
ROOT = Path(os.environ.get("BAZI_QUERY_ROOT", str(config.ROOT))).resolve()
#: 更新器脚本随 git 走（2026-11-10 起）：查询包里的 `bazi_month_relation_tengod_dev_v1/tools/`。
#: 配置留在查询包根目录，**不进仓库**，git pull 不会碰它。
PKG_DIR = Path(__file__).resolve().parents[1]
UPDATER = Path(os.environ.get("BAZI_CLIENT_UPDATER",
                              str(PKG_DIR / "tools" / "client_update.py"))).resolve()
CONFIG = Path(os.environ.get("BAZI_CLIENT_CONFIG",
                             str(ROOT / "update_client.json"))).resolve()
STATE = ROOT / "update_state.json"
LOG = ROOT / "update.log"
#: 新版更新器源码里一定有这个名字：loop 单例锁与每轮互斥锁拆开了（2026-11-10）。
#: 旧版把唯一的锁攥到进程结束，页面按钮会被永远判成「已在运行」——必须让按钮说出来。
NEW_UPDATER_MARKER = "update_loop.lock"
RUN_TIMEOUT_SECONDS = 600

_MUTEX = threading.RLock()
_RUNNING = False          # 页面按钮自己拉起的那一轮
_AUTO_RUNNING = False     # 服务内置巡检正在跑的那一轮
_AUTO_STARTED = False
_LOOP_LOCK = None         # 持有循环单例锁的句柄（必须留引用，否则会被 GC 释放）
_UPDATER_MOD = None
_UPDATER_MOD_PATH: str | None = None
_LAST: dict = {"result": None, "message": None, "date": None, "finished_at_bjt": None}

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_LOG_STAMP_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\s*")

#: 输出里的一句话 → 结果码。顺序即优先级：同一行可能同时含「下载」和别的词，
#: 口径必须唯一（例如 401 的「下载被拒」不是网络离线）。
_PATTERNS = (
    ("busy", ("已有另一个更新器",)),
    ("auth", ("[需处理]", "口令不正确", "401")),
    ("no_config", ("配置不完整",)),
    ("no_package", ("服务器还没有更新包", "包暂不可下载")),
    ("offline", ("取清单失败", "下载失败")),
    ("failed", ("[失败]", "字节数不符", "sha256 不符")),
    ("updated", ("更新完成：数据已到",)),
    ("current", ("已是最新",)),
)


def set_root(root, updater=None, config_path=None) -> dict:
    """把查询包根指到别处（仅自测）：临时目录里放假的更新器，绝不碰生产文件。"""
    global ROOT, UPDATER, CONFIG, STATE, LOG
    ROOT = Path(root).resolve()
    UPDATER = (Path(updater).resolve() if updater
               else (PKG_DIR / "tools" / "client_update.py").resolve())
    CONFIG = (Path(config_path).resolve() if config_path
              else ROOT / "update_client.json")
    STATE = ROOT / "update_state.json"
    LOG = ROOT / "update.log"
    with _MUTEX:
        _LAST.update({"result": None, "message": None, "date": None,
                      "finished_at_bjt": None})
    return {"root": str(ROOT)}


def _now_bjt() -> datetime:
    return datetime.now(BJT)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _read_json(path: Path) -> dict:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _read_source() -> str:
    try:
        return UPDATER.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def installed() -> bool:
    return UPDATER.is_file() and CONFIG.is_file()


def updater_ready() -> bool:
    """装的是不是拆锁后的新版；旧版运行会让按钮永远撞锁。"""
    return NEW_UPDATER_MARKER in _read_source()


def _tail_line(max_bytes: int = 8192) -> tuple[str | None, str | None]:
    """日志最后一条非空行 + 文件最后写入时刻（epoch → 北京口径，不做本地时区换算）。"""
    try:
        st = LOG.stat()
        with LOG.open("rb") as fh:
            size = st.st_size
            fh.seek(max(0, size - max_bytes))
            raw = fh.read()
    except OSError:
        return None, None
    lines = [ln.strip() for ln in raw.decode("utf-8", "replace").splitlines() if ln.strip()]
    line = (lines[-1][:200] if lines else None)
    return line, _iso(datetime.fromtimestamp(st.st_mtime, BJT))


def _last_success() -> dict:
    state = _read_json(STATE)
    date = str(state.get("date") or "")[:10]
    return {
        "date": date if _DATE_RE.fullmatch(date) else None,
        "applied_at_bjt": state.get("applied_at_bjt") or None,
        "entries": state.get("entries"),
    }


def status() -> dict:
    log_line, log_at = _tail_line()
    success = _last_success()
    with _MUTEX:
        last = dict(_LAST)
        manual = _RUNNING
        auto = _AUTO_RUNNING
    # 本机是「发布端」还是「客户机」：发布端根目录下有 exports/update_pkgs/latest.json，
    # 它的数据由开发流水线产出，不需要也不应该自己从服务器下载。客户包里没有这个目录。
    publisher = (ROOT / "exports" / "update_pkgs" / "latest.json").is_file()
    return {
        "role": "publisher" if publisher else "client",
        "installed": installed(),
        "updater_ready": updater_ready(),
        # 旧版安装器会在查询包根目录留一份 client_update.py，它会把锁攥到进程结束。
        # 新版脚本在仓库里；看到根目录那份就要求按说明改名，否则按钮会永远显示「正在更新」。
        "legacy_root_updater": (ROOT / "client_update.py").is_file(),
        "running": manual or auto,
        "manual_running": manual,
        "auto_running": auto,
        "local_date": success["date"],
        "last_success_bjt": success["applied_at_bjt"],
        "last_result": last["result"],
        "last_message": last["message"],
        "last_finished_bjt": last["finished_at_bjt"],
        "auto_log_line": log_line,
        "auto_log_at_bjt": log_at,
    }


def _clean_line(line: str) -> str:
    s = _LOG_STAMP_RE.sub("", line.strip())
    return s[:220]


def _interpret(output: str, returncode: int) -> tuple[str, str, str | None]:
    """把更新器的 stdout/stderr 翻成 (结果码, 一句人话, 日期)。不猜：没命中就如实说。"""
    lines = [ln for ln in output.splitlines() if ln.strip()]
    for code, keys in _PATTERNS:
        for ln in reversed(lines):
            if any(k in ln for k in keys):
                msg = _clean_line(ln)
                m = _DATE_RE.search(ln)
                return code, msg, (m.group(1) if m else None)
    tail = _clean_line(lines[-1]) if lines else ""
    if returncode == 0:
        return "current", tail or "更新器已完成检查", None
    return "failed", tail or f"更新器退出码 {returncode}", None


def _set_last(result: str, message: str, date: str | None) -> None:
    with _MUTEX:
        _LAST.update({"result": result, "message": message, "date": date,
                      "finished_at_bjt": _iso(_now_bjt())})


def _worker() -> None:
    global _RUNNING
    result, message, date = "failed", "更新器没有回报结果", None
    try:
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"          # Windows 控制台默认 cp936，强制子进程说 UTF-8
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run(
            [sys.executable or "python", str(UPDATER), "--once",
             "--root", str(ROOT), "--config", str(CONFIG)],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=RUN_TIMEOUT_SECONDS, env=env)
        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        result, message, date = _interpret(output, proc.returncode)
    except subprocess.TimeoutExpired:
        result, message = "failed", f"更新超过 {RUN_TIMEOUT_SECONDS // 60} 分钟没有结束，已停止等待"
    except OSError as exc:
        result, message = "failed", f"更新器起不来：{type(exc).__name__}"
    except Exception as exc:  # noqa: BLE001 —— 按钮面绝不能把异常抛给请求线程
        result, message = "failed", f"更新器异常：{type(exc).__name__}"
    finally:
        if result in ("updated", "current") and not date:
            date = _last_success()["date"]
        _set_last(result, message, date)
        with _MUTEX:
            _RUNNING = False


def start_daily() -> tuple[str, dict]:
    """启动一轮日更。立即返回，结果由 status() 轮询。"""
    global _RUNNING
    if (ROOT / "exports" / "update_pkgs" / "latest.json").is_file():
        return "PUBLISHER", {"error": "PUBLISHER",
                             "message": "本机是数据发布端，不需要日更（客户机会自动拉取）"}
    if not installed():
        return "NOT_INSTALLED", {"error": "NOT_INSTALLED",
                                 "message": "这台电脑还没有安装数据更新器，请联系开发方"}
    if not updater_ready():
        return "UPDATER_OLD", {"error": "UPDATER_OLD",
                               "message": "更新器是旧版（旧版会长期占用锁），请联系开发方安装新版"}
    if (ROOT / "client_update.py").is_file():
        return "LEGACY_ROOT", {"error": "LEGACY_ROOT",
                               "message": "根目录还有旧版更新器文件，请按《客户机更新说明》改名后再试"}
    with _MUTEX:
        if _RUNNING or _AUTO_RUNNING:
            return "ALREADY_RUNNING", {"error": "ALREADY_RUNNING",
                                       "message": "本机已有一轮日更在跑，请稍候"}
        _RUNNING = True
    _set_last("running", "正在检查更新…", None)
    threading.Thread(target=_worker, name="daily-update", daemon=True).start()
    return "STARTED", {"started": True, "started_at_bjt": _iso(_now_bjt())}


# ---------------------------------------------------------------- 服务内置自动巡检
#
# 2026-11-10 起不再依赖 Start_Windows.bat 拉起根目录脚本：服务启动时自己起一条守护线程，
# 口径与原更新器一致（启动即查；工作日 19:00-22:59 每 30 分钟，其余每 3 小时）。
# 只在「客户机 + 装了更新器 + 没有旧版根目录脚本」时启动；发布端永远不启动。

def _load_updater_module():
    """只读导入仓库内的更新器（纯标准库）：让巡检复用同一套下载/校验/替换实现。"""
    global _UPDATER_MOD, _UPDATER_MOD_PATH
    import importlib.util
    path = str(UPDATER)
    with _MUTEX:
        if _UPDATER_MOD is not None and _UPDATER_MOD_PATH == path:
            return _UPDATER_MOD
    try:
        spec = importlib.util.spec_from_file_location("client_update_repo", path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except BaseException:  # noqa: BLE001 —— 含 SystemExit：坏脚本绝不能把服务带走
        return None
    with _MUTEX:
        _UPDATER_MOD, _UPDATER_MOD_PATH = mod, path
    return mod


def _set_auto(running: bool) -> None:
    global _AUTO_RUNNING
    with _MUTEX:
        _AUTO_RUNNING = running


def _loop_forever(mod) -> None:
    cfg = mod.load_json(CONFIG)
    quiet = bool(cfg.get("quiet"))
    last_ts = 0.0
    while True:
        try:
            now = mod.now_bjt()
            if last_ts == 0.0 or mod.due(now, last_ts):
                _set_auto(True)
                try:
                    mod.run_cycle(ROOT, cfg, quiet=quiet)
                finally:
                    _set_auto(False)
                last_ts = time.time()
        except Exception:  # noqa: BLE001 —— 巡检线程任何异常都不能退出
            _set_auto(False)
        time.sleep(60)


def start_auto_loop() -> dict:
    """服务启动时调用一次。绝不抛异常；返回给人看的启动结论。"""
    global _AUTO_STARTED, _LOOP_LOCK
    if os.environ.get("BAZI_DISABLE_AUTO_UPDATE") == "1":
        return {"started": False, "reason": "已用 BAZI_DISABLE_AUTO_UPDATE=1 关闭"}
    if (ROOT / "exports" / "update_pkgs" / "latest.json").is_file():
        return {"started": False, "reason": "本机是数据发布端，不需要自动下载"}
    if not installed():
        return {"started": False, "reason": "未安装更新器（缺脚本或配置）"}
    if not updater_ready():
        return {"started": False, "reason": "更新器是旧版，未启动"}
    if (ROOT / "client_update.py").is_file():
        return {"started": False,
                "reason": "根目录还有旧版更新器文件，请按《客户机更新说明》改名后重启"}
    with _MUTEX:
        if _AUTO_STARTED:
            return {"started": False, "reason": "已启动过"}
        _AUTO_STARTED = True
    mod = _load_updater_module()
    if mod is None:
        with _MUTEX:
            _AUTO_STARTED = False
        return {"started": False, "reason": "更新器脚本导入失败"}
    lock = mod._acquire_lock(ROOT / "update_loop.lock")
    if lock is None:
        with _MUTEX:
            _AUTO_STARTED = False
        return {"started": False, "reason": "已有另一个常驻更新器在运行"}
    _LOOP_LOCK = lock
    threading.Thread(target=_loop_forever, args=(mod,), name="client-update-loop",
                     daemon=True).start()
    return {"started": True, "reason": "后台更新已启动（启动即查，19:00-22:59 每 30 分钟）"}
