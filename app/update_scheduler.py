"""节气月自动更新的调度器骨架 + 状态口径（T1）。

边界（红线，务必守住）：
  * 本模块**从不**写/建/替换任何 `.sqlite`，从不联网、从不碰 NAS；
  * 它只做四件事：文件锁、计划持久化、到点判定（纯只读比较）、以 subprocess 拉起
    入账入口；流水线本体属于 T2；
  * **入账语义（2026-10-22 用户裁定）**：交节之后入账的是「刚过完的上一个月」，月没走完
    不许入账。所以自动路径不再按「本流月首个交易日」判该跑，而是：
      ① 只读取 `monthly_facts` 里有数据的最后流月 `M_last`；
      ② 取 `calendar_months` 里 `M_last` 的下一个流月 `M_next`（待入账月）；
      ③ 触发条件 = `now_bjt > M_next.end_at` **且** `tools/post_month.py` 的
         `can_post_month(M_next)` 三项 check 全过；
      ④ 拉起的是 `python3 tools/post_month.py --month <M_next> --mode staging`
         （**永远不含 promote**；staging 只写 `work/post/<month>/`，不碰正式库）。
    旧语义（按 `target_first_trade_date` 判）只在**显式注入 term** 的桩缝里保留，
    生产两个调用点（tick_once / db_status）都不传 term；见 `evaluate_trigger()`。
  * 一切「今天 / 当前 / 20:00 到没到」判断都走 `bjtime`（固定 UTC+8 北京口径），
    本机系统时区（America/Los_Angeles）一律不参与业务判断；
  * 调度线程 `daemon=True`，整条循环 try/except 包住：任何异常只登记进状态/日志，
    绝不拖垮请求面（HTTP 服务照常 200）。

文件（默认在包根 `work/`，可用 `BAZI_WORK_DIR` 指到别处做隔离自测）：
  * `work/update.lock`            —— 互斥锁（原子 O_EXCL 创建，内容 json）
  * `work/update.disabled`        —— 停用哨兵：存在即调度不触发、手动端点 503
  * `work/update_plan.json`       —— 本流月计划与尝试计数（**调度器是唯一写者**）
  * `work/update_state.json`      —— 当前/最近一轮进度（拉起方写初态，之后归流水线）
  * `work/db_fingerprints.json`   —— 各库指纹权威来源（流水线跑完写入；缺失时派生弱指纹）
  * `work/update_last_decision.json` —— 最近一次判定快照（调度器唯一写者，供状态端点读）

手动触发是**两段式**（用户裁定）：`preview_update()` 只做只读检查并签发一枚 10 分钟内有效、
绑定当时状态范围哈希的 `preview_token`（令牌只存本进程内存，不落盘），`manual_start()` 必须
带这枚令牌才允许拉起；自动路径不受影响——五道闸全过就直接拉起，不等第二次确认。
两条路径拉起的子进程都默认 `--dry-run`；真跑要 `work/execute.confirmed`，本模块从不创建它。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from decimal import getcontext
from pathlib import Path

import api
import bjtime
import config

PKG_ROOT = Path(__file__).resolve().parents[1]
#: configure() 可重绑的实际包根；拉起流水线时要按它推导三根，不许让子进程缺省回落。
BASE_ROOT = PKG_ROOT
WORK = Path(os.environ.get("BAZI_WORK_DIR", str(PKG_ROOT / "work"))).resolve()
LOGS = PKG_ROOT / "logs"
REPORTS = PKG_ROOT / "reports"
TOOLS = PKG_ROOT / "tools"
# 本地只读镜像根（不联网、不连 NAS）：可用 BAZI_SOURCE_MIRROR_ROOT 指到临时副本做自测。
# 缺省指向项目根下已并入的 source_mirror（只读 JSON 的读取），
# 本调度器从不往这里写一个字节。
DOCS_ROOT = Path(os.environ.get("BAZI_DOCS_ROOT", str(PKG_ROOT.parent)))
MIRROR_ROOT = Path(os.environ.get("BAZI_SOURCE_MIRROR_ROOT",
                                  str(DOCS_ROOT / "source_mirror")))
DAILY_DIR = MIRROR_ROOT / "正式源" / "全A日K_合并版"
SOURCE_MANIFEST = MIRROR_ROOT / "source_manifest.json"
SOURCE_README = DAILY_DIR / "README.json"
SOURCE_MERGE_REPORT = DAILY_DIR / "merge_report.json"
# 2026-11-10：NAS 侧 README 永远停在 2026-10-15，月更门禁会被它锁死。
# 优先读「日更真实产物」（内容字段，不看 mtime）：拉取清单 / 夜间就绪 / 当月层 meta。
LIVE_MANIFEST = DOCS_ROOT / "source_mirror_live" / "fetch_manifest.json"
LIVE_READY = DOCS_ROOT / "source_mirror_live" / "nightly_data_ready.json"
LIVE_SLICE = DOCS_ROOT / "source_input" / "live" / "current_month.sqlite"
SHA_ARTIFACT_NAME = "2026.zip"          # source_sha12 取这一只年度包的 sha256 前 12 位

LOCK_FILE = WORK / "update.lock"
DISABLED_FILE = WORK / "update.disabled"
PLAN_FILE = WORK / "update_plan.json"
STATE_FILE = WORK / "update_state.json"
FINGERPRINT_FILE = WORK / "db_fingerprints.json"
DECISION_FILE = WORK / "update_last_decision.json"
SOURCE_BASELINE_FILE = WORK / "source_publication_baseline.json"
RECORD_MD = REPORTS / "更新记录.md"
PIPELINE_ENTRY = TOOLS / "run_monthly_update.py"
#: 自动入账入口（2026-10-22 裁定）：只以 `--mode staging` 拉起，**永远不含 promote**。
#: 入口跟随 `TOOLS` 重绑，自测可以用替身脚本记录 argv（绝不跑真的 staging/promote）。
POST_MONTH_ENTRY = TOOLS / "post_month.py"
#: 待入账月解析结果的短缓存（`monthly_facts` 的「最后有数据流月」查询实测 ~0.16s，
#: 而 /api/db_status 会被页面轮询，不能每次请求都全表排序）。库真被换掉时最多滞后 60 秒，
#: 与指纹缓存同一量级的容忍度；测试可 `refresh=True` 绕过。
PENDING_CACHE_TTL_SECONDS = 60

TICK_SECONDS = 60                 # 每 60 秒醒一次，只做纯只读比较
CATCHUP_DELAY_SECONDS = 30        # 进程启动后延迟 30 秒做一次补跑检查
TRIGGER_HOUR_BJT = 20             # 触发时刻：首个交易日北京时间 20:00
MAX_ATTEMPTS_PER_MONTH = 3        # 同一节气月累计尝试上限（含源未就绪）
# 陈旧锁回收口径（2026-10-22 返工裁定）：**锁里 pid 已死即回收**，不再等 mtime 满 36 小时
# （真出故障时人等一天半不可接受）。为挡「pid 被复用」这个坑，锁里额外记录持锁进程的启动
# 时刻，回收前比对：启动时刻对不上 ⇒ 这个 pid 已换了主人 ⇒ 原持锁者同样已死，照旧回收。
PROC_START_TOLERANCE_SECONDS = 180
LOG_TAIL_LINES = 40
# 调度器**自身**的构建串。它不是关系引擎的规则版本，绝不能冒充 `rule_version`（G-E 比的是
# 引擎口径；冒充会让 G-E 永远「一致」，等于把那道闸关掉）。引擎口径见 READ 侧函数
# `read_relation_rule_version()`。
# 2026-10-22 入账语义改造把它升了一位：plan 行按构建串换代会重建（换月即重置 attempts 的
# 同一机制）。旧语义在 10-22 把 M09 误判成「该跑」并记了 attempts=1；那一次发生在月没走完
# 之前，不该占新语义的 3 次额度。换代后首 tick 会重建 M09 的 plan（attempts/manual 归零），
# 这是**有意**的一次性重置，不是把计数藏起来。
SCHEDULER_BUILD = RULE_VERSION = "T1-sched-2026.10.23-postmonth"
# G-E 比较用的引擎规则版本缺省锁定串（与 T2 `RULE_VERSION_DEFAULT` 同值）；
# 真实值以主库 `source_versions` 影子跑批那行为准，取不到 ⇒ null ⇒ G-E 保守拒跑。
RULE_LOCK_DEFAULT = "bazi-relation-research-v1.3.0-provisional"
RUN_ID_RE = re.compile(r"^MU-\d{8}T\d{6}$")   # 与 T2 编排器同规格：文件名里不许有冒号
MIN_FREE_BYTES = 40 * 1024 ** 3               # G_D 卷余量下限（**缺省值**；有编排器时读它的 min_free_bytes()）
PREVIEW_TOKEN_TTL_SECONDS = 10 * 60           # 两段式确认：预览结论 10 分钟内有效
# 预计耗时口径（2026-10-22 fixround3 如实更正）：这是**常量、未实测**——本机从来没有过一次
# 走完的真跑，所以没有任何实测耗时可引。取 60 分钟的依据是负责人裁定的口径：
# 追加引擎四项（FACT_APPEND_SPECS）尚未接线 ⇒ 增量语义不成立 ⇒ 按「全量重建」这一最慢路径估。
ESTIMATED_MINUTES = 60
ESTIMATED_MINUTES_BASIS = ("常量、未实测（没有任何一次真跑的实测耗时）；"
                           "增量引擎四项未接线 ⇒ 按全量重建口径估")
#: 流水线总闸门数（G_A..G_G）：预览只覆盖其中一部分，未覆盖的部分必须**照实声明**，
#: 不许把「预览五闸全过」说成「七闸全过」。
PIPELINE_GATES_TOTAL = 7
PIPELINE_GATES_NOT_COVERED = (
    {"id": "G_F", "why": "需要本轮 staging 库才能比历史不变性，预览不建 staging（纯只读）"},
    {"id": "G_G", "why": "漂移源缺省是本轮批次目录，预览没有批次；"
                        "编排器在无批次时只给「本轮未验证」的空 PASS，不能当放行依据"},
)
#: 深判据（逐件 sha256 重算）的短缓存 TTL。实测本机 `verify_seals` 一次 6.8 秒（85 件），
#: 而 `collect_preview_state()` 也被 /api/db_status 调用，不能每次页面读数都重算一遍；
#: 5 分钟内的重复采集复用同一结论，并由调度 tick 在后台续热（首屏永远命中热缓存）。
#: 结论本身仍是编排器算出来的，只是**最多滞后 5 分钟**——滞后多久会随
#: `preview_basis_asof_bjt` 一起回显，不假装是「此刻实时」。
DEEP_GATE_TTL_SECONDS = 300
SUCCESS_STATES = ("SUCCESS", "SUCCEEDED", "DONE", "OK")
FAILED_STATES = ("FAILED", "ERROR", "ABORTED", "CANCELLED")

# 决策码（对外可读，不含任何本机绝对路径）
D_NOT_DUE = "NOT_DUE"
D_DISABLED = "DISABLED"
D_RUNNING = "ALREADY_RUNNING"
D_SUCCEEDED = "SUCCEEDED_THIS_MONTH"
D_MAX_ATTEMPTS = "STOPPED_MAX_ATTEMPTS"
D_COOLDOWN = "COOLDOWN_TODAY"
D_SOURCE_NOT_READY = "SOURCE_NOT_READY"
D_SESSION_NOT_CLOSED = "SESSION_NOT_CLOSED"
#: 入账语义（2026-10-22 裁定）的三条「明确原因码」：待入账月没走完 / 最后交易日没收盘 /
#: 源还没覆盖到最后交易日。它们与 `post_month.can_post_month()` 的三项 check 一一对应。
D_MONTH_NOT_ENDED = "MONTH_NOT_ENDED"
D_LAST_TD_NOT_CLOSED = "LAST_TRADING_DAY_NOT_CLOSED"
#: 判据本体（tools/post_month.py）导入/执行失败时的 fail-closed 码：绝不因为「判不了」就放行。
D_GATE_UNAVAILABLE = "POSTING_GATE_UNAVAILABLE"
POSTING_BLOCK_REASONS = (D_MONTH_NOT_ENDED, D_LAST_TD_NOT_CLOSED, D_SOURCE_NOT_READY)
#: `post_month.can_post_month()` 的 check id → 调度器的明确原因码（一一对应，不另造口径）。
_CHECK_TO_REASON = {
    "MONTH_ENDED": D_MONTH_NOT_ENDED,
    "LAST_TRADING_DAY_CLOSED": D_LAST_TD_NOT_CLOSED,
    "MARKET_DATA_THROUGH_LAST_TRADING_DAY": D_SOURCE_NOT_READY,
}
D_NO_CALENDAR = "CALENDAR_MISSING"
D_DUE = "DUE"
D_LAUNCHED = "LAUNCHED"
D_LOCK_HELD = "LOCK_HELD"
D_ENTRY_MISSING = "PIPELINE_ENTRY_MISSING"
D_LOCK_STALE_RENAMED = "STALE_LOCK_RENAMED"
D_ERROR = "ERROR"

_MUTEX = threading.RLock()
_CHILD: subprocess.Popen | None = None
_CHILD_META: dict | None = None
_THREAD: threading.Thread | None = None
_SCHED_STARTED_AT_BJT: str | None = None
_SCHED_PORT: int | None = None
_LAST_FP_SIG: tuple | None = None
#: 测试用「当前时刻」注入（仅自测）：优先级 = 函数参数 > BAZI_NOW_BJT 环境变量 > 真实钟。
#: 生产进程不设环境变量、也不调 set_now_override_bjt()，行为与原来一致。
_NOW_OVERRIDE_BJT: datetime | None = None
# 两段式确认：预览令牌只存本进程内存（不落盘 ⇒ 不新增任何可被伪造的持久状态）。
# 服务重启后旧令牌一律 401 PREVIEW_STALE「请重新检查」，这是 fail-closed，不是缺陷。
_PREVIEW_TOKENS: dict[str, dict] = {}

#: 令牌语义（2026-10-22 fixround3 如实更正文案，行为不变）：它**不是「用一次就销毁」**，
#: 而是**绑定签发那一刻的状态快照**：只要状态快照的范围哈希不变且没过 10 分钟，同一枚令牌
#: 可以重复校验（改了才失效 = fail-closed 的方向没变）。选「改文案」而不是「加消费删除」的
#: 理由：删除会让「点了 start 但进程起不来 ⇒ 必须重新跑一遍检查」这条重试路径变复杂，
#: 而文案与实况对齐是零行为风险的。真消费制如需，另议。

#: 编排器模块（tools/run_monthly_update.py）的只读导入结果：预览要用**同一实现**判
#: G_A/G_B/G_C，而不是在这边重写一套浅判据。导入失败只登记原因，不抛到请求面。
_PIPELINE_MOD = None
_PIPELINE_IMPORT_ERROR: str | None = None
#: 入账判据模块（tools/post_month.py）的只读导入结果：调度器**不重写**那三项 check，
#: 直接调它的 `can_post_month()`，避免「两边各判一套」慢慢漂移。
_POST_MONTH_MOD = None
_POST_MONTH_IMPORT_ERROR: str | None = None
#: 待入账月解析缓存：{dataset: {"at": monotonic, "value": dict|None}}
_PENDING_CACHE: dict = {}
#: 深判据缓存：{"asof_monotonic": float, "result": dict}
_DEEP_CACHE: dict = {}

PLAN_FIELDS = ("month_id", "target_first_trade_date", "attempts", "manual_attempts",
               "last_status", "last_attempt_bjt", "succeeded_bjt", "scheduler_build")


def set_roots(root=None, work_dir=None) -> dict:
    """把包根/工作目录显式传进来（server.py 起线程时调用），同步重绑所有落盘路径。

    `work_dir` 也可由环境变量 `BAZI_WORK_DIR` 指定：自测用它把锁/计划/状态写到临时目录，
    绝不碰生产 `work/`，更不会碰任何 `.sqlite`。
    """
    global WORK, LOGS, REPORTS, TOOLS, PIPELINE_ENTRY, LOCK_FILE, DISABLED_FILE
    global PLAN_FILE, STATE_FILE, FINGERPRINT_FILE, DECISION_FILE, RECORD_MD
    global SOURCE_BASELINE_FILE, POST_MONTH_ENTRY
    global DOCS_ROOT, MIRROR_ROOT, DAILY_DIR, SOURCE_MANIFEST, SOURCE_README
    global SOURCE_MERGE_REPORT
    global BASE_ROOT
    base = Path(root).resolve() if root else PKG_ROOT
    BASE_ROOT = base
    wd = (Path(work_dir).resolve() if work_dir
          else (Path(os.environ["BAZI_WORK_DIR"]).resolve()
                if os.environ.get("BAZI_WORK_DIR") else base / "work"))
    WORK, LOGS, REPORTS, TOOLS = wd, base / "logs", base / "reports", base / "tools"
    PIPELINE_ENTRY = TOOLS / "run_monthly_update.py"
    POST_MONTH_ENTRY = TOOLS / "post_month.py"
    LOCK_FILE = WORK / "update.lock"
    DISABLED_FILE = WORK / "update.disabled"
    PLAN_FILE = WORK / "update_plan.json"
    STATE_FILE = WORK / "update_state.json"
    FINGERPRINT_FILE = WORK / "db_fingerprints.json"
    DECISION_FILE = WORK / "update_last_decision.json"
    RECORD_MD = REPORTS / "更新记录.md"
    # 镜像根单独可指（自测用临时副本喂 JSON，验证「mtime 变了不代表源更新」这条反向断言）
    docs = Path(os.environ["BAZI_DOCS_ROOT"]).resolve() if os.environ.get(
        "BAZI_DOCS_ROOT") else DOCS_ROOT
    mirror = Path(os.environ["BAZI_SOURCE_MIRROR_ROOT"]).resolve() if os.environ.get(
        "BAZI_SOURCE_MIRROR_ROOT") else (docs / "source_mirror")
    DOCS_ROOT, MIRROR_ROOT = docs, mirror
    DAILY_DIR = MIRROR_ROOT / "正式源" / "全A日K_合并版"
    SOURCE_MANIFEST = MIRROR_ROOT / "source_manifest.json"
    SOURCE_README = DAILY_DIR / "README.json"
    SOURCE_MERGE_REPORT = DAILY_DIR / "merge_report.json"
    return {"work": str(WORK), "logs": str(LOGS), "reports": str(REPORTS),
            "mirror": str(MIRROR_ROOT)}


# ---------------------------------------------------------------- 基础工具

def _ensure_dirs() -> None:
    for d in (WORK, LOGS, REPORTS):
        d.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path):
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _write_json_atomic(path: Path, obj) -> bool:
    """写临时文件 + os.replace：读者永远只会看到完整 JSON。失败只返回 False，不抛。"""
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}.{uuid.uuid4().hex[:6]}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def fingerprint_signature():
    """`work/db_fingerprints.json` 里各库 sha12 的有序快照（用来侦测「换库发生了」）。"""
    doc = _read_json(FINGERPRINT_FILE)
    if not isinstance(doc, dict):
        return None
    dbs = doc.get("databases") if isinstance(doc.get("databases"), dict) else doc.get("dbs")
    if not isinstance(dbs, dict):
        return None
    sig = tuple(sorted(
        (str(k), str(_pick(v, _SHA_KEYS) or _pick(v, _BYTES_KEYS) or ""))
        for k, v in dbs.items() if isinstance(v, dict)))
    return sig or None


def maybe_invalidate_caches() -> int:
    """库指纹变了 → 清掉本进程 api 的三个结果缓存（T2 在另一个进程里清不动）。

    契约见 `work/db_fingerprints.json` 的 `cache_handoff`：编排器只写指纹文件，
    由服务内置调度器在下一次 tick 检测到变化后调用 `api.invalidate_caches()`，
    否则换库成功但页面会一直显示旧统计直到进程重启。首轮只登记基线，不清缓存。
    """
    global _LAST_FP_SIG
    sig = fingerprint_signature()
    if sig is None:
        return 0
    if _LAST_FP_SIG is None:
        _LAST_FP_SIG = sig
        return 0
    if sig == _LAST_FP_SIG:
        return 0
    _LAST_FP_SIG = sig
    try:
        n = api.invalidate_caches()
    except Exception as exc:  # noqa: BLE001
        _log(f"缓存失效调用失败（不影响请求面）：{type(exc).__name__}")
        return 0
    _log(f"检测到数据库指纹变化 → 已清 api 结果缓存 {n} 条（页面下次请求即读新库）")
    return n


def _log(line: str) -> None:
    """只打到服务启动窗口；本模块不自建日志轮转（logs/ 下按 run_id 单独成文件）。"""
    try:
        print(f"[scheduler {bjtime.now_iso_bjt()}] {line}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 —— 打日志失败绝不能影响请求面
        pass


def current_now_bjt() -> datetime:
    """当前北京时刻：测试注入 > BAZI_NOW_BJT 环境变量 > 真实钟（生产默认走真实钟）。

    2026-10-22 改造要求「把日期注入做成可测的」——否则「11-09 会跑、10-23 不会跑」这条
    判据只能靠嘴上说。注入面**只影响判定/状态文案**：`new_run_id()`、日志时间戳仍走真实钟，
    这样自测不会产出看起来像产品轮次的假 run_id。
    """
    if _NOW_OVERRIDE_BJT is not None:
        return _NOW_OVERRIDE_BJT
    raw = os.environ.get("BAZI_NOW_BJT")
    if raw:
        dt = bjtime.parse_bjt(raw)
        if dt is not None:
            return dt
    return bjtime.now_bjt()


def set_now_override_bjt(value) -> str | None:
    """注入「当前时刻」（仅测试）：`value` 为北京口径时间串/日期/None。

    返回归一化后的 `+08:00` 串（清除注入时返回 None）。生产代码不调用它。
    """
    global _NOW_OVERRIDE_BJT
    if value in (None, ""):
        _NOW_OVERRIDE_BJT = None
        return None
    dt = bjtime.parse_bjt(value)
    if dt is None:
        raise ValueError(f"BAZI_NOW_BJT 形态不合：{value!r}（要带 +08:00 的时间串或日期）")
    _NOW_OVERRIDE_BJT = dt
    return bjtime.iso_bjt(dt)


def now_iso_bjt(now: datetime | None = None) -> str:
    """当前北京时刻的对外串（带 +08:00）；`now` 给定时按它落。"""
    return bjtime.iso_bjt(now or current_now_bjt())


def assert_run_id(run_id) -> str:
    """run_id 必须能被编排器的 `^MU-\\d{8}T\\d{6}$` 吃下，否则**当场抛**。

    为什么要在这里断言：2026-10-22 那轮生成的串是 `upd_20261022174029_e2d820db`，
    `tools/run_monthly_update.py` 第 2623 行按 `RUN_ID_RE` 硬校验，直接
    `BAD_RUN_ID …：必须形如 MU-YYYYMMDDTHHMMSS（不许冒号）` 退出
    （证据：`logs/update_upd_20261022174029_e2d820db.log`）。
    也就是说**自动路径从头到尾一次都没有真正把流水线拉起来过**，而调度器自己记的是
    「已拉起」——错误被埋在子进程的 stderr 里，页面看不出来。让它在生成处就炸，
    比让它在 30 秒后从子进程日志里冒出来便宜得多。
    """
    s = str(run_id or "")
    if not RUN_ID_RE.match(s):
        raise AssertionError(
            f"BAD_RUN_ID {s!r}：必须匹配 {RUN_ID_RE.pattern}"
            "（MU-YYYYMMDDTHHMMSS，北京时间数字、无冒号、无下划线前缀）")
    return s


def new_run_id(prefix: str = "MU") -> str:
    """`MU-YYYYMMDDTHHMMSS`（北京时分秒，无冒号）—— 与 T2 编排器 `RUN_ID_RE` 同规格。

    流水线侧对 run_id 做正则硬校验（文件名里不许有冒号），格式不合会被直接拒跑，
    所以这里生成完立刻 `assert_run_id`：不合规格就不许离开本函数。
    `prefix` 参数保留只是为了调用签名兼容；传非 `MU` 会立刻断言失败，这是有意的。
    """
    rid = "MU-" + bjtime.now_bjt().strftime("%Y%m%dT%H%M%S")
    return assert_run_id(rid)


def is_disabled() -> bool:
    return DISABLED_FILE.exists()


def pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # 存在但不属于我：算活着
    except OSError:
        return False
    return True


# `ps -o etime=` 形态：[[dd-]hh:]mm:ss（本机实测 `01:51:52`），个别版本带百分秒。
_ETIME_RE = re.compile(r"^(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)(?:\.\d+)?$")


def proc_start_epoch(pid):
    """进程启动时刻（epoch 秒）：`ps -p <pid> -o etime=` 反推。

    为什么需要它：`os.kill(pid, 0)` 只能证明「这个 pid 上有进程」，证明不了「还是原来那个」。
    pid 会被回收复用 —— 只看存活会把接管了这个号码的无辜进程当成持锁者，从而永远回收不掉
    那把锁；反过来若只按 mtime 等 36 小时，真故障时人得等一天半。两个都不对，所以要
    「pid 已死 **或** 启动时刻对不上」才回收。
    这一句是只读查询：`ps -p <单个pid>`，绝不发信号、绝不按命令行匹配、绝不批量杀进程。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        res = subprocess.run(["ps", "-p", str(pid), "-o", "etime="],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if res.returncode != 0:
        return None
    m = _ETIME_RE.match((res.stdout or "").strip())
    if not m:
        return None
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    elapsed = d * 86400 + h * 3600 + mi * 60 + s
    return time.time() - elapsed


def holder_is_live(existing: dict) -> tuple[bool, str]:
    """锁里的持锁进程到底还算不算「本人」。返回 (是否算活着, 判据说明)。

    * pid 不存在 → 已死（可回收）；
    * pid 存在但启动时刻与锁里记录的对不上（差 > 180 秒）→ 这是 pid 复用，原持锁者已死；
    * pid 存在、启动时刻吻合 → 本人还在，绝不回收；
    * 老版本锁里没有启动时刻 → 只能信 `os.kill`，判活即不回收（保守）。
    """
    pid = existing.get("pid")
    if not pid_alive(pid):
        return False, "pid_not_alive"
    recorded = bjtime.parse_bjt(existing.get("holder_started_at_bjt"))
    if recorded is None:
        return True, "no_recorded_start_trust_pid"
    cur = proc_start_epoch(pid)
    if cur is None:
        return True, "start_unreadable_trust_pid"
    delta = abs(cur - recorded.timestamp())
    if delta > PROC_START_TOLERANCE_SECONDS:
        return False, f"pid_reused_start_delta_{int(delta)}s"
    return True, f"same_process_delta_{int(delta)}s"


# ---------------------------------------------------------------- 文件锁

def read_lock() -> dict | None:
    """锁内容 + 存活事实。文件不存在返回 None；内容坏了也照实带上 `_corrupt`。"""
    try:
        st = LOCK_FILE.stat()
    except OSError:
        return None
    data = _read_json(LOCK_FILE)
    if data is None:
        data = {"pid": None, "port": None, "run_id": None, "acquired_at_bjt": None,
                "kind": None, "_corrupt": True}
    age = max(0.0, time.time() - st.st_mtime)
    live, why = holder_is_live(data)
    data["_mtime_bjt"] = bjtime.iso_bjt(datetime.fromtimestamp(st.st_mtime, bjtime.BJT))
    data["_age_seconds"] = round(age, 1)
    data["_pid_alive"] = pid_alive(data.get("pid"))
    data["_holder_live"] = live
    data["_live_reason"] = why
    return data


def acquire_lock(port, run_id: str, kind: str):
    """原子创建 `work/update.lock`。返回 (是否拿到, 码, 锁信息)。

    陈旧锁：锁里的持锁进程已死（pid 不存在，或 pid 被复用、启动时刻对不上）
    → 只**改名**为 `update.lock.stale.<runid>`（绝不删除：删除不可逆，留给负责人授权）
    并登记一次，然后重试创建一次。不再要求「mtime 也超 36 小时」——那一条会让真故障
    现场等一天半才能重试。run_id 不合 `MU-` 规格一律拒收（不当锁的持有者）。
    """
    assert_run_id(run_id)
    _ensure_dirs()
    if is_disabled():
        return False, D_DISABLED, None
    with _MUTEX:
        retried = False
        while True:
            try:
                fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                existing = read_lock()
                if (existing is not None and existing.get("_holder_live") is False
                        and not retried):
                    if _rename_stale_lock(run_id, existing):
                        retried = True
                        continue
                    return False, D_LOCK_HELD, existing
                return False, D_LOCK_HELD, existing
            except OSError as exc:
                return False, D_ERROR, {"message": f"{type(exc).__name__}：锁文件无法创建"}
            payload = {"pid": os.getpid(), "port": port, "run_id": run_id,
                       "acquired_at_bjt": bjtime.now_iso_bjt(), "kind": kind,
                       "scheduler_build": SCHEDULER_BUILD}
            started = proc_start_epoch(os.getpid())
            if started is not None:      # 取不到就留 null：判活时退回「只信 os.kill」的保守口径
                payload["holder_started_at_bjt"] = bjtime.iso_bjt(
                    datetime.fromtimestamp(started, bjtime.BJT))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False)
            except OSError as exc:
                _unlink_quiet(LOCK_FILE)
                return False, D_ERROR, {"message": f"{type(exc).__name__}：锁文件写入失败"}
            return True, "ACQUIRED", payload


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _rename_stale_lock(run_id: str, existing: dict) -> bool:
    """陈旧锁改名保留（**不删除**），并登记一次。"""
    target = WORK / f"update.lock.stale.{run_id}"
    if target.exists():
        target = WORK / f"update.lock.stale.{run_id}.{bjtime.now_bjt().strftime('%H%M%S')}"
    with _MUTEX:
        try:
            LOCK_FILE.rename(target)
        except OSError as exc:
            _log(f"陈旧锁改名失败：{type(exc).__name__}")
            return False
    _log(f"陈旧锁已改名保留 {target.name}（原 pid={existing.get('pid')} "
         f"判据={existing.get('_live_reason')}，持锁 "
         f"{int((existing.get('_age_seconds') or 0) / 60)} 分钟）")
    append_update_record(
        trigger="scheduler", status="STALE_LOCK_RENAMED", error_code="STALE_LOCK",
        note=f"陈旧锁 pid={existing.get('pid')} 判据={existing.get('_live_reason')}，"
             f"改名保留为 {target.name}（未删除，删除是不可逆动作、留给负责人授权）")
    return True


