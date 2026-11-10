"""应用内常驻：每天 19:05 起最多 4 次尝试，把 NAS 当日数据刷进"当月层"。

背景（2026-10-29 起）：NAS 夜间链改为 17:00 开跑，约 18:10 齐（对方估算 55–70 分钟，
最晚重试顺延到 19:00）。本任务因此从 19:05 起尝试：
    19:05 / 19:40 / 20:20 / 21:00
成功判据 = 拉取门禁目标日相比上次前进（数据真的更新了）；四档都没等到就当天收工。

边界：
- 只调用 scripts/pull_nas_daily.py 与 scripts/build_live_slice.py（两者都有门禁与哈希校验）
- 不碰正式库、不碰月度调度器、不碰 NAS 写路径
- 线程 daemon=True，任何异常只进日志，不影响查询服务
- 状态/日志：<app>/data/live_daily_state.json、<app>/data/live_daily.log
- 周末不跑；节假日跑也只会因"目标日未前进"计一次失败，无害
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BJT = timezone(timedelta(hours=8))
SLOTS = ((19, 5), (19, 40), (20, 20), (21, 0))
MIN_RETRY_GAP = timedelta(minutes=20)
_state_lock = threading.Lock()
# flock 的句柄必须一直被引用：局部变量被垃圾回收时文件会关闭，锁随即释放（2026-10-29 踩过）
_lock_handles: list = []


def _now() -> datetime:
    return datetime.now(BJT)


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _log(path: Path, line: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"[{_now().strftime('%F %T')}] {line}\n")
    except OSError:
        pass


def next_action(state: dict, now: datetime):
    """纯函数：返回 'run' / 'wait'。便于单测，不碰 IO。

    state: {date, attempts, done, last_attempt_at}
    """
    today = now.strftime("%F")
    if state.get("date") != today:
        return "run" if _past_first_slot(now) else "wait"
    if state.get("done"):
        return "wait"
    attempts = int(state.get("attempts") or 0)
    if attempts >= len(SLOTS):
        return "wait"
    slot_h, slot_m = SLOTS[attempts]
    slot_dt = now.replace(hour=slot_h, minute=slot_m, second=0, microsecond=0)
    if now < slot_dt:
        return "wait"
    last = state.get("last_attempt_at") or ""
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if now - last_dt < MIN_RETRY_GAP:
                return "wait"
        except ValueError:
            pass
    return "run"


def _past_first_slot(now: datetime) -> bool:
    h, m = SLOTS[0]
    return now >= now.replace(hour=h, minute=m, second=0, microsecond=0)


def _scripts_dir(root: Path) -> Path:
    """拉取/建库脚本在项目根（dev_v1 的上一级）的 scripts/ 下；
    兼容有人把脚本放 dev_v1/scripts 的情况。"""
    for cand in (root.parent / "scripts", root / "scripts"):
        if (cand / "pull_nas_daily.py").is_file():
            return cand
    return root.parent / "scripts"


_py_cache: str = ""


def _python_for_pull(log_path: Path | None = None) -> str:
    """选一个装了 paramiko 的解释器跑拉取（服务进程的 uv-python 可能没有）。"""
    global _py_cache
    if _py_cache:
        return _py_cache
    cands = [sys.executable, "/opt/homebrew/bin/python3",
             "/opt/homebrew/bin/python3.14", "/opt/homebrew/bin/python3.13",
             "/usr/local/bin/python3", "/usr/bin/python3"]
    for c in cands:
        if not c or not os.path.exists(c):
            continue
        try:
            r = subprocess.run([c, "-c", "import paramiko"],
                               capture_output=True, timeout=15)
            if r.returncode == 0:
                _py_cache = c
                return c
        except (OSError, subprocess.TimeoutExpired):
            continue
    # 一个都没探到：宁可当天就在日志里写明白，也不要每晚静默 rc=1
    # （brew 把 /opt/homebrew/bin/python3 换成没装 paramiko 的大版本踩过这个坑）
    _py_cache = sys.executable
    if log_path is not None:
        _log(log_path, f"未找到能 import paramiko 的 Python（试了 {len(cands)} 个），"
                       f"退回 {sys.executable}；补一个："
                       f"/opt/homebrew/bin/python3.14 -m pip install --user paramiko")
    return _py_cache


def _mirror_dir(root: Path) -> Path:
    """拉取原件目录：项目根下的 source_mirror_live/（兼容 dev_v1 下的情况）。"""
    for cand in (root.parent / "source_mirror_live", root / "source_mirror_live"):
        if cand.is_dir():
            return cand
    return root.parent / "source_mirror_live"


def today_watch_actions(done: bool, target: str, prev_target: str, disabled: bool) -> list[str]:
    """纯函数：日更成功后是否需要刷新交易日表、生成名单、计分。"""
    if disabled or not done or not target or target == prev_target:
        return []
    return ["sessions", "generate", "score"]


TW_UV_PYTHON = "/Users/zhangxingjin/.local/share/uv/python/cpython-3.11-macos-aarch64-none/bin/python3"
TW_DEFAULT_PYTHONPATH = ("/Users/zhangxingjin/.cache/stock-analyzer/"
                         "quality-py311-20261012/lib/python3.11/site-packages")
_tw_py_cache: str = ""


def _today_watch_env() -> dict:
    """子进程环境：PYTHONPATH 没设时补上启动脚本的默认值（F3）。"""
    env = os.environ.copy()
    if not env.get("PYTHONPATH"):
        env["PYTHONPATH"] = TW_DEFAULT_PYTHONPATH
    return env


def _python_for_today_watch(cands: list[str] | None = None, env: dict | None = None) -> str:
    """选一个装了 numpy/pandas/duckdb/pyarrow 的解释器跑今日关注脚本。

    8737 若不用启动器启动（如 `cd app && python3 server.py`），sys.executable 可能缺包；
    逐个探测 sys.executable 与 uv 3.11，都不行时返回空串，由调用方写明确日志。
    """
    global _tw_py_cache
    if cands is None and _tw_py_cache:
        return _tw_py_cache
    probe_env = _today_watch_env() if env is None else env
    candidates = [sys.executable, TW_UV_PYTHON] if cands is None else list(cands)
    for c in candidates:
        if not c or not os.path.exists(c):
            continue
        try:
            r = subprocess.run([c, "-c", "import numpy, pandas, duckdb, pyarrow"],
                               capture_output=True, timeout=30, env=probe_env)
            if r.returncode == 0:
                if cands is None:
                    _tw_py_cache = c
                return c
        except (OSError, subprocess.TimeoutExpired):
            continue
    return ""


def _today_watch_dir(root: Path) -> Path:
    return Path(os.environ.get("BAZI_TODAY_DIR", str(root.parent / "today_watch_v1")))


def _run_today_watch(root: Path, target: str, log_path: Path) -> dict:
    """日更成功后刷新交易日表→生成名单→计分；失败只进日志与状态，不影响日更成功判定。"""
    state_path = root / "data" / "today_watch_state.json"
    state = _load_state(state_path) or {}
    prev = state.get("last_target") or ""
    disabled = os.environ.get("BAZI_TODAY_WATCH", "1") == "0"
    actions = today_watch_actions(True, target, prev, disabled)
    state.update({"date": _now().strftime("%F"), "target": target,
                  "disabled": disabled, "attempted_at": _now().isoformat()})
    if not actions:
        state["skipped"] = "disabled" if disabled else "no_action"
        _save_state(state_path, state)
        _log(log_path, f"今日关注：跳过（{state['skipped']}）")
        return state

    today_dir = _today_watch_dir(root)
    py = _python_for_today_watch()
    if not py:
        state["python"] = {"found": False}
        state["skipped"] = "no_python"
        for name in actions:
            state[name] = {"ok": False, "rc": None, "tail": "skipped: no usable python"}
        state["done"] = False
        state["last_target"] = target
        _save_state(state_path, state)
        _log(log_path, "今日关注：未找到可用的 Python 解释器"
                       "（需要 numpy/pandas/duckdb/pyarrow），跳过 sessions/generate/score")
        return state
    state["python"] = {"found": True, "path": py}
    env = _today_watch_env()

    steps = [
        ("sessions", today_dir / "scripts" / "tw11_sessions.py", []),
        ("generate", today_dir / "scripts" / "tw51_generate.py", ["--asof", target]),
        ("score", today_dir / "scripts" / "tw52_score.py", []),
    ]
    all_ok = True
    stop_chain = False
    for name, script, args in steps:
        if stop_chain:
            state[name] = {"ok": False, "rc": None, "tail": "skipped: previous step failed"}
            all_ok = False
            _log(log_path, f"今日关注 {name}：跳过（sessions 失败）")
            continue
        if not script.is_file():
            state[name] = {"ok": False, "rc": None, "tail": "script missing"}
            all_ok = False
            stop_chain = name == "sessions"
            _log(log_path, f"今日关注 {name}：脚本不存在 {script}")
            continue
        try:
            r = subprocess.run([py, str(script), *args], cwd=str(today_dir), env=env,
                               capture_output=True, text=True, timeout=600)
            tail = ((r.stdout or "") + (r.stderr or "")).strip()[-500:]
            ok = r.returncode == 0
            state[name] = {"ok": ok, "rc": r.returncode, "tail": tail}
            if not ok:
                all_ok = False
                stop_chain = name == "sessions"
            _log(log_path, f"今日关注 {name} rc={r.returncode} tail={tail[-200:]}")
        except subprocess.TimeoutExpired:
            state[name] = {"ok": False, "rc": None, "tail": "timeout 600s"}
            all_ok = False
            stop_chain = name == "sessions"
            _log(log_path, f"今日关注 {name}：超时 600s")
        except Exception as exc:  # noqa: BLE001
            state[name] = {"ok": False, "rc": None, "tail": f"{type(exc).__name__}: {exc}"}
            all_ok = False
            stop_chain = name == "sessions"
            _log(log_path, f"今日关注 {name} 异常：{type(exc).__name__}: {exc}")
    state["done"] = all_ok
    state["last_target"] = target
    _save_state(state_path, state)
    return state


def _run_package(root: Path, log_path: Path) -> None:
    """日更成功后打客户机更新包（scripts/build_update_package.py）。

    失败只进日志，绝不改日更成功判定：数据已经落库，包下一档/明天重打即可，
    而且包总是包含整月切片，晚一天也是自愈的。
    """
    script = _scripts_dir(root) / "build_update_package.py"
    if not script.is_file():
        _log(log_path, f"更新包：脚本不存在 {script}，跳过")
        return
    for attempt in (1, 2):
        try:
            r = subprocess.run([sys.executable, str(script)],
                               capture_output=True, text=True, timeout=600)
            tail = ((r.stdout or "") + (r.stderr or "")).strip()[-400:]
            _log(log_path, f"更新包 attempt {attempt}: rc={r.returncode} {tail}")
            if r.returncode == 0:
                return
        except subprocess.TimeoutExpired:
            _log(log_path, f"更新包 attempt {attempt}: 超时 600s")
        except Exception as exc:  # noqa: BLE001
            _log(log_path, f"更新包 attempt {attempt} 异常：{type(exc).__name__}: {exc}")
        time.sleep(3)


def _run_once(root: Path, state: dict, state_path: Path) -> dict:
    log_path = root / "data" / "live_daily.log"
    py = _python_for_pull(log_path)
    scripts = _scripts_dir(root)
    pull = scripts / "pull_nas_daily.py"
    build = scripts / "build_live_slice.py"
    state = dict(state)
    state["attempts"] = int(state.get("attempts") or 0) + 1
    state["last_attempt_at"] = _now().isoformat()
    _log(log_path, f"attempt {state['attempts']}/{len(SLOTS)} 开始：{pull.name} + {build.name}")
    try:
        r1 = subprocess.run([py, str(pull)], capture_output=True, text=True, timeout=900)
        if r1.returncode != 0:
            _log(log_path, f"pull rc={r1.returncode} err={r1.stderr.strip()[-400:]}")
        else:
            r2 = subprocess.run([py, str(build)], capture_output=True, text=True, timeout=900)
            if r2.returncode != 0:
                _log(log_path, f"build rc={r2.returncode} err={r2.stderr.strip()[-400:]}")
            else:
                man = _load_state(_mirror_dir(root) / "fetch_manifest.json")
                target = man.get("target_trade_date") or ""
                prev = state.get("last_target") or ""
                _log(log_path, f"pull+build OK target={target} prev={prev} "
                               f"gate={man.get('gate')}")
                if target and target != prev:
                    state["done"] = True
                    state["last_target"] = target
                    _log(log_path, f"成功：当月层已更新到 {target}")
                    _run_today_watch(root, target, log_path)
                else:
                    _log(log_path, "目标日未前进（可能节假日或 NAS 尚未产出），留待下一档")
                # 包由整月切片生成，同一天重复打会自动跳过（幂等）；放这里是让
                # 重启后即使当天已 done 也能补出缺失的包。
                _run_package(root, log_path)
    except subprocess.TimeoutExpired:
        _log(log_path, "子进程超时（900s）")
    except Exception as exc:  # noqa: BLE001
        _log(log_path, f"异常：{type(exc).__name__}: {exc}")
    _save_state(state_path, state)
    return state


def _acquire_lock(lock_path: Path, log_path: Path) -> bool:
    """非阻塞抢日更锁；抢到就长期持有句柄（句柄被 GC 时锁会自动释放，2026-10-29 踩过）。"""
    fh = lock_path.open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        fh.close()
        # 写进日志文件，避免失败只停留在启动终端里（2026-10-29 踩过：只看到"没起来"查不到原因）
        _log(log_path, f"本档跳过：日更锁被其它实例占用（{type(exc).__name__}: {exc}）")
        return False
    _lock_handles.append(fh)
    return True


def start(root: Path) -> dict:
    """由 server.py 调用：起 daemon 线程（幂等，多次调用只起一个）。

    锁改到每档运行前才抢（原来在启动时抢一次，抢不到就永久让位）：
    占锁的那个常驻进程一旦被杀/随会话消失，待命实例会在下一档自动接管，
    不需要重启页面服务。2026-11-04 就是靠启动时那一次抢锁定了"孤儿守护占锁、
    页面服务永不重试"，结果当天四档全废。
    """
    if os.environ.get("BAZI_DISABLE_JOBS") == "1":
        return {"started": False, "reason": "BAZI_DISABLE_JOBS=1"}
    state_path = root / "data" / "live_daily_state.json"
    log_path = root / "data" / "live_daily.log"
    lock_path = root / "data" / "live_daily.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    owned = False

    def loop():
        nonlocal owned
        state = _load_state(state_path)
        if state.get("date") != _now().strftime("%F"):
            state = {"date": _now().strftime("%F"), "attempts": 0, "done": False,
                     "last_target": state.get("last_target") or ""}
            _save_state(state_path, state)
        _log(log_path, f"日更线程启动（19:05/19:40/20:20/21:00 四档；上次目标 "
                       f"{state.get('last_target') or '—'}）")
        while True:
            try:
                now = _now()
                today = now.strftime("%F")
                with _state_lock:
                    cur = _load_state(state_path) or state
                    if cur.get("date") != today:
                        cur = {"date": today, "attempts": 0, "done": False,
                               "last_target": cur.get("last_target") or ""}
                        _save_state(state_path, cur)
                    if now.weekday() < 5 and next_action(cur, now) == "run":
                        if owned or _acquire_lock(lock_path, log_path):
                            owned = True
                            cur = _run_once(root, cur, state_path)
            except Exception:  # noqa: BLE001
                time.sleep(30)
            time.sleep(20)

    def guarded():
        try:
            loop()
        except Exception as exc:  # noqa: BLE001
            _log(log_path, f"日更线程异常退出：{type(exc).__name__}: {exc}")

    t = threading.Thread(target=guarded, name="daily-live-refresh", daemon=True)
    t.start()
    return {"started": True, "slots": [f"{h:02d}:{m:02d}" for h, m in SLOTS]}
