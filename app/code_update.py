"""客户机「代码更新」：版本号徽章一键拉取 GitHub 最新代码。

与数据日更（update_center.py）互补：日更走更新服务器下载数据包，不动代码；
这里走 git 拉代码，不动数据。页面顶栏的版本号徽章是唯一入口。

边界（红线）：
  * 只对既定 origin/main 做 fetch 与 --ff-only 合并，绝不接受任意 URL/分支/命令参数；
  * 绝不执行 reset / clean / checkout -- 等破坏性命令；任何失败旧代码原样保留，
    服务照常跑（merge --ff-only 失败时 HEAD 不动，这正是它比 git pull 稳的原因）；
  * 不做自重启：拉取成功后面板提示人用桌面脚本手动关闭再开启，新代码才生效；
  * 本机不是 git 安装（老客户包 / 沙箱副本）时如实降级，绝不假装能更新。

为什么记录 RUNNING_HEAD：服务进程启动那一刻的 HEAD。拉取成功后磁盘上的 HEAD
前移而进程内存里还是旧代码——徽章据此显示「待重启生效」，人重启后自然归位。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import config
import update_center

BJT = timezone(timedelta(hours=8))
#: 仓库根（客户机 = 含 .git 的 bazi_month_relation_tengod_dev_v1 那层）。
#: 测试用 set_root() 指到临时仓库，绝不碰真仓。
REPO_DIR = Path(os.environ.get("BAZI_CODE_REPO",
                               str(Path(__file__).resolve().parents[1]))).resolve()
#: 查询包根（客户机 = 仓库上一层）：publisher 判定与《客户机更新说明.md》同步目标。
ROOT = Path(os.environ.get("BAZI_QUERY_ROOT", str(config.ROOT))).resolve()
DOC_NAME = "客户机更新说明.md"

#: 页面每 60s 轮询一次状态，10 分钟内不重复 fetch（点徽章可 force）。
FETCH_TTL_SECONDS = 600
FETCH_TIMEOUT_SECONDS = 30
#: 月度包日更能占 60 分钟，代码合并只会被慢网拖住；5 分钟足够，超时按失败处理。
MERGE_TIMEOUT_SECONDS = 300

_KEEP = object()  # set_root() 的哨兵：不传 running_head 就保留进程启动时冻结的那个


class GitError(Exception):
    """git 命令失败（含超时 / 没有 git）。message 是给人看的一句话。"""


def _clean_err(raw: str) -> str:
    """git 的报错取最后一行非空内容，够人看懂就行，别把整段 stderr 摆到按钮上。"""
    lines = [ln.strip() for ln in (raw or "").splitlines() if ln.strip()]
    return (lines[-1][:200] if lines else "git 命令失败")


#: 测试注入缝隙：给定 git 参数列表，返回 (rc, stdout, stderr)。None = 走真 git。
_GIT_RUNNER: Callable[[list[str]], tuple[int, str, str]] | None = None


def _run_git(args: list[str], timeout: float = FETCH_TIMEOUT_SECONDS) -> str:
    """跑一条 git 命令，返回 stdout（strip 后）。失败抛 GitError。"""
    if _GIT_RUNNER is not None:  # 测试：假 git 记录 argv、按剧本回话
        rc, out, err = _GIT_RUNNER(list(args))
        if rc != 0:
            raise GitError(_clean_err(err or out or f"git {' '.join(args)} 退出码 {rc}"))
        return (out or "").strip()
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"            # Windows 控制台默认 cp936，强制子进程说 UTF-8
    env["PYTHONIOENCODING"] = "utf-8"
    env["GIT_TERMINAL_PROMPT"] = "0"   # 绝不卡在交互输凭据：缺凭据要快速失败，不能挂住 HTTP 线程
    # SSH 认证失败同样要立刻报错；客户机自己配了 GIT_SSH_COMMAND 就尊重它的。
    env.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes")
    cmd = ["git", "-C", str(REPO_DIR)] + args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, env=env)
    except FileNotFoundError:
        raise GitError("本机没有 git 命令，请按《客户机更新说明》联系开发方")
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} 超过 {int(timeout)} 秒没有结束，已停止等待")
    if proc.returncode != 0:
        raise GitError(_clean_err(proc.stderr or proc.stdout
                                  or f"git {' '.join(args)} 退出码 {proc.returncode}"))
    return (proc.stdout or "").strip()


def _head_at_import() -> str | None:
    """进程启动那一刻的 HEAD。失败（无 git / 非 git 目录）就 None，不影响导入。"""
    try:
        return _run_git(["rev-parse", "--short", "HEAD"])
    except Exception:  # noqa: BLE001 —— 任何意外都不能挡住模块导入
        return None


RUNNING_HEAD = _head_at_import()

_STATE = threading.RLock()   # 保护 _UPDATING / _LAST / 缓存读写（快进快出）
_FETCH_LOCK = threading.RLock()  # 保护 git 网络操作（fetch/merge）串行，不并发碰 refs
_UPDATING = False            # 页面按钮自己拉起的那一轮
_CACHE: dict | None = None   # 上一次完整检测的结果（fetch + 盘点）
_CACHE_AT = 0.0              # epoch 秒
_UNAVAILABLE_REASON: str | None = None
_LAST: dict = {"result": None, "message": None, "finished_at_bjt": None}


def _now_bjt() -> datetime:
    return datetime.now(BJT)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _set_last(result: str, message: str) -> None:
    with _STATE:
        _LAST.update({"result": result, "message": message,
                      "finished_at_bjt": _iso(_now_bjt())})


def set_root(repo_dir=None, git_runner=None, query_root=None, running_head=_KEEP) -> dict:
    """把仓库根/查询包根指到别处（仅自测）：临时目录里放假 git，绝不碰生产仓库。"""
    global REPO_DIR, ROOT, _GIT_RUNNER, _CACHE, _CACHE_AT, _UNAVAILABLE_REASON, RUNNING_HEAD
    if repo_dir is not None:
        REPO_DIR = Path(repo_dir).resolve()
    if query_root is not None:
        ROOT = Path(query_root).resolve()
    _GIT_RUNNER = git_runner
    with _STATE:
        _UPDATING = False
        _CACHE, _CACHE_AT = None, 0.0
        _LAST.update({"result": None, "message": None, "finished_at_bjt": None})
    _UNAVAILABLE_REASON = None
    if running_head is not _KEEP:
        RUNNING_HEAD = running_head
    return {"repo_dir": str(REPO_DIR), "root": str(ROOT)}


def _git_available() -> bool:
    """本机代码是不是 git 安装，且 git 命令可用。原因写进 _UNAVAILABLE_REASON。"""
    global _UNAVAILABLE_REASON
    if not (REPO_DIR / ".git").exists():
        _UNAVAILABLE_REASON = "本机代码不是 git 安装（没有 .git），请按《客户机更新说明》更新"
        return False
    if shutil.which("git") is None:
        _UNAVAILABLE_REASON = "本机没有 git 命令，请按《客户机更新说明》联系开发方"
        return False
    _UNAVAILABLE_REASON = None
    return True


def _data_update_running() -> bool:
    """数据日更有没有一轮在跑：跑着时不能换代码，防止替换正在执行的 client_update.py。"""
    try:
        return bool(update_center.status().get("running"))
    except Exception:  # noqa: BLE001 —— 日更状态读不到时按「没在跑」处理，不牵连代码更新
        return False


_CACHE_KEYS = ("fetch_ok", "behind", "local_head", "remote_head",
               "remote_subject", "dirty", "checked_at_bjt")


def _detect_locked() -> dict:
    """一次完整检测（fetch 远端 + 盘点本地）。调用方必须已持有 _FETCH_LOCK。

    fetch 失败不代表本地盘点也失败：dirty / 本地 HEAD 不依赖网络，照常尽力返回。
    """
    out: dict = {"fetch_ok": False, "behind": None, "local_head": None,
                 "remote_head": None, "remote_subject": None, "dirty": False,
                 "checked_at_bjt": _iso(_now_bjt())}
    try:
        _run_git(["fetch", "origin", "main"])
        out["fetch_ok"] = True
        out["local_head"] = _run_git(["rev-parse", "--short", "HEAD"])
        out["remote_head"] = _run_git(["rev-parse", "--short", "origin/main"])
        raw = _run_git(["rev-list", "--count", "HEAD..origin/main"])
        out["behind"] = int(raw) if raw.isdigit() else None
        out["remote_subject"] = _run_git(["log", "-1", "--format=%s", "origin/main"])
    except GitError as exc:
        out["detect_error"] = str(exc)
    try:
        out["local_head"] = _run_git(["rev-parse", "--short", "HEAD"])
        out["dirty"] = bool(_run_git(["status", "--porcelain"]))
    except GitError:
        pass
    return out


def _restarted(local_head) -> bool:
    """磁盘 HEAD 是否已领先于进程内存里的代码（拉取成功但还没重启）。"""
    return bool(RUNNING_HEAD and local_head and RUNNING_HEAD != local_head)


def status(force: bool = False) -> dict:
    """版本徽章的状态接口。绝不抛异常：git 坏了就如实降级，页面照常。"""
    global _CACHE, _CACHE_AT
    publisher = (ROOT / "exports" / "update_pkgs" / "latest.json").is_file()
    st: dict = {
        "available": _git_available(),
        "reason": _UNAVAILABLE_REASON,
        "role": "publisher" if publisher else "client",
        "fetch_ok": None, "behind": None, "local_head": None,
        "remote_head": None, "remote_subject": None,
        "dirty": False, "updating": False, "restarting_needed": False,
        "checked_at_bjt": None,
        "last_result": _LAST["result"], "last_message": _LAST["message"],
        "last_finished_bjt": _LAST["finished_at_bjt"],
    }
    if not st["available"]:
        return st
    with _STATE:
        st["updating"] = _UPDATING
    if st["updating"]:
        # 合并进行中：不并发碰 refs，回最近一次缓存即可
        with _STATE:
            if _CACHE:
                st.update({k: _CACHE.get(k) for k in _CACHE_KEYS})
        st["restarting_needed"] = _restarted(st.get("local_head"))
        return st
    with _STATE:
        cache = dict(_CACHE) if _CACHE else None
        cache_at = _CACHE_AT
    if force or cache is None or (time.time() - cache_at) >= FETCH_TTL_SECONDS:
        with _FETCH_LOCK:
            # 双检：排到锁时可能别的线程刚刷完（页面多标签同时轮询很常见）
            with _STATE:
                cache = dict(_CACHE) if _CACHE else None
                cache_at = _CACHE_AT
            if force or cache is None or (time.time() - cache_at) >= FETCH_TTL_SECONDS:
                cache = _detect_locked()
                with _STATE:
                    _CACHE, _CACHE_AT = dict(cache), time.time()
    if cache:
        st.update({k: cache.get(k) for k in _CACHE_KEYS})
    st["restarting_needed"] = _restarted(st.get("local_head"))
    return st


def _sync_doc() -> None:
    """拉取成功后把仓库里最新的《客户机更新说明.md》同步到查询包根（仿手工流程 2.2）。

    同步失败不影响代码更新本身——说明文件晚一点也无所谓，绝不为它回滚任何东西。
    """
    src, dst = REPO_DIR / DOC_NAME, ROOT / DOC_NAME
    try:
        if src.is_file() and dst.parent.is_dir() and src.resolve() != dst.resolve():
            shutil.copyfile(src, dst)
    except OSError:
        pass


def _worker() -> None:
    global _UPDATING, _CACHE, _CACHE_AT
    result, message = "failed", "更新没有回报结果"
    try:
        with _FETCH_LOCK:  # 与 status() 的检测串行；merge 期间页面轮询看到 updating=True 不会来抢
            _run_git(["fetch", "origin", "main"])
            raw = _run_git(["rev-list", "--count", "HEAD..origin/main"])
            behind = int(raw) if raw.isdigit() else 0
            if behind <= 0:
                result, message = "current", "已是最新，无需更新"
            else:
                _run_git(["merge", "--ff-only", "origin/main"],
                         timeout=MERGE_TIMEOUT_SECONDS)
                head = _run_git(["rev-parse", "--short", "HEAD"])
                result = "updated"
                message = f"代码已更新到 {head}（前进了 {behind} 个提交）"
                _sync_doc()
    except GitError as exc:
        result, message = "failed", str(exc)
    except Exception as exc:  # noqa: BLE001 —— 按钮面绝不能把异常抛给请求线程
        result, message = "failed", f"更新异常：{type(exc).__name__}"
    finally:
        _set_last(result, message)
        with _STATE:
            _UPDATING = False
            _CACHE, _CACHE_AT = None, 0.0  # 缓存作废：下次 status() 重新盘点合并后的 HEAD


def start_update() -> tuple[str, dict]:
    """启动一轮代码更新。立即返回，结果由 status() 轮询。"""
    global _UPDATING
    if (ROOT / "exports" / "update_pkgs" / "latest.json").is_file():
        return "PUBLISHER", {"error": "PUBLISHER",
                             "message": "本机是数据发布端，代码更新由开发方完成"}
    if not _git_available():
        return "NOT_GIT", {"error": "NOT_GIT",
                           "message": _UNAVAILABLE_REASON or "本机代码不是 git 安装"}
    if _data_update_running():
        return "DATA_UPDATE_RUNNING", {"error": "DATA_UPDATE_RUNNING",
                                       "message": "数据日更正在进行，等它完成再更新代码"}
    try:
        dirty = bool(_run_git(["status", "--porcelain"]))
    except GitError as exc:
        return "GIT_BROKEN", {"error": "GIT_BROKEN",
                              "message": f"git 状态读取失败：{exc}"}
    if dirty:
        return "DIRTY_WORKTREE", {"error": "DIRTY_WORKTREE",
                                  "message": "代码目录有本地改动，不能自动更新"
                                             "（绝不自动丢弃改动），请联系开发方"}
    with _STATE:
        if _UPDATING:
            return "ALREADY_RUNNING", {"error": "ALREADY_RUNNING",
                                       "message": "已有一轮代码更新在跑，请稍候"}
        _UPDATING = True
    _set_last("running", "正在拉取最新代码…")
    threading.Thread(target=_worker, name="code-update", daemon=True).start()
    return "STARTED", {"started": True, "started_at_bjt": _iso(_now_bjt())}