def release_lock(run_id: str | None = None) -> bool:
    """释放=删除，但只删自己那一把（run_id 对不上就什么都不动）。"""
    with _MUTEX:
        data = _read_json(LOCK_FILE)
        if data is None and not LOCK_FILE.exists():
            return False
        if run_id is not None and data is not None and data.get("run_id") != run_id:
            return False
        _unlink_quiet(LOCK_FILE)
        return True


# ---------------------------------------------------------------- 计划 / 状态

def load_plan() -> dict | None:
    plan = _read_json(PLAN_FILE)
    if plan is None:
        return None
    clean = {k: plan.get(k) for k in PLAN_FIELDS}
    # 兼容：早期把「调度器自身构建串」写在 `rule_version` 字段里（那是个用错对象的字段名，
    # G-E 比的是引擎规则版本，不是调度器版本）。读进来时归位到 `scheduler_build`。
    if not clean.get("scheduler_build"):
        clean["scheduler_build"] = plan.get("rule_version")
    # 计数一律归一成整数：老 plan 文件里没有 manual_attempts 这一列，缺省按 0 次如实登记。
    for k in ("attempts", "manual_attempts"):
        try:
            clean[k] = int(clean.get(k) or 0)
        except (TypeError, ValueError):
            clean[k] = 0
    return clean


def save_plan(plan: dict) -> bool:
    clean = {k: plan.get(k) for k in PLAN_FIELDS}
    clean["scheduler_build"] = (clean.get("scheduler_build") or plan.get("rule_version")
                                or SCHEDULER_BUILD)
    return _write_json_atomic(PLAN_FILE, clean)


def new_plan(month_id: str, target_first_trade_date: str | None) -> dict:
    return {"month_id": month_id, "target_first_trade_date": target_first_trade_date,
            "attempts": 0, "manual_attempts": 0, "last_status": None,
            "last_attempt_bjt": None, "succeeded_bjt": None,
            "scheduler_build": SCHEDULER_BUILD}


def load_state() -> dict | None:
    return _read_json(STATE_FILE)


def write_state(state: dict) -> bool:
    return _write_json_atomic(STATE_FILE, state)


def _write_decision(payload: dict) -> bool:
    return _write_json_atomic(DECISION_FILE, payload)


def load_decision() -> dict | None:
    return _read_json(DECISION_FILE)


def _is_failed(status) -> bool:
    st = str(status or "").upper()
    return st in FAILED_STATES or st.endswith("FAILED")


def _bump_attempt(plan: dict, decision: str, now: datetime | None = None) -> dict:
    """**自动**路径的尝试计数：`attempts` 参与「同一节气月累计上限」（MAX_ATTEMPTS_PER_MONTH）。

    手动发起走 `_bump_manual_attempt`，不累加这一列（裁定：手动是用户显式发起，
    不该把自动重试的额度吃掉），但两列都会在面板上如实分开显示。
    `now` 只为自测注入日期用（生产缺省走真实北京钟），落盘的 `last_attempt_bjt` 与注入时刻一致。
    """
    plan = dict(plan)
    plan["attempts"] = int(plan.get("attempts") or 0) + 1
    plan["last_status"] = decision
    plan["last_attempt_bjt"] = now_iso_bjt(now)
    save_plan(plan)
    return plan


def _bump_manual_attempt(plan: dict, last_status: str) -> dict:
    """手动发起计数（`manual_attempts`）：只记次数，不参与自动重试上限。"""
    plan = dict(plan or new_plan(None, None))
    try:
        plan["manual_attempts"] = int(plan.get("manual_attempts") or 0) + 1
    except (TypeError, ValueError):
        plan["manual_attempts"] = 1
    plan["last_status"] = last_status
    save_plan(plan)
    return plan


# ---------------------------------------------------------------- 历法（只读）

def term_context(con, now: datetime | None = None) -> dict | None:
    """当前节气月 + 下一交节（只读查 `calendar_months`，全部按北京口径比较）。

    SQL 里只按 `substr(start_at,1,10)` 做粗筛（日期前缀的字典序==时间序，与偏移写法无关），
    精确比较一律在 Python 里用 `bjtime.parse_bjt()` 完成，不做串比串。
    """
    now = bjtime.to_bjt(now or current_now_bjt())
    lo = (now - timedelta(days=400)).date().isoformat()
    hi = (now + timedelta(days=400)).date().isoformat()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT month_id, start_term, start_at, end_at FROM calendar_months"
            " WHERE substr(start_at,1,10) >= ? AND substr(start_at,1,10) <= ?"
            " ORDER BY start_at", (lo, hi)).fetchall()]
    except Exception as exc:  # noqa: BLE001
        _log(f"历法查询失败：{type(exc).__name__}")
        return None
    parsed = []
    for r in rows:
        s, e = bjtime.parse_bjt(r["start_at"]), bjtime.parse_bjt(r["end_at"])
        if s is None or e is None:
            continue
        parsed.append({**r, "_start": s, "_end": e})
    if not parsed:
        return None
    current = next((r for r in parsed if r["_start"] <= now < r["_end"]), None)
    if current is None:
        return None
    nxt = next((r for r in parsed if r["_start"] > now), None)
    out = {"month_id": current["month_id"], "start_term": current["start_term"],
           "start_at_bjt": bjtime.iso_bjt(current["_start"]),
           "end_at_bjt": bjtime.iso_bjt(current["_end"]),
           "boundary_at_bjt": bjtime.iso_bjt(current["_start"])}
    if nxt:
        out.update({"next_month_id": nxt["month_id"], "next_start_term": nxt["start_term"],
                    "next_boundary_at_bjt": bjtime.iso_bjt(nxt["_start"])})
    else:
        out.update({"next_month_id": None, "next_start_term": None,
                    "next_boundary_at_bjt": None})
    return out


# ---------------------------------------------------------------- 待入账月（只读）

#: 「最后有数据流月」按 (solar_year, month_no) 排序取，**不依赖 month_id 的字符串序**：
#: 当前库的 id 是两位补零（M01..M12），字符串序恰好可用；但这不是契约
#: （`post_month.months_before()` 明确警告过不许按字符串排：一旦出现 `2026-M9` 这种写法，
#: `MAX(month_id)` 会把 M9 排到 M10 后面）。多写一列排序没有成本，别赌 id 的写法。
#: 表名一律限定 `main.`：本库会只读附带 cap 扩展库，未限定名的写操作会解析到附件库
#: （已知坑），SELECT 也照同一规矩写，避免哪天附件库出现同名表时静默换源。
_LAST_MONTH_SQL = (
    "SELECT month_id, solar_year, month_no FROM main.monthly_facts"
    " ORDER BY solar_year DESC, month_no DESC LIMIT 1")
_NEXT_MONTH_SQL = (
    "SELECT month_id, solar_year, month_no, month_pillar, start_at, end_at"
    " FROM main.calendar_months WHERE solar_year > ? OR (solar_year = ? AND month_no > ?)"
    " ORDER BY solar_year, month_no LIMIT 1")


def resolve_pending_month(con=None, dataset: str = "ashare", refresh: bool = False):
    """待入账月 = 「`monthly_facts` 里有数据的最后流月」在 `calendar_months` 里的下一个流月。

    返回 dict（全只读、不写任何东西）：
      `last_month_id` / `pending_month` / `pending_month_pillar` /
      `pending_month_start_at_bjt` / `pending_month_ends_at_bjt` / `_start` / `_end`
    任一环节拿不到（库空、历法缺下一个月、时间串解析失败）⇒ None（fail-closed）。
    """
    ds = dataset if dataset in getattr(api, "DATASETS", {}) else "ashare"
    mono = time.monotonic()
    if not refresh:
        hit = _PENDING_CACHE.get(ds)
        if hit is not None and (mono - hit["at"]) <= PENDING_CACHE_TTL_SECONDS:
            return hit["value"]
    own = None
    value = None
    try:
        if con is None:
            own = api.connect(ds)
            con = own
        last = con.execute(_LAST_MONTH_SQL).fetchone()
        if last is None:
            _log("monthly_facts 里没有任何流月 ⇒ 待入账月无法确定（fail-closed）")
            return None
        last_id, year, month_no = last[0], int(last[1]), int(last[2])
        nxt = con.execute(_NEXT_MONTH_SQL, (year, year, month_no)).fetchone()
        if nxt is None:
            _log(f"calendar_months 里没有 {last_id} 的下一个流月 ⇒ 待入账月无法确定")
            return None
        start = bjtime.parse_bjt(nxt[4])
        end = bjtime.parse_bjt(nxt[5])
        if start is None or end is None:
            _log(f"{nxt[0]} 的 start_at/end_at 解析失败 ⇒ 待入账月无法确定")
            return None
        value = {
            "last_month_id": last_id,
            "pending_month": nxt[0],
            "pending_month_pillar": nxt[3],
            "pending_month_start_at_bjt": bjtime.iso_bjt(start),
            "pending_month_ends_at_bjt": bjtime.iso_bjt(end),
            "_start": start,
            "_end": end,
        }
    except Exception as exc:  # noqa: BLE001 —— 只读探测，任何失败都返回 None 而不是抛
        _log(f"待入账月解析失败（fail-closed）：{type(exc).__name__}")
        value = None
    finally:
        if own is not None:
            try:
                own.close()
            except Exception:  # noqa: BLE001
                pass
    if value is not None:               # 不缓存 None：库/历法临时读不到时下一 tick 立刻重试
        _PENDING_CACHE[ds] = {"at": mono, "value": value}
    return value


