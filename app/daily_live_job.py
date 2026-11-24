"""应用内常驻：每天 19:05 起最多 4 次尝试，把 NAS 当日数据刷进"当月层"。

背景（2026-10-29 起）：NAS 夜间链改为 17:00 开跑，约 18:10 齐（对方估算 55–70 分钟，
最晚重试顺延到 19:00）。本任务因此从 19:05 起尝试：
    19:05 / 19:40 / 20:20 / 21:00
成功判据 = 拉取门禁目标日相比上次前进（数据真的更新了）；四档都没等到就当天收工。

边界：
- 调用 scripts/pull_nas_daily.py 与 scripts/build_live_slice.py（两者都有门禁与哈希校验）；
  建库成功后再调 slot88_full_market_v1/scripts/sync_live_60m.py 刷新当月 60 分钟线
  （100%段「本月线」用；失败只记日志，不影响日线）
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

# 模拟盘（雪球组合）引擎：可选模块——easytrader 缺失/任何原因 import 失败都不影响本文件主链
try:
    from paper_trade import engine as paper_engine
except Exception:  # noqa: BLE001
    paper_engine = None

BJT = timezone(timedelta(hours=8))
AM_SLOTS = ((11, 40), (11, 55), (12, 15), (12, 40))
INTRA_SLOTS = ((10, 5), (10, 35), (11, 5), (13, 35), (14, 5), (14, 35), (15, 5), (15, 12))
INTRA_END = (15, 25)
SLOTS = ((19, 5), (19, 40), (20, 20), (21, 0))
MIN_AM_RETRY_GAP = timedelta(minutes=12)
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


def _past_first_am_slot(now: datetime) -> bool:
    h, m = AM_SLOTS[0]
    return now >= now.replace(hour=h, minute=m, second=0, microsecond=0)


def next_am_action(state: dict, now: datetime) -> str:
    """纯函数：午盘半日更新（11:40 起至晚间首档前）返回 'run' / 'wait'。"""
    if _past_first_slot(now):
        return "wait"
    today = now.strftime("%F")
    if state.get("date") != today:
        return "run" if _past_first_am_slot(now) else "wait"
    if state.get("am_done") or state.get("done"):
        return "wait"
    attempts = int(state.get("am_attempts") or 0)
    if attempts >= len(AM_SLOTS):
        return "wait"
    slot_h, slot_m = AM_SLOTS[attempts]
    slot_dt = now.replace(hour=slot_h, minute=slot_m, second=0, microsecond=0)
    if now < slot_dt:
        return "wait"
    last = state.get("am_last_attempt_at") or ""
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if now - last_dt < MIN_AM_RETRY_GAP:
                return "wait"
        except ValueError:
            pass
    return "run"


def next_intra_action(state: dict, now: datetime) -> str:
    """纯函数：盘中 30 分钟档（10:05…15:05）返回 'run' / 'wait'。"""
    if _past_first_slot(now):
        return "wait"
    if now >= now.replace(hour=INTRA_END[0], minute=INTRA_END[1], second=0, microsecond=0):
        return "wait"
    done = set(state.get("intra_slots_done") or [])
    due = []
    for h, m in INTRA_SLOTS:
        slot = f"{h:02d}:{m:02d}"
        if now >= now.replace(hour=h, minute=m, second=0, microsecond=0) and slot not in done:
            due.append(slot)
    if not due:
        return "wait"
    last = state.get("intra_last_attempt_at") or ""
    if last:
        try:
            if now - datetime.fromisoformat(last) < timedelta(minutes=5):
                return "wait"
        except ValueError:
            pass
    return "run"


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


def _run_sync_live_60m(root: Path, py: str, log_path: Path) -> None:
    """把 NAS 当日 60 分钟线刷成本地 parquet（100%段「本月线」读它）。

    只影响 60m 文件；失败只进日志，绝不改日更成功判定：日线已经落库，
    60m 缺一天不能让整次日更判失败，下一档/明天会自愈。
    """
    script = root.parent / "slot88_full_market_v1" / "scripts" / "sync_live_60m.py"
    if not script.is_file():
        _log(log_path, f"60分钟线：脚本不存在 {script}，跳过")
        return
    for attempt in (1, 2):
        try:
            r = subprocess.run([py, str(script), "--days", "120"],
                               capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            _log(log_path, "60分钟线同步：超时 600s")
            return
        except Exception as exc:  # noqa: BLE001
            _log(log_path, f"60分钟线同步异常：{type(exc).__name__}: {exc}")
            return
        tail = ((r.stdout or "") + (r.stderr or "")).strip()[-300:]
        _log(log_path, f"60分钟线同步 attempt {attempt} rc={r.returncode} {tail}")
        if r.returncode == 0:
            return
        time.sleep(3)


def _run_sync_live_30m(root: Path, py: str, log_path: Path) -> dict:
    """刷 NAS 30 分钟线到本地 parquet，返回 parquet meta（失败返回空 dict）。"""
    script = root.parent / "slot88_full_market_v1" / "scripts" / "sync_live_30m.py"
    if not script.is_file():
        _log(log_path, f"30分钟线：脚本不存在 {script}，跳过")
        return {}
    for attempt in (1, 2):
        try:
            r = subprocess.run([py, str(script), "--days", "30"],
                               capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            _log(log_path, "30分钟线同步：超时 600s")
            return {}
        except Exception as exc:  # noqa: BLE001
            _log(log_path, f"30分钟线同步异常：{type(exc).__name__}: {exc}")
            return {}
        tail = ((r.stdout or "") + (r.stderr or "")).strip()[-300:]
        _log(log_path, f"30分钟线同步 attempt {attempt} rc={r.returncode} {tail}")
        if r.returncode == 0:
            meta_path = root.parent / "slot88_full_market_v1" / "data" / "live_30m_meta.json"
            try:
                return json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
        time.sleep(3)
    return {}


def survivor_report_actions(disabled: bool, script_exists: bool) -> bool:
    """纯函数：切片重建成功后是否要生成生存者报告。便于单测，不碰 IO。"""
    return (not disabled) and script_exists


def _survivor_dir(root: Path) -> Path:
    """生存者报告模块目录（BAZI_SURVIVOR_DIR 可覆盖，与 BAZI_TODAY_DIR 同理）。"""
    return Path(os.environ.get("BAZI_SURVIVOR_DIR",
                               str(root.parent / "survivor_report_v1")))


def _feishu_push_enabled() -> bool:
    """飞书推送独立开关：BAZI_SURVIVOR_FEED=0 时完全不入队不发送。"""
    return os.environ.get("BAZI_SURVIVOR_FEISHU", "1") != "0"


def _feishu_push_script(root: Path) -> Path:
    return _survivor_dir(root) / "scripts" / "sr_feishu_push.py"


def _parse_last_json(text: str) -> dict:
    """取 stdout 最后一个非空 JSON 行（生成器协议：成功/skipped 都有稳定字段）。"""
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            return obj if isinstance(obj, dict) else {}
        except ValueError:
            continue
    return {}


def _feishu_enqueue(root: Path, log_path: Path, args: list) -> None:
    """向飞书推送收件箱入队一个事件（纯本地追加，秒回；失败只记日志不影响主流程）。"""
    if not _feishu_push_enabled():
        return
    script = _feishu_push_script(root)
    if not script.is_file():
        _log(log_path, f"飞书推送：脚本缺失 {script}，跳过")
        return
    # 推送器只用标准库，不需要 duckdb；找不到今日关注解释器时用当前解释器兜底
    py = _python_for_today_watch() or sys.executable
    try:
        r = subprocess.run([py, str(script), *args, "--enqueue-only"],
                           cwd=str(script.parent), capture_output=True, text=True,
                           timeout=15, env=_today_watch_env())
        tail = ((r.stdout or "") + (r.stderr or "")).strip()[-200:]
        _log(log_path, f"飞书推送入队 rc={r.returncode} {tail}")
    except Exception as exc:  # noqa: BLE001
        _log(log_path, f"飞书推送入队异常：{type(exc).__name__}: {exc}")


_feishu_drain_busy = False


def _feishu_drain(root: Path, log_path: Path) -> None:
    """后台 drain：消费收件箱、按顺序补发 pending（独立线程，绝不阻塞日更循环）。"""
    global _feishu_drain_busy
    if not _feishu_push_enabled():
        return
    script = _feishu_push_script(root)
    if not script.is_file():
        return
    if _feishu_drain_busy:
        return
    _feishu_drain_busy = True

    def _run():
        global _feishu_drain_busy
        try:
            py = _python_for_today_watch() or sys.executable
            r = subprocess.run([py, str(script), "--drain"],
                               cwd=str(script.parent), capture_output=True, text=True,
                               timeout=120, env=_today_watch_env())
            tail = ((r.stdout or "") + (r.stderr or "")).strip()[-200:]
            _log(log_path, f"飞书推送drain rc={r.returncode} {tail}")
        except Exception as exc:  # noqa: BLE001
            _log(log_path, f"飞书推送drain异常：{type(exc).__name__}: {exc}")
        finally:
            _feishu_drain_busy = False

    threading.Thread(target=_run, name="feishu-push-drain", daemon=True).start()


def _feishu_failure(root: Path, log_path: Path, trigger: str, error: str) -> None:
    """报告生成失败：入队红色故障卡并触发补发检查。"""
    _feishu_enqueue(root, log_path, ["--failure", "--trigger", trigger or "",
                                     "--error", error])
    _feishu_drain(root, log_path)


def _run_survivor_report(root: Path, log_path: Path, trigger: str = "") -> None:
    """切片重建成功后生成生存者半小时报告（失败只进日志，绝不影响数据更新判定）。

    - 生成器自身按切片 built_at 幂等：同 asof 不重写报告
    - 产物：<survivor_dir>/reports/survivor_*.md/json + latest.md/json（网页读它）
    - 会话标签由切片 meta 自带：盘中 30m 档=INTRADAY、午盘=AM、晚间全量=PM/EOD
    - 解释器需要 duckdb（读 nas_source.duckdb 桥接历史），复用 _python_for_today_watch 探测
    - BAZI_SURVIVOR_REPORT=0 可禁用；BAZI_SURVIVOR_FEISHU=0 可单独禁用飞书推送
    - 生成成功/失败后都会入队飞书事件（2026-11-20）：入队纯本地秒回，
      发送在后台 drain 线程，绝不拖延打包
    """
    sdir = _survivor_dir(root)
    script = sdir / "scripts" / "sr_gen_report.py"
    disabled = os.environ.get("BAZI_SURVIVOR_REPORT", "1") == "0"
    if not survivor_report_actions(disabled, script.is_file()):
        reason = "disabled" if disabled else f"脚本缺失 {script}"
        _log(log_path, f"生存者报告：跳过（{reason}）")
        if not disabled:
            # 显式禁用不告警；脚本缺失属结构性故障，按计划发红色故障卡
            try:
                _feishu_failure(root, log_path, trigger, reason)
            except Exception as exc:  # noqa: BLE001
                _log(log_path, f"飞书推送路径异常（不影响主流程）：{type(exc).__name__}: {exc}")
        return
    py = _python_for_today_watch()
    if not py:
        reason = "未找到含 duckdb 的 Python 解释器"
        _log(log_path, f"生存者报告：{reason}，跳过")
        try:
            _feishu_failure(root, log_path, trigger, reason)
        except Exception as exc:  # noqa: BLE001
            _log(log_path, f"飞书推送路径异常（不影响主流程）：{type(exc).__name__}: {exc}")
        return
    gen_failed = False
    info = {}
    tail = ""
    try:
        r = subprocess.run([py, str(script)], cwd=str(sdir), env=_today_watch_env(),
                           capture_output=True, text=True, timeout=300)
        tail = ((r.stdout or "") + (r.stderr or "")).strip()[-400:]
        _log(log_path, f"生存者报告 rc={r.returncode} {tail[-300:]}")
        gen_failed = r.returncode != 0
        if not gen_failed:
            info = _parse_last_json(r.stdout)
        elif not tail:
            tail = f"rc={r.returncode}"
    except subprocess.TimeoutExpired:
        tail = "生成器超时 300s"
        _log(log_path, f"生存者报告：{tail}")
        gen_failed = True
    except Exception as exc:  # noqa: BLE001
        tail = f"{type(exc).__name__}: {exc}"
        _log(log_path, f"生存者报告异常：{tail}")
        gen_failed = True
    # 模拟盘定稿：PM/EOD 归档落档后固化次日计划（读 info["json"] 指向的归档，
    # 不读 latest.json）。失败只告警，不影响数据更新/报告/打包主链。
    if paper_engine is not None and trigger == "PM" and not gen_failed:
        try:
            paper_res = paper_engine.finalize_from_report(info)
            _log(log_path, f"模拟盘定稿：{paper_res.get('status')} "
                           f"{paper_res.get('plan_id') or ''}")
        except Exception as exc:  # noqa: BLE001
            _log(log_path, f"模拟盘定稿异常（不影响主流程）：{type(exc).__name__}: {exc}")
    # 模拟盘盘中试探：INTRADAY 档在连续竞价时段出现 B8 新买信号 → 1/3 试探
    # （报告 S3 打法：盘中试探、收盘确认后由 PM 计划补满）
    if paper_engine is not None and trigger == "INTRADAY" and not gen_failed:
        try:
            probe_res = paper_engine.finalize_intraday(info)
            _log(log_path, f"模拟盘盘中试探：{probe_res.get('status')} "
                           f"{probe_res.get('plan_id') or ''}")
        except Exception as exc:  # noqa: BLE001
            _log(log_path, f"模拟盘盘中试探异常（不影响主流程）：{type(exc).__name__}: {exc}")
    # 飞书推送独立兜底：推送路径任何问题都不影响主流程（打包照常进行）
    try:
        if gen_failed:
            _feishu_failure(root, log_path, trigger, tail or "生成失败")
        elif info.get("skipped"):
            # 幂等跳过：不重复推送，只补发历史 pending（含上次发送失败的报告）
            _feishu_drain(root, log_path)
        elif info.get("json"):
            _feishu_enqueue(root, log_path,
                            ["--report", str(info["json"]), "--trigger", trigger,
                             "--built-at", str(info.get("built_at") or "")])
            _feishu_drain(root, log_path)
        else:
            _log(log_path, "飞书推送：生成器输出无报告路径，跳过入队")
    except Exception as exc:  # noqa: BLE001
        _log(log_path, f"飞书推送路径异常（不影响主流程）：{type(exc).__name__}: {exc}")


def _paper_tick_before_package(root: Path, log_path: Path, market_as_of=None) -> None:
    """打包前跑一次模拟盘 tick：本档的执行/持仓快照/收盘净值与报告同包下发。

    tick 自带分钟防重，主循环稍后再调只会 idle；失败只记日志，绝不拦包。
    market_as_of 来自行情管道，页面据此区分行情截止与快照刷新时间。
    """
    if paper_engine is None:
        return
    try:
        paper_engine.tick(_now())
        # 本地账本：每档报告后立刻按本档最新切片重估，页面就能看到最新盈亏
        paper_engine.snapshot_now("slot", market_as_of=market_as_of)
    except Exception as exc:  # noqa: BLE001
        _log(log_path, f"模拟盘 tick（打包前）异常（不影响主流程）："
                       f"{type(exc).__name__}: {exc}")


def _run_package(root: Path, log_path: Path, intra: bool = False) -> None:
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
            cmd = [sys.executable, str(script)]
            if intra:
                cmd.append("--intraday")
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
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
                # 顺序：先刷 60m、再出报告、最后打包——包里有 live_60m.parquet 和
                # 生存者报告，顺序反了就会把旧文件打进去（2026-11-20 报告随包下发）。
                _run_sync_live_60m(root, py, log_path)
                _run_survivor_report(root, log_path, "PM")
                _paper_tick_before_package(root, log_path,
                                           market_as_of=f"{target} 15:00" if target else None)
                _run_package(root, log_path)
    except subprocess.TimeoutExpired:
        _log(log_path, "子进程超时（900s）")
    except Exception as exc:  # noqa: BLE001
        _log(log_path, f"异常：{type(exc).__name__}: {exc}")
    _save_state(state_path, state)
    return state


def _run_am_once(root: Path, state: dict, state_path: Path) -> dict:
    """午盘半日更新：先从 NAS 同步 60m，再用 2026.zip + 当日 60m(h=1,2) 合成 AM 切片并打包。

    注意：午盘不跑 _run_today_watch，避免用半日价格污染今日关注前瞻台账。
    """
    log_path = root / "data" / "live_daily.log"
    py = _python_for_pull(log_path)
    scripts = _scripts_dir(root)
    build = scripts / "build_live_slice.py"
    state = dict(state)
    state["am_attempts"] = int(state.get("am_attempts") or 0) + 1
    state["am_last_attempt_at"] = _now().isoformat()
    _log(log_path, f"AM attempt {state['am_attempts']}/{len(AM_SLOTS)} 开始："
                   f"sync_live_60m.py + {build.name} --overlay-live60")
    try:
        _run_sync_live_60m(root, py, log_path)
        r2 = subprocess.run([py, str(build), "--overlay-live60"],
                            capture_output=True, text=True, timeout=900)
        if r2.returncode != 0:
            _log(log_path, f"AM build rc={r2.returncode} err={r2.stderr.strip()[-400:]}")
        else:
            out_info = {}
            try:
                out_info = json.loads(r2.stdout.strip() or "{}")
            except ValueError:
                pass
            target = str(out_info.get("latest") or "")
            state["am_done"] = True
            if target:
                state["am_last_target"] = target
            _log(log_path, f"成功：午盘当月层已更新到 {target} (AM)")
            # 先出报告再打包：包里有生存者报告，顺序反了就会打进上一档的旧报告
            _run_survivor_report(root, log_path, "AM")
            _paper_tick_before_package(root, log_path,
                                       market_as_of=f"{target} 11:30" if target else None)
            _run_package(root, log_path)
    except subprocess.TimeoutExpired:
        _log(log_path, "AM 子进程超时（900s）")
    except Exception as exc:  # noqa: BLE001
        _log(log_path, f"AM 异常：{type(exc).__name__}: {exc}")
    _save_state(state_path, state)
    return state


def _run_intra_once(root: Path, state: dict, state_path: Path) -> dict:
    """盘中 30 分钟更新：同步 30m → 合成当日临时日K → 出包。

    - 最新 30m 柱未前进时只记档、不重建不出包（避免每档改 sha 让客户重下）
    - 不跑 today_watch
    """
    log_path = root / "data" / "live_daily.log"
    py = _python_for_pull(log_path)
    scripts = _scripts_dir(root)
    build = scripts / "build_live_slice.py"
    state = dict(state)
    now = _now()
    done = list(state.get("intra_slots_done") or [])
    for h, m in INTRA_SLOTS:
        slot = f"{h:02d}:{m:02d}"
        if now >= now.replace(hour=h, minute=m, second=0, microsecond=0) and slot not in done:
            done.append(slot)
    state["intra_slots_done"] = done
    state["intra_last_attempt_at"] = now.isoformat()
    try:
        meta30 = _run_sync_live_30m(root, py, log_path)
        asof_time = str(meta30.get("asof_time") or "")
        if not asof_time:
            _log(log_path, "盘中30m：同步未拿到 asof_time，跳过建库")
            _save_state(state_path, state)
            return state
        if str(meta30.get("asof") or "") != now.strftime("%F"):
            _log(log_path, f"盘中30m：最新数据日 {meta30.get('asof')} 不是今天（休市），跳过建库")
            _save_state(state_path, state)
            return state
        if asof_time == (state.get("intra_last_bar") or ""):
            _log(log_path, f"盘中30m：最新柱仍为 {asof_time}，不重建")
            _save_state(state_path, state)
            return state
        r2 = subprocess.run([py, str(build), "--overlay-live30"],
                            capture_output=True, text=True, timeout=900)
        if r2.returncode != 0:
            _log(log_path, f"盘中30m build rc={r2.returncode} err={r2.stderr.strip()[-400:]}")
        else:
            out_info = {}
            try:
                out_info = json.loads(r2.stdout.strip() or "{}")
            except ValueError:
                pass
            target = str(out_info.get("latest") or "")
            state["intra_last_bar"] = asof_time
            state["intra_last_target"] = target
            _log(log_path, f"盘中30m：当月层已更新到 {target} {asof_time} (INTRADAY)")
            # 先出报告再打包：包里带当天各档报告，顺序反了就会打进上一档的旧报告
            _run_survivor_report(root, log_path, "INTRADAY")
            quote_date = str(meta30.get("asof") or target or "")
            _paper_tick_before_package(
                root, log_path,
                market_as_of=f"{quote_date} {asof_time}" if quote_date else None)
            _run_package(root, log_path, intra=True)
    except subprocess.TimeoutExpired:
        _log(log_path, "盘中30m 子进程超时（900s）")
    except Exception as exc:  # noqa: BLE001
        _log(log_path, f"盘中30m 异常：{type(exc).__name__}: {exc}")
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


def _publisher_role(root: Path) -> bool:
    """发布端 = 有 exports/update_pkgs/latest.json（客户机永远不会有，更新器不覆盖 exports/）。"""
    env = os.environ.get("BAZI_ROLE", "").strip().lower()
    if env == "publisher":
        return True
    if env == "client":
        return False
    return (root.parent / "exports" / "update_pkgs" / "latest.json").is_file()


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
    if not _publisher_role(root):
        _log(log_path, "非发布端角色（无 exports/update_pkgs/latest.json），"
                       "日线/盘中流水线不启动；数据由更新器拉取")
        return {"started": True, "role": "client", "slots": [],
                "am_slots": [], "intra_slots": []}

    def loop():
        nonlocal owned
        last_feishu_drain = 0.0
        state = _load_state(state_path)
        if state.get("date") != _now().strftime("%F"):
            state = {"date": _now().strftime("%F"), "attempts": 0, "done": False,
                     "last_target": state.get("last_target") or "",
                     "am_attempts": 0, "am_done": False,
                     "am_last_target": state.get("am_last_target") or "",
                     "intra_slots_done": [], "intra_last_bar": ""}
            _save_state(state_path, state)
        _log(log_path, f"日更线程启动（盘中30m {[f'{h:02d}:{m:02d}' for h, m in INTRA_SLOTS]} / "
                       f"午盘 {[f'{h:02d}:{m:02d}' for h, m in AM_SLOTS]} / "
                       f"晚间 19:05/19:40/20:20/21:00；上次目标 "
                       f"{state.get('last_target') or '—'}）")
        while True:
            try:
                now = _now()
                today = now.strftime("%F")
                with _state_lock:
                    cur = _load_state(state_path) or state
                    if cur.get("date") != today:
                        cur = {"date": today, "attempts": 0, "done": False,
                               "last_target": cur.get("last_target") or "",
                               "am_attempts": 0, "am_done": False,
                               "am_last_target": cur.get("am_last_target") or "",
                               "intra_slots_done": [], "intra_last_bar": ""}
                        _save_state(state_path, cur)
                    if now.weekday() < 5:
                        if next_intra_action(cur, now) == "run":
                            if owned or _acquire_lock(lock_path, log_path):
                                owned = True
                                cur = _run_intra_once(root, cur, state_path)
                        elif next_am_action(cur, now) == "run":
                            if owned or _acquire_lock(lock_path, log_path):
                                owned = True
                                cur = _run_am_once(root, cur, state_path)
                        elif next_action(cur, now) == "run":
                            if owned or _acquire_lock(lock_path, log_path):
                                owned = True
                                cur = _run_once(root, cur, state_path)
                # 模拟盘：独立状态/独立锁；只读交易日内历（不绑 weekday），tick 内部
                # 按分钟防重（09:35 执行 / 30 分钟回读 / 15:12 收盘快照）
                if paper_engine is not None:
                    try:
                        paper_engine.tick(now)
                    except Exception as exc:  # noqa: BLE001
                        _log(log_path, f"模拟盘 tick 异常（不影响主流程）："
                                       f"{type(exc).__name__}: {exc}")
                # 飞书 pending 补发：每 5 分钟一次（含非交易时段，覆盖断网恢复）
                if time.time() - last_feishu_drain >= 300:
                    last_feishu_drain = time.time()
                    _feishu_drain(root, log_path)
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
    return {"started": True,
            "am_slots": [f"{h:02d}:{m:02d}" for h, m in AM_SLOTS],
            "slots": [f"{h:02d}:{m:02d}" for h, m in SLOTS]}


if __name__ == "__main__":
    _root = Path(__file__).resolve().parents[1]
    _state_path = _root / "data" / "live_daily_state.json"
    _cur = _load_state(_state_path)
    if "--intra-now" in sys.argv:
        _res = _run_intra_once(_root, _cur, _state_path)
        print(json.dumps(_res, ensure_ascii=False, indent=2))
    elif "--am-now" in sys.argv:
        _res = _run_am_once(_root, _cur, _state_path)
        print(json.dumps(_res, ensure_ascii=False, indent=2))
    elif "--pm-now" in sys.argv:
        _res = _run_once(_root, _cur, _state_path)
        print(json.dumps(_res, ensure_ascii=False, indent=2))
    else:
        print("用法: python3 daily_live_job.py [--intra-now | --am-now | --pm-now]", file=sys.stderr)
        raise SystemExit(2)