def _last_weekday_on_or_before(d):
    """该日或之前最近的周一~周五（与 post_month 的 last_trading_day 口径一致；不含节假日）。"""
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _load_post_month_module():
    """只读导入入账入口 `tools/post_month.py`（用**同一实现**判那三项 check）。

    为什么不重写一套：重写就会慢慢与 `post_month.can_post_month()` 漂移，而「调度器判过、
    真入口拒跑」正是要避免的事故。导入本身不碰库、不联网、不建目录；模块体的两个全局
    副作用（`sys.dont_write_bytecode=True`、`getcontext().prec=60`）在这里**保存-还原**，
    绝不让 60 位小数精度渗进本服务的其它计算。

    入口固定取**包根**下的 `tools/post_month.py`（不是可被 `set_roots` 重绑的临时根），
    这样自测用替身脚本验拉起 argv 时，不会把判据本体也换成替身。
    """
    global _POST_MONTH_MOD, _POST_MONTH_IMPORT_ERROR
    with _MUTEX:
        if _POST_MONTH_MOD is not None or _POST_MONTH_IMPORT_ERROR is not None:
            return _POST_MONTH_MOD
        entry = PKG_ROOT / "tools" / "post_month.py"
        saved_prec = getcontext().prec
        saved_dwb = sys.dont_write_bytecode
        try:
            if not entry.is_file():
                raise OSError(f"入账入口不存在：{entry.name}")
            import importlib.util
            spec = importlib.util.spec_from_file_location("post_month_gate_readonly",
                                                          str(entry))
            if spec is None or spec.loader is None:
                raise OSError("无法为入账入口建导入规格")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            sys.dont_write_bytecode = True      # 不在 tools/ 下留 .pyc
            try:
                spec.loader.exec_module(mod)     # 模块体只定义常量/函数，不触碰任何路径
            except BaseException:
                sys.modules.pop(spec.name, None)
                raise
        except Exception as exc:  # noqa: BLE001 —— 判据不可用只登记，绝不放行
            _POST_MONTH_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
            _log(f"入账判据不可用（post_month 导入失败，将 fail-closed）："
                 f"{_POST_MONTH_IMPORT_ERROR}")
            return None
        finally:
            sys.dont_write_bytecode = saved_dwb
            getcontext().prec = saved_prec
        _POST_MONTH_MOD = mod
        return mod


# ---------------------------------------------------------------- 源探测（本地只读镜像）

_SPAN_RE = re.compile(r"^(\d{8})\.\.(\d{8})$")


def _read_mirror_json(path: Path):
    """镜像里的小 JSON（README/merge_report/source_manifest 都只有 KB 级）。只读，不解析大文件。"""
    return _read_json(path)


def _span_end(raw) -> str | None:
    """`"19901219..20261014"` → `"2026-10-14"`；形态不合一律 None（不猜、不回填今天）。"""
    m = _SPAN_RE.match(str(raw or "").strip())
    if not m:
        return None
    end = m.group(2)
    try:
        dt = datetime.strptime(end, "%Y%m%d")
    except ValueError:
        return None
    return dt.date().isoformat()


def probe_source() -> dict:
    """从**本地 A股镜像**只读探测源态：末交易日、源包哈希前 12 位、行数/文件数。

    数据源（全部本地、只读 JSON，**不联网、不连 NAS、不读 parquet/duckdb、不跑重活**）：
      * `source_mirror/source_manifest.json` —— 60 文件逐文件 sha256 与 bytes；
      * `source_mirror/正式源/全A日K_合并版/README.json` —— `规模.跨度` 与 行/票/交易日；
      * `source_mirror/正式源/全A日K_合并版/merge_report.json` —— `per_year`、`total_rows`。

    判据为什么**绝不用 mtime**（这条是被现实打过一遍才写死的）：
      2026-10-22 有一次误触把镜像 60 个文件**重新下载**了一遍，60 个文件的 mtime 全被刷成
      当天；事后逐文件哈希核对，内容与 10-15 的发布件 **60/60 全等**。也就是说：mtime 变了
      而源根本没变。若拿 mtime 当「源是否更新」的判据，调度器会在一夜之间把「源已就绪」
      判成真，然后拉起一轮本该被 SOURCE_NOT_READY 拦住的写库流水线。反过来，mtime 不变也
      不能证明源没变（重新落盘可以保留时间戳）。所以本函数只认**内容与跨度字段**：
      末交易日取 README `规模.跨度` 的右端，源身份取 manifest 里 `2026.zip` 的 sha256 前 12 位，
      与文件时间戳无关。

    拿不到就一律 None（fail-closed）：`evaluate_trigger` 见 None ⇒ SOURCE_NOT_READY，不硬跑。
    """
    out = {"source_max_trade_date": None, "source_sha12": None, "source_version": None,
           "file_count": None, "manifest_total_bytes": None, "readme_rows": None,
           "readme_trade_days": None, "merge_total_rows": None,
           "merge_year_rows": None, "span": None, "basis": [], "notes": []}
    live_dates = []
    man_live = _read_json(LIVE_MANIFEST)
    if isinstance(man_live, dict):
        for k in ("zip_latest_date_sample", "target_trade_date"):
            v = str(man_live.get(k) or "")[:10]
            if len(v) == 10:
                live_dates.append(v)
        sha = str(man_live.get("sha256") or "").strip().lower()
        if len(sha) >= 12:
            out["source_sha12"] = sha[:12]
            out["basis"].append("source_mirror_live/fetch_manifest.json:sha256")
    ready = _read_json(LIVE_READY)
    if isinstance(ready, dict):
        v = str(((ready.get("daily") or {}).get("latest_trade_date")) or "")[:10]
        if len(v) == 10:
            live_dates.append(v)
    try:
        import sqlite3 as _sq
        if LIVE_SLICE.is_file():
            con = _sq.connect(LIVE_SLICE.resolve().as_uri() + "?mode=ro", uri=True)
            row = con.execute("SELECT v FROM meta WHERE k='latest_trade_date'").fetchone()
            con.close()
            if row and row[0]:
                live_dates.append(str(row[0])[:10])
    except Exception:  # noqa: BLE001
        pass
    if live_dates:
        out["source_max_trade_date"] = max(live_dates)
        out["basis"].append("source_mirror_live + 当月层 meta（数据字段，非 README/mtime）")

    readme = _read_mirror_json(SOURCE_README)
    if isinstance(readme, dict):
        size = readme.get("规模")
        if isinstance(size, dict):
            span = size.get("跨度")
            out["span"] = span
            end = _span_end(span)
            if end:
                if not out["source_max_trade_date"]:
                    out["source_max_trade_date"] = end
                    out["basis"].append("README.json:规模.跨度（兜底）")
            else:
                out["notes"].append(f"README.json 的 跨度 字段形态不合：{span!r}")
            for src_key, out_key in (("行", "readme_rows"), ("交易日", "readme_trade_days")):
                v = size.get(src_key)
                if isinstance(v, int):
                    out[out_key] = v
        else:
            out["notes"].append("README.json 里没有 规模 对象")
    else:
        out["notes"].append(f"读不到 {SOURCE_README.name}")

    merge = _read_mirror_json(SOURCE_MERGE_REPORT)
    if isinstance(merge, dict):
        if isinstance(merge.get("total_rows"), int):
            out["merge_total_rows"] = merge["total_rows"]
        per_year = merge.get("per_year")
        if isinstance(per_year, dict) and out["source_max_trade_date"]:
            year = out["source_max_trade_date"][:4]
            cell = per_year.get(year)
            if isinstance(cell, dict) and isinstance(cell.get("rows"), int):
                out["merge_year_rows"] = cell["rows"]
                out["basis"].append(f"merge_report.json:per_year.{year}.rows>0")
        if out["merge_total_rows"] is not None:
            out["basis"].append("merge_report.json:total_rows")
    else:
        out["notes"].append(f"读不到 {SOURCE_MERGE_REPORT.name}")

    man = _read_mirror_json(SOURCE_MANIFEST)
    if isinstance(man, dict):
        out["source_version"] = man.get("source_version")
        if isinstance(man.get("file_count"), int):
            out["file_count"] = man["file_count"]
        if isinstance(man.get("total_bytes"), int):
            out["manifest_total_bytes"] = man["total_bytes"]
        files = man.get("files")
        hit = None
        if isinstance(files, list):
            for f in files:
                if isinstance(f, dict) and str(f.get("path", "")).endswith(
                        "/正式源/全A日K_合并版/" + SHA_ARTIFACT_NAME):
                    hit = f
                    break
            if hit is None:
                for f in files:
                    if isinstance(f, dict) and str(f.get("path", "")).endswith(
                            SHA_ARTIFACT_NAME):
                        hit = f
                        break
        if hit:
            sha = str(hit.get("sha256") or "").strip().lower()
            short = _short_sha(sha) if len(sha) >= 12 else None
            if short:
                out["source_sha12"] = short
                out["basis"].append(f"source_manifest.json:{hit.get('path')}")
            else:
                out["notes"].append("manifest 里 2026.zip 的 sha256 字段不是 12 位十六进制"
                                    "（绝不用该文件的 mtime 代替）")
        else:
            out["notes"].append(f"source_manifest.json 的 files[] 里找不到 {SHA_ARTIFACT_NAME}")
    else:
        out["notes"].append(f"读不到 {SOURCE_MANIFEST.name}")

    # 交叉核对：T2 的 baseline 文件若存在且与镜像不一致，照实登记，**不改判据**（镜像为准）。
    base = _read_json(SOURCE_BASELINE_FILE)
    if isinstance(base, dict):
        b_val = str(base.get("max_trade_date") or "").strip()
        if b_val and b_val != (out["source_max_trade_date"] or ""):
            out["notes"].append(f"work/source_publication_baseline.json 记的是 {b_val}，"
                                "与本地镜像 README 跨度不一致：以镜像为准，请人工核对")
    return out


def probe_source_max_trade_date(month_id: str | None = None):
    """兼容旧调用点：只要源末交易日（`YYYY-MM-DD`）；探不到返回 None ⇒ SOURCE_NOT_READY。

    `month_id` 目前不参与取数（判据是「源末交易日 >= 本流月首个交易日」）。留形参是为了
    集成阶段若源里存在交易日空洞时改成「取该流月内最早的已入库交易日」。
    """
    return probe_source().get("source_max_trade_date")


# ---------------------------------------------------------------- 关系引擎规则版本（G-E 口径）

def read_relation_rule_version(con=None):
    """从**主库** `source_versions` 取影子跑批那一行的 `version`（G-E 真正要比的串）。

    只读：`api.connect()` 已经是 `mode=ro` + `PRAGMA query_only=ON`；本函数只发 SELECT，
    而且把表名**限定到 `main.`** —— 本项目有两个已知坑：未限定名的 DROP/CREATE 会解析到
    只读附件库（`cap`）报 "readonly database"，`ANALYZE` 不带 schema 会波及附件库。
    所以这里既不建对象也不 ANALYZE。

    唯一性也是判据的一部分：命中 0 行或多于 1 个不同 version ⇒ 返回 None（不挑一个看起来
    顺眼的）。调用方（G-E）拿到 None 必须**保守拒跑**（fail-closed），绝不能退回调度器
    自己的构建串冒充引擎口径 —— 那样 G-E 会永远「一致」，等于把这道闸关掉。
    """
    own = None
    try:
        if con is None:
            own = api.connect("ashare")
            con = own
        rows = con.execute(
            "SELECT DISTINCT version FROM main.source_versions"
            " WHERE kind = 'frozen_db'"
            "   AND (name LIKE '%影子%' OR path LIKE '%v13_shadow%')").fetchall()
    except Exception as exc:  # noqa: BLE001 —— 读不到就是 null，不抛给请求面
        _log(f"规则版本读取失败（G-E 将 fail-closed）：{type(exc).__name__}")
        return None
    finally:
        if own is not None:
            try:
                own.close()
            except Exception:  # noqa: BLE001
                pass
    vals = [str(r[0]).strip() for r in rows if r and str(r[0] or "").strip()]
    if len(vals) != 1:
        _log(f"source_versions 里影子跑批行命中 {len(vals)} 个不同 version，不采信（返回 null）")
        return None
    return vals[0]


# ---------------------------------------------------------------- 触发判定（纯比较，无副作用）

def _decision_base(now: datetime) -> dict:
    """决策快照的基础形状：**既有字段一个不少**，新语义的字段先按 null 占位。"""
    return {"decision": None, "month_id": None, "target_first_trade_date": None,
            "planned_at_bjt": None, "eligible_at_bjt": None, "boundary_at_bjt": None,
            "next_boundary_at_bjt": None, "source_max_trade_date": None, "attempts": 0,
            "note": "", "asof_bjt": bjtime.iso_bjt(now),
            # ↓ 2026-10-22 入账语义新增（供后台/联调；前端不改也能显示 status/decision 与 note）
            "last_posted_month_id": None, "pending_month": None,
            "pending_month_start_at_bjt": None, "pending_month_ends_at_bjt": None,
            "blocked_reason": None, "last_trading_day_in_window": None,
            "gate_checks": None}


def evaluate_trigger(now: datetime | None = None, con=None, term: dict | None = None,
                     plan: dict | None = None, probe=None) -> dict:
    """当前该不该**入账**。**只读**：不写 plan、不建锁、不拉起进程。

    两条路：
      * `term=None`（生产路径：tick_once / db_status 都这么调）⇒ `evaluate_posting_due()`，
        按 2026-10-22 裁定：待入账月 = `monthly_facts` 最后流月的下一个流月，必须
        `now > M_next.end_at` **且** `post_month.can_post_month(M_next)` 三项全过；
      * `term=<历法 dict>`（**只为既有自测保留的桩缝**）⇒ `_evaluate_trigger_legacy()`，
        逐字保留旧判据（按交节后首个交易日 20:00）。这不是第二条产品路径：生产两个调用点
        都不传 term，传了也只是「注入历法」这一件事，旧测试用它证明各分支行为没漂。
    """
    now = bjtime.to_bjt(now or current_now_bjt())
    if is_disabled():
        out = _decision_base(now)
        out.update(decision=D_DISABLED, note="停用哨兵 work/update.disabled 存在，调度不触发")
        return out
    if term is not None:
        return _evaluate_trigger_legacy(now=now, con=con, term=term, plan=plan, probe=probe)
    return evaluate_posting_due(now=now, con=con, plan=plan, probe=probe)


def _evaluate_trigger_legacy(now: datetime, con=None, term: dict | None = None,
                             plan: dict | None = None, probe=None) -> dict:
    """**旧判据原文**（按 `target_first_trade_date` 判该跑）——只在显式注入 term 时走。

    保留它有两个用处，都不是「留着当产品路径」：
      ① 既有 184 条断言逐条还按原语义验收；
      ② 新测试可以拿它算「若把判据改回旧语义会怎样」，给新断言提供判别力反例。
    """
    now = bjtime.to_bjt(now or current_now_bjt())
    probe = probe or probe_source_max_trade_date
    out = _decision_base(now)
    own_con = None
    try:
        if term is None:
            if con is None:
                own_con = api.connect("ashare")
                con = own_con
            term = term_context(con, now)
    finally:
        if own_con is not None:
            try:
                own_con.close()
            except Exception:  # noqa: BLE001
                pass
    if not term:
        out.update(decision=D_NO_CALENDAR, note="calendar_months 里查不到当前节气月")
        return out

    month_id = term["month_id"]
    out.update(month_id=month_id, boundary_at_bjt=term["boundary_at_bjt"],
               next_boundary_at_bjt=term.get("next_boundary_at_bjt"))
    target = bjtime.first_trading_day_on_or_after(term["boundary_at_bjt"])
    if target is None:
        out.update(decision=D_NO_CALENDAR, note="交节时间串解析失败")
        return out
    planned = bjtime.iso_bjt(datetime.combine(target, datetime.min.time(),
                                              tzinfo=bjtime.BJT).replace(
        hour=TRIGGER_HOUR_BJT))
    out.update(target_first_trade_date=target.isoformat(), planned_at_bjt=planned)

    if plan is None:
        plan = load_plan()
    if plan is None or plan.get("month_id") != month_id \
            or plan.get("scheduler_build") not in (None, SCHEDULER_BUILD):
        plan = new_plan(month_id, target.isoformat())
    out["attempts"] = int(plan.get("attempts") or 0)

    if plan.get("succeeded_bjt"):
        out.update(decision=D_SUCCEEDED, note="本流月已成功更新过，不再重复触发")
        return out
    if out["attempts"] >= MAX_ATTEMPTS_PER_MONTH:
        out.update(decision=D_MAX_ATTEMPTS,
                   note=f"同一节气月累计尝试已达 {MAX_ATTEMPTS_PER_MONTH} 次，停止并如实登记")
        return out
    if now < bjtime.parse_bjt(planned):
        out.update(decision=D_NOT_DUE, note=f"未到触发时刻 {planned}")
        return out

    last_attempt = bjtime.parse_bjt(plan.get("last_attempt_bjt"))
    if last_attempt is not None and last_attempt.date() == now.date():
        out.update(decision=D_COOLDOWN, note="今日已尝试过一次，按「次日重试」等待下一个北京日")
        return out
    if not bjtime.is_session_closed(target, now):
        out.update(decision=D_SESSION_NOT_CLOSED, note="目标交易日尚未收盘，不硬跑")
        return out

    src_max = probe(month_id)
    out["source_max_trade_date"] = src_max
    if src_max is None:
        out.update(decision=D_SOURCE_NOT_READY,
                   note="源探测无结果（本地只读镜像的 README.json 规模.跨度 / "
                        "merge_report.json / source_manifest.json 取不到可用值）："
                        "不硬跑")
        return out
    if str(src_max) < target.isoformat():
        out.update(decision=D_SOURCE_NOT_READY,
                   note=f"源里最新交易日 {src_max} 还没到本流月首个交易日 {target.isoformat()}")
        return out
    out.update(decision=D_DUE, note="命中：可以拉起流水线（默认 --dry-run）")
    return out


def evaluate_posting_due(now: datetime | None = None, con=None, plan: dict | None = None,
                         probe=None) -> dict:
    """**入账语义**（2026-10-22 裁定）的判定本体：只读、无副作用。

    判据（顺序即原因码优先级）：
      0. `work/update.disabled` 在位 ⇒ DISABLED（由 evaluate_trigger 拦在前面）；
      1. 待入账月 `M_next` 解析不出来 ⇒ CALENDAR_MISSING（fail-closed，不硬跑）；
      2. `M_next` 已入账成功过 ⇒ SUCCEEDED_THIS_MONTH；
      3. `now_bjt <= M_next.end_at` ⇒ **MONTH_NOT_ENDED**（月没走完，绝不允许入账）；
      4. 该月累计尝试达 3 次 ⇒ STOPPED_MAX_ATTEMPTS；
      5. 今日已尝试过 ⇒ COOLDOWN_TODAY；
      6. `post_month.can_post_month(M_next)` 三项 check 的第一个失败项：
         LAST_TRADING_DAY_CLOSED ⇒ **LAST_TRADING_DAY_NOT_CLOSED**；
         MARKET_DATA_THROUGH_LAST_TRADING_DAY ⇒ **SOURCE_NOT_READY**；
         未知 check / 判据模块不可用 ⇒ POSTING_GATE_UNAVAILABLE（fail-closed）；
      7. 全过 ⇒ DUE。

    返回字段见 `_decision_base()`；另带 `pending_month` / `pending_month_ends_at_bjt` /
    `blocked_reason` / `eligible_at_bjt` / `last_trading_day_in_window` / `gate_checks`。
    `planned_at_bjt` = 判据给出的「交节后首个交易日 20:00」（`first_eligible_date`）。
    """
    now = bjtime.to_bjt(now or current_now_bjt())
    probe = probe or probe_source_max_trade_date
    out = _decision_base(now)

    own_con = None
    term, pending = None, None
    try:
        if con is None:
            own_con = api.connect("ashare")
            con = own_con
        term = term_context(con, now)
        pending = resolve_pending_month(con=con, dataset="ashare")
    finally:
        if own_con is not None:
            try:
                own_con.close()
            except Exception:  # noqa: BLE001
                pass

    if not term:
        out.update(decision=D_NO_CALENDAR,
                   note="calendar_months 里查不到当前节气月（判不了「月走完没」）：不硬跑")
        return out
    # 当前节气月只用于既有字段/页面口径；**判据不拿它当「该跑」**。
    out.update(month_id=term["month_id"], boundary_at_bjt=term["boundary_at_bjt"],
               next_boundary_at_bjt=term.get("next_boundary_at_bjt"))
    if not pending:
        out.update(decision=D_NO_CALENDAR,
                   note="读不到 monthly_facts 的最后流月，或它在 calendar_months 里没有下一个"
                        "流月：待入账月无法确定，不硬跑")
        return out

    mid = pending["pending_month"]
    start, end = pending["_start"], pending["_end"]
    pillar = pending.get("pending_month_pillar") or (mid or "")
    out.update(last_posted_month_id=pending["last_month_id"], pending_month=mid,
               pending_month_start_at_bjt=pending["pending_month_start_at_bjt"],
               pending_month_ends_at_bjt=pending["pending_month_ends_at_bjt"])

    # 本地兜底的最早入账时刻（只在判据模块不可用时使用；正常以 post_month 的
    # `first_eligible_date` 为准，两者对 M09 都是 2026-11-09T20:00）。
    local_day = bjtime.first_trading_day_on_or_after(pending["pending_month_ends_at_bjt"])
    local_eligible = (datetime.combine(local_day, datetime.min.time(), tzinfo=bjtime.BJT)
                      .replace(hour=TRIGGER_HOUR_BJT)) if local_day else None

    src_max = probe(mid)
    out["source_max_trade_date"] = src_max

    mod = _load_post_month_module()
    gate = None
    if mod is not None:
        try:
            # covered_days 传空表：调度器**不扫 zip**，只用三项 check 的布尔结论；
            # `coverage` 细目是 post_month dry-run/staging 自己算的东西，这里不冒充。
            gate = mod.can_post_month(
                {"month_id": mid, "_start": start, "_end": end,
                 "start_at": pending["pending_month_start_at_bjt"],
                 "end_at": pending["pending_month_ends_at_bjt"]},
                now, src_max, [])
        except Exception as exc:  # noqa: BLE001
            _log(f"can_post_month 调用失败（fail-closed）：{type(exc).__name__}")
            gate = None

    eligible = local_eligible
    last_td = _last_weekday_on_or_before(end.date()).isoformat()
    if isinstance(gate, dict):
        got = bjtime.parse_bjt(gate.get("first_eligible_date"))
        if got is not None:
            eligible = got
        last_td = gate.get("last_trading_day_in_window") or last_td
        out["gate_checks"] = [dict(c) for c in (gate.get("checks") or [])]
    out["last_trading_day_in_window"] = last_td
    if eligible is not None:
        out.update(target_first_trade_date=eligible.date().isoformat(),
                   planned_at_bjt=bjtime.iso_bjt(eligible),
                   eligible_at_bjt=bjtime.iso_bjt(eligible))

    if plan is None:
        plan = load_plan()
    if plan is None or plan.get("month_id") != mid \
            or plan.get("scheduler_build") not in (None, SCHEDULER_BUILD):
        plan = new_plan(mid, out.get("target_first_trade_date"))
    out["attempts"] = int(plan.get("attempts") or 0)

    if plan.get("succeeded_bjt"):
        out.update(decision=D_SUCCEEDED,
                   note=f"{mid} 已入账成功过，不再重复触发")
        return out
    if now <= end:
        tail = f"（最早 {out['eligible_at_bjt']}）" if out.get("eligible_at_bjt") else ""
        out.update(decision=D_MONTH_NOT_ENDED, blocked_reason=D_MONTH_NOT_ENDED,
                   note=f"{pillar}月未结束，{end:%m-%d %H:%M} 后入账{tail}")
        return out
    if out["attempts"] >= MAX_ATTEMPTS_PER_MONTH:
        out.update(decision=D_MAX_ATTEMPTS,
                   note=f"{mid} 累计尝试已达 {MAX_ATTEMPTS_PER_MONTH} 次，停止并如实登记")
        return out
    last_attempt = bjtime.parse_bjt(plan.get("last_attempt_bjt"))
    if last_attempt is not None and last_attempt.date() == now.date():
        prev = plan.get("last_status")
        out.update(decision=D_COOLDOWN,
                   blocked_reason=prev if prev in POSTING_BLOCK_REASONS else None,
                   note="今日已尝试过一次，按「次日重试」等待下一个北京日")
        return out
    if gate is None:
        out.update(decision=D_GATE_UNAVAILABLE, blocked_reason=D_GATE_UNAVAILABLE,
                   note=f"入账判据不可用（{_POST_MONTH_IMPORT_ERROR or 'can_post_month 抛错'}）："
                        "不硬跑")
        return out
    failed = list(gate.get("failed_checks") or [])
    if failed:
        first = str(failed[0])
        reason = _CHECK_TO_REASON.get(first)
        if reason == D_SOURCE_NOT_READY:
            if src_max is None:
                note = ("源探测无结果（本地只读镜像 README/merge_report/source_manifest "
                        "都取不到可用值）：不硬跑")
            else:
                note = (f"源里最新交易日 {src_max} 还没覆盖 {mid} 的最后交易日 {last_td}"
                        f"（{last_td} 收盘后才可入账）")
        elif reason == D_LAST_TD_NOT_CLOSED:
            note = f"{mid} 的最后交易日 {last_td} 15:00 尚未收盘，不硬跑"
        elif reason == D_MONTH_NOT_ENDED:
            note = f"{pillar}月未结束，{end:%m-%d %H:%M} 后入账"
        else:
            # post_month 以后若新增 check，调度器不认识 ⇒ 一律 fail-closed，绝不判 DUE。
            reason = D_GATE_UNAVAILABLE
            note = f"post_month.can_post_month 返回未知失败项 {first}：保守拒跑"
        out.update(decision=reason, blocked_reason=reason, note=note)
        return out
    if gate.get("can_post_month") is not True:
        out.update(decision=D_GATE_UNAVAILABLE, blocked_reason=D_GATE_UNAVAILABLE,
                   note="can_post_month 未给出 True 且没有失败项：保守拒跑")
        return out
    out.update(decision=D_DUE,
               note=f"命中：{mid} 已走完且源覆盖到最后交易日 {last_td}，"
                    "可拉起 post_month --mode staging（不 promote）")
    return out


# ---------------------------------------------------------------- 拉起流水线

def child_info() -> dict | None:
    with _MUTEX:
        if _CHILD is None:
            return None
        return {**(_CHILD_META or {}), "alive": _CHILD.poll() is None,
                "returncode": _CHILD.returncode}


def launch_pipeline(trigger: str, port=None, dry_run: bool = True, run_id: str | None = None):
    """先写初态状态文件，再 subprocess.Popen 起流水线（脱离服务进程组）。

    返回 (是否成功, 码, {run_id, log, ...})。流水线入口缺失时如实登记，不崩、不假装 202。
    """
    global _CHILD, _CHILD_META
    run_id = assert_run_id(run_id or new_run_id())
    _ensure_dirs()
    if not PIPELINE_ENTRY.is_file():
        msg = "流水线入口缺失：tools/run_monthly_update.py（T2 交付物，尚未存在）"
        _log(f"拉起取消：{msg}")
        append_update_record(trigger=trigger, status="SKIPPED", error_code=D_ENTRY_MISSING,
                             note="未拉起任何进程；tools/run_monthly_update.py 不存在")
        return False, D_ENTRY_MISSING, {"run_id": run_id, "message": msg}
    with _MUTEX:
        if _CHILD is not None and _CHILD.poll() is None:
            return False, D_RUNNING, {"run_id": (_CHILD_META or {}).get("run_id")}
        # 只有**持锁者**能拉起：绕过锁直接调用本函数一律拒绝（防无协调的并发拉起）
        held = _read_json(LOCK_FILE)
        if held is None or held.get("run_id") != run_id:
            return False, "LOCK_NOT_HELD", {"run_id": run_id,
                                            "message": "未持锁，拒绝拉起"}
        # 拉起方写初态；此后该文件的写者移交给流水线进程，调度器只读。
        write_state({"status": "RUNNING", "stage": "init", "run_id": run_id,
                     "trigger": trigger, "started_at_bjt": bjtime.now_iso_bjt(),
                     "stages": [], "pid": None, "error": None})
        log_path = LOGS / f"update_{run_id}.log"
        # 三根必须显式传下去：编排器已无「回落真实路径」的缺省，不给就 rc=4 NO_ROOT_CONFIGURED。
        # 这条是跨组件契约——只测假桩的自测永远发现不了（BAD_RUN_ID 是同一类漏法）。
        cmd = [sys.executable or "python3", "-u", str(PIPELINE_ENTRY),
               "--run-id", run_id, "--trigger", trigger,
               "--project-root", str(BASE_ROOT.parent),
               "--docs-root", str(DOCS_ROOT),
               "--dev-root", str(BASE_ROOT)]
        if dry_run:
            cmd.append("--dry-run")
        fh = None
        try:
            fh = open(log_path, "a", encoding="utf-8")
            fh.write(f"\n===== {bjtime.now_iso_bjt()} 拉起 trigger={trigger} "
                     f"run_id={run_id} dry_run={dry_run} =====\n")
            fh.flush()
            _CHILD = subprocess.Popen(cmd, cwd=str(PKG_ROOT), stdout=fh,
                                      stderr=subprocess.STDOUT, start_new_session=True)
            _CHILD_META = {"run_id": run_id, "trigger": trigger, "port": port,
                           "started_at_bjt": bjtime.now_iso_bjt(),
                           "dry_run": bool(dry_run), "log": f"logs/{log_path.name}",
                           "pid": _CHILD.pid}
        except OSError as exc:
            write_state({"status": "FAILED", "stage": "launch", "run_id": run_id,
                         "trigger": trigger, "started_at_bjt": bjtime.now_iso_bjt(),
                         "stages": [], "error": {"code": "LAUNCH_FAILED",
                                                 "message": f"{type(exc).__name__}：流水线进程起不来"}})
            _CHILD = _CHILD_META = None
            return False, "LAUNCH_FAILED", {"run_id": run_id,
                                            "message": "LAUNCH_FAILED"}
        finally:
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass
        _log(f"已拉起流水线 run_id={run_id} trigger={trigger} dry_run={dry_run} "
             f"pid={_CHILD.pid if _CHILD else None}")
        return True, "LAUNCHED", dict(_CHILD_META or {"run_id": run_id})


# ---------------------------------------------------------------- 拉起入账入口（staging）

MONTH_ID_RE = re.compile(r"^\d{4}-M\d{1,2}$")


def build_post_month_cmd(month_id: str) -> list[str]:
    """自动入账的唯一命令行：`python3 tools/post_month.py --month <M> --mode staging`。

    三条硬约束（写在这里就是让改的人先撞上 AssertionError）：
      ① `--mode` 只能是 `staging`（真替换 formal DB 的 promote 本轮**明确未实现**，也不许自动调）；
      ② 不许带 `--allow-incomplete`（月没走完就入账的口子只给人工显式使用）；
      ③ month_id 必须形如 `YYYY-Mx`（防参数注入，也防把空串当月份传下去）。
    """
    mid = str(month_id or "")
    if not MONTH_ID_RE.match(mid):
        raise AssertionError(f"BAD_MONTH_ID {mid!r}：必须形如 2026-M09")
    cmd = [sys.executable or "python3", "-u", str(POST_MONTH_ENTRY),
           "--month", mid, "--mode", "staging"]
    if "promote" in cmd or "--allow-incomplete" in cmd:
        raise AssertionError("自动入账路径绝不允许 promote / --allow-incomplete")
    return cmd


def launch_post_month(month_id: str, trigger: str, port=None,
                      run_id: str | None = None, now: datetime | None = None):
    """拉起 `tools/post_month.py --mode staging`（**只写 work/post/<month>/，不碰正式库**）。

    与 `launch_pipeline()` 同样的安全机制：只有持锁者能拉起、先写初态状态文件、日志落
    `logs/update_<run_id>.log`、子进程 `start_new_session=True` 脱离服务进程组。
    返回 (是否成功, 码, {run_id, log, mode, month_id, pid})。
    """
    global _CHILD, _CHILD_META
    run_id = assert_run_id(run_id or new_run_id())
    now = bjtime.to_bjt(now or current_now_bjt())
    _ensure_dirs()
    if not POST_MONTH_ENTRY.is_file():
        msg = ("入账入口缺失：tools/post_month.py"
               "（自动路径只以 staging 拉起，promote 未实现且永不由调度器调用）")
        _log(f"拉起取消：{msg}")
        append_update_record(trigger=trigger, status="SKIPPED", error_code=D_ENTRY_MISSING,
                             note="未拉起任何进程；tools/post_month.py 不存在")
        return False, D_ENTRY_MISSING, {"run_id": run_id, "message": msg}
    try:
        cmd = build_post_month_cmd(month_id)
    except AssertionError as exc:
        _log(f"拉起取消：{exc}")
        return False, "BAD_MONTH_ID", {"run_id": run_id, "message": str(exc)}
    with _MUTEX:
        if _CHILD is not None and _CHILD.poll() is None:
            return False, D_RUNNING, {"run_id": (_CHILD_META or {}).get("run_id")}
        # 只有**持锁者**能拉起：与 launch_pipeline 同一条硬闸。
        held = _read_json(LOCK_FILE)
        if held is None or held.get("run_id") != run_id:
            return False, "LOCK_NOT_HELD", {"run_id": run_id,
                                            "message": "未持锁，拒绝拉起"}
        write_state({"status": "RUNNING", "stage": "init", "run_id": run_id,
                     "trigger": trigger, "mode": "staging", "post_month": True,
                     "target_month": month_id, "pending_month": month_id,
                     "started_at_bjt": bjtime.iso_bjt(now),
                     "stages": [], "pid": None, "error": None})
        log_path = LOGS / f"update_{run_id}.log"
        fh = None
        try:
            fh = open(log_path, "a", encoding="utf-8")
            fh.write(f"\n===== {bjtime.iso_bjt(now)} 拉起入账入口 trigger={trigger} "
                     f"run_id={run_id} month={month_id} mode=staging =====\n")
            fh.write("ARGV " + json.dumps(cmd, ensure_ascii=False) + "\n")
            fh.flush()
            _CHILD = subprocess.Popen(cmd, cwd=str(PKG_ROOT), stdout=fh,
                                      stderr=subprocess.STDOUT, start_new_session=True)
            _CHILD_META = {"run_id": run_id, "trigger": trigger, "port": port,
                           "started_at_bjt": bjtime.iso_bjt(now),
                           "mode": "staging", "post_month": True, "month_id": month_id,
                           "dry_run": False, "log": f"logs/{log_path.name}",
                           "pid": _CHILD.pid}
        except OSError as exc:
            write_state({"status": "FAILED", "stage": "launch", "run_id": run_id,
                         "trigger": trigger, "mode": "staging", "post_month": True,
                         "target_month": month_id, "started_at_bjt": bjtime.iso_bjt(now),
                         "stages": [], "error": {"code": "LAUNCH_FAILED",
                                                 "message": f"{type(exc).__name__}："
                                                            "入账进程起不来"}})
            _CHILD = _CHILD_META = None
            return False, "LAUNCH_FAILED", {"run_id": run_id,
                                            "message": "LAUNCH_FAILED"}
        finally:
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass
        _log(f"已拉起入账入口 run_id={run_id} trigger={trigger} month={month_id} "
             f"mode=staging pid={_CHILD.pid if _CHILD else None}")
        return True, "LAUNCHED", dict(_CHILD_META or {"run_id": run_id})


#: 子进程已退出、但状态文件里还停在「进行中」时用来合成终态的那几个词：
#: 调度器写的初态是 RUNNING，编排器中途态也是 RUNNING/PLANNING/…；这些一律**不是终态**。
_NON_TERMINAL_STATES = ("RUNNING", "PLANNING", "PENDING", "QUEUED", "STARTING",
                        "INIT", "ACCEPTING", "STAGING")


def _synth_terminal_state(state: dict, rc, run_id: str, trigger: str | None) -> dict:
    """子进程已经退出而状态仍是「进行中」时，按退出码**合成**一份终态（P1-2 兜底）。

    合成不等于编造：`error.code` 明说是调度器合成的（`SCHEDULER_SYNTHESIZED_*`），
    `message` 带上退出码和日志尾部第一条像错误的那行（rc=4 的 `NO_ROOT_CONFIGURED`
    就是这么从 stderr 里被看见的，不再静默）。
    """
    out = dict(state or {})
    now_iso = bjtime.now_iso_bjt()
    tail = _log_tail(run_id) if run_id else []
    hint = ""
    for line in reversed(tail):
        s = str(line).strip()
        if not s or s.startswith("====="):
            continue
        if ("ERROR" in s.upper()) or ("CONFIGURED" in s.upper()) or ("FAIL" in s.upper()) \
                or ("Traceback" in s):
            hint = s[:200]
            break
    if not hint:
        hint = (str(tail[-1])[:200] if tail else "本轮没有任何日志行回报结束")
    if rc == 0:
        out["status"] = "EXITED_OK"
        out["error"] = None
        out["error_code"] = None
        out["error_note"] = ""
    else:
        out["status"] = "EXITED_FAILED"
        out["error_code"] = f"SCHEDULER_SYNTHESIZED_EXIT_{rc}"
        out["error_note"] = (f"流水线已退出（退出码 {rc}），但没有写下终态；"
                             f"本终态由调度器按退出码合成。日志摘要：{hint}")[:400]
        out["error"] = {"code": out["error_code"], "message": out["error_note"]}
    out["run_id"] = run_id or out.get("run_id")
    out["trigger"] = trigger or out.get("trigger")
    out["started_at_bjt"] = out.get("started_at_bjt") or now_iso
    out["updated_at_bjt"] = now_iso
    out["finished_at_bjt"] = now_iso
    out["exit_rc"] = rc
    out["terminal_synthesized_by"] = "update_scheduler"
    return out


def _reap_child() -> None:
    """子进程结束时：把结果落进 plan（调度器是 plan 唯一写者），并释放自己那把锁。

    P1-2 的关键改动：以前「状态还是 RUNNING」也会被当成终态登记进 plan（`last_status=RUNNING`），
    于是页面永远显示 RUNNING、按钮永久禁死、`update_plan.json` 也被写脏。现在：
      * 状态里是**真终态** ⇒ 照实登记；
      * 状态缺失 / 仍是「进行中」 ⇒ 按退出码**合成**终态并**写回状态文件**（含 finished_at_bjt
        与 error{code,message}），plan.last_status 用同一个终态，绝不落 "RUNNING"。
    """
    global _CHILD, _CHILD_META
    with _MUTEX:
        if _CHILD is None or _CHILD.poll() is None:
            return
        meta = dict(_CHILD_META or {})
        rc = _CHILD.returncode
        state = load_state() or {}
        status = str(state.get("status") or "").upper()
        run_id = meta.get("run_id") or state.get("run_id")
        trigger = meta.get("trigger") or state.get("trigger")
        plan = load_plan() or new_plan(state.get("month_id"), None)
        plan = dict(plan)
        if status in SUCCESS_STATES:
            done = state.get("finished_at_bjt") or state.get("succeeded_bjt")
            plan["succeeded_bjt"] = (done if bjtime.parse_bjt(done)
                                     else bjtime.now_iso_bjt())
            plan["last_status"] = "SUCCESS"
        elif status and status not in _NON_TERMINAL_STATES:
            # 流水线自报了终态（FAILED / BLOCKED / DRY_RUN_PLAN / PROMOTED_EXTENSION_FAILED…）
            # ⇒ 照实登记，**绝不**因为 rc==0 就把 dry-run 轮次算成「本月已成功」
            plan["last_status"] = status
        else:
            # 子进程已退出，状态却还停在「进行中」（或压根没写状态）⇒ 合成终态并写回，
            # 这样页面上一定有结束时刻与原因，前端也就不会无限轮询。
            synth = _synth_terminal_state(state, rc, run_id, trigger)
            write_state(synth)
            _log(f"合成终态 run_id={run_id} status={synth['status']} rc={rc}"
                 f"（状态文件原本停在 {status or '空'}）")
            state = synth
            status = synth["status"]
            plan["last_status"] = status
        save_plan(plan)
        _log(f"流水线结束 run_id={run_id} status={plan['last_status']} rc={rc}")
        _CHILD = _CHILD_META = None
        release_lock(run_id)


def manual_start(port=None, run_id: str | None = None, preview_token: str | None = None):
    """手动端点的实现：**两段式确认**（用户裁定）—— 先 `/preview` 拿令牌，再带令牌 start。

    返回 (http 语义码, payload)。缺 token → `NO_PREVIEW_TOKEN`(400)；
    token 过期/不属于本进程/状态范围已变 → `PREVIEW_STALE`(401，提示重新检查)；
    202 = 已拉起；409 = 锁被占（带当前 run_id）；503 = 停用哨兵；500 = 入口缺失/起不来。
    拉起的子进程**仍然默认 `--dry-run`**：真跑要 `work/execute.confirmed` 存在，本函数
    既不创建也不绕过那道门。自动路径不走这里（五道闸全过就直接写，不等第二次确认）。
    """
    ok, code, _entry = validate_preview_token(preview_token)
    if not ok:
        return code, {"error": code,
                      "message": ("缺少 preview_token：请先调用 POST /api/db_update/preview"
                                  if code == "NO_PREVIEW_TOKEN" else
                                  "预览结论已过期或与当前状态不一致，请重新检查")}
    _ensure_dirs()
    if is_disabled():
        return "DISABLED", {"error": D_DISABLED}
    with _MUTEX:
        _reap_child()
        if _CHILD is not None and _CHILD.poll() is None:
            return "HELD", {"error": "ALREADY_RUNNING",
                            "run_id": (_CHILD_META or {}).get("run_id")}
        run_id = run_id or new_run_id()
        assert_run_id(run_id)
        ok, code, info = acquire_lock(port, run_id, "manual")
    if not ok:
        if code == D_DISABLED:
            return "DISABLED", {"error": D_DISABLED}
        if code == D_LOCK_HELD:
            return "HELD", {"error": "ALREADY_RUNNING", "run_id": (info or {}).get("run_id")}
        return "ERROR", {"error": code or "LOCK_FAILED"}
    ok2, code2, info2 = launch_pipeline("manual", port=port, dry_run=True, run_id=run_id)
    if not ok2:
        release_lock(run_id)
        if code2 == D_ENTRY_MISSING:
            return "ENTRY_MISSING", {"error": D_ENTRY_MISSING, "run_id": run_id}
        return "LAUNCH_FAILED", {"error": "LAUNCH_FAILED", "run_id": run_id}
    # 手动发起计数：只累加 `manual_attempts`，**不**参与「同一节气月自动重试上限」
    # （裁定：手动是用户显式发起，不该吃掉自动重试额度）；面板会把两列分开显示。
    _bump_manual_attempt(load_plan() or new_plan(None, None), "LAUNCHED")
    return "STARTED", {"run_id": run_id, "trigger": "manual",
                       "started_at_bjt": info2.get("started_at_bjt"),
                       "dry_run": info2.get("dry_run", True), "log": info2.get("log")}


# ---------------------------------------------------------------- 两段式确认：预览与令牌

# 预览里 G_B 只核对「封印清单在不在、读不读得动」，逐件 sha256 重算留给流水线：
# 预览必须秒级返回，而且它是只读检查，不该为了给你一个数字去哈希 1.2GB 的镜像包。
# (相对路径, 名称, 基准根)：project=本仓库所在的项目根，docs=~/Documents 那棵活车间。
SEAL_LISTS = (
    ("v13_shadow_ready_20261018/release_candidate/SHA256SUMS.txt",
     "release_candidate/SHA256SUMS.txt", "project"),
    ("bazi_month_relation_local_v1/work/frozen_baseline.txt",
     "local_v1/work/frozen_baseline.txt", "project"),
    ("v13_shadow_fullmarket_20261018/v13_full_shadow_manifest.sha256",
     "v13_full_shadow_manifest.sha256", "project"),
    ("bazi_history_relation_v1/dist/SHA256SUMS.txt",
     "bazi_history_relation_v1/dist/SHA256SUMS.txt", "docs"),
)


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


def _disk_gib(path: Path):
    """卷余量（GiB，保留一位小数）。取不到返回 (None, None)。只 stat，不写不建。"""
    try:
        usage = shutil.disk_usage(str(path))
    except OSError:
        return None, None
    return round(usage.free / 1024 ** 3, 1), round(usage.total / 1024 ** 3, 1)


def _load_pipeline_module():
    """只读导入编排器模块（`tools/run_monthly_update.py`），让预览用**同一实现**判闸门。

    目的（P1-1）：浅判据会说「五闸全过」而流水线同一棵树上跑出来是 3/7——那是骗用户。
    导入失败/入口缺失一律不抛，只登记原因；调用方据此把该闸判成 `pass=null`
    （「本预览不判：<原因>」），**绝不判成 pass=true**。
    """
    global _PIPELINE_MOD, _PIPELINE_IMPORT_ERROR
    with _MUTEX:
        if _PIPELINE_MOD is not None or _PIPELINE_IMPORT_ERROR is not None:
            return _PIPELINE_MOD
        entry = PIPELINE_ENTRY
        try:
            if not entry.is_file():
                raise OSError(f"流水线入口不存在：{entry.name}")
            import importlib.util
            spec = importlib.util.spec_from_file_location("mu_pipeline_readonly", str(entry))
            if spec is None or spec.loader is None:
                raise OSError("无法为流水线入口建导入规格")
            mod = importlib.util.module_from_spec(spec)
            # 必须先注册进 sys.modules 再执行：3.14 的 dataclass 处理要用
            # `sys.modules[cls.__module__].__dict__` 解析注解，未注册会直接 AttributeError。
            sys.modules[spec.name] = mod
            try:
                spec.loader.exec_module(mod)   # 模块体只定义常量/函数，不触碰任何路径
            except BaseException:
                sys.modules.pop(spec.name, None)
                raise
        except Exception as exc:  # noqa: BLE001 —— 预览面绝不能因为导入问题而 500
            _PIPELINE_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
            _log(f"预览深判据不可用（编排器模块导入失败）：{_PIPELINE_IMPORT_ERROR}")
            return None
        _PIPELINE_MOD = mod
        return mod


def _pipeline_paths(mod):
    """按与 `launch_pipeline()` 完全相同的三根构造 Paths（不新造一套根口径）。"""
    return mod.resolve_paths(project_root=str(BASE_ROOT.parent),
                             docs_root=str(DOCS_ROOT), dev_root=str(BASE_ROOT))


def _verdict_to_gate_fields(verdict, code, message, detail) -> dict:
    """流水线 GateResult → 预览闸门字段：PASS=true / FAIL=false / PENDING=null(不判)。

    PENDING 一律标成「本预览不判」，绝不因为「没数据」就写成通过。
    """
    if verdict == "PASS":
        return {"pass": True, "detail": message, "checks": detail}
    if verdict == "FAIL":
        return {"pass": False, "detail": f"{code}：{message}" if code else message,
                "checks": detail}
    return {"pass": None, "detail": f"本预览不判：{message}", "checks": detail}


def _deep_preview_gates(now: datetime, include_seals: bool = False) -> dict:
    """用编排器同一实现算深判据（G_A 发布态 / G_B 逐件 sha256 / G_C 影子门禁 / G_D 卷）。

    只读：不建目录、不起进程、不写文件。**G_B 的逐件哈希（90 件/17.4GB/约 1 分钟）是
    本机唯一的重量级 I/O**：2026-10-29 起只在 `include_seals=True`（即用户点「更新」做
    预览、或手动发起前）才现算；启动预热、后台 60 秒 tick、`/api/db_status` 一律不哈希，
    把 G_B 判成 null（=「未校验」，前端已按「本预览不判」如实展示），绝不判成 PASS。
    拿不到结论时 `available=False` + 原因，调用方把相关闸门判成 null。
    """
    out = {"available": False, "reason": None, "gates": {}, "min_free_bytes": None,
           "min_free_source": None, "volume_path_label": None,
           "volume_free_bytes": None, "volume_total_bytes": None,
           "gates_total_in_pipeline": PIPELINE_GATES_TOTAL,
           "gates_not_covered_by_preview": [dict(x) for x in PIPELINE_GATES_NOT_COVERED],
           "gates_covered_by_preview": None,
           "last_run": None}
    mod = _load_pipeline_module()
    if mod is None:
        out["reason"] = f"编排器模块不可用（{_PIPELINE_IMPORT_ERROR}）⇒ 本预览不判"
        return out
    try:
        P = _pipeline_paths(mod)
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"三根构造失败（{type(exc).__name__}）⇒ 本预览不判"
        return out
    gates = {}
    # ---- G_A：正式源发布侦测（read_publication_stats + baseline + publication_changed）
    try:
        stats = mod.read_publication_stats(P)
        baseline = None
        if P.baseline_file.exists():
            baseline = json.loads(P.baseline_file.read_text(encoding="utf-8"))
        target = mod.resolve_target_month(P, None, now, mod.resolve_calendar(P))
        ga = mod.gate_a(stats, baseline, target, force_source_check=False,
                        write_baseline=None)
        gates["G_A"] = _verdict_to_gate_fields(ga.verdict, ga.code, ga.message,
                                               dict(ga.detail or {}))
        out["target_month_id"] = target.get("month_id")
        out["target_first_trading_day"] = str(target.get("first_trading_day") or "") or None
    except Exception as exc:  # noqa: BLE001
        gates["G_A"] = {"pass": None,
                        "detail": f"本预览不判：G_A 取数失败（{type(exc).__name__}）",
                        "checks": {}}
    # ---- G_B：逐件 sha256 重算（verify_seals 本体，不再用「清单在位可读」冒充）
    # B 方案（2026-10-29）：启动/后台周期/首页状态一律不哈希，只在 include_seals=True
    # （点「更新」预览、发起前检查）时逐件回读；其余场合如实判 null=「未校验」。
    if not include_seals:
        gates["G_B"] = {"pass": None,
                        "detail": ("未校验：逐件封印哈希只在点『更新』做预览时执行，"
                                   "启动与后台周期不再全量读盘（本次判定不下结论）"),
                        "checks": {"deferred": True}}
    else:
        try:
            verified, bad = mod.verify_seals(P)
            from_list = [b for b in bad if b.get("reason") != "hash_mismatch"]
            mismatched = [b for b in bad if b.get("reason") == "hash_mismatch"]
            total = len(verified) + len(bad)
            detail = {"verified_count": len(verified), "bad_count": len(bad),
                      "total_entries": total,
                      "mismatched": [{"seal": str(b.get("seal")),
                                      "expected": b.get("expected"), "actual": b.get("actual")}
                                     for b in mismatched[:20]],
                      "missing_or_unreadable": [{"seal": str(b.get("seal")),
                                                 "reason": b.get("reason")}
                                                for b in from_list[:20]]}
            if bad:
                verdict, code = "FAIL", mod.E_SEAL_MISMATCH
                msg = (f"封印件校验失败 {len(bad)} 项（哈希不符 {len(mismatched)}、"
                       f"清单登记但文件不可用 {len(from_list)}，共 {total} 件）：拒绝继续，绝不覆盖")
            else:
                verdict, code, msg = "PASS", "", f"{len(verified)} 件封印件逐件 sha256 回读一致"
            gates["G_B"] = _verdict_to_gate_fields(verdict, code, msg, detail)
        except Exception as exc:  # noqa: BLE001
            gates["G_B"] = {"pass": None,
                            "detail": f"本预览不判：封印件逐件哈希取数失败（{type(exc).__name__}）",
                            "checks": {}}
    # ---- G_C：影子跑批门禁（读影子 manifest 本体，不再只看主库那一行留痕）
    try:
        runday = now.strftime("%Y%m%d")
        shadow_dir = P.project_root / f"v13_shadow_fullmarket_{runday}"
        shadow_man = None
        if (shadow_dir / "v13_full_shadow_manifest.json").exists():
            shadow_man = json.loads((shadow_dir / "v13_full_shadow_manifest.json")
                                    .read_text(encoding="utf-8"))
        gc = mod.gate_c(shadow_man, None, node7_ran=False, node11_ran=False)
        gates["G_C"] = _verdict_to_gate_fields(gc.verdict, gc.code, gc.message,
                                               dict(gc.detail or {}))
        gates["G_C"]["shadow_manifest_read"] = bool(shadow_man)
    except Exception as exc:  # noqa: BLE001
        gates["G_C"] = {"pass": None,
                        "detail": f"本预览不判：影子跑批清单读取失败（{type(exc).__name__}）",
                        "checks": {}}
    # ---- G_D：卷余量按流水线同一卷（注入根的父卷）+ 下限读编排器自己的 min_free_bytes()
    try:
        min_free = int(mod.min_free_bytes())
        out["min_free_source"] = "tools/run_monthly_update.min_free_bytes()"
    except Exception:  # noqa: BLE001
        min_free = MIN_FREE_BYTES
        out["min_free_source"] = "update_scheduler.MIN_FREE_BYTES（编排器不可用时的缺省）"
    out["min_free_bytes"] = min_free
    try:
        usage = shutil.disk_usage(str(P.project_root))
        out["volume_free_bytes"] = int(usage.free)
        out["volume_total_bytes"] = int(usage.total)
        # 只回显卷的**名字**，不回显绝对路径（本服务的所有响应面都不外泄本机路径）
        out["volume_path_label"] = Path(P.project_root).name
    except OSError:
        pass
    gates["G_D"] = _verdict_to_gate_fields(
        "PASS" if (out["volume_free_bytes"] or 0) > min_free else "FAIL",
        "" if (out["volume_free_bytes"] or 0) > min_free else mod.E_DISK_LOW,
        (f"写入卷（{out['volume_path_label']}）余量 "
         f"{round((out['volume_free_bytes'] or 0) / 1024 ** 3, 2)} GiB "
         f"{'>' if (out['volume_free_bytes'] or 0) > min_free else '≤'} 下限 "
         f"{round(min_free / 1024 ** 3, 1)} GiB；dry-run 不生成写前副本"
         if (out["volume_free_bytes"] or 0) > min_free else
         f"写入卷（{out['volume_path_label']}）余量不足下限 {round(min_free / 1024 ** 3, 1)} GiB"),
        {"volume": out["volume_path_label"], "min_free_bytes": min_free})
    out["gates"] = gates
    out["gates_covered_by_preview"] = ["G_A", "G_B", "G_C", "G_D"]
    # ---- 上一轮（本编排器自己写的）终态里的写入目标冲突数：让用户看见「这一轮本身还写不进去」
    st = load_state()
    if isinstance(st, dict) and st.get("run_id"):
        conflicts = st.get("write_conflicts")
        out["last_run"] = {
            "run_id": st.get("run_id"), "status": st.get("status"),
            "mode": st.get("mode"), "finished_at_bjt": st.get("finished_at_bjt"),
            "write_conflicts": (len(conflicts) if isinstance(conflicts, list) else None),
            "gates": {k: {"verdict": (v or {}).get("verdict"),
                          "code": (v or {}).get("code"),
                          "message": str((v or {}).get("message") or "")[:160]}
                      for k, v in (st.get("gates") or {}).items()
                      if isinstance(v, dict)} or None}
    out["available"] = True
    return out


def deep_preview_gates(now: datetime, include_seals: bool = False) -> dict:
    """带短缓存的深判据；`include_seals` 决定是否做 G_B 的逐件哈希（默认不做）。

    「轻结论」（不哈希）与「全结论」（逐件哈希）各缓存一份，互不顶替：
    轻结论给启动预热 / tick / `page db_status` 用；全结论只在点「更新」预览时现算。
    缓存条目带 `preview_basis_asof_bjt`（这份结论是几点算的），预览响应原样回显 ⇒
    结论最多滞后 `DEEP_GATE_TTL_SECONDS`，不假装是此刻实时。
    """
    key = "full" if include_seals else "light"
    with _MUTEX:
        ent = _DEEP_CACHE.get(key) or {}
        cached, stamp = ent.get("result"), ent.get("stamp")
        if (isinstance(cached, dict) and stamp is not None
                and (time.monotonic() - stamp) < DEEP_GATE_TTL_SECONDS):
            return cached
    started = time.monotonic()
    res = _deep_preview_gates(now, include_seals=include_seals)
    res["preview_basis_asof_bjt"] = bjtime.now_iso_bjt()
    res["preview_basis_elapsed_seconds"] = round(time.monotonic() - started, 1)
    with _MUTEX:
        _DEEP_CACHE[key] = {"result": res, "stamp": time.monotonic()}
    return res


def _memory_watermark() -> dict:
    """内存水位：总量走 `os.sysconf`，可用量走只读的 `vm_stat`（macOS 无 /proc/meminfo）。

    拿不到就 null，不猜。这只是给人看的参考值，不是任何闸门的判据。
    """
    out = {"total_gib": None, "available_gib": None, "used_ratio": None, "basis": None}
    try:
        total = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGESIZE")
        out["total_gib"] = round(total / 1024 ** 3, 1)
    except (ValueError, OSError):
        total = None
    try:
        res = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5)
        page = None
        stats = {}
        # 首行实测：`Mach Virtual Memory Statistics: (page size of 16384 bytes)`
        m0 = re.search(r"page size of (\d+) bytes", (res.stdout or ""), re.I)
        if m0:
            page = int(m0.group(1))
        for line in (res.stdout or "").splitlines():
            # 数据行实测形如 `Pages free:                                   219590.`（带句点）
            m = re.match(r"^([A-Za-z][A-Za-z ]*?):\s+(\d+)\.?\s*$", line.strip())
            if m:
                stats[m.group(1).strip()] = int(m.group(2))
        if page and total:
            avail_pages = (stats.get("Pages free", 0) + stats.get("Pages inactive", 0)
                           + stats.get("Pages speculative", 0))
            out["available_gib"] = round(avail_pages * page / 1024 ** 3, 1)
            out["used_ratio"] = round(1 - avail_pages * page / total, 3)
            out["basis"] = "vm_stat(free+inactive+speculative) + SC_PHYS_PAGES"
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    if out["basis"] is None and total:
        out["basis"] = "SC_PHYS_PAGES（可用量取不到：本机无 vm_stat）"
    return out


def _seal_list_state() -> list[dict]:
    """四份封印清单：只做 exists + 非空 + 数得清行数 的只读核对，绝不重算大文件哈希。

    `project` 根 = 本包所在项目的上一级；`docs` 根 = `DOCS_ROOT`（自测可整体指到临时树）。
    """
    project_root = PKG_ROOT.parent
    out = []
    for rel, label, base in SEAL_LISTS:
        item = {"seal": label, "path": rel, "base": base, "exists": False,
                "bytes": None, "lines": None, "unreadable": None}
        try:
            root = project_root if base == "project" else DOCS_ROOT
            p = (Path(root) / rel).resolve()
            st = p.stat()
            item["exists"] = p.is_file() and st.st_size > 0
            item["bytes"] = st.st_size
            if item["exists"]:
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    item["lines"] = sum(1 for _ in fh)
        except OSError as exc:
            item["unreadable"] = type(exc).__name__
        out.append(item)
    return out


def _shadow_batch_state(con=None) -> dict:
    """影子跑批那行在本机主库里的留痕（只读 SELECT，main. 限定，不 ANALYZE）。"""
    out = {"version": None, "rows": None, "name": None, "notes": []}
    own = None
    try:
        if con is None:
            own = api.connect("ashare")
            con = own
        rows = con.execute(
            "SELECT name, version, rows FROM main.source_versions"
            " WHERE kind = 'frozen_db'"
            "   AND (name LIKE '%影子%' OR path LIKE '%v13_shadow%')").fetchall()
        if len(rows) == 0:
            out["notes"].append("source_versions 里没有影子跑批行")
        elif len({str(r["version"]) for r in rows}) != 1:
            out["notes"].append("影子跑批行有多个不同的 version，不采信")
        else:
            r = rows[0]
            out["version"] = str(r["version"]).strip()
            out["name"] = str(r["name"])
            out["rows"] = int(r["rows"]) if r["rows"] is not None else None
    except Exception as exc:  # noqa: BLE001
        out["notes"].append(f"读取失败：{type(exc).__name__}")
    finally:
        if own is not None:
            try:
                own.close()
            except Exception:  # noqa: BLE001
                pass
    return out


def _cached_full_gates() -> dict | None:
    """只回读「全结论」（含 G_B 逐件哈希）的新鲜缓存；没有就返回 None，绝不当场现算。

    给 `current_preview_grant`（每次 db_status 都会调）用：那里绝不能触发 17.4GB 哈希。
    """
    with _MUTEX:
        ent = _DEEP_CACHE.get("full") or {}
        if (isinstance(ent.get("result"), dict) and ent.get("stamp") is not None
                and (time.monotonic() - ent["stamp"]) < DEEP_GATE_TTL_SECONDS):
            return ent["result"]
    return None


def collect_preview_state(con=None, term: dict | None = None,
                          include_seals: bool = False,
                          seals_cached_only: bool = False) -> dict:
    """薄封装：自己开的连接一定要还回去（长驻服务里不能漏连接）。

    `include_seals=True` 时 G_B 逐件哈希现算（约 1 分钟）；默认 False = 不读盘、G_B 判 null。
    `seals_cached_only=True`：只认新鲜的「全结论」缓存，没有就退回轻判据（G_B=null），不读盘。
    """
    own_con = None
    if con is None:
        try:
            own_con = api.connect("ashare")
            con = own_con
        except Exception as exc:  # noqa: BLE001 —— 库连不上也只是让相关字段 null，不抛
            _log(f"预览采集连库失败：{type(exc).__name__}")
            con = None
    try:
        return _collect_preview_state(con=con, term=term, include_seals=include_seals,
                                      seals_cached_only=seals_cached_only)
    finally:
        if own_con is not None:
            try:
                own_con.close()
            except Exception:  # noqa: BLE001
                pass


def _collect_preview_state(con=None, term: dict | None = None,
                           include_seals: bool = False,
                           seals_cached_only: bool = False) -> dict:
    """一次只读采集：镜像探测 + 引擎规则版本 + 库内末交易日 + 五道闸判据 + 范围哈希。

    **从不写库、从不拉起进程、从不建锁**；所有子项失败都只是把 pass 判 False 或值判 null。
    连接由外层 `collect_preview_state` 负责开与关；这里传进来的 con 一律只用 SELECT。
    """
    now = current_now_bjt()
    probe = probe_source()
    src_max = probe.get("source_max_trade_date")
    if term is None:
        try:
            term = (term_context(con, now) if con is not None else None) or {}
        except Exception:  # noqa: BLE001
            term = {}
    target = bjtime.first_trading_day_on_or_after(term.get("boundary_at_bjt")) \
        if term.get("boundary_at_bjt") else None
    target_s = target.isoformat() if target else None
    rule_version = read_relation_rule_version(con=con)
    shadow = _shadow_batch_state(con=con)
    last_month, end_at, _ = _last_month_with_data(dataset="ashare", con=con)
    db_last = None
    if end_at:
        e = bjtime.parse_bjt(end_at)
        if e is not None:
            db_last = min(e.date(), bjtime.today_bjt()).isoformat()
    plan = load_plan()
    lock = read_lock()
    free_gib, total_gib = _disk_gib(PKG_ROOT)
    seals = _seal_list_state()

    disabled = is_disabled()
    execute_confirmed = (WORK / "execute.confirmed").exists()
    entry_ok = PIPELINE_ENTRY.is_file()

    gates = []

    # 深判据：G_A/G_B/G_C/G_D 直接走编排器同一实现（read_publication_stats+baseline、
    # verify_seals 逐件哈希、gate_c 读影子 manifest、gate_d 同卷同下限）。
    # G_B 逐件哈希只在 include_seals=True（点「更新」预览）时现算；seals_cached_only 只认
    # 新鲜的全结论缓存（给令牌活跃性检查用，绝不现算）；其余场合轻判据、G_B 判 null。
    if include_seals:
        deep = deep_preview_gates(now, include_seals=True)
    elif seals_cached_only:
        deep = _cached_full_gates() or deep_preview_gates(now)
    else:
        deep = deep_preview_gates(now)
    dgates = deep.get("gates") or {}

    def add(gid, name, passed, detail, **checks):
        gates.append({"id": gid, "name": name, "pass": passed, "detail": detail,
                      "checks": checks})

    def add_deep(gid, name, fallback_msg, **extra_checks):
        """把深判据结果落成一条闸门：True/False/None 三态原样带出，None = 本预览不判。"""
        got = dgates.get(gid)
        checks = dict(extra_checks)
        if not isinstance(got, dict):
            add(gid, name, None, f"本预览不判：{deep.get('reason') or fallback_msg}", **checks)
            return
        # 编排器的 detail 里可能带一个恰好叫 detail 的键（gate_a 失败分支就是这么写的），
        # 与 add() 的位置参数撞名 ⇒ 改名带出，不改判据本身。
        merged = dict(got.get("checks") or {})
        for clash in ("gid", "name", "passed", "detail"):
            if clash in merged:
                merged[f"gate_{clash}"] = merged.pop(clash)
        checks.update(merged)
        add(gid, name, got.get("pass"), got.get("detail") or fallback_msg, **checks)

    # G_A 正式源发布侦测：编排器口径 = 正式源 zip 的 size/sha 与 baseline 不同 **且** 覆盖目标流月
    add_deep("G_A", "正式源发布侦测（编排器同实现）", "源发布态无法与编排器对齐",
             source_max_trade_date=src_max, target_first_trade_date=target_s,
             source_sha12=probe.get("source_sha12"), source_version=probe.get("source_version"),
             basis=list(probe.get("basis") or []), notes=list(probe.get("notes") or []),
             baseline_file_present=(WORK / "source_publication_baseline.json").exists())

    # G_B 冻结件哈希：**逐件 sha256 重算**（不再拿「清单文件在位可读」冒充校验）
    add_deep("G_B", "冻结件逐件哈希（编排器同实现）", "封印件哈希无法与编排器对齐",
             seal_lists=seals)

    # G_C 影子跑批门禁：读影子 manifest 本体（主库那行留痕只作参考，不再当判据）
    add_deep("G_C", "影子跑批门禁（编排器同实现）", "影子跑批门禁无法与编排器对齐",
             local_source_versions_row=shadow)

    # G_D 卷余量：与流水线同一卷（注入根的父卷）、下限读编排器 min_free_bytes()
    add_deep("G_D", "写前副本卷余量（编排器同卷同下限）", "卷余量无法与编排器对齐",
             disk_free_gib=free_gib, disk_total_gib=total_gib,
             volume_path_label=deep.get("volume_path_label"),
             min_free_bytes=deep.get("min_free_bytes"),
             min_free_source=deep.get("min_free_source"))

    # G_E 关系引擎规则版本锁（取不到 ⇒ null ⇒ 保守拒跑；绝不拿调度器构建串冒充）
    locked = RULE_LOCK_DEFAULT
    lock_src = "内置缺省"
    rl = WORK / "rule_version.lock"
    try:
        if rl.exists():
            locked = rl.read_text(encoding="utf-8").strip() or RULE_LOCK_DEFAULT
            lock_src = "work/rule_version.lock"
    except OSError:
        pass
    if rule_version is None:
        g_e = False
        g_e_msg = ("主库 source_versions 取不到关系引擎规则版本 ⇒ null ⇒ G_E 保守拒跑"
                   "（不许拿调度器构建串冒充）")
    else:
        g_e = rule_version == locked
        g_e_msg = (f"规则版本 {rule_version} 与锁定串 {locked}（{lock_src}）一致"
                   if g_e else f"规则口径变了（{rule_version} ≠ 锁定的 {locked}），需人工确认")
    add("G_E", "关系引擎规则版本锁", g_e, g_e_msg,
        rule_version=rule_version, locked_version=locked, lock_source=lock_src)

    # 范围哈希：令牌绑定它。仓库/闸门状态一变动，令牌立刻失效（快照不匹配就要重新检查）。
    scope = {
        "work": str(WORK),
        "mirror": str(MIRROR_ROOT),
        "pipeline_entry": str(PIPELINE_ENTRY),
        "pipeline_entry_exists": entry_ok,
        "execute_confirmed": execute_confirmed,
        "disabled": disabled,
        "probe": {k: probe.get(k) for k in
                  ("source_max_trade_date", "source_sha12", "source_version", "file_count",
                   "merge_total_rows", "span")},
        "rule_version": rule_version,
        "month_id": term.get("month_id"),
        "target_first_trade_date": target_s,
        "db_last_trade_date": db_last,
        "db_last_month_id": last_month,
        "plan": plan,
        "lock": None if lock is None else {"run_id": lock.get("run_id"),
                                           "pid": lock.get("pid")},
        "gates": [{"id": g["id"], "pass": g["pass"]} for g in gates],
        # 深判据是否真的算出来了（不可用时也要进哈希，否则「不判」轮与「判了」轮同一哈希）
        "deep_available": bool(deep.get("available")),
        "volume_free_bytes": deep.get("volume_free_bytes"),
        "db_sig": fingerprint_signature(),
    }
    # 只有**每一条**给出的闸门都真是 True 才算全过；出现 None（不判）或 False 一律不算过。
    # 注意：这是「预览覆盖的那几条」的结论，不等于流水线七闸全过（见 gates_total_in_pipeline）。
    all_pass = bool(gates) and all(g["pass"] is True for g in gates)
    last_run = deep.get("last_run") or {}
    covered = list(deep.get("gates_covered_by_preview") or ["G_A", "G_B", "G_C", "G_D"])
    if "G_E" not in covered:
        covered.append("G_E")
    vol_free = deep.get("volume_free_bytes")
    vol_total = deep.get("volume_total_bytes")
    return {
        "asof_bjt": bjtime.iso_bjt(now),
        "probe": probe,
        "source_max_trade_date": src_max,
        "source_sha12": probe.get("source_sha12"),
        "lag_trading_days": _lag_trading_days(db_last, src_max),
        "db_last_trade_date": db_last,
        "db_last_month_id": last_month,
        "month_id": term.get("month_id"),
        "boundary_at_bjt": term.get("boundary_at_bjt"),
        "target_first_trade_date": target_s,
        "rule_version": rule_version,
        "scheduler_build": SCHEDULER_BUILD,
        "gates": gates,
        "all_gates_pass": all_pass,
        # 预览覆盖面照实声明：流水线一共 7 道闸，本预览判其中 5 道（G_E 用调度器口径），
        # 未覆盖的两道（G_F/G_G）由流水线跑到那一步才判——本字段存在的目的就是不许说「全过」。
        "gates_total_in_pipeline": deep.get("gates_total_in_pipeline") or PIPELINE_GATES_TOTAL,
        "gates_covered_by_preview": covered,
        "gates_not_covered_by_preview": deep.get("gates_not_covered_by_preview") or [],
        "preview_basis_available": bool(deep.get("available")),
        "preview_basis_reason": deep.get("reason"),
        "preview_basis_asof_bjt": deep.get("preview_basis_asof_bjt"),
        "preview_basis_ttl_seconds": DEEP_GATE_TTL_SECONDS,
        "estimated_minutes": ESTIMATED_MINUTES,
        "estimated_minutes_basis": ESTIMATED_MINUTES_BASIS,
        "disk_free_gib": (round(vol_free / 1024 ** 3, 1) if vol_free is not None else free_gib),
        "disk_total_gib": (round(vol_total / 1024 ** 3, 1) if vol_total is not None
                           else total_gib),
        "disk_min_free_bytes": deep.get("min_free_bytes"),
        "disk_min_free_source": deep.get("min_free_source"),
        "volume_path_label": deep.get("volume_path_label"),
        "memory": _memory_watermark(),
        "disabled": disabled,
        "execute_confirmed": execute_confirmed,
        "pipeline_entry_exists": entry_ok,
        "mode": "dry_run",
        # 这一轮 start 之后会不会写库：编排器只认真跑哨兵 `work/execute.confirmed`，
        # 它不在位时**任何文案都不许说「会写库」**（缺文件时 start 只会再走一次 dry-run）。
        "start_writes_db": bool(execute_confirmed),
        "scope": scope,
        "scope_hash": _hash_text(_canonical(scope))[:16],
        # 上一轮（本编排器自己落的终态）的写入目标冲突数：让用户看见「这一轮本身还写不进去」
        "last_run_id": last_run.get("run_id"),
        "last_run_status": last_run.get("status"),
        "last_run_finished_at_bjt": last_run.get("finished_at_bjt"),
        "last_run_write_conflicts": last_run.get("write_conflicts"),
        "last_run_gates": last_run.get("gates"),
    }


def issue_preview_token(state: dict) -> dict:
    """令牌 = 预览结论内容（闸门结论 + 关键数值）的 sha256 前 16 位；绑 10 分钟 + 范围哈希。"""
    core = {
        "gates": [{"id": g["id"], "pass": g["pass"]} for g in state["gates"]],
        "source_max_trade_date": state["source_max_trade_date"],
        "source_sha12": state["source_sha12"],
        "target_first_trade_date": state["target_first_trade_date"],
        "month_id": state["month_id"],
        "db_last_trade_date": state["db_last_trade_date"],
        "rule_version": state["rule_version"],
        "estimated_minutes": state["estimated_minutes"],
        "scope_hash": state["scope_hash"],
    }
    token = _hash_text(_canonical(core))[:16]
    issued = bjtime.now_bjt()
    expires = issued + timedelta(seconds=PREVIEW_TOKEN_TTL_SECONDS)
    with _MUTEX:
        for stale in [t for t, v in _PREVIEW_TOKENS.items()
                      if bjtime.parse_bjt(v.get("expires_at_bjt")) is not None
                      and bjtime.parse_bjt(v["expires_at_bjt"]) < issued]:
            _PREVIEW_TOKENS.pop(stale, None)
        _PREVIEW_TOKENS[token] = {
            "issued_at_bjt": bjtime.iso_bjt(issued),
            "expires_at_bjt": bjtime.iso_bjt(expires),
            "scope_hash": state["scope_hash"],
            "gates": core["gates"],
            "all_gates_pass": state["all_gates_pass"],
        }
    return {"preview_token": token, "issued_at_bjt": bjtime.iso_bjt(issued),
            "expires_at_bjt": bjtime.iso_bjt(expires),
            "ttl_seconds": PREVIEW_TOKEN_TTL_SECONDS, "scope_hash": state["scope_hash"]}


def validate_preview_token(token):
    """校验令牌：返回 (是否通过, 码, 令牌记录)。

    缺 → `NO_PREVIEW_TOKEN`；不是本进程签发的 / 过期 / 状态快照变了 → `PREVIEW_STALE`。
    三种情况都**不拉起任何进程**，也不告诉你差在哪（避免把内部状态当侧信道）。

    语义（fixround3 把文案与实况对齐，行为不变）：这枚令牌**不是「一次性消耗品」**，
    它绑定的是「签发那一刻的状态快照」——同一份快照在 10 分钟内可重复校验，
    状态一变（闸门/镜像/锁/plan/库指纹任一）scope_hash 就不同，立刻 PREVIEW_STALE。
    方向仍然是 fail-closed：只有「状态没动过」才会放行。
    """
    if not isinstance(token, str) or not token.strip():
        return False, "NO_PREVIEW_TOKEN", None
    with _MUTEX:
        entry = _PREVIEW_TOKENS.get(token.strip())
    if entry is None:
        return False, "PREVIEW_STALE", None
    exp = bjtime.parse_bjt(entry.get("expires_at_bjt"))
    if exp is None or exp < bjtime.now_bjt():
        return False, "PREVIEW_STALE", entry
    try:
        # 令牌里绑的是「全结论」（含 G_B）的快照 ⇒ 复核也必须用同一口径，否则必然 PREVIEW_STALE。
        # 全结论缓存（5 分钟）通常直接命中；过期才会真哈希一次（此时用户确实在点「启动」）。
        current = collect_preview_state(include_seals=True)["scope_hash"]
    except Exception as exc:  # noqa: BLE001 —— 采不到状态就当作范围变了，fail-closed
        _log(f"令牌复核时状态采集失败，按 PREVIEW_STALE 处理：{type(exc).__name__}")
        return False, "PREVIEW_STALE", entry
    if current != entry.get("scope_hash"):
        return False, "PREVIEW_STALE", entry
    return True, "OK", entry


def preview_update(con=None, term: dict | None = None) -> dict:
    """`POST /api/db_update/preview`：只读检查 + 签发令牌。**绝不写库、绝不拉起流水线**。

    停用哨兵在位时**照样跑**（它是纯只读检查，屏蔽它等于让用户看不见闸门逐条）；
    拒绝发生在「确认并写库」那一步（`manual_start` 如实返回 503 DISABLED）。
    """
    # B 方案（2026-10-29）：这是唯一的「真更新」入口——G_B 逐件哈希在这里现算
    # （约 1 分钟，页面按钮显示「检查中…」，不会假装 5 分钟内有效）。
    state = collect_preview_state(con=con, term=term, include_seals=True)
    tok = issue_preview_token(state)
    not_covered = "、".join(x["id"] for x in state["gates_not_covered_by_preview"])
    if state["disabled"]:
        next_step = ("本结论只是只读检查结果。停用哨兵 work/update.disabled 在位："
                     "自动与手动都不会触发，「确认并写库」这一步会被后端如实拒绝（503）。"
                     "恢复由负责人删除该文件（本接口不会代删）。")
    elif any(g["pass"] is None for g in state["gates"]):
        undecided = "、".join(g["id"] for g in state["gates"] if g["pass"] is None)
        next_step = (f"有闸门本预览判不了（{undecided}）：不给可确认的结论，"
                     f"需由负责人裁定后重新检查。")
    elif not state["all_gates_pass"]:
        failed = "、".join(g["id"] for g in state["gates"] if g["pass"] is not True)
        next_step = (f"闸门未全过（{failed}）：先处置未过项，再重新检查。"
                     f"另：流水线共 {state['gates_total_in_pipeline']} 道闸，"
                     f"本预览未覆盖 {not_covered}。")
    elif not state["pipeline_entry_exists"]:
        next_step = "流水线入口缺失（tools/run_monthly_update.py）：本轮 start 会 500"
    elif not state["start_writes_db"]:
        next_step = ("带 preview_token POST /api/db_update/start 完成第二段确认。"
                     "**本轮不会写库**：work/execute.confirmed 不在位，"
                     "start 只会起一轮 dry-run（编排器只打印写入目标，一个字节都不写库）。")
    else:
        next_step = ("带 preview_token POST /api/db_update/start 完成第二段确认。"
                     "work/execute.confirmed 在位 ⇒ 本轮 start 起的流水线会写库；"
                     "确认前请再看一眼「本轮写入目标冲突数」。")
    return {
        "asof_bjt": state["asof_bjt"],
        "preview_token": tok["preview_token"],
        "issued_at_bjt": tok["issued_at_bjt"],
        "expires_at_bjt": tok["expires_at_bjt"],
        "ttl_seconds": tok["ttl_seconds"],
        "scope_hash": tok["scope_hash"],
        "gates": state["gates"],
        "all_gates_pass": state["all_gates_pass"],
        "gates_total_in_pipeline": state["gates_total_in_pipeline"],
        "gates_covered_by_preview": state["gates_covered_by_preview"],
        "gates_not_covered_by_preview": state["gates_not_covered_by_preview"],
        "preview_basis_available": state["preview_basis_available"],
        "preview_basis_reason": state["preview_basis_reason"],
        "month_id": state["month_id"],
        "target_first_trade_date": state["target_first_trade_date"],
        "source_max_trade_date": state["source_max_trade_date"],
        "source_sha12": state["source_sha12"],
        "db_last_trade_date": state["db_last_trade_date"],
        "db_last_month_id": state["db_last_month_id"],
        "lag_trading_days": state["lag_trading_days"],
        "rule_version": state["rule_version"],
        "scheduler_build": state["scheduler_build"],
        "estimated_minutes": state["estimated_minutes"],
        "estimated_minutes_basis": state["estimated_minutes_basis"],
        "disk_free_gib": state["disk_free_gib"],
        "disk_min_free_bytes": state["disk_min_free_bytes"],
        "disk_min_free_source": state["disk_min_free_source"],
        "volume_path_label": state["volume_path_label"],
        "memory": state["memory"],
        "mode": state["mode"],
        "disabled": state["disabled"],
        "execute_confirmed": state["execute_confirmed"],
        # 本接口自己一个字节都不写（只读检查 + 内存里签一枚令牌）；
        # 「这一轮 start 之后会不会写库」看 start_writes_db（= execute.confirmed 在不在位）。
        "writes_anything": False,
        "start_writes_db": state["start_writes_db"],
        "last_run_id": state["last_run_id"],
        "last_run_status": state["last_run_status"],
        "last_run_finished_at_bjt": state["last_run_finished_at_bjt"],
        "last_run_write_conflicts": state["last_run_write_conflicts"],
        "last_run_gates": state["last_run_gates"],
        "attempts_auto": int((load_plan() or {}).get("attempts") or 0),
        "attempts_manual": int((load_plan() or {}).get("manual_attempts") or 0),
        "next_step": next_step,
    }


def current_preview_grant() -> dict | None:
    """db_status 用：现在有没有一枚仍然有效（未过期且范围哈希没变）的令牌。"""
    now = bjtime.now_bjt()
    with _MUTEX:
        live = {t: v for t, v in _PREVIEW_TOKENS.items()
                if (bjtime.parse_bjt(v.get("expires_at_bjt"))) is not None
                and bjtime.parse_bjt(v["expires_at_bjt"]) >= now}
    if not live:
        return None
    try:
        # 只认新鲜的全结论缓存：db_status 每次都走这里，绝不能触发 17.4GB 逐件哈希；
        # 全结论超过 5 分钟即视为令牌不再活跃（UI 只给「重新检查」，fail-closed）。
        current = collect_preview_state(seals_cached_only=True)["scope_hash"]
    except Exception:  # noqa: BLE001
        return None
    for token, entry in live.items():
        if entry.get("scope_hash") == current:
            return {"token": token, "expires_at_bjt": entry.get("expires_at_bjt"),
                    "all_gates_pass": entry.get("all_gates_pass")}
    return None


def available_actions(state_bits: dict | None = None) -> list:
    """前端按这个决定显示哪个按钮：`["preview"]` / `["preview","start"]` / `[]`。

    * 停用哨兵在 → `[]`（自动手动都不触发，别显示可点的按钮）；
    * 有进程/有活锁 → `[]`（正在跑，重复触发没意义）；
    * 否则至少给 `preview`；只有手里有**仍然匹配当前状态**的有效令牌时才追加 `start`。
    """
    bits = state_bits or {}
    if bits.get("disabled") or bits.get("running"):
        return []
    acts = ["preview"]
    if bits.get("has_valid_preview_token"):
        acts.append("start")
    return acts


# ---------------------------------------------------------------- 调度线程

def scheduler_info() -> dict:
    with _MUTEX:
        alive = bool(_THREAD is not None and _THREAD.is_alive())
    return {"pid": os.getpid() if _SCHED_STARTED_AT_BJT else None,
            "port": _SCHED_PORT, "started_at_bjt": _SCHED_STARTED_AT_BJT, "alive": alive}


def start(port: int | None = None, root=None, work_dir=None) -> dict:
    """在 main() 里调用一次：起补跑检查 + 60 秒轮询。重复调用只生效一次。"""
    if os.environ.get("BAZI_DISABLE_JOBS") == "1":
        raise RuntimeError("BAZI_DISABLE_JOBS=1（测试实例禁用调度器）")
    global _THREAD, _SCHED_STARTED_AT_BJT, _SCHED_PORT
    if root is not None or work_dir is not None:
        set_roots(root=root, work_dir=work_dir)
    with _MUTEX:
        if _THREAD is not None and _THREAD.is_alive():
            return scheduler_info()
        _SCHED_PORT = port
        _SCHED_STARTED_AT_BJT = bjtime.now_iso_bjt()
        _THREAD = threading.Thread(target=_loop, name="db-update-scheduler", daemon=True)
        _THREAD.start()
    _log(f"调度线程已启动 pid={os.getpid()} port={port} work={WORK}"
         f"（tick={TICK_SECONDS}s, 补跑延迟={CATCHUP_DELAY_SECONDS}s,"
         f" 调度器构建 {SCHEDULER_BUILD}）")
    return scheduler_info()


def _loop() -> None:
    """整条循环 try/except：任何异常只登记，绝不冒泡到请求面。"""
    try:
        try:
            # 只预热「轻判据」（G_A/G_C/G_D，毫秒级）；G_B 逐件哈希自 2026-10-29 起
            # 不再在启动时执行（17.4GB 会吃满外置盘、拖慢首屏，改到点「更新」预览时现算）。
            deep_preview_gates(bjtime.now_bjt())
        except Exception as exc:  # noqa: BLE001
            _log(f"启动预热深判据失败（闸门将判 null，不判通过）：{type(exc).__name__}")
        time.sleep(CATCHUP_DELAY_SECONDS)
        tick_once(trigger="catchup")        # 服务重启错过了 20:00 的补跑检查
        while True:
            time.sleep(TICK_SECONDS)
            tick_once(trigger="auto")
    except Exception as exc:  # noqa: BLE001
        _log(f"调度线程已退出（请求面不受影响）：{type(exc).__name__}: {exc}")
        try:
            _write_decision({"decision": D_ERROR, "at": bjtime.now_iso_bjt(),
                             "note": f"调度线程异常：{type(exc).__name__}"})
        except Exception:  # noqa: BLE001
            pass


def tick_once(trigger: str = "auto", now: datetime | None = None) -> dict:
    """一个 tick：只做纯只读比较 + 计划/决策落盘；命中才走拉起。

    `now` 只给自测注入「当前时刻」用（生产缺省走 `current_now_bjt()`）。
    """
    now = bjtime.to_bjt(now or current_now_bjt())
    decision: dict = {"decision": D_ERROR, "at": bjtime.iso_bjt(now), "trigger": trigger}
    try:
        _ensure_dirs()
        _reap_child()
        # 轻判据缓存续热（毫秒级，纯只读）：G_B 逐件哈希只在「更新」预览时现算，
        # 后台 tick 不再读 17.4GB 封印件（2026-10-29 B 方案）。
        with _MUTEX:
            idle_child = _CHILD is None or _CHILD.poll() is not None
        if idle_child:
            try:
                deep_preview_gates(now)
            except Exception as exc:  # noqa: BLE001
                _log(f"深判据续热失败（闸门将判 null，不判通过）：{type(exc).__name__}")
        maybe_invalidate_caches()          # 换库后清进程内缓存（T2 在别的进程清不动）
        con = None
        try:
            con = api.connect("ashare")
            decision = evaluate_trigger(now=now, con=con)
        finally:
            if con is not None:
                try:
                    con.close()
                except Exception:  # noqa: BLE001
                    pass
        decision["trigger"] = trigger
        code = decision.get("decision")
        # 计划行按**待入账月**落盘（新语义）；显式注入 term 的旧路径没有 pending_month ⇒ 用 month_id。
        plan_month = decision.get("pending_month") or decision.get("month_id")

        # 每个流月的计划行由调度器落盘（唯一写者），换月即重置 attempts
        if plan_month:
            cur_plan = load_plan()
            if (cur_plan is None or cur_plan.get("month_id") != plan_month
                    or cur_plan.get("scheduler_build") not in (None, SCHEDULER_BUILD)):
                fresh = new_plan(plan_month,
                                 decision.get("target_first_trade_date"))
                # 「手动发起 M 次」不能因为补建 plan 行而被悄悄清零：
                # 老行没有月份 ⇒ 它是 manual_start 自己建的桩行（先手动、tick 后到），
                # 把它的 manual_attempts 带过来；真正换月则照旧按月重新起算。
                if cur_plan and cur_plan.get("month_id") is None:
                    fresh["manual_attempts"] = int(cur_plan.get("manual_attempts") or 0)
                save_plan(fresh)

        if code in (D_DISABLED,):
            _write_decision(decision)
            return decision
        if code == D_RUNNING:
            _write_decision(decision)
            return decision

        if code == D_DUE:
            with _MUTEX:
                ok, acode, info = acquire_lock(_SCHED_PORT, new_run_id(), trigger)
            if not ok:
                decision["decision"] = D_LOCK_HELD if acode == D_LOCK_HELD else acode
                decision["lock_run_id"] = (info or {}).get("run_id")
                _write_decision(decision)
                return decision
            lock_run_id = (info or {}).get("run_id")
            plan = load_plan() or new_plan(plan_month,
                                           decision.get("target_first_trade_date"))
            _bump_attempt(plan, D_LAUNCHED, now=now)
            if decision.get("pending_month"):
                # 2026-10-22 入账语义：自动路径只以 staging 拉起 post_month（绝不 promote）。
                ok2, lcode, linfo = launch_post_month(
                    decision["pending_month"], trigger, port=_SCHED_PORT,
                    run_id=lock_run_id, now=now)
                mode = "staging"
            else:
                # 桩缝路径（term 注入）保留旧拉起口径；生产不会走到这里。
                ok2, lcode, linfo = launch_pipeline(trigger, port=_SCHED_PORT,
                                                    dry_run=True, run_id=lock_run_id)
                mode = "dry_run"
            decision["launch"] = {"ok": ok2, "code": lcode, "run_id": lock_run_id,
                                  "mode": mode, "month_id": plan_month,
                                  "log": (linfo or {}).get("log")}
            if not ok2:
                release_lock(lock_run_id)
                plan = load_plan() or plan
                plan = dict(plan)
                plan["last_status"] = lcode
                save_plan(plan)
                _log(f"拉起未成功：{lcode}（锁已释放，不重试到下一个北京日）")
            _write_decision(decision)
            return decision

        if code in (D_SOURCE_NOT_READY, D_SESSION_NOT_CLOSED, D_MAX_ATTEMPTS, D_NO_CALENDAR):
            plan = load_plan() or new_plan(plan_month,
                                           decision.get("target_first_trade_date"))
            if code != D_MAX_ATTEMPTS:
                last = bjtime.parse_bjt(plan.get("last_attempt_bjt"))
                if last is None or last.date() != now.date():
                    plan = _bump_attempt(plan, code, now=now)
                    decision["attempts"] = plan["attempts"]
            else:
                plan = dict(plan)
                plan["last_status"] = code
                save_plan(plan)
        _write_decision(decision)
        return decision
    except Exception as exc:  # noqa: BLE001 —— 兜底：任何异常只登记，不冒泡
        _log(f"tick 异常（请求面不受影响）：{type(exc).__name__}: {exc}")
        decision = {"decision": D_ERROR, "at": bjtime.iso_bjt(now), "trigger": trigger,
                    "note": f"{type(exc).__name__}"}
        _write_decision(decision)
        return decision


# ---------------------------------------------------------------- 指纹 / 状态口径

def _db_paths() -> dict:
    """五个库的路径（只读；缺失时值为 None）。"""
    paths = {"ashare": config.ASHARE_DB, "etf": config.ETF_DB, "cap_tier": config.CAP_DB}
    try:
        import v5_api
        paths["v5_slice"] = v5_api.SLICE_DB
    except Exception:  # noqa: BLE001
        paths["v5_slice"] = None
    try:
        import dayun_api
        paths["gender"] = dayun_api.EXT_DBS.get("ashare")
    except Exception:  # noqa: BLE001
        paths["gender"] = None
    return paths


def _short_sha(token) -> str | None:
    s = str(token or "").strip().lower()
    if len(s) >= 12 and all(c in "0123456789abcdef" for c in s[:12]):
        return s[:12]
    return None


# T2 的 `work/db_fingerprints.json` 用 `databases.{ashare_main,etf,gender_dayun,
# market_cap_tier,...}` + `sha256_12` / `size_bytes` / `generated_at_bjt`；
# 早期约定写的是 `dbs.{ashare,...}` + `sha12` / `bytes` / `built_at_bjt`。两种都吃。
_DB_KEY_ALIASES = {
    "ashare": ("ashare", "ashare_main", "main", "ashare_v1"),
    "etf": ("etf", "etf_v1", "etf_main"),
    "gender": ("gender", "gender_dayun", "dayun_ashare", "gender_dayun_ashare"),
    "cap_tier": ("cap_tier", "market_cap_tier", "cap", "cap_tier_v1"),
    "v5_slice": ("v5_slice", "v5", "slice", "v5_lowpoint_slice"),
}
_SHA_KEYS = ("sha12", "sha256_12", "sha256_12prefix", "sha256")
_BYTES_KEYS = ("bytes", "size_bytes", "size")
_BUILT_KEYS = ("built_at_bjt", "mtime_bjt", "generated_at_bjt", "built_at")


def _pick(d: dict, keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def db_fingerprints() -> dict:
    """`work/db_fingerprints.json` 是权威来源；缺失/对不上时按 sqlite 的 size+mtime 派生弱指纹。

    5GB 大库**禁止**每请求全量哈希：本函数只 stat + 读那个小 JSON，绝不 read 库文件。
    权威条目只有在「它描述的 path 就是服务真正在读的那个文件」时才采信，否则退回弱指纹
    并标 `path_mismatch:true`（T2 现在给 gender 记的是 gender_dayun_v1.sqlite，而 8737 读的是
    ..._proxy.sqlite ⇒ 这一项照实回落弱指纹，不拿别人家的文件哈希冒充当前库）。
    """
    authoritative = _read_json(FINGERPRINT_FILE)
    entries = {}
    if authoritative:
        for container in ("dbs", "databases"):
            blob = authoritative.get(container)
            if isinstance(blob, dict):
                entries = blob
                break
        else:
            entries = {k: v for k, v in authoritative.items()
                       if isinstance(v, (dict, str)) and k in _DB_KEY_ALIASES}
        top_built = _pick(authoritative, _BUILT_KEYS)
    else:
        top_built = None
    resolved = {}
    for name, path in _db_paths().items():
        try:
            resolved[name] = Path(path).resolve() if path else None
        except (OSError, RuntimeError):
            resolved[name] = None
    out = {}
    for name, path in resolved.items():
        got = None
        mismatch = False
        for alias in _DB_KEY_ALIASES.get(name, (name,)):
            val = entries.get(alias) if isinstance(entries, dict) else None
            if val is None:
                continue
            if not isinstance(val, dict):
                val = {"sha12": val}
            sha = _short_sha(_pick(val, _SHA_KEYS))
            entry_built = bjtime.parse_bjt(_pick(val, _BUILT_KEYS) or top_built)
            stated = val.get("path")
            if sha and stated and path is not None:
                try:
                    if Path(str(stated)).resolve() != path:
                        mismatch = True
                        sha = None                # 不是同一个文件：不采信
                except OSError:
                    mismatch = True
                    sha = None
            if sha:
                b = _pick(val, _BYTES_KEYS)
                got = {"sha12": sha, "bytes": int(b) if isinstance(b, int) else None,
                       "built_at_bjt": bjtime.iso_bjt(entry_built) if entry_built else None,
                       "weak": False, "present": bool(val.get("exists", True))}
                break
        if got is None:
            try:
                st = Path(path).stat()
                mt = datetime.fromtimestamp(st.st_mtime, bjtime.BJT)
                got = {"sha12": f"st:{st.st_size}-{bjtime.iso_bjt(mt)}",
                       "bytes": int(st.st_size),
                       "built_at_bjt": bjtime.iso_bjt(mt),
                       "weak": True, "present": True}
            except (OSError, TypeError, ValueError):
                got = {"sha12": None, "bytes": None, "built_at_bjt": None,
                       "weak": False, "present": False}
            if mismatch:
                got["path_mismatch"] = True
        got.setdefault("weak", False)
        out[name] = got
    return out


def _last_month_with_data(dataset: str = "ashare", con=None):
    """复用既有「库里最后有数据流月」口径（api.calendar_now 的 data_span.last_month）。

    **按当前 dataset 取**（P2 修正）：以前 con 为 None 时硬编连 A股库、又把
    `api.calendar_now(con, None, "ashare")` 写死，dataset="etf" 时会拿 A股库的月份
    冒充 ETF 库（两库恰好同期时看不出来）。现在连接与历法口径都跟着 dataset 走。
    """
    ds = dataset if dataset in getattr(api, "DATASETS", {}) else "ashare"
    own = None
    try:
        if con is None:
            own = api.connect(ds)
            con = own
        payload = api.calendar_now(con, None, ds)
        span = payload.get("data_span") or {}
        last_month = span.get("last_month")
        end_at = None
        for m in payload.get("months") or []:
            if m.get("month_id") == last_month:
                end_at = m.get("end_at")
                break
        return last_month, end_at, payload
    except Exception as exc:  # noqa: BLE001
        _log(f"库内最后流月读取失败：{type(exc).__name__}")
        return None, None, None
    finally:
        if own is not None:
            try:
                own.close()
            except Exception:  # noqa: BLE001
                pass


def _lag_trading_days(db_date: str | None, src_date: str | None) -> int | None:
    """源里最新交易日相对库里最后有数据日的**交易日**差距（周一~周五口径，不含法定节假日）。

    任一侧拿不到就返回 null —— 不填 0、不填空串。
    """
    a, b = bjtime.parse_bjt(db_date), bjtime.parse_bjt(src_date)
    if a is None or b is None:
        return None
    a, b = a.date(), b.date()
    if b <= a:
        return 0
    n, d = 0, a + timedelta(days=1)
    while d <= b:
        if bjtime.is_trading_weekday(d):
            n += 1
        d += timedelta(days=1)
    return n


def _log_tail(run_id: str, lines: int = LOG_TAIL_LINES):
    path = LOGS / f"update_{run_id}.log"
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    parts = raw.splitlines()
    return parts[-lines:] if parts else []


def next_planned_bjt(decision: dict | None, plan: dict | None,
                     now: datetime | None = None) -> str | None:
    """下一次计划执行（北京串）。

    语义（2026-10-22 裁定）：**交节后首个交易日 20:00**（= 待入账月走完后、源就绪时的
    最早入账时刻，取自 `post_month.can_post_month().first_eligible_date`）；该时刻已过而仍被
    阻断 ⇒ 顺延到下一个交易日 20:00（重试节奏）。已成功 / 尝试耗尽 ⇒ None。
    注意：源未就绪时这个值只是**重试节奏**，不代表源届时一定就绪（没有这种预言能力）。
    """
    d = decision or {}
    code = d.get("decision")
    planned = bjtime.parse_bjt(d.get("eligible_at_bjt") or d.get("planned_at_bjt"))
    if (plan or {}).get("succeeded_bjt") or code in (D_SUCCEEDED, D_MAX_ATTEMPTS):
        return None
    if planned is None:
        return None
    now = bjtime.to_bjt(now or current_now_bjt())
    if now < planned:
        return bjtime.iso_bjt(planned)
    if code in (D_SOURCE_NOT_READY, D_SESSION_NOT_CLOSED, D_COOLDOWN, D_LAUNCHED, D_DUE,
                D_LAST_TD_NOT_CLOSED):
        nxt = now.date() + timedelta(days=1)
        while not bjtime.is_trading_weekday(nxt):
            nxt += timedelta(days=1)
        return bjtime.iso_bjt(datetime.combine(nxt, datetime.min.time(),
                                               tzinfo=bjtime.BJT).replace(
            hour=TRIGGER_HOUR_BJT))
    return None


def overall_status(plan: dict | None, state: dict | None, decision: dict | None,
                   lock: dict | None) -> str:
    if is_disabled():
        return D_DISABLED
    # 用 `_holder_live`（pid 存活 **且** 启动时刻与锁里记录的一致）而不是裸 `_pid_alive`：
    # pid 被复用成的无辜进程不该让首页显示「正在跑」，也不该让锁永远回收不掉。
    if lock and lock.get("_holder_live"):
        return "RUNNING"
    st = str((state or {}).get("status") or "").upper()
    if st in SUCCESS_STATES and (plan or {}).get("succeeded_bjt"):
        return "SUCCESS"
    if _is_failed(st):
        return "FAILED"
    code = (decision or {}).get("decision")
    if code == D_NOT_DUE:
        return "WAITING"
    if code == D_COOLDOWN:
        # 冷却日本身不是状态：把上一轮的真实阻断码留着（否则首页会退回 IDLE）
        return (plan or {}).get("last_status") or D_COOLDOWN
    if code in (D_SOURCE_NOT_READY, D_SESSION_NOT_CLOSED, D_MAX_ATTEMPTS, D_NO_CALENDAR,
                D_DISABLED, D_ERROR, D_MONTH_NOT_ENDED, D_LAST_TD_NOT_CLOSED,
                D_GATE_UNAVAILABLE):
        return code
    if (plan or {}).get("succeeded_bjt"):
        return "SUCCESS"
    return "IDLE"


def error_payload(plan, state, decision) -> dict | None:
    """状态端点里的 `error`：优先流水线自己写的 error，其次调度侧阻断码。"""
    err = (state or {}).get("error")
    if isinstance(err, dict) and err.get("code"):
        return {"code": str(err.get("code")), "message": str(err.get("message") or "")[:200]}
    st = str((state or {}).get("status") or "").upper()
    # T2 的终态把错误码写在顶层 error_code（NODE_FAILED / GATED / PIPELINE_ALREADY_RUNNING…）
    code_top = (state or {}).get("error_code")
    if code_top and (_is_failed(st) or st in ("BLOCKED", "STAGED_ONLY")):
        return {"code": str(code_top),
                "message": str((state or {}).get("error_message")
                               or f"流水线终态 {st}")[:200]}
    if _is_failed(st):
        return {"code": st, "message": "上一轮流水线未完成"}
    code = (decision or {}).get("decision")
    if code in (D_SOURCE_NOT_READY, D_MAX_ATTEMPTS, D_NO_CALENDAR, D_ENTRY_MISSING, D_ERROR,
                D_MONTH_NOT_ENDED, D_LAST_TD_NOT_CLOSED, D_GATE_UNAVAILABLE):
        # 2026-10-22：入账语义的三条阻断码也走这里 ⇒ 页面（既有渲染逻辑）会把
        # `note` 的可读文案（如「戊戌月未结束，11-07 17:52 后入账」）显示出来。
        return {"code": code, "message": str((decision or {}).get("note") or "")[:200]}
    if _is_failed((plan or {}).get("last_status")):
        return {"code": str(plan.get("last_status")), "message": "上一轮流水线未完成"}
    return None


def db_status(con=None, dataset: str = "ashare") -> dict:
    """首页那一行的全部口径：**已带 +08:00 的字符串**，前端不做时区换算。

    从未成功过的字段一律 null（不填 0、不填空串）。
    """
    plan = load_plan()
    state = load_state()
    decision = load_decision()
    lock = read_lock()
    try:
        age = time.time() - DECISION_FILE.stat().st_mtime
    except OSError:
        age = None
    fresh = bool(decision) and age is not None and age < 15 * 60
    if not fresh:
        try:
            decision = evaluate_trigger(con=con if dataset == "ashare" else None)
        except Exception as exc:  # noqa: BLE001
            decision = {"decision": D_ERROR, "note": f"{type(exc).__name__}"}
    last_month, end_at, _payload = _last_month_with_data(
        dataset=dataset, con=con if dataset == "ashare" else None)
    db_last = None
    if end_at:
        e = bjtime.parse_bjt(end_at)
        if e is not None:
            today = current_now_bjt().date()
            d = min(e.date(), today)          # 未走完的流月不越过今天
            db_last = d.isoformat()
    src_max = (decision or {}).get("source_max_trade_date")
    # `collect_preview_state` 的库内口径是 **A股主库**（探测的也是 A股正式源镜像）。
    # dataset="etf" 时绝不能拿它的 db_* / lag 覆盖 ETF 库自己的读数（P2：两库恰好同期才没露馅）。
    probe = {}
    if dataset == "ashare":
        try:
            probe = collect_preview_state(
                con=con,
                term={"month_id": (decision or {}).get("month_id"),
                      "boundary_at_bjt": (decision or {}).get("boundary_at_bjt"),
                      "next_boundary_at_bjt": (decision or {}).get("next_boundary_at_bjt")}
                if (decision or {}).get("boundary_at_bjt") else None)
        except Exception as exc:  # noqa: BLE001 —— 状态面绝不能因为探测失败而 500
            _log(f"db_status 采集失败（字段回落 null）：{type(exc).__name__}")
    if src_max is None:
        src_max = probe.get("source_max_trade_date")
    if src_max is None and dataset != "ashare":
        # ETF 数据集不跑整份 A股预览采集（那套闸门是主库口径），但正式源发布线是同一份
        # 本地镜像 JSON 探测，拿它补源侧读数；口径在 `source_side_basis` 里写清，不含糊。
        try:
            src_max = probe_source().get("source_max_trade_date")
        except Exception as exc:  # noqa: BLE001
            _log(f"db_status 源侧补数失败（{dataset}）：{type(exc).__name__}")
    succeeded = (plan or {}).get("succeeded_bjt")
    if dataset == "ashare":
        db_last = probe.get("db_last_trade_date") or db_last
    lag = (probe.get("lag_trading_days") if dataset == "ashare"
           and probe.get("lag_trading_days") is not None
           else _lag_trading_days(db_last, src_max))
    # 待入账月（2026-10-22 新口径）：A股走「决策快照优先，缺失则只读解析一次（60 秒短缓存）」；
    # **其它 dataset 一律只用该库自己的解析结果**——决策快照是 A股调度器写的，拿它冒充 ETF 的
    # 待入账月就是 P2 那类「两库同期才没露馅」的错，不能重犯。
    pending = None
    try:
        pending = resolve_pending_month(con=con if dataset == "ashare" else None,
                                        dataset=dataset)
    except Exception as exc:  # noqa: BLE001 —— 状态面不能因此 500
        _log(f"db_status 待入账月解析失败（字段回落 null）：{type(exc).__name__}")
    dec = decision if dataset == "ashare" else {}
    pending_month = (dec or {}).get("pending_month") or (pending or {}).get("pending_month")
    pending_end = ((dec or {}).get("pending_month_ends_at_bjt")
                   or (pending or {}).get("pending_month_ends_at_bjt"))
    pending_start = ((dec or {}).get("pending_month_start_at_bjt")
                     or (pending or {}).get("pending_month_start_at_bjt"))
    last_posted = ((dec or {}).get("last_posted_month_id")
                   or (pending or {}).get("last_month_id"))
    grant = current_preview_grant()
    acts = available_actions({"disabled": is_disabled(),
                              "running": bool(lock and lock.get("_holder_live"))
                                         or bool(child_info()),
                              "has_valid_preview_token": bool(grant)})
    return {
        "asof_bjt": now_iso_bjt(),
        # 关系引擎的规则版本（G-E 真正比的那个数）；取不到就是 null ⇒ G-E fail-closed。
        # 以前这里填的是调度器自己的构建串，那是拿错了对象：会让 G-E 永远「一致」。
        "rule_version": probe.get("rule_version"),
        "rule_version_source": ("main.source_versions:frozen_db 影子跑批行"
                                if probe.get("rule_version") else "unavailable_fail_closed"),
        "scheduler_build": SCHEDULER_BUILD,
        "status": overall_status(plan, state, decision, lock),
        "scheduler": scheduler_info(),
        "month_id": (decision or {}).get("month_id") or (plan or {}).get("month_id"),
        "boundary_at_bjt": (decision or {}).get("boundary_at_bjt"),
        "next_boundary_at_bjt": (decision or {}).get("next_boundary_at_bjt"),
        "target_first_trade_date": (decision or {}).get("target_first_trade_date")
                                   or (plan or {}).get("target_first_trade_date"),
        # ↓ 2026-10-22 入账语义：待入账月 = 库内最后有数据流月的下一个流月（前端暂不读，
        #   先供后台/联调；页面文案由 status/decision + error.message（note）承担）。
        "pending_month": pending_month,
        "pending_month_ends_at_bjt": pending_end,
        "pending_month_start_at_bjt": pending_start,
        "last_posted_month_id": last_posted,
        "blocked_reason": (dec or {}).get("blocked_reason"),
        "eligible_at_bjt": (dec or {}).get("eligible_at_bjt"),
        "last_trading_day_in_window": (dec or {}).get("last_trading_day_in_window"),
        "db_last_month_id": ((probe.get("db_last_month_id") or last_month)
                             if dataset == "ashare" else last_month),
        "db_last_trade_date": db_last,
        "db_scope_dataset": dataset,
        "source_max_trade_date": src_max,
        # 源身份用 manifest 里 2026.zip 的 sha256 前 12 位；**不是文件 mtime**（理由见
        # probe_source 的文档：2026-10-22 误触重下载把 60 个文件 mtime 全刷成当天，
        # 内容却与 10-15 发布件 60/60 全等 ⇒ mtime 已被证明不能当「源是否更新」的判据）。
        "source_sha12": probe.get("source_sha12"),
        "source_probe_basis": (probe.get("probe") or {}).get("basis") or [],
        "source_probe_notes": (probe.get("probe") or {}).get("notes") or [],
        "lag_trading_days": lag,
        # 滞后口径逐条标明：db 侧跟着当前 dataset 的那个库；源侧是本地正式源镜像的发布线
        # （本机没有为 ETF 单独探测一条发布线，两侧不是同一个数据集时这一条只作参考）。
        "lag_basis": ("ashare 库 vs ashare 正式源镜像" if dataset == "ashare"
                      else f"{dataset} 库 vs 正式源镜像（本机未单独探测 {dataset} 发布线）"),
        "source_side_basis": "本地只读镜像 source_manifest/README（不联网、不连 NAS）",
        "available_actions": acts,
        "preview_required": True,
        "preview_token_expires_at_bjt": (grant or {}).get("expires_at_bjt"),
        # 两列分开：`attempts` 是**自动**路径的累计尝试（受 MAX_ATTEMPTS_PER_MONTH 上限管），
        # `manual_attempts` 是用户显式点「更新 → 确认并写库」发起的次数（不占自动额度）。
        # 以前只有前者，用户会误以为「自动重试没用完」其实手动已经点了好几轮。
        "attempts": int((plan or {}).get("attempts") or 0),
        "attempts_auto": int((plan or {}).get("attempts") or 0),
        "attempts_manual": int((plan or {}).get("manual_attempts") or 0),
        "max_attempts_per_month": MAX_ATTEMPTS_PER_MONTH,
        "last_attempt_bjt": (plan or {}).get("last_attempt_bjt"),
        "last_status": (plan or {}).get("last_status"),
        "last_success_bjt": succeeded,
        "next_planned_bjt": next_planned_bjt(decision, plan),
        "decision": (decision or {}).get("decision"),
        "trigger": (decision or {}).get("trigger"),
        "lock": None if lock is None else {
            "held": True, "run_id": lock.get("run_id"), "pid": lock.get("pid"),
            "kind": lock.get("kind"), "acquired_at_bjt": lock.get("acquired_at_bjt"),
            "age_seconds": lock.get("_age_seconds"), "pid_alive": lock.get("_pid_alive"),
            "holder_live": lock.get("_holder_live"), "live_reason": lock.get("_live_reason")},
        "running_run_id": (lock or {}).get("run_id") if lock and lock.get("_holder_live") else None,
        "state": None if state is None else {
            "run_id": state.get("run_id"), "status": state.get("status"),
            "stage": state.get("stage"), "trigger": state.get("trigger"),
            "mode": state.get("mode"),
            "started_at_bjt": state.get("started_at_bjt") or state.get("created_at_bjt"),
            "updated_at_bjt": state.get("updated_at_bjt"),
            "finished_at_bjt": state.get("finished_at_bjt"),
            "error_code": state.get("error_code"),
            # T1 约定用 stages，T2 用 nodes：两个都给，前端读一个就行
            "stages": state.get("stages") or state.get("nodes") or [],
            "current_node": (state.get("nodes") or [{}])[-1].get("key")
                            if isinstance(state.get("nodes"), list) and state.get("nodes")
                            else None,
            "target_month": state.get("target_month"),
            "source_max_trade_date_detected": state.get("source_max_trade_date_detected")},
        "dbs": db_fingerprints(),
        "error": error_payload(plan, state, decision),
        "note": str((decision or {}).get("note") or "")[:200],
        "disabled": is_disabled(),
    }


def update_state_payload(run_id: str | None = None) -> dict:
    """`/api/db_update/state?run_id=`：状态文件 + 计划 + 锁 + 日志尾部最后 40 行。

    读之前先回收一次（P1-2）：这个端点正是前端每 5 秒轮询的那个，子进程一退出就能立刻
    在这里看到合成/移交后的终态，而不是等下一个 60 秒 tick 才结束「进行中」。
    回收只做自己那件事（写 plan/state、放锁），异常一律兜住，不影响读数面。
    """
    try:
        _reap_child()
    except Exception as exc:  # noqa: BLE001
        _log(f"state 端点回收子进程异常（不影响读数）：{type(exc).__name__}")
    state = load_state()
    plan = load_plan()
    lock = read_lock()
    target = run_id or (state or {}).get("run_id") or (lock or {}).get("run_id")
    err = None
    if not target:
        err = {"code": "NO_RUN_ID", "message": "还没有任何一轮更新"}
    log_tail = _log_tail(target) if target else []
    return {
        "asof_bjt": bjtime.now_iso_bjt(),
        "run_id": target,
        "requested_run_id": run_id,
        "state": state if (state is None or target is None
                           or state.get("run_id") in (None, target)) else None,
        "state_run_id": (state or {}).get("run_id"),
        "plan": plan,
        "lock": None if lock is None else {
            "held": True, "run_id": lock.get("run_id"), "pid": lock.get("pid"),
            "kind": lock.get("kind"), "acquired_at_bjt": lock.get("acquired_at_bjt"),
            "age_seconds": lock.get("_age_seconds"), "pid_alive": lock.get("_pid_alive"),
            "holder_live": lock.get("_holder_live"), "live_reason": lock.get("_live_reason")},
        "log_file": f"logs/update_{target}.log" if target else None,
        "log_tail": log_tail,
        "log_tail_lines": len(log_tail),
        "child": child_info(),
        "scheduler": scheduler_info(),
        "error": err,
    }


# ---------------------------------------------------------------- 更新记录（原子追加）

RECORD_HEADER = ("# 更新记录\n\n"
                 "由 `tools/run_monthly_update.py` 在每轮流水线结束时追加一行；"
                 "调度器事件（陈旧锁改名、流水线入口缺失）登记在同一张表。\n\n"
                 "| 北京时间 | trigger | status | 错误码 | 一句话 |\n"
                 "|---|---|---|---|---|\n")


def append_update_record(trigger: str, status: str, error_code: str | None,
                         note: str) -> bool:
    """每轮结束（成功或失败）由流水线侧追加一行；调度器只在陈旧锁改名时登记一次。

    原子追加（O_APPEND + fsync）；失败只返回 False，绝不影响主流程。旧文件只登记不删除。
    """
    safe_note = str(note or "").replace("|", "¦").replace("\n", " ")[:200]
    line = (f"| {bjtime.now_iso_bjt()} | {str(trigger or '-')} | {str(status or '-')} "
            f"| {str(error_code or '-')} | {safe_note} |\n")
    try:
        REPORTS.mkdir(parents=True, exist_ok=True)
        need_header = not RECORD_MD.exists() or RECORD_MD.stat().st_size == 0
        with open(RECORD_MD, "a", encoding="utf-8") as fh:
            if need_header:
                fh.write(RECORD_HEADER)
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        return True
    except OSError as exc:
        _log(f"更新记录追加失败（不影响主流程）：{type(exc).__name__}")
        return False
