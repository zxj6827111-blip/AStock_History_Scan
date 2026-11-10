#!/usr/bin/env python3
"""T2 月度数据更新流水线编排器（run_monthly_update.py）.

契约（已确认，不扩大范围）
==========================
把「NAS 正式源只读镜像 → 投影 → 身份主表 → 全市场扫描 → 关系库重建 → 桥接导出 →
影子跑批 → 主库 stage1-7 → 扩展联动 → ETF 支路 → 独立核验 → stats/reports/v5 切片 →
状态与指纹」13 个既有节点表驱动成一条可审计的流水线。

安全纪律（代码层面强制）
------------------------
1. **默认 --dry-run**：不执行任何子进程，且只允许落 **dry-run 写入白名单** 上的两件自有状态件
   （`work/update_state.json` + `logs/update_<run_id>.log`，见 `DRY_RUN_WRITE_WHITELIST`）。
   白名单之外任何 enabled=True 的写入都会被 `assert_dry_run_writable()` 当场拒绝
   （`DRY_RUN_WRITE_DENIED`），指纹/更新记录/baseline/库仍是一个字节都不写。
   放开这两件的原因（P1-2）：**任何一轮都必须有终态**——`status` + `finished_at_bjt` +
   `error{code,message}`；成功计划轮 `DRY_RUN_PLAN`，编排器非零退出 `EXITED_FAILED`。
   白名单会原样打印在 `--print-plan` 与每轮 dry-run 日志里。
2. 五道闸 G_A..G_E 是显式纯函数（可判定、可单测）；只有 ``--execute`` **且**
   ``--accept-gates`` **且** 七闸（G_A..G_G）全 PASS，才会：原子替换正式主库/ETF 库、
   向 ``extensions/update_all_after_main_db.sh`` 传 ``--accept``。
   任何一闸失败 → 绝不传 --accept（decide_accept() 双向可断言）。
3. 构建阶段一切产物只写带 run_id 的新目录 / staging 路径；正式库改由
   ``os.replace`` 原子替换（替换前先做 G_D 写前副本并逐文件 sha256 回读）。
   失败即丢弃 staging，正式库一字节不动。
4. 封印件（4 份 SHA256SUMS/manifest/frozen_baseline）只读校验，任何写入意图直接失败。
5. 时间戳一律固定 UTC+8 aware datetime，字段后缀 ``_bjt``；不用 naive now()、
   不引入 zoneinfo/tzdata、不用 UTC。
6. 只读 ``app/api.py``/``app/server.py``/调度器，不修改；缓存清交由服务侧调度器
   检测 ``work/db_fingerprints.json`` 后完成（见 FINGERPRINT_HANDOFF）。

依赖：仅标准库。真跑（NAS/扫描/影子/stage1-7）留到集成阶段由负责人发起。

增量语义（20261022 用户裁定，取代「每月全量重建整库」）
========================================================
* **缺省无根**（第一优先项）：``Paths`` 不再回落到旧 Documents 树（已并入项目根）。
  三个根必须由 ``MONTHLY_UPDATE_{PROJECT,DOCS,DEV}_ROOT`` / ``--*-root`` / ``--root``
  显式给出；一个都不给 ⇒ ``NO_ROOT_CONFIGURED`` 直接拒绝启动（连 --dry-run 也不跑）。
  真实树路径只作为「识别用常量」保留（``KNOWN_REAL_*_ROOT``），绝不再当缺省值。
* **三层节点表**：事实层（追加）只允许写新流月的行；聚合层（stats_*、
  relation_type_market_stats、页面五统计）每次从事实表全量确定性重算（不近似、
  不跳过、不降格）；验收层（stage4-7、影子、独立核验）保持全量。
* **G-F 历史不变性门禁**：替换前把新库与旧库在重叠区间逐表比对（每表行数、
  每票×年行级校验和、聚合层历史键数值），要求逐字节等值；只允许「新增流月」
  造成新增。判据里没有「偏离阈值」这种东西——重叠区间就是等值。
* **G-G 历史漂移侦测**：纯增量会静默跳过上游对历史的修订（600018.SH 跨主体
  拼接、2026 补 39 行、suspend_d 全市场重拉），故每月对批次/镜像历史区间做
  按票×年的轻量校验和，与 ``work/history_drift_baseline.json`` 比对；不一致 ⇒
  拒绝增量追加并报 ``HISTORY_DRIFT_DETECTED``，需要一次人工裁定的全量对齐。
  基线只在门禁通过时更新，且走原子写。
* ``--mode incremental``（缺省）/ ``--mode full``（季度互验）；``--compare-run`` 对账。
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------- 常量

BJT = timezone(timedelta(hours=8))
MIN_FREE_BYTES = 40 * 1024 ** 3            # G_D 卷余量下限
RULE_VERSION_DEFAULT = "bazi-relation-research-v1.3.0-provisional"
RUN_ID_RE = re.compile(r"^MU-\d{8}T\d{6}$")   # 文件名里不许有冒号
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TRIGGERS = ("auto", "manual", "catchup")
ASSIGN = (9, 30)                            # 与 independent_verify.py 同口径（09:30 锚点归月）
STATE_SCHEMA = "astock-monthly-update-state-v1"
FINGERPRINT_SCHEMA = "astock-db-fingerprints-v1"
BASELINE_SCHEMA = "astock-source-publication-baseline-v1"
DRIFT_SCHEMA = "astock-history-drift-baseline-v1"
LAYER_REPORT_SCHEMA = "astock-monthly-update-layer-report-v1"
MODES = ("incremental", "full")
MODE_INCREMENTAL = "incremental"
MODE_FULL = "full"

# 三层节点表的层名（节点登记与 run 报告都用这套词）
LAYER_SOURCE = "源层（只读取源，不改库）"
LAYER_FACT = "事实层（追加）"
LAYER_AGG = "聚合层（全量确定性重算）"
LAYER_ACCEPT = "验收层（保持全量）"
LAYER_DELIVER = "交付层（原子替换与记账）"
LAYERS = (LAYER_SOURCE, LAYER_FACT, LAYER_AGG, LAYER_ACCEPT, LAYER_DELIVER)
# 节点 mode_policy：append=只写新流月；full_recompute=每轮全量重扫重算；
# acceptance=验收保持全量不许降格；readonly=不改库；deliver=替换与记账。
POLICY_APPEND = "append"
POLICY_FULL_REBUILD = "full_rebuild"     # --mode full：事实层照旧整表重建（互验用）
POLICY_FULL_RECOMPUTE = "full_recompute"
POLICY_ACCEPTANCE = "acceptance"
POLICY_READONLY = "readonly"
POLICY_DELIVER = "deliver"
# 聚合层/验收层在增量模式下也不许被跳过（「增量」不等于「少算」）
NO_SKIP_POLICIES = (POLICY_FULL_RECOMPUTE, POLICY_ACCEPTANCE)

LARGE_INPUT_LIMIT = 1 * 1024 ** 3           # >1GiB 的输入用头尾摘要，避免每节点全量哈希
SHADOW_GATE_PASS_KEYS = (
    "PRE_RUN_RECONCILIATION", "FULL_SHADOW_STAGE_A", "FULL_SHADOW_STAGE_B",
    "FULL_SHADOW_KEYSET", "FULL_SHADOW_AB_LEAKAGE", "FULL_SHADOW_VERSION_ISOLATION",
)
SHADOW_GATE_YES_KEYS = ("FULL_SHADOW_FROZEN_UNCHANGED", "RESEARCH_SHADOW_COMPLETE")
SHADOW_GATE_MUST_STAY_NO = ("PRODUCTION_READY",)

# 错误码（写进状态与 reports/更新记录.md）
E_SOURCE_NOT_READY = "SOURCE_NOT_READY"
E_SOURCE_BASELINE_MISSING = "SOURCE_BASELINE_MISSING"
E_SEAL_MISMATCH = "SEAL_HASH_MISMATCH"
E_SEAL_MISSING = "SEAL_FILE_MISSING"
E_SEALED_TARGET = "SEALED_TARGET_CONFLICT"
E_DISK_LOW = "DISK_SPACE_LOW"
E_BACKUP_MISMATCH = "BACKUP_SHA_MISMATCH"
E_RULE_VERSION = "RULE_VERSION_MISMATCH"
E_GATE_FAILED = "GATE_FAILED"
E_ACCEPT_WITHOUT_GATES = "ACCEPT_WITHOUT_GATES"
E_NODE_FAILED = "NODE_FAILED"
E_DISABLED = "PIPELINE_DISABLED"
E_ARTIFACT_MISSING = "ARTIFACT_MISSING"
# 缺省无根（本轮事故的第一优先项修复）
E_NO_ROOT = "NO_ROOT_CONFIGURED"
# G-F 历史不变性
E_HISTORY_MUTATED = "HISTORY_MUTATED"
E_HISTORY_ROWS_LOST = "HISTORY_ROWS_LOST"
E_HISTORY_TABLE_DROPPED = "HISTORY_TABLE_DROPPED"
E_UNEXPECTED_NEW_ROWS = "UNEXPECTED_NEW_ROWS"
E_STAGING_NOT_VERIFIABLE = "STAGING_NOT_VERIFIABLE"
# G-G 历史漂移侦测
E_HISTORY_DRIFT = "HISTORY_DRIFT_DETECTED"
E_DRIFT_BASELINE_MISSING = "HISTORY_DRIFT_BASELINE_MISSING"
E_DRIFT_BASELINE_WRITE = "DRIFT_BASELINE_WRITE_CONFLICT"
# 增量语义纪律
E_AGG_SKIP = "AGGREGATE_SKIP_FORBIDDEN"
E_APPEND_SCOPE_EMPTY = "INCREMENTAL_NO_NEW_MONTH"
E_COMPARE_RUN = "COMPARE_RUN_UNAVAILABLE"
E_BAD_MODE = "BAD_MODE"
# P1-2：dry-run 写入白名单之外的落盘企图、编排器自身非零退出的终态码
E_DRY_RUN_WRITE_DENIED = "DRY_RUN_WRITE_DENIED"
E_EXITED_FAILED = "EXITED_FAILED"


PY = sys.executable or "python3"
BASH = "/bin/bash"


# ---------------------------------------------------------------- 时间纪律

def now_bjt() -> datetime:
    """固定 UTC+8 的 aware datetime（绝不返回 naive、绝不用 UTC）。"""
    return datetime.now(BJT)


def iso_bjt(dt: datetime) -> str:
    return dt.astimezone(BJT).strftime("%Y-%m-%dT%H:%M:%S") + "+08:00"


def date_bjt(dt: datetime) -> str:
    return dt.astimezone(BJT).strftime("%Y-%m-%d")


def compact_bjt(dt: datetime) -> str:
    return dt.astimezone(BJT).strftime("%Y%m%dT%H%M%S")


def parse_bjt_instant(text: str) -> datetime:
    """解析库内 `2026-11-07T17:52:04+08` 之类的交节时刻（缺时区按 +08 补）。"""
    s = (text or "").strip()
    if not s:
        raise ValueError("empty instant")
    s = s.replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if re.search(r"[+-]\d{2}$", s):
        s += ":00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BJT)
    return dt.astimezone(BJT)


# ---------------------------------------------------------------- 摘要工具

def sha256_file(path: Path, chunk: int = 1 << 22) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for blk in iter(lambda: f.read(chunk), b""):
            h.update(blk)
    return h.hexdigest()


def digest_of(path: Path, limit: int = LARGE_INPUT_LIMIT) -> dict:
    """节点输入指纹：<=1GiB 全量 sha256；更大的取「头 4MiB + 尾 4MiB + 大小 + mtime」。

    返回 {"sha12": ..., "mode": "full"|"head_tail", "size": ...}，避免每月重复
    对 5GB 级输入做全量哈希（全量哈希只在流水线收尾做一次，见 fingerprints）。
    """
    try:
        st = path.stat()
    except OSError:
        return {"sha12": None, "mode": "missing", "size": None}
    if st.st_size <= limit:
        return {"sha12": sha256_file(path)[:12], "mode": "full", "size": st.st_size}
    h = hashlib.sha256()
    edge = 4 * 1024 * 1024
    with path.open("rb") as f:
        h.update(f.read(edge))
        try:
            f.seek(max(0, st.st_size - edge))
            h.update(f.read(edge))
        except OSError:
            pass
    h.update(str(st.st_size).encode())
    h.update(str(st.st_mtime_ns).encode())
    return {"sha12": h.hexdigest()[:12], "mode": "head_tail", "size": st.st_size}


MIN_FREE_ENV = "MONTHLY_UPDATE_MIN_FREE_BYTES"      # 自测/联调用：覆盖 40GiB 下限
RELATION_REBUILD_ENV = "MONTHLY_UPDATE_RELATION_REBUILD"
SCANNER_CLI_DEFAULT = ("/Users/zhangxingjin/.qoder-cn/skills/astock-bazi-history-scanner"
                      "/scripts/scanner.py")


def min_free_bytes() -> int:
    raw = os.environ.get(MIN_FREE_ENV)
    try:
        return int(raw) if raw else MIN_FREE_BYTES
    except ValueError:
        return MIN_FREE_BYTES


def relation_rebuild_authorized() -> bool:
    """人工授权开关：仅当集成阶段负责人裁定「关系库允许原地重建（写回 snapshot/evidence/dist，
    并随之重发 SHA256SUMS）」时才置位。缺省关闭 ⇒ 节点 5 的封印件写入被 G_B 直接判失败。"""
    return os.environ.get(RELATION_REBUILD_ENV, "") == "authorized"


def scanner_cli() -> Path:
    return Path(os.environ.get("SCANNER_CLI") or SCANNER_CLI_DEFAULT)


# ---------------------------------------------------------------- 路径集

@dataclass
class Paths:
    """所有路径集中在这里，方便用 mktemp 假树做单元自测。"""
    project_root: Path                      # /Volumes/.../AStock_History_Scan
    docs_root: Path                         # project_root（数据已并入）
    dev_root: Path                          # project_root/bazi_month_relation_tengod_dev_v1

    @property
    def local_v1(self) -> Path:
        return self.project_root / "bazi_month_relation_local_v1"

    @property
    def local_scripts(self) -> Path:
        return self.local_v1 / "scripts"

    @property
    def local_db_dir(self) -> Path:
        return self.local_v1 / "database"

    @property
    def main_db(self) -> Path:
        return self.local_db_dir / "bazi_month_relation_local_v1.sqlite"

    @property
    def extensions_sh(self) -> Path:
        return self.local_v1 / "extensions" / "update_all_after_main_db.sh"

    @property
    def etf_v1(self) -> Path:
        return self.project_root / "bazi_month_relation_etf_v1"

    @property
    def etf_scripts(self) -> Path:
        return self.etf_v1 / "scripts"

    @property
    def etf_db(self) -> Path:
        return self.etf_v1 / "database" / "bazi_month_relation_etf_v1.sqlite"

    @property
    def shadow_old(self) -> Path:
        return self.project_root / "v13_shadow_fullmarket_20261018"

    @property
    def release_candidate(self) -> Path:
        return self.project_root / "v13_shadow_ready_20261018" / "release_candidate"

    @property
    def relation_v1(self) -> Path:
        return self.docs_root / "bazi_history_relation_v1"

    @property
    def relation_dist(self) -> Path:
        return self.relation_v1 / "dist"

    @property
    def relation_sealed_db(self) -> Path:
        return self.relation_dist / "history_stock_relations_v1.sqlite"

    @property
    def scripts(self) -> Path:
        return self.docs_root / "scripts"

    @property
    def mirror(self) -> Path:
        return self.docs_root / "source_mirror"

    @property
    def daily_dir(self) -> Path:
        return self.mirror / "正式源" / "全A日K_合并版"

    @property
    def source_input(self) -> Path:
        return self.docs_root / "source_input"

    @property
    def batches(self) -> Path:
        return self.docs_root / "batches"

    @property
    def work(self) -> Path:
        return self.dev_root / "work"

    @property
    def state_file(self) -> Path:
        return self.work / "update_state.json"

    @property
    def baseline_file(self) -> Path:
        return self.work / "source_publication_baseline.json"

    @property
    def rule_lock(self) -> Path:
        return self.work / "rule_version.lock"

    @property
    def disable_flag(self) -> Path:
        return self.work / "update.disabled"

    @property
    def lock_file(self) -> Path:
        return self.work / "pipeline.lock"

    @property
    def execute_confirmed(self) -> Path:
        """人工哨兵：只有负责人创建了这个文件，真树 --execute 才被允许。"""
        return self.work / "execute.confirmed"

    @property
    def fingerprints(self) -> Path:
        return self.work / "db_fingerprints.json"

    @property
    def drift_baseline(self) -> Path:
        """G-G 历史漂移基线（按票×年的历史校验和）；只在门禁通过时原子更新。"""
        return self.work / "history_drift_baseline.json"

    @property
    def runs_dir(self) -> Path:
        return self.work / "runs"

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def layer_report_path(self, run_id: str) -> Path:
        """三层对账报告：G-F/G-G 结论 + 每节点 sha/rows/耗时，供 --compare-run。"""
        return self.run_dir(run_id) / "layer_report.json"

    @property
    def logs(self) -> Path:
        return self.dev_root / "logs"

    @property
    def reports(self) -> Path:
        return self.dev_root / "reports"

    @property
    def update_log_md(self) -> Path:
        return self.reports / "更新记录.md"

    @property
    def backups(self) -> Path:
        return self.dev_root / "backups"


ENV_PROJECT_ROOT = "MONTHLY_UPDATE_PROJECT_ROOT"
ENV_DOCS_ROOT = "MONTHLY_UPDATE_DOCS_ROOT"
ENV_DEV_ROOT = "MONTHLY_UPDATE_DEV_ROOT"
ENV_ROOT = "MONTHLY_UPDATE_ROOT"
DEV_DIR_NAME = "bazi_month_relation_tengod_dev_v1"

# 只用于「识别这是不是现网真实树」，**绝不再当缺省值**（20261022 事故的根因就是
# Paths 缺省回落到旧 Documents 树，单测于是打到生产链路）。
KNOWN_REAL_PROJECT_ROOT = Path("/Volumes/workSSD/software development/AStock_History_Scan")
# 兼容旧名字：旧调用点/旧测试引用过 DEFAULT_PROJECT_ROOT；它现在只是识别常量。
DEFAULT_PROJECT_ROOT = KNOWN_REAL_PROJECT_ROOT


def known_real_docs_root() -> Path:
    """现网 docs 位置（已并入项目根；仅用于识别，不作为任何取值来源）。"""
    return KNOWN_REAL_PROJECT_ROOT


class NoRootConfigured(Exception):
    """缺省无根：根目录没有任何显式注入时报的错（绝不静默用真实路径）。"""


def resolve_paths(*, project_root: str | None = None, docs_root: str | None = None,
                  dev_root: str | None = None, root: str | None = None,
                  env: dict | None = None) -> Paths:
    """根目录只能来自显式注入：CLI 参数 > 各自环境变量 > --root/ROOT 摊平。

    三者不齐 ⇒ ``NoRootConfigured``（上层打印 ``NO_ROOT_CONFIGURED`` 并非零退出）。
    函数体内没有任何 Path.home()/KNOWN_REAL_* 兜底——这是本文件最重要的一条改动。
    """
    env = dict(os.environ) if env is None else env
    proj = project_root or env.get(ENV_PROJECT_ROOT)
    docs = docs_root or env.get(ENV_DOCS_ROOT)
    dev = dev_root or env.get(ENV_DEV_ROOT)
    base = root or env.get(ENV_ROOT)
    if base:
        proj = proj or base
        docs = docs or base
        dev = dev or str(Path(base) / DEV_DIR_NAME)
    missing = [name for name, val in ((ENV_PROJECT_ROOT + " 或 --project-root", proj),
                                      (ENV_DOCS_ROOT + " 或 --docs-root", docs),
                                      (ENV_DEV_ROOT + " 或 --dev-root", dev)) if not val]
    if missing:
        raise NoRootConfigured(
            f"{E_NO_ROOT}：根目录未显式配置，拒绝启动（本工具已无「回落到真实路径」的缺省）。"
            f"缺失项={missing}。自测请用 --root $(mktemp -d) 一把指向假树；"
            f"现网真跑由负责人显式注入三根 + work/execute.confirmed 人工哨兵。")
    return Paths(project_root=Path(proj), docs_root=Path(docs), dev_root=Path(dev))


def default_paths(env: dict | None = None) -> Paths:
    """兼容旧调用点：现在等价于 resolve_paths()，**没有任何缺省根**。"""
    return resolve_paths(env=env)


def is_production_tree(P: Paths) -> bool:
    """是否指向现网真实树（只与识别常量比对，不作为取值来源）。"""
    try:
        proj, docs, dev = (P.project_root.resolve(), P.docs_root.resolve(),
                           P.dev_root.resolve())
    except OSError:
        return False
    if proj == KNOWN_REAL_PROJECT_ROOT.resolve():
        return True
    if dev == (KNOWN_REAL_PROJECT_ROOT / DEV_DIR_NAME).resolve():
        return True
    return docs == known_real_docs_root().resolve()


E_ALT_ROOT_UNAUTHORIZED = "ALT_ROOT_NOT_AUTHORIZED"
E_EXECUTE_NOT_CONFIRMED = "EXECUTE_NOT_CONFIRMED"


def guard_roots(P: Paths, execute: bool, log: Tee | None) -> tuple[bool, str]:
    """防误触：真树 --execute 需要人工哨兵文件；改根 --execute 需要显式开关。

    这一道是本轮自测事故的直接教训（假树环境变量漏设 ⇒ 单元测试打到真链路），
    因此宁可多一道「人不动一下就不许真跑」的闸。真跑由负责人在集成阶段发起。
    加强点：docs_root 落到旧 Documents 树也按现网树对待
    （事故里节点 1/2/3 写的全是 docs 树），判定不再依赖「是否等于缺省值」——
    缺省值现在已经不存在了。
    """
    if not execute:
        return True, ""
    if not is_production_tree(P):
        if os.environ.get("MONTHLY_UPDATE_ALLOW_ALT_ROOT", "") != "1":
            return False, (f"{E_ALT_ROOT_UNAUTHORIZED}：根目录被改到 {P.project_root}"
                           f"（docs={P.docs_root}），非现网树真跑需 "
                           "MONTHLY_UPDATE_ALLOW_ALT_ROOT=1")
        return True, ""
    if not P.execute_confirmed.exists():
        return False, (f"{E_EXECUTE_NOT_CONFIRMED}：现网树缺人工哨兵文件 "
                       f"{P.execute_confirmed}；真跑前由负责人创建该文件（默认绝不许真跑）")
    return True, ""



# ---------------------------------------------------------------- dry-run 写入白名单（P1-2）
#
# 本轮唯一放开的口子。原设计是「dry-run 一个字节都不写」，那连带把**终态**也没写：
# 页面于是永远显示 `RUNNING / 阶段 init`、`#db-update-btn` 自己禁死、前端每 5 秒无限轮询
# （`work/update_state.json` 里 `finished_at_bjt=null`、`error=null` 就是现场证据）。
# 现在放开**且只放开**本编排器自有的两件状态件，让任何一轮都有结束时刻与原因：
#   1) work/update_state.json     —— 本轮状态（status / finished_at_bjt / error{code,message}）
#   2) logs/update_<run_id>.log   —— 本轮日志（rc≠0 时的 stderr 摘要就在这件里）
# 其它任何写入目标（正式库 / staging / batches/ / reports/更新记录.md / 指纹 /
# source baseline / 漂移基线 / 扩展库 / 层报告 / work/runs/**）在 dry-run 下**仍然必须 0 字节**：
# 由 `_assert_dry_run_writable()` 在真正落盘之前拦下（拦不住就抛，绝不静默写），
# 并由 `--print-plan` 把下面这份白名单原样打印出来（不是「见代码」）。
DRY_RUN_WRITE_WHITELIST = (
    "work/update_state.json",        # == Paths.state_file
    "logs/update_<run_id>.log",      # == Paths.logs / f"update_{run_id}.log"
)


class DryRunWriteDenied(RuntimeError):
    """dry-run 里试图写白名单之外的目标：直接拒绝，不写、不假装成功。"""


#: dry-run 保护下的允许清单（resolve 后的绝对路径字符串）；None = 本进程没启动 dry-run 保护
#: （真跑，或单测直接调 atomic_write_*，此时行为与今天完全一致）。
_DRY_RUN_ALLOWED: set[str] | None = None


def dry_run_whitelist(P: "Paths", run_id: str) -> tuple[Path, Path]:
    """白名单的**绝对路径**版本（比较时两侧都 resolve，避开 /var→/private/var 这类软链）。"""
    return (P.state_file, P.logs / f"update_{run_id}.log")


def set_dry_run_guard(P: "Paths", run_id: str, active: bool) -> None:
    """main() 在拿到三根与合法 run_id 之后调用一次；execute 传 active=False 关掉保护。"""
    global _DRY_RUN_ALLOWED
    if not active:
        _DRY_RUN_ALLOWED = None
        return
    _DRY_RUN_ALLOWED = {str(p.resolve()) for p in dry_run_whitelist(P, run_id)}


def assert_dry_run_writable(path: Path) -> None:
    """所有**要落盘**的写入都必须先过这里（atomic_write_* / append_line / Tee.open）。"""
    if _DRY_RUN_ALLOWED is None:
        return
    if str(Path(path).resolve()) not in _DRY_RUN_ALLOWED:
        raise DryRunWriteDenied(
            f"{E_DRY_RUN_WRITE_DENIED}：dry-run 只允许写 "
            + "、".join(DRY_RUN_WRITE_WHITELIST)
            + f"；本轮试图写 {path}")


def whitelist_banner(P: "Paths", run_id: str) -> list[str]:
    """给日志 / `--print-plan` 的原文行：白名单是什么、不在名单里的会怎样。"""
    return [
        "DRY_RUN_WRITE_WHITELIST（dry-run 允许落盘的**只有**这两件自有状态件）：",
        f"  * {DRY_RUN_WRITE_WHITELIST[0]}  -> {P.state_file.name}（本轮状态，终态也写这里）",
        f"  * {DRY_RUN_WRITE_WHITELIST[1]}  -> update_{run_id}.log（本轮日志）",
        "  其余目标（库/batches/reports/指纹/baseline/层报告）在 dry-run 下仍 0 字节；"
        f"越界写入直接 {E_DRY_RUN_WRITE_DENIED} 拒绝。",
    ]


# ---------------------------------------------------------------- 日志（tee）

class Tee:
    """tee 到 stdout；落盘到 logs/update_<run_id>.log 的口径见上面的终态白名单。

    2026-10-22 fixround3：dry-run 也要有终态可查（页面上「RUNNING 永不结束」是用户可见死锁），
    所以本编排器**自有**的两件状态件——`work/update_state.json` 与
    `logs/update_<run_id>.log`——在 dry-run 下同样落盘；除此之外的一切写入目标
    （库/批次/reports/baseline/指纹/更新记录）在 dry-run 下仍然只打印 `WOULD WRITE`，0 字节。
    """

    def __init__(self, log_path: Path | None, enabled: bool):
        self.log_path = log_path
        self.enabled = enabled
        self._fh = None
        self.lines: list[str] = []

    def open(self) -> None:
        if self.enabled and self.log_path is not None:
            assert_dry_run_writable(self.log_path)   # 日志文件也在白名单内（P1-2）
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.log_path.open("a", encoding="utf-8")

    def __call__(self, msg: str) -> None:
        stamp = iso_bjt(now_bjt())
        line = f"[{stamp}] {msg}"
        self.lines.append(line)
        print(line, flush=True)
        if self._fh is not None:
            self._fh.write(line + "\n")
            self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


# ---------------------------------------------------------------- 原子写

def atomic_write_json(path: Path, obj: dict, *, enabled: bool, log: Tee | None = None) -> bool:
    """临时文件 + os.replace。dry-run（enabled=False）时只登记不落盘。

    2026-10-22 P1-2：`enabled=True` 但落在 dry-run 轮次时，先过
    `assert_dry_run_writable(path)` —— dry-run 里只允许写白名单那两件自有状态件。
    """
    payload = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    if not enabled:
        if log:
            log(f"WOULD WRITE (dry-run, 0 字节) {path} ({len(payload)} bytes)")
        return False
    assert_dry_run_writable(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + f".tmp.{os.getpid()}")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    if log:
        log(f"WROTE {path}")
    return True


def atomic_write_text(path: Path, text: str, *, enabled: bool, log: Tee | None = None) -> bool:
    if not enabled:
        if log:
            log(f"WOULD WRITE (dry-run, 0 字节) {path} ({len(text)} bytes)")
        return False
    assert_dry_run_writable(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + f".tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    if log:
        log(f"WROTE {path}")
    return True


def append_line(path: Path, line: str, *, enabled: bool, header: str | None = None,
                log: Tee | None = None) -> bool:
    if not enabled:
        if log:
            log(f"WOULD APPEND (dry-run, 0 字节) {path}: {line}")
        return False
    assert_dry_run_writable(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() and header:
        path.write_text(header, encoding="utf-8")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    if log:
        log(f"APPENDED {path}")
    return True


# ---------------------------------------------------------------- 节点模型

@dataclass
class WriteTarget:
    path: Path
    new_only: bool = False          # 必须是不存在的新路径（run_id 目录 / 新批次目录）
    promotion: bool = False         # 仅节点 13 的原子替换目标
    sealed_rebuild: bool = False    # 写封印件：默认判失败，仅人工授权开关放行
    note: str = ""


@dataclass
class Node:
    index: int
    key: str
    title: str
    cmds: list[list[str]] = field(default_factory=list)
    env: dict = field(default_factory=dict)
    cwd: Path | None = None
    inputs: list[Path] = field(default_factory=list)
    writes: list[WriteTarget] = field(default_factory=list)
    internal: str = ""               # 非子进程节点的内置动作名
    rows_sql: tuple | None = None    # (db_path, sql) 收尾统计行数
    note: str = ""
    layer: str = LAYER_SOURCE        # 三层节点表归属（见 LAYER_*）
    mode_policy: str = POLICY_READONLY   # append/full_recompute/acceptance/readonly/deliver
    skip_allowed: bool = True        # 聚合层/验收层在增量模式下也不许被跳过
    append_tables: tuple = ()        # 事实层节点负责追加的 (role, table)
    incremental_note: str = ""       # 增量语义下这一步的现状/待接线说明
    cmd_layers: tuple = ()           # 与 cmds 平行：每条命令属哪一层（stage 类节点用）
    cmd_policies: tuple = ()         # 与 cmds 平行：每条命令的增量策略



@dataclass
class GateResult:
    gate: str
    verdict: str                     # PASS / FAIL / PENDING
    code: str = ""
    message: str = ""
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"gate": self.gate, "verdict": self.verdict, "code": self.code,
                "message": self.message, "detail": self.detail}


def layers_of(n: "Node") -> tuple:
    """节点归属层（可多属：stage 类节点一条命令一层）。"""
    seq = [n.layer] + list(getattr(n, "cmd_layers", ()) or ())
    return tuple(dict.fromkeys(x for x in seq if x))


def policies_of(n: "Node") -> tuple:
    seq = [n.mode_policy] + list(getattr(n, "cmd_policies", ()) or ())
    return tuple(dict.fromkeys(x for x in seq if x))


def ok(gate: str, message: str, **detail) -> GateResult:
    return GateResult(gate, "PASS", "", message, detail)


def fail(gate: str, code: str, message: str, **detail) -> GateResult:
    return GateResult(gate, "FAIL", code, message, detail)


def pending(gate: str, code: str, message: str, **detail) -> GateResult:
    return GateResult(gate, "PENDING", code, message, detail)


# ---------------------------------------------------------------- 目标流月/交易日历

def load_calendar_terms(db_path: Path) -> list[dict]:
    """从主库（或批次库）calendar_months 读交节时刻；只读连接，绝不开写。

    列名兼容 month_id / term(名) / start_at。库里时间是 `+08:00` 精确到秒/微秒。
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(calendar_months)")}
        if "month_id" not in cols or "start_at" not in cols:
            raise ValueError(f"calendar_months 列不符: {sorted(cols)}")
        term_col = next((c for c in ("start_term", "term", "term_name", "jieqi",
                                    "solar_term") if c in cols), None)
        sql = "SELECT month_id, start_at" + (f", {term_col}" if term_col else "")
        sql += " FROM calendar_months ORDER BY start_at"
        out = []
        for row in con.execute(sql):
            item = {"month_id": row[0], "start_at": row[1]}
            if term_col:
                item["term"] = row[2]
            out.append(item)
        return out
    finally:
        con.close()


def resolve_calendar(P: Paths) -> list[dict]:
    """找能读到 calendar_months 的库：正式主库 → 最新批次库（逐个试，读不到不中断规划）。"""
    cands: list[Path] = []
    if P.main_db.exists():
        cands.append(P.main_db)
    if P.batches.exists():
        newest = [b / "data.sqlite" for b in sorted(P.batches.iterdir(), reverse=True)
                  if (b / "data.sqlite").exists()][:3]      # 只回退到最近 3 批，不逐个翻
        cands += newest
    for cand in cands:
        try:
            terms = load_calendar_terms(cand)
            if terms:
                return terms
        except Exception as exc:                          # noqa: BLE001
            print(f"WARN calendar from {cand}: {exc}", file=sys.stderr)
    return []


def month_of_instant(terms: list[dict], when: datetime) -> dict | None:
    cur = None
    for t in terms:
        try:
            start = parse_bjt_instant(t["start_at"])
        except Exception:                                  # noqa: BLE001
            continue
        if start <= when:
            cur = {**t, "start_instant": start}
        else:
            break
    return cur


def first_bar_date(target: dict) -> date:
    """新流月可能出现的第一个 bar 日期：09:30 锚点。交节时刻 > 09:30 ⇒ 当日仍属上月。"""
    inst = target["start_instant"]
    d = inst.date()
    if (inst.hour, inst.minute) > ASSIGN:
        d = d + timedelta(days=1)
    return d


def next_weekday(d: date) -> date:
    cur = d
    while cur.weekday() >= 5:
        cur += timedelta(days=1)
    return cur


def trading_day_from_mirror(zip_path: Path, member_prefix: str, since: date) -> date | None:
    """从只读镜像的年包（stdlib zipfile，不依赖 duckdb）取实际行情日期。"""
    if not zip_path.exists():
        return None
    try:
        with zipfile.ZipFile(zip_path) as zf:
            name = next((n for n in zf.namelist() if n.endswith(f"{member_prefix}.csv")), None)
            if not name:
                return None
            with zf.open(name) as fh:
                header = fh.readline().decode("utf-8", "replace").strip().split(",")
                try:
                    idx = header.index("datetime")
                except ValueError:
                    idx = 1
                best: date | None = None
                for raw in fh:
                    parts = raw.decode("utf-8", "replace").strip().split(",")
                    if len(parts) <= idx:
                        continue
                    s = parts[idx]
                    if "-" not in s or len(s) < 10:
                        continue
                    try:
                        d = date.fromisoformat(s[:10])
                    except ValueError:
                        continue
                    if d >= since and (best is None or d < best):
                        best = d
                return best
    except (zipfile.BadZipFile, OSError):
        return None


def resolve_target_month(P: Paths, as_of: str | None, now: datetime,
                         terms: list[dict]) -> dict:
    """目标流月 + 首个交易日估计。

    --as-of 显式给出 ⇒ 目标流月 = 该日（09:30 锚点）所在流月；
    否则目标流月 = 触发时刻（北京）所在流月，as_of 由侦测到的源末交易日决定。
    """
    if as_of:
        anchor = datetime.combine(date.fromisoformat(as_of), datetime.min.time().replace(
            hour=ASSIGN[0], minute=ASSIGN[1]), tzinfo=BJT)
        basis = "cli --as-of"
    else:
        anchor = now
        basis = "now_bjt (触发时刻)"
    target = month_of_instant(terms, anchor)
    if target is None:
        return {"resolved": False, "basis": basis, "anchor": iso_bjt(anchor),
                "reason": "calendar_terms_unavailable"}
    fbd = first_bar_date(target)
    year = fbd.year
    real = trading_day_from_mirror(P.daily_dir / f"{year}.zip", "000001.SZ", fbd)
    est = next_weekday(fbd)
    first_trading_day = real or est
    return {
        "resolved": True, "basis": basis, "anchor": iso_bjt(anchor),
        "month_id": target["month_id"], "term": target.get("term"),
        "term_start_at_bjt": iso_bjt(target["start_instant"]),
        "first_possible_bar_date": fbd.isoformat(),
        "first_trading_day": first_trading_day.isoformat(),
        "first_trading_day_basis": "source_datetime" if real else "weekend_estimate(无交易日历)",
        "weekend_estimate": est.isoformat(),
    }


# ---------------------------------------------------------------- G_A 发布侦测

def read_publication_stats(P: Paths) -> dict:
    """从本地只读镜像的 source_manifest.json + README/merge_report 侦测发布态（不联网）。"""
    manifest_path = P.mirror / "source_manifest.json"
    if not manifest_path.exists():
        return {"available": False, "reason": "source_manifest_missing",
                "manifest_path": str(manifest_path)}
    man = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = {f["path"]: {"size": f.get("bytes"), "sha256": f.get("sha256")}
             for f in man.get("files", [])}
    zips = {k: v for k, v in files.items()
            if k.startswith("正式源/全A日K_合并版/") and k.endswith(".zip")}
    max_date, date_src = None, None
    readme = P.daily_dir / "README.json"
    if readme.exists():
        try:
            span = json.loads(readme.read_text(encoding="utf-8")).get("规模", {}).get("跨度", "")
            if ".." in span:
                max_date = span.split("..")[-1]
                date_src = "README.json:规模.跨度"
        except Exception:                                  # noqa: BLE001
            pass
    if max_date is None:
        rep = P.daily_dir / "merge_report.json"
        if rep.exists():
            blob = json.dumps(json.loads(rep.read_text(encoding="utf-8")))
            found = re.findall(r"20\d{6}", blob)
            if found:
                max_date = max(found)
                date_src = "merge_report.json:max(date)"
    iso_max = None
    if max_date:
        s = max_date.replace("-", "")
        iso_max = f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return {"available": True, "source_version": man.get("source_version"),
            "fetched_at": man.get("fetched_at_utc"), "publication_files": zips,
            "file_count": len(zips), "max_trade_date": iso_max,
            "max_trade_date_source": date_src,
            "manifest_sha256": digest_of(manifest_path)["sha12"]}


def publication_changed(stats: dict, baseline: dict) -> bool:
    """任一正式源 zip 的 size/sha 与上次记录不同即为「有新发布」。"""
    prev = baseline.get("publication_files", {})
    cur = stats.get("publication_files", {})
    if not prev:
        return True
    for path, info in cur.items():
        old = prev.get(path)
        if old is None:
            return True
        if old.get("size") != info.get("size") or old.get("sha256") != info.get("sha256"):
            return True
    return False


def gate_a(stats: dict, baseline: dict | None, target: dict, *,
           force_source_check: bool = False, write_baseline=None) -> GateResult:
    """G_A：远端正式源发布侦测。

    PASS 需同时满足：(1) 正式源 zip 的 size/sha 与 baseline 不同；
    (2) README/merge_report 的最大交易日 ≥ 目标流月首个交易日。
    """
    if not stats.get("available"):
        return fail("G_A", E_SEAL_MISSING, "本地镜像缺 source_manifest.json，无法侦测发布态",
                    detail=stats)
    if not target.get("resolved"):
        return fail("G_A", E_ARTIFACT_MISSING, "读不到 calendar_months，无法确定目标流月",
                    detail=target)
    if baseline is None:
        if force_source_check and write_baseline is not None:
            write_baseline(stats)
            return ok("G_A", "baseline 缺失：按 --force-source-check 人工确认建档，本轮放行",
                      bootstrapped=True, max_trade_date=stats.get("max_trade_date"),
                      target_month=target["month_id"])
        return fail("G_A", E_SOURCE_BASELINE_MISSING,
                    "work/source_publication_baseline.json 不存在，无历史发布态可比；"
                    "需人工用 --force-source-check 建档后才会放行",
                    detail={"baseline_path_missing": True,
                            "max_trade_date": stats.get("max_trade_date")})
    changed = publication_changed(stats, baseline)
    first_day = target["first_trading_day"]
    max_day = stats.get("max_trade_date")
    covered = bool(max_day) and max_day >= first_day
    detail = {
        "publication_changed": changed,
        "baseline_recorded_at_bjt": baseline.get("recorded_at_bjt"),
        "baseline_max_trade_date": baseline.get("max_trade_date"),
        "current_max_trade_date": max_day,
        "target_month_id": target["month_id"],
        "target_term_start_at_bjt": target.get("term_start_at_bjt"),
        "target_first_trading_day": first_day,
        "first_trading_day_basis": target.get("first_trading_day_basis"),
    }
    if changed and covered:
        return ok("G_A", f"正式源已变且行情覆盖目标流月 {target['month_id']} 首个交易日 {first_day}",
                  **detail)
    if not changed:
        return fail("G_A", E_SOURCE_NOT_READY,
                    f"正式源发布线未推进（仍停在 {max_day}），等上游发布，本机只如实拒绝",
                    **detail)
    return fail("G_A", E_SOURCE_NOT_READY,
                f"源已变但行情未覆盖目标流月 {target['month_id']} 首个交易日 {first_day}"
                f"（源末交易日 {max_day}）", **detail)


# ---------------------------------------------------------------- G_B 冻结件哈希

def parse_sums_file(path: Path, base: Path, kind: str) -> list[tuple[Path, str]]:
    """解析 `<sha>  <path>` 清单；kind=relative 以清单所在目录为基准，absolute 用绝对路径。"""
    entries: list[tuple[Path, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or len(parts[0]) != 64:
            continue
        digest, rel = parts[0], parts[1].lstrip(" *")
        p = Path(rel) if kind == "absolute" else (base / rel)
        entries.append((p.resolve() if p.is_absolute() else p, digest))
    return entries


def shadow_manifest_entries(shadow_dir: Path) -> list[tuple[Path, str]]:
    """影子跑批的外部哈希：manifest.sha256 → manifest.json → 其 files[] 与 input.sha256。"""
    out: list[tuple[Path, str]] = []
    ext = shadow_dir / "v13_full_shadow_manifest.sha256"
    if ext.exists():
        out += parse_sums_file(ext, shadow_dir, "relative")
        try:
            man = json.loads((shadow_dir / "v13_full_shadow_manifest.json")
                             .read_text(encoding="utf-8"))
        except Exception:                                  # noqa: BLE001
            return out
        for f in man.get("files", []):
            if f.get("sha256"):
                out.append((shadow_dir / f["path"], f["sha256"]))
        inp = man.get("input", {})
        if inp.get("sha256") and inp.get("path"):
            out.append((Path(inp["path"]), inp["sha256"]))
    return out


def collect_seals(P: Paths) -> list[tuple[str, Path, str]]:
    """四套封印清单 → (来源, 文件, 期望 sha256)，按路径去重。"""
    seen: dict[Path, tuple[str, str]] = {}
    specs = [
        ("release_candidate/SHA256SUMS.txt", P.release_candidate / "SHA256SUMS.txt", "relative"),
        ("local_v1/work/frozen_baseline.txt", P.local_v1 / "work" / "frozen_baseline.txt", "absolute"),
        ("bazi_history_relation_v1/dist/SHA256SUMS.txt", P.relation_dist / "SHA256SUMS.txt", "relative"),
    ]
    out: list[tuple[str, Path, str]] = []
    for name, path, kind in specs:
        if not path.exists():
            out.append((name, path, ""))
            continue
        for f, digest in parse_sums_file(path, path.parent, kind):
            if f not in seen:
                seen[f] = (name, digest)
    shadow_seal = P.shadow_old / "v13_full_shadow_manifest.sha256"
    src_name = "v13_shadow_fullmarket_20261018/v13_full_shadow_manifest.sha256+manifest"
    if not shadow_seal.exists():
        out.append((src_name, shadow_seal, ""))
    else:
        for f, digest in shadow_manifest_entries(P.shadow_old):
            if f not in seen:
                seen[f] = (src_name, digest)
    for f, (name, digest) in seen.items():
        out.append((name, f, digest))
    return out


def load_seal_waivers(P: Paths) -> list[dict]:
    """A 裁定（2026-10-22）：原封印清单一个字节不动，另读 `work/seal_baseline_v2.json`
    里逐条核验过差异原因的登记项（原因取自 V1.3 集成验收报告原文）。

    这不是放宽判据：
      * 该文件不存在 ⇒ 返回空，校验行为与今天完全一致；
      * 已登记的每一条，必须**当前实测 sha256 与登记值逐字节相同**才免于拒绝
        （缺件则要求 status=missing_allowed）；登记不符仍 FAIL；
      * 未登记的任何不一致仍照旧 FAIL。
    """
    f = P.dev_root / "work" / "seal_baseline_v2.json"
    if not f.exists():
        return []
    try:
        doc = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return []
    return list(doc.get("items") or [])


def _waiver_for(path: Path, actual: str | None, waivers: list[dict]) -> dict | None:
    s = str(path).replace("\\", "/")
    for it in waivers:
        rel = (it.get("path") or "").strip().lstrip("./").replace("\\", "/")
        if not rel or not s.endswith("/" + rel):
            continue
        st = it.get("status")
        if st == "deviation_registered_current" and actual and it.get("sha256") == actual:
            return it
        if st == "missing_allowed" and actual is None:
            return it
    return None


def verify_seals(P: Paths, *, hash_fn=sha256_file) -> tuple[list[dict], list[dict]]:
    sealed: list[dict] = []
    bad: list[dict] = []
    waivers = load_seal_waivers(P)
    for name, path, digest in collect_seals(P):
        if not digest:
            bad.append({"seal": name, "path": str(path), "reason": "seal_list_missing"})
            continue
        got = hash_fn(path) if path.exists() else None
        wv = _waiver_for(path, got, waivers)
        if wv is not None:
            sealed.append({"seal": name, "path": str(path), "expected": digest[:12],
                           "actual": (got[:12] if got else None),
                           "bytes": (path.stat().st_size if got else 0),
                           "waived": True, "waiver_status": wv.get("status"),
                           "waiver_reason": wv.get("reason")})
            continue
        if got is None:
            bad.append({"seal": name, "path": str(path), "reason": "file_missing"})
            continue
        item = {"seal": name, "path": str(path), "expected": digest[:12],
                "actual": got[:12], "bytes": path.stat().st_size}
        sealed.append(item)
        if got != digest:
            bad.append({**item, "reason": "hash_mismatch"})
    return sealed, bad


def sealed_path_set(P: Paths) -> set[Path]:
    return {p for _, p, _ in collect_seals(P)}


def protected_dirs(P: Paths) -> list[Path]:
    """禁写目录。批次按「当前已存在的每个批次目录」逐个登记，
    这样本轮新批次目录仍然可写、旧批次（含冻结批）一律不可动。"""
    existing_batches = []
    if P.batches.exists():
        existing_batches = [p for p in sorted(P.batches.iterdir()) if p.is_dir()]
    return [
        P.relation_dist,
        P.release_candidate,
        P.shadow_old,
        *existing_batches,
        P.backups,
    ]


def derived_inplace_allowlist(P: Paths) -> set[Path]:
    """明确允许的「就地重建派生件」+ 编排器自己的记账件（dev_root/work、reports）。
    对应脚本内路径不可改（不在允许改动的两个脚本之内），且无任何封印哈希登记。"""
    return {
        P.source_input / "nas_source.duckdb",
        P.source_input / "build_stats.json",
        P.source_input / "stock_master.csv",
        P.docs_root / "reports" / "universe_notes.json",
        P.mirror,                                             # 节点 1 只读镜像目录（设计如此）
        P.local_v1 / "work" / "validation.json",
        P.local_v1 / "reports",                               # 主库派生报告件
        P.etf_v1 / "work",
        P.etf_v1 / "data",
        P.etf_v1 / "reports",
        P.dev_root / "work",                                  # 状态/baseline/指纹/本轮暂存
        P.dev_root / "logs",
        P.dev_root / "data" / "v5_lowpoint_slice.sqlite",
        P.update_log_md,                                      # reports/更新记录.md（追加一行）
    }


def audit_write_targets(nodes: list[Node], *, sealed: set[Path], protected: list[Path],
                        allow_inplace: set[Path], run_id: str, runday: str, P: Paths,
                        self_created: tuple = ()) -> tuple[list[dict], list[dict]]:
    """强制「本轮所有产物只写带 run_id 的新目录」；写封印件 = 直接失败而不是覆盖。

    self_created：本轮开跑前确认不存在、由本轮自己创建出来的路径（pre 阶段已按 new_only
    校验过它们当时不存在），收尾复评时不再把它们当成越界覆盖。
    """
    run_scope = [
        P.work / "runs" / run_id,
        P.local_db_dir / f"staging_{run_id}.sqlite",
        P.etf_v1 / "database" / f"staging_{run_id}.sqlite",
        P.project_root / f"v13_shadow_fullmarket_{runday}",
        P.relation_v1 / "out" / run_id,
        P.backups / f"pre_monthly_{runday}",
    ]
    planned: list[dict] = []
    conflicts: list[dict] = []
    new_prefixes = {wt.path for n in nodes for wt in n.writes if wt.new_only}
    for node in nodes:
        for wt in node.writes:
            path = wt.path
            entry = {"node": node.index, "key": node.key, "path": str(path),
                     "new_only": wt.new_only, "promotion": wt.promotion, "note": wt.note}
            planned.append(entry)
            resolved = Path(path)
            if wt.promotion:
                continue                                   # 仅节点 13 七闸全过后执行
            if wt.sealed_rebuild and relation_rebuild_authorized():
                continue                                   # 人工授权的关系库原地重建
            under_new = any(str(resolved) == str(nd)
                            or str(resolved).startswith(str(nd) + os.sep)
                            for nd in new_prefixes)
            created_now = any(str(resolved) == str(s)
                              or str(resolved).startswith(str(s) + os.sep)
                              for s in self_created)
            in_run_scope = any(str(resolved).startswith(str(s)) for s in run_scope)
            in_allow = any(str(resolved) == str(a) or str(resolved).startswith(str(a) + os.sep)
                           for a in allow_inplace)
            blocked_by = [d for d in protected if str(resolved) == str(d)
                          or str(resolved).startswith(str(d) + os.sep)]
            # 本轮自己创建出来的目录（开跑前审过当时不存在）不算封印件/受保护目录
            blocked_by = [d for d in blocked_by
                          if not any(str(d).startswith(str(s)) for s in self_created)]
            if resolved in sealed and not created_now:
                conflicts.append({**entry, "reason": "sealed_or_protected_target"})
                continue
            if blocked_by:
                conflicts.append({**entry, "reason": "sealed_or_protected_target",
                                  "protected_by": [str(d) for d in blocked_by]})
                continue
            if wt.new_only and resolved.exists() and not any(
                    str(resolved) == str(s) or str(resolved).startswith(str(s) + os.sep)
                    for s in self_created):
                conflicts.append({**entry, "reason": "new_only_target_exists"})
                continue
            if not (in_run_scope or in_allow or under_new):
                conflicts.append({**entry, "reason": "target_outside_run_scope"})
    return planned, conflicts


def gate_b(P: Paths, nodes: list[Node], run_id: str, runday: str, *,
           hash_fn=sha256_file, self_created: tuple = ()) -> GateResult:
    sealed_paths = sealed_path_set(P)
    verified, bad = verify_seals(P, hash_fn=hash_fn)
    if bad:
        return fail("G_B", E_SEAL_MISMATCH,
                    f"封印件校验失败 {len(bad)} 项（拒绝继续，绝不覆盖）",
                    mismatches=bad[:20], verified_count=len(verified))
    planned, conflicts = audit_write_targets(
        nodes, sealed=sealed_paths, protected=protected_dirs(P),
        allow_inplace=derived_inplace_allowlist(P), run_id=run_id, runday=runday, P=P,
        self_created=self_created)
    if conflicts:
        return fail("G_B", E_SEALED_TARGET,
                    "本轮计划写入的目标里有封印件/越界路径，直接失败而不是覆盖",
                    conflicts=conflicts[:20], planned_count=len(planned))
    wv = [x for x in verified if x.get("waived")]
    wv_note = (f"；其中 {len(wv)} 项按 seal_baseline_v2 登记豁免（原因见该文件与 "
               f"reports/更新记录.md 的 SEAL_BASELINE_V2 行）") if wv else ""
    return ok("G_B", f"{len(verified)} 件封印件哈希一致{wv_note}；{len(planned)} 个写入目标全部落在 "
                     f"run_id 新目录或明示允许的派生件",
              verified_count=len(verified), planned_count=len(planned),
              waived_count=len(wv), sample_verified=verified[:3])


# ---------------------------------------------------------------- G_C 复算一致

def gate_c(shadow_manifest: dict | None, verify_report: dict | None, *,
           node7_ran: bool, node11_ran: bool) -> GateResult:
    """G_C：影子跑批 9 门禁全 PASS + independent_verify（09:30 锚点）复算一致。"""
    if shadow_manifest is None:
        if not node7_ran:
            return pending("G_C", E_ARTIFACT_MISSING,
                           "影子跑批尚未执行（节点 7 未跑），门禁证据待生成")
        return fail("G_C", E_ARTIFACT_MISSING, "影子跑批清单缺失，无法判定 9 门禁")
    gates = shadow_manifest.get("gates", {})
    failed = [k for k in SHADOW_GATE_PASS_KEYS if gates.get(k) != "PASS"]
    failed += [k for k in SHADOW_GATE_YES_KEYS if gates.get(k) != "YES"]
    prod = gates.get("PRODUCTION_READY")
    detail = {"shadow_failed_gates": failed, "shadow_gates":
              {k: gates.get(k) for k in (*SHADOW_GATE_PASS_KEYS, *SHADOW_GATE_YES_KEYS,
                                         *SHADOW_GATE_MUST_STAY_NO)},
              "production_ready": prod}
    if prod != "NO":
        return fail("G_C", E_GATE_FAILED,
                    "PRODUCTION_READY 必须恒为 NO（影子跑批不许自称生产就绪）", **detail)
    verdict = (shadow_manifest.get("verification") or {}).get("verdict")
    if verdict != "PASS":
        return fail("G_C", E_GATE_FAILED, f"影子跑批 verification.verdict={verdict}", **detail)
    if failed:
        return fail("G_C", E_GATE_FAILED, f"影子跑批门禁未全过: {failed}", **detail)
    if verify_report is None:
        if not node11_ran:
            return pending("G_C", E_ARTIFACT_MISSING,
                           "独立核验尚未执行（节点 11 未跑）", **detail)
        return fail("G_C", E_ARTIFACT_MISSING, "独立核验报告缺失，无法判定复算一致", **detail)
    status = verify_report.get("status")
    diff = verify_report.get("difference_count")
    detail.update({"verify_status": status, "verify_difference_count": diff,
                   "verify_assign_anchor": "09:30",
                   "recomputations": verify_report.get("independent_recomputations")})
    if status == "PASS" and diff == 0:
        return ok("G_C", f"9 门禁全 PASS 且独立复算 {detail['recomputations']} 条 0 差异",
                  **detail)
    return fail("G_C", E_GATE_FAILED, f"独立复算不一致: status={status} differences={diff}",
                **detail)


# ---------------------------------------------------------------- G_D 写前副本

def gate_d(plan: list[tuple[Path, Path]], *, volume: Path, min_free: int = MIN_FREE_BYTES,
           disk_usage=shutil.disk_usage, do_copy: bool = True,
           hash_fn=sha256_file) -> GateResult:
    """G_D：卷余量 > 40GiB；旧主库/旧 ETF 库复制到 backups/pre_monthly_<date>/ 并 sha256 回读。"""
    usage = disk_usage(str(volume))
    detail = {"volume": str(volume), "free_bytes": usage.free,
              "min_free_bytes": min_free, "free_gib": round(usage.free / 1024 ** 3, 2)}
    if usage.free <= min_free:
        return fail("G_D", E_DISK_LOW,
                    f"卷余量 {detail['free_gib']}GiB ≤ 40GiB，不做写前副本也不替换", **detail)
    needed = sum(src.stat().st_size for src, _ in plan if src.exists())
    if usage.free - needed <= min_free:
        return fail("G_D", E_DISK_LOW,
                    f"扣掉本轮副本 {round(needed / 1024 ** 3, 2)}GiB 后余量不足 40GiB",
                    copy_needed_bytes=needed, **detail)
    if not do_copy:
        return ok("G_D", "dry-run：只核对余量，未生成副本", copy_planned=len(plan),
                  sources=[str(s) for s, _ in plan], **detail)
    checked: list[dict] = []
    for src, dst in plan:
        if not src.exists():
            checked.append({"src": str(src), "skipped": "source_absent"})
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            return fail("G_D", E_SEALED_TARGET, f"副本目标已存在，拒绝覆盖: {dst}", **detail)
        tmp = dst.with_suffix(dst.suffix + f".part.{os.getpid()}")
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        src_sha, dst_sha = hash_fn(src), hash_fn(dst)
        item = {"src": str(src), "dst": str(dst), "src_sha256_12": src_sha[:12],
                "dst_sha256_12": dst_sha[:12], "bytes": dst.stat().st_size}
        checked.append(item)
        if src_sha != dst_sha:
            return fail("G_D", E_BACKUP_MISMATCH, f"写前副本 sha256 回读不一致: {src}",
                        copies=checked, **detail)
    return ok("G_D", f"写前副本 {len(checked)} 件已复制并逐文件 sha256 回读一致",
              copies=checked, **detail)


# ---------------------------------------------------------------- G_E 规则版本锁

def read_rule_version(P: Paths) -> tuple[str | None, str]:
    """当前规则版本：优先封印的规则包 policy_research_default.json。"""
    policy = P.release_candidate / "rules" / "policy_research_default.json"
    if policy.exists():
        try:
            data = json.loads(policy.read_text(encoding="utf-8"))
            v = data.get("relation_rule_version")
            if v:
                return v, str(policy)
        except Exception:                                  # noqa: BLE001
            pass
    return None, str(policy)


def gate_e(P: Paths) -> GateResult:
    """G_E：规则版本必须仍等于 work/rule_version.lock 的锁定串（缺省 v1.3.0-provisional）。"""
    locked = RULE_VERSION_DEFAULT
    src = "内置缺省"
    if P.rule_lock.exists():
        locked = P.rule_lock.read_text(encoding="utf-8").strip() or RULE_VERSION_DEFAULT
        src = str(P.rule_lock)
    current, cur_src = read_rule_version(P)
    detail = {"locked_version": locked, "lock_source": src,
              "current_version": current, "current_source": cur_src}
    if current is None:
        return fail("G_E", E_ARTIFACT_MISSING, "读不到当前规则版本（规则包缺失）", **detail)
    if current != locked:
        return fail("G_E", E_GATE_FAILED,
                    f"规则口径变了（{current} ≠ 锁定的 {locked}），需人工确认后才允许重跑",
                    **detail)
    return ok("G_E", f"规则版本仍为 {locked}，与锁定串一致", **detail)


# ---------------------------------------------------------------- 分层登记表（G-F/G-G 判据）

FOLD_MOD = (1 << 128) - 15          # 顺序无关折叠的模（2^128-15，非 2 的幂 ⇒ 交换律仍有效）


def row_digest(payload: tuple) -> int:
    """行级校验和：blake2b-16(repr(整行)) 的无符号整数（顺序无关折叠用）。

    repr() 对 str/int/float/None/bytes 有类型区分（字符串带引号、float 最短往返），
    所以 1 与 "1" 与 1.0 不会撞在一起 —— 「逐字节等值」在 Python 侧的落地形式。
    """
    d = hashlib.blake2b(repr(payload).encode("utf-8"), digest_size=16).digest()
    return int.from_bytes(d, "big")


def _open_ro(db: Path) -> sqlite3.Connection:
    """只读连接：mode=ro 既不建文件也不写 -journal，任何门禁都不再有写库的可能。"""
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


@contextlib.contextmanager
def ro(db: Path):
    con = _open_ro(db)
    try:
        yield con
    finally:
        con.close()


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    """表存在性（与「列」严格分开：把列名当表名判会把门禁整体架空）。"""
    row = con.execute("SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=? "
                      "UNION ALL SELECT 1 FROM sqlite_temp_master WHERE type='table' AND name=?",
                      (table, table)).fetchone()
    return row is not None


def table_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute('PRAGMA table_info("%s")' % table.replace('"', '""'))]


def _month_where(month_col: str | None, exclude: tuple, alias: str = "") -> tuple[str, list]:
    if not month_col or not exclude:
        return "", []
    pre = f"{alias}." if alias else ""
    ph = ",".join("?" for _ in exclude)
    return f' WHERE "{month_col}" NOT IN ({ph})', list(exclude)


def overlap_digest(pairs: list) -> str:
    """把「同一票×年在旧库/新库的校验和」按序折叠成一个摘要，供跨轮对账用。"""
    blob = json.dumps(pairs, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def group_signature(db: Path, table: str, group_cols: tuple, month_col: str | None,
                    exclude_months: tuple, payload_cols: tuple,
                    pk_cols: tuple) -> dict:
    """一次全表扫描得到 {票×年 → 行数 + 顺序无关校验和}，只在重叠区间上算。

    校验和 = sum(行摘要) mod (2^128-15) 与 xor(行摘要) 双通道 + 主键集合折叠，
    所以「同一组里两行互换」不会假阳、「多一行/少一行/改一个值」一定假阴不了。
    """
    cols = list(group_cols) + list(payload_cols) + list(pk_cols)
    where, params = _month_where(month_col, exclude_months)
    sql = "SELECT %s FROM \"%s\"%s" % (",".join('"%s"' % c for c in cols), table, where)
    g_n, p_n = len(group_cols), len(payload_cols)
    out: dict[str, dict] = {}
    total = 0
    pk_fold = 0
    with ro(db) as con:
        for row in con.execute(sql, params):
            total += 1
            key = "|".join("" if v is None else str(v) for v in row[:g_n])
            g = out.get(key)
            if g is None:
                g = out[key] = {"rows": 0, "sum": 0, "xor": 0}
            dg = row_digest(tuple(row[g_n:g_n + p_n]))
            g["rows"] += 1
            g["sum"] = (g["sum"] + dg) % FOLD_MOD
            g["xor"] ^= dg
            pk_fold = (pk_fold + row_digest(tuple(row[g_n + p_n:]))) % FOLD_MOD
    for g in out.values():
        g["sig"] = f"{g['sum']:032x}{g['xor']:032x}"
        del g["sum"], g["xor"]
    return {"groups": out, "rows": total, "group_count": len(out),
            "pk_fold": f"{pk_fold:032x}"}


def distinct_values(db: Path, table: str, col: str, month_col: str | None,
                    months: tuple) -> list[str]:
    """聚合层专用：新流月到底动了哪些键值（这些聚合行允许变，其余必须等值）。"""
    if not months:
        return []
    ph = ",".join("?" for _ in months)
    sql = f'SELECT DISTINCT "{col}" FROM "{table}" WHERE "{month_col}" IN ({ph})'
    with ro(db) as con:
        return [str(r[0]) for r in con.execute(sql, list(months)) if r[0] is not None]


@dataclass(frozen=True)
class InvarianceSpec:
    role: str                 # main / etf / cap_tier
    table: str
    layer: str                # fact / aggregate
    group_cols: tuple         # 「票×年」（聚合层多为「票」，见 touch）
    month_col: str | None     # 流月列（重叠区间按它切）
    pk_cols: tuple            # 历史行主键集合
    touch: tuple = ()         # 聚合层：((本表列, 取值来源表, 来源列), ...)
    enabled: bool = True
    reason: str = ""          # enabled=False 时说明卡在哪（不许假装能查）


# 事实层：历史行不动、只 INSERT 新流月；G-F 用「排除新流月」的重叠区间逐字节比对。
# 聚合层：每轮全量重扫重算，因此不存在「历史行」，改为「新流月影响不到的键必须等值」。
# 主键里出现的列一律保留在载荷里；monthly_relations.seq 是行号（重建时必然重排），
# 属于非确定性列，从载荷中排除、但仍作为主键的一部分参与 pk 折叠。
NON_DETERMINISTIC_COLS = frozenset({"seq"})

MAIN_FACT_TABLES = ("monthly_facts", "monthly_relations", "month_relation_types")

INVARIANCE_SPECS: tuple[InvarianceSpec, ...] = (
    InvarianceSpec("main", "monthly_facts", "fact", ("identity_code", "solar_year"),
                   "month_id", ("identity_code", "month_id")),
    InvarianceSpec("main", "monthly_relations", "fact", ("identity_code", "solar_year"),
                   "month_id", ("identity_code", "month_id", "record_kind", "relation_type",
                                "flow_pillar")),
    InvarianceSpec("main", "month_relation_types", "fact", ("identity_code", "solar_year"),
                   "month_id", ("identity_code", "month_id", "record_kind", "relation_type")),
    # 聚合层的「组」取该表的逻辑键（票×维度），这样「新流月影响不到的那一行」
    # 才是可判定的：同键行必须逐字节等值，只有键值落在新流月影响集里的行允许重算。
    InvarianceSpec("main", "stats_pillar60", "aggregate",
                   ("identity_code", "flow_pillar"), None,
                   ("identity_code", "flow_pillar"),
                   touch=(("flow_pillar", "monthly_facts", "flow_pillar"),)),
    InvarianceSpec("main", "stats_branch12", "aggregate",
                   ("identity_code", "sample_policy", "branch"), None,
                   ("identity_code", "sample_policy", "branch"),
                   touch=(("branch", "monthly_facts", "month_branch"),)),
    InvarianceSpec("main", "stats_relation_types", "aggregate",
                   ("identity_code", "record_kind", "relation_type"), None,
                   ("identity_code", "record_kind", "relation_type"),
                   touch=(("relation_type", "month_relation_types", "relation_type"),)),
    InvarianceSpec("main", "stats_relation_pillar", "aggregate",
                   ("identity_code", "record_kind", "relation_type", "flow_pillar",
                    "month_branch", "solar_term"), None,
                   ("identity_code", "record_kind", "relation_type", "flow_pillar"),
                   touch=(("relation_type", "month_relation_types", "relation_type"),
                          ("flow_pillar", "monthly_facts", "flow_pillar"),
                          ("month_branch", "monthly_facts", "month_branch"))),
    # 页面五所需统计的落库口径就是 stats_pillar60 + relation_type_market_stats；
    # 后者是全市场 28 行（按关系类型），新流月会让同类型的行受影响，其余必须等值。
    InvarianceSpec("main", "relation_type_market_stats", "aggregate", ("(market)",), None,
                   ("category_id", "item"),
                   touch=(("item", "month_relation_types", "relation_type"),)),
    InvarianceSpec("etf", "monthly_facts", "fact", ("identity_code", "solar_year"),
                   "month_id", ("identity_code", "month_id")),
    InvarianceSpec("etf", "monthly_relations", "fact", ("identity_code", "solar_year"),
                   "month_id", ("identity_code", "month_id", "record_kind", "relation_type")),
    InvarianceSpec("etf", "month_relation_types", "fact", ("identity_code", "solar_year"),
                   "month_id", ("identity_code", "month_id", "record_kind", "relation_type")),
    InvarianceSpec("etf", "stats_pillar60", "aggregate", ("identity_code", "flow_pillar"), None,
                   ("identity_code", "flow_pillar"),
                   touch=(("flow_pillar", "monthly_facts", "flow_pillar"),)),
    InvarianceSpec("etf", "relation_type_market_stats", "aggregate", ("(market)",), None,
                   ("category_id", "item"),
                   touch=(("item", "month_relation_types", "relation_type"),)),
    # 市值分档扩展库：现网是「主库换完后由 extensions/update_all_after_main_db.sh 原地重建」，
    # 本轮没有 staging 副本可比 ⇒ 登记为未接线，不假装通过（TODO 见 INTEGRATION_TODOS）。
    InvarianceSpec("cap_tier", "cap_tier_monthly", "fact", ("identity_code", "solar_year"),
                   "month_id", ("identity_code", "month_id"), enabled=False,
                   reason="扩展库无 staging 路径：update_all_after_main_db.sh 就地重建，"
                          "G-F 需要它先落 staging_<run_id>.sqlite（TODO）"),
    # ETF 关系（pair 关系）在 ETF 库内即 monthly_relations/month_relation_types，已登记。
)

# 事实层「追加」接线表：orchestrator 能直接 ATTACH+INSERT 的才 wired=True。
# 现实：主库 monthly_facts 的 66 列里 relation_type_list/stem_ten_god/benchmark_*/row_sha256
# 由 stage2 从影子库 JSON 炸开得到，编排器无法在不复制 stage2 逻辑的前提下逐字节复现，
# 因此这些步骤标 wired=False，改为「以现网库为底的 staging + 注入增量环境变量 + G-F 兜底」。
FACT_APPEND_SPECS: tuple[dict, ...] = (
    {"role": "main", "table": "monthly_facts", "month_col": "month_id", "wired": False,
     "source": "batch_data_sqlite",
     "reason": "stage2 的投影要 join 影子库/十神/基准，编排器不复制该逻辑 ⇒ 交给脚本 append 开关"},
    {"role": "main", "table": "monthly_relations", "month_col": "month_id", "wired": False,
     "source": "shadow_run_sqlite", "reason": "同上（row_json 炸开）"},
    {"role": "main", "table": "month_relation_types", "month_col": "month_id", "wired": False,
     "source": "derived_from_monthly_relations", "reason": "由 monthly_relations 去重派生"},
    {"role": "etf", "table": "monthly_facts", "month_col": "month_id", "wired": False,
     "source": "etf_full_facts", "reason": "ETF 支路 04f/05f 整表重建，需脚本侧 append 开关"},
)

INTEGRATION_TODOS = (
    "stage2_facts_relations.py / ETF 04f,05f：需要 MONTHLY_UPDATE_MODE=incremental + "
    "MONTHLY_UPDATE_NEW_MONTHS 的只追加新流月分支（当前整表 DROP/CREATE，"
    "G-F 仍会逐字节核对历史 ⇒ 语义安全但不是少算）",
    "extensions/update_all_after_main_db.sh：市值分档/大运扩展需要 staging 副本，"
    "否则 cap_tier_monthly 的历史不变性无法在替换前证明（G-F 已登记 enabled=False）",
    "节点 2/3/11/12 需要 duckdb：本机系统 python3 无该模块且禁止 pip install ⇒ "
    "只在集成阶段用既有环境执行（自测一律假树 + 桩脚本）",
    "app/update_scheduler.py 生成的 run_id 必须是 MU-YYYYMMDDTHHMMSS（本工具 BAD_RUN_ID "
    "硬拒）；调度器与页面显示侧的格式对齐由 T1 负责，本文件不改它",
)


def role_dbs(P: Paths, ctx: Context, *, staging: bool) -> dict[str, Path]:
    """role → 库路径。staging=False 取现网旧库，True 取本轮新库（未替换前）。"""
    old = {"main": P.main_db, "etf": P.etf_db,
           "cap_tier": P.local_v1 / "extensions" / "market_cap_tier_v1" / "database" /
           "market_cap_tier_v1.sqlite"}
    new = dict(old)
    if staging:
        new["main"] = P.local_db_dir / f"staging_{ctx.run_id}.sqlite"
        new["etf"] = P.etf_v1 / "database" / f"staging_{ctx.run_id}.sqlite"
    return new if staging else old


def is_readable_db(path: Path) -> bool:
    if not Path(path).exists():
        return False
    try:
        with ro(Path(path)) as con:
            con.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        return True
    except Exception:                                       # noqa: BLE001
        return False


def derive_new_months(P: Paths, ctx: Context, log: Tee | None) -> tuple:
    """本轮允许追加的新流月 = 最新批次里已有、现网主库里还没有的 month_id。

    取不到批次（未跑节点 4）⇒ 退回目标流月单个；都没有 ⇒ 空元组（增量层什么都不许加）。
    """
    old_months: set[str] = set()
    if is_readable_db(P.main_db):
        try:
            with ro(P.main_db) as con:
                cols = set(table_columns(con, "monthly_facts"))
                if table_exists(con, "monthly_facts") and "month_id" in cols:
                    old_months = {r[0] for r in con.execute(
                        "SELECT DISTINCT month_id FROM monthly_facts")}
        except Exception:                                   # noqa: BLE001
            pass
    new_months: set[str] = set()
    batch = ctx.batch_dir
    if batch:
        cand = Path(batch) / "data.sqlite"
        if is_readable_db(cand):
            try:
                with ro(cand) as con:
                    cols = set(table_columns(con, "monthly_facts"))
                    if table_exists(con, "monthly_facts") and "month_id" in cols:
                        new_months = {r[0] for r in con.execute(
                            "SELECT DISTINCT month_id FROM monthly_facts")}
            except Exception:                               # noqa: BLE001
                pass
    diff = sorted(new_months - old_months)
    if not diff and ctx.target.get("resolved"):
        tm = ctx.target.get("month_id")
        if tm and tm not in old_months:
            diff = [tm]
    if log:
        log(f"NEW_MONTHS({len(diff)}) {','.join(diff) or '-'} "
            f"(old_main_months={len(old_months)}, batch_months={len(new_months)})")
    return tuple(diff)


# ---------------------------------------------------------------- G-F 历史不变性门禁

def spec_payload_cols(con: sqlite3.Connection, spec: InvarianceSpec) -> list[str] | None:
    """该表能进入不变性口径的载荷列：登记列不全 ⇒ 如实标 UNVERIFIED，不假装通过。"""
    cols = table_columns(con, spec.table)
    if not cols:
        return None
    need = set(spec.group_cols) | set(spec.pk_cols)
    if spec.month_col:
        need.add(spec.month_col)
    need -= {"(market)"}
    if not need.issubset(set(cols)):
        return None
    return [c for c in cols if c not in NON_DETERMINISTIC_COLS]



def compare_fact_table(old_db: Path, new_db: Path, spec: InvarianceSpec,
                       new_months: tuple) -> dict:
    """事实层：重叠区间（排除新流月）逐字节等值 + 只允许新流月造成新增。"""
    res: dict = {"table": spec.table, "role": spec.role, "layer": spec.layer,
                 "verdict": "CHECKED", "differences": []}
    with ro(old_db) as ocon:
        ocols = spec_payload_cols(ocon, spec)
    if not ocols:
        res["verdict"] = "UNVERIFIED_SCHEMA"
        return res
    payload = tuple(ocols)
    # 先看清「这张表自己的新增流月」：旧库没有、新库有的 month_id。
    # 重叠区间 = 排除（本轮裁定的新流月 ∪ 该表自己的新增流月），其余一律必须逐字节等值。
    months_old = set(_months_of(old_db, spec)) if spec.month_col else set()
    with ro(new_db) as ncon:
        total_new = ncon.execute(f'SELECT COUNT(*) FROM "{spec.table}"').fetchone()[0]
        months_new = set()
        if spec.month_col:
            months_new = {r[0] for r in ncon.execute(
                f'SELECT DISTINCT "{spec.month_col}" FROM "{spec.table}"')}
        table_added_months = tuple(sorted(m for m in months_new if m not in months_old))
        allow = tuple(sorted(set(new_months) | set(table_added_months)))
        if spec.month_col and allow:
            ph2 = ",".join("?" for _ in allow)
            added_rows = ncon.execute(
                f'SELECT COUNT(*) FROM "{spec.table}" WHERE "{spec.month_col}" IN ({ph2})',
                list(allow)).fetchone()[0]
        else:
            added_rows = 0
    old_sig = group_signature(old_db, spec.table, spec.group_cols, spec.month_col, (), payload,
                              spec.pk_cols)
    new_sig = group_signature(new_db, spec.table, spec.group_cols, spec.month_col,
                              allow, payload, spec.pk_cols)
    res.update(old_rows=old_sig["rows"], new_rows_overlap=new_sig["rows"],
               old_groups=old_sig["group_count"], new_groups_overlap=new_sig["group_count"],
               pk_fold_old=old_sig["pk_fold"], pk_fold_new_overlap=new_sig["pk_fold"])
    diff = res["differences"]
    if new_sig["pk_fold"] != old_sig["pk_fold"]:
        diff.append({"kind": "HISTORY_MUTATED", "why": "历史区间主键集合折叠不等"
                                                       "（历史行被改写/增删）"})
    missing = sorted(set(old_sig["groups"]) - set(new_sig["groups"]))
    for k in missing[:20]:
        diff.append({"kind": E_HISTORY_ROWS_LOST, "group": k,
                     "old_rows": old_sig["groups"][k]["rows"], "new_rows": None})
    mutated = []
    for k, og in old_sig["groups"].items():
        ng = new_sig["groups"].get(k)
        if ng is not None and (ng["sig"] != og["sig"] or ng["rows"] != og["rows"]):
            mutated.append({"kind": E_HISTORY_MUTATED, "group": k,
                            "old_rows": og["rows"], "new_rows": ng["rows"],
                            "old_sig": og["sig"][:24], "new_sig": ng["sig"][:24]})
    diff.extend(mutated[:20])
    res["mutated_groups"] = len(mutated)
    res["missing_groups"] = len(missing)
    common = sorted(set(old_sig["groups"]) & set(new_sig["groups"]))
    res["overlap_groups"] = len(common)
    res["overlap_digest"] = overlap_digest(
        [[k, old_sig["groups"][k]["sig"], new_sig["groups"][k]["sig"],
          old_sig["groups"][k]["rows"]] for k in common])
    ml = [m for m in months_old if m not in set(allow)]
    res["overlap_month_min"], res["overlap_month_max"] = (min(ml), max(ml)) if ml else (None, None)
    # 只允许「新增流月」造成新增：全表行数 = 重叠区间行数 + 新增流月行数
    res.update(new_rows_total=total_new, added_month_rows=added_rows,
               added_months=list(table_added_months),
               unexpected_added_rows=total_new - (new_sig["rows"] + added_rows))
    extra_months = sorted(set(table_added_months) - set(new_months))
    if extra_months:
        diff.append({"kind": E_UNEXPECTED_NEW_ROWS,
                     "why": f"该表出现了不在本轮裁定新增流月集合里的新流月: "
                            f"{extra_months[:20]}（增量语义只允许新流月造成新增）"})
    if res["unexpected_added_rows"] != 0:
        diff.append({"kind": E_UNEXPECTED_NEW_ROWS,
                     "why": f"新增行数 {total_new - new_sig['rows']} 不等于新增流月行数 "
                            f"{added_rows}"})
    if total_new < new_sig["rows"]:
        diff.append({"kind": E_HISTORY_ROWS_LOST, "why": "全表行数小于重叠区间行数"})
    res["hard"] = any(d.get("kind") in (E_HISTORY_MUTATED, E_HISTORY_ROWS_LOST,
                                        E_UNEXPECTED_NEW_ROWS, "HISTORY_MUTATED") for d in diff)
    return res


def _months_of(db: Path, spec: InvarianceSpec):
    with ro(db) as con:
        for r in con.execute(f'SELECT DISTINCT "{spec.month_col}" FROM "{spec.table}"'):
            yield r[0]


def compare_aggregate_table(old_db: Path, new_db: Path, spec: InvarianceSpec,
                            new_db_for_touch: Path, new_months: tuple) -> dict:
    """聚合层：新流月影响不到的键必须逐字节等值；影响到的键把新旧数值一起打印。"""
    res: dict = {"table": spec.table, "role": spec.role, "layer": spec.layer,
                 "verdict": "CHECKED", "differences": [], "affected_keys": 0}
    with ro(old_db) as ocon:
        ocols = spec_payload_cols(ocon, spec)
    if not ocols:
        res["verdict"] = "UNVERIFIED_SCHEMA"
        return res
    payload = tuple(ocols)
    gc = spec.group_cols
    if gc == ("(market)",):
        gc = ("category_id", "item")
    old_sig = group_signature(old_db, spec.table, gc, None, (), payload, spec.pk_cols)
    new_sig = group_signature(new_db, spec.table, gc, None, (), payload, spec.pk_cols)
    touched: set[str] = set()
    for col, src_table, src_col in spec.touch:
        try:
            touched.update(distinct_values(new_db_for_touch, src_table, src_col,
                                           "month_id", new_months))
        except Exception:                                   # noqa: BLE001
            pass
    res["touched_values"] = sorted(touched)[:40]
    diff = res["differences"]
    mutated = 0
    for k, og in old_sig["groups"].items():
        ng = new_sig["groups"].get(k)
        if ng is None:
            if not touched or not any(part in touched for part in k.split("|")):
                diff.append({"kind": E_HISTORY_ROWS_LOST, "group": k, "why": "聚合行消失"})
            continue
        if ng["sig"] == og["sig"] and ng["rows"] == og["rows"]:
            continue
        if touched and any(part in touched for part in k.split("|")):
            mutated += 1
            if len(diff) < 40:
                diff.append({"kind": "AGGREGATE_RECOMPUTED", "group": k,
                             "old_rows": og["rows"], "new_rows": ng["rows"],
                             "old_sig": og["sig"][:24], "new_sig": ng["sig"][:24],
                             "why": "该行含新流月影响到的键 ⇒ 属允许重算，数值已登记待人工查阅"})
            continue
        diff.append({"kind": E_HISTORY_MUTATED, "group": k, "old_rows": og["rows"],
                     "new_rows": ng["rows"], "old_sig": og["sig"][:24],
                     "new_sig": ng["sig"][:24],
                     "why": "新流月影响不到这一行，数值却变了 ⇒ 聚合口径漂了（不许放行）"})
    res["affected_keys"] = mutated
    res["old_rows"] = old_sig["rows"]
    res["new_rows"] = new_sig["rows"]
    stable = sorted(k for k in set(old_sig["groups"]) & set(new_sig["groups"])
                    if not (touched and any(part in touched for part in k.split("|"))))
    res["stable_groups"] = len(stable)
    res["overlap_digest"] = overlap_digest(
        [[k, old_sig["groups"][k]["sig"], new_sig["groups"][k]["sig"],
          old_sig["groups"][k]["rows"]] for k in stable])
    res["hard"] = any(str(d.get("kind")).startswith(("HISTORY", "UNEXPECTED")) or
                      d.get("kind") == E_HISTORY_MUTATED for d in diff)
    return res


def gate_f(P: Paths, ctx: Context, *, stage: str, log: Tee) -> GateResult:
    """G-F：替换前，新库与旧库在重叠区间必须逐字节等值（无阈值）。"""
    old_dbs = role_dbs(P, ctx, staging=False)
    new_dbs = role_dbs(P, ctx, staging=True)
    if stage != "finalize":
        return pending("G_F", "PENDING_STAGING", "G_F 需要本轮新库（staging）已建好；"
                                                "计划/预检阶段只能标 PENDING")
    checked: list[dict] = []
    unverified: list[dict] = []
    hard: list[dict] = []
    for spec in INVARIANCE_SPECS:
        if not spec.enabled:
            unverified.append({"table": spec.table, "role": spec.role,
                               "reason": f"DISABLED_SPEC: {spec.reason}"})
            continue
        old_db, new_db = old_dbs[spec.role], new_dbs[spec.role]
        if not is_readable_db(old_db):
            unverified.append({"table": spec.table, "role": spec.role,
                               "reason": f"旧库不可读或不存在（首建？): {old_db}"})
            continue
        with ro(old_db) as con:
            old_has = table_exists(con, spec.table)
        if not old_has:
            unverified.append({"table": spec.table, "role": spec.role,
                               "reason": "旧库无此表 ⇒ 无历史可保护"})
            continue
        if not is_readable_db(new_db):
            hard.append({"table": spec.table, "role": spec.role, "kind": E_STAGING_NOT_VERIFIABLE,
                         "why": f"旧库有历史、新库 {new_db} 却读不出 ⇒ 无法证明历史不变"})
            continue
        with ro(new_db) as con:
            if not table_exists(con, spec.table):
                hard.append({"table": spec.table, "role": spec.role,
                             "kind": E_HISTORY_TABLE_DROPPED, "why": "新库把旧库已有的表弄没了"})
                continue
        try:
            if spec.layer == "fact":
                rec = compare_fact_table(old_db, new_db, spec, ctx.new_months)
            else:
                rec = compare_aggregate_table(old_db, new_db, spec, new_dbs["main"],
                                              ctx.new_months)
        except Exception as exc:                             # noqa: BLE001
            hard.append({"table": spec.table, "role": spec.role, "kind": E_STAGING_NOT_VERIFIABLE,
                         "why": f"比对异常: {type(exc).__name__}: {exc}"})
            continue
        if rec["verdict"] == "UNVERIFIED_SCHEMA":
            unverified.append({"table": spec.table, "role": spec.role,
                               "reason": "登记列在当前表里不存在 ⇒ 不在不变性口径内"})
            continue
        checked.append(rec)
        if rec.get("hard"):
            for d in rec["differences"]:
                if d.get("kind") not in ("AGGREGATE_RECOMPUTED",):
                    hard.append({**d, "table": spec.table, "role": spec.role})
    for rec in checked:
        for d in rec["differences"][:12]:
            log(f"G_F DIFF {rec['role']}.{rec['table']} "
                f"{json.dumps(d, ensure_ascii=False)}")
    detail = {"new_months": list(ctx.new_months), "checked_tables": len(checked),
              "unverified_tables": unverified, "hard_differences": hard[:40],
              "hard_difference_count": len(hard),
              "per_table": [{k: v for k, v in rec.items() if k != "differences"}
                            for rec in checked],
              "differences_printed": [{"table": r["table"],
                                       "diffs": r["differences"][:8]} for r in checked]}
    if hard:
        return fail("G_F", hard[0]["kind"],
                    f"历史不变性被破坏：{len(hard)} 处硬差异（重叠区间必须逐字节等值，"
                    f"无阈值）；本轮绝不替换正式库", **detail)
    if not checked:
        log(f"G_F NOTE 没有任何表落在不变性口径内（{len(unverified)} 项未验证），"
            f"本轮历史不变性无法证明 ⇒ 仅在假树/首建里可放行")
        return ok("G_F", f"无历史可比（checked=0, unverified={len(unverified)}）", **detail)
    return ok("G_F", f"{len(checked)} 张表重叠区间逐字节等值，新增只来自新流月 "
                     f"{list(ctx.new_months)}", **detail)


# ---------------------------------------------------------------- G-G 历史漂移侦测

DRIFT_SOURCE_ENV = "MONTHLY_UPDATE_DRIFT_SOURCE"           # batch(缺省) | mirror
DRIFT_SAMPLE_ENV = "MONTHLY_UPDATE_DRIFT_SAMPLE_PERMILLE"  # 千分比抽样，1000=全量
DRIFT_INCLUDE_ENV = "MONTHLY_UPDATE_DRIFT_MUST_INCLUDE"    # 逗号分隔必抽票
DRIFT_MUST_INCLUDE_DEFAULT = ("600018.SH", "000001.SZ", "000001.SH")


def drift_sample_permille() -> int:
    raw = os.environ.get(DRIFT_SAMPLE_ENV)
    try:
        v = int(raw) if raw else 250                        # 缺省千分之 250（约 1400 只票）
    except ValueError:
        v = 250
    return max(1, min(1000, v))


_DRIFT_SEL_CACHE: dict[tuple[str, int], bool] = {}


def drift_stock_selected(stock_id: str, permille: int) -> bool:
    """确定性抽样（sha256 前 4 字节 mod 1000 < 千分比）+ 必抽清单。

    必抽清单默认含 600018.SH（跨主体拼接修复那件）、000001.SZ/000001.SH 基准；
    每月同一批票 ⇒ 基线比对才有可比性（抽样规则变了 ⇒ 视为换口径，需重建基线）。
    """
    ck = (stock_id, permille)
    hit = _DRIFT_SEL_CACHE.get(ck)
    if hit is not None:
        return hit
    must = [s.strip() for s in
            (os.environ.get(DRIFT_INCLUDE_ENV) or ",".join(DRIFT_MUST_INCLUDE_DEFAULT)).split(",")
            if s.strip()]
    d = hashlib.sha256(f"drift:{stock_id}".encode()).digest()
    sel = stock_id in must or (int.from_bytes(d[:4], "big") % 1000) < permille
    _DRIFT_SEL_CACHE[ck] = sel
    return sel



def batch_drift_signature(P: Paths, ctx: Context) -> dict | None:
    """按票×年对批次历史区间（排除新流月）做轻量抽样校验和；一次扫描，无副作用。"""
    batch = Path(ctx.batch_dir) / "data.sqlite" if ctx.batch_dir else None
    if not batch or not is_readable_db(batch):
        return None
    permille = drift_sample_permille()
    out: dict[str, dict] = {}
    try:
        with ro(batch) as con:
            if not table_exists(con, "monthly_facts"):
                return None
            cols = table_columns(con, "monthly_facts")
            if not {"stock_id", "solar_year", "month_id"}.issubset(set(cols)):
                return None
            payload = tuple(c for c in cols if c != "seq")
            sql = "SELECT %s FROM monthly_facts" % ",".join('"%s"' % c for c in payload)
            params: list = []
            if ctx.new_months:
                sql += " WHERE month_id NOT IN (%s)" % ",".join("?" for _ in ctx.new_months)
                params = list(ctx.new_months)
            for row in con.execute(sql, params):
                rec = dict(zip(payload, row))
                sid = str(rec.get("stock_id"))
                if not drift_stock_selected(sid, permille):
                    continue
                key = f"{sid}|{rec.get('solar_year')}"
                g = out.setdefault(key, {"rows": 0, "sum": 0, "xor": 0})
                dg = row_digest(tuple(rec[c] for c in payload))
                g["rows"] += 1
                g["sum"] = (g["sum"] + dg) % FOLD_MOD
                g["xor"] ^= dg
    except Exception:                                        # noqa: BLE001
        return None
    for g in out.values():
        g["sig"] = f"{g['sum']:032x}{g['xor']:032x}"
        del g["sum"], g["xor"]
    return {"source": "batch", "path": str(batch), "permille": permille, "groups": out}


def mirror_drift_signature(P: Paths, ctx: Context) -> dict | None:
    """镜像侧抽样校验和（stdlib zipfile 逐年读成员 CSV）：批次缺席时用它兜底。"""
    permille = drift_sample_permille()
    years = sorted(p.stem for p in P.daily_dir.glob("*.zip"))
    if not years:
        return None
    out: dict[str, dict] = {}
    for year in years:
        try:
            with zipfile.ZipFile(P.daily_dir / f"{year}.zip") as zf:
                for name in zf.namelist():
                    sid = Path(name).stem
                    if not drift_stock_selected(sid, permille):
                        continue
                    acc = out.setdefault(f"{sid}|{year}", {"rows": 0, "sum": 0, "xor": 0})
                    with zf.open(name) as fh:
                        fh.readline()
                        for raw in fh:
                            dg = row_digest((sid, year, raw.rstrip(b"\r\n")))
                            acc["rows"] += 1
                            acc["sum"] = (acc["sum"] + dg) % FOLD_MOD
                            acc["xor"] ^= dg
        except (OSError, zipfile.BadZipFile):
            return None
    for g in out.values():
        g["sig"] = f"{g['sum']:032x}{g['xor']:032x}"
        del g["sum"], g["xor"]
    return {"source": "mirror", "path": str(P.daily_dir), "permille": permille, "groups": out}


def current_drift_signature(P: Paths, ctx: Context) -> dict | None:
    """漂移校验和的取数来源：缺省用本轮批次（一次扫描，最便宜）；
    mirror 需要显式 MONTHLY_UPDATE_DRIFT_SOURCE=mirror（逐 zip 逐成员读，故意不自动降级，
    免得有人在 dry-run 里莫名其妙扫完整面镜像）。"""
    source = (os.environ.get(DRIFT_SOURCE_ENV) or "batch").strip() or "batch"
    if source == "mirror":
        return mirror_drift_signature(P, ctx)
    return batch_drift_signature(P, ctx)


def gate_g(P: Paths, ctx: Context, *, log: Tee) -> tuple[GateResult, dict | None]:
    """G-G：上游改过历史就必须拒绝增量追加（纯增量的盲点专门由这道闸堵）。

    full 模式整库重算，历史漂移不改变结果 ⇒ 只登记不拦（PASS + note）。
    """
    sig = current_drift_signature(P, ctx)
    if sig is None:
        return ok("G_G", "无可读的漂移源（批次/镜像都取不到按票×年的历史校验和）⇒ 本轮未验证",
                  checked_groups=0, source_unavailable=True), None
    groups = sig["groups"]
    baseline: dict | None = None
    if P.drift_baseline.exists():
        try:
            baseline = json.loads(P.drift_baseline.read_text(encoding="utf-8"))
        except Exception as exc:                             # noqa: BLE001
            return fail("G_G", E_DRIFT_BASELINE_MISSING,
                        f"漂移基线损坏：{P.drift_baseline}（{exc}）", checked_groups=len(groups)), sig
    if baseline is None:
        detail = {"checked_groups": len(groups), "baseline_path": str(P.drift_baseline),
                  "force": ctx.force_drift_baseline}
        if ctx.force_drift_baseline:
            return ok("G_G", "漂移基线缺失：按人工 --force-drift-baseline 建档放行一次",
                      bootstrapped=True, **detail), sig
        return fail("G_G", E_DRIFT_BASELINE_MISSING,
                    "work/history_drift_baseline.json 不存在，无历史校验和可比；"
                    "需人工用 --force-drift-baseline（或 MONTHLY_UPDATE_FORCE_DRIFT_BASELINE=1）"
                    "建档后才会放行增量", **detail), sig
    prev = baseline.get("groups", {})
    drifted, vanished = [], []
    for key, rec in prev.items():
        cur = groups.get(key)
        if cur is None:
            vanished.append({"group": key, "old_sig": str(rec.get("sig"))[:24],
                             "now": "MISSING"})
        elif cur.get("sig") != rec.get("sig") or cur.get("rows") != rec.get("rows"):
            drifted.append({"group": key, "old_rows": rec.get("rows"),
                            "new_rows": cur["rows"], "old_sig": str(rec.get("sig"))[:24],
                            "new_sig": cur["sig"][:24]})
    added = sorted(set(groups) - set(prev))
    for d in (drifted + vanished)[:20]:
        log(f"G_G DRIFT {json.dumps(d, ensure_ascii=False)}")
    detail = {"checked_groups": len(groups), "source": sig["source"],
              "drift_source_path": sig["path"],
              "baseline_run_id": baseline.get("run_id"),
              "baseline_recorded_at_bjt": baseline.get("recorded_at_bjt"),
              "drifted_count": len(drifted), "vanished_count": len(vanished),
              "added_groups": len(added), "sample_permille": sig["permille"],
              "drifted": drifted[:40], "vanished": vanished[:40]}
    if drifted or vanished:
        if ctx.mode == MODE_FULL:
            log(f"G_G NOTE full 模式整库重算，{len(drifted) + len(vanished)} 处历史漂移"
                f"不拦（季度互验用）")
            return ok("G_G", f"检出 {len(drifted) + len(vanished)} 处历史漂移，"
                             f"full 模式整库重算故不拦", **detail), sig
        return fail("G_G", E_HISTORY_DRIFT,
                    f"上游确实改过历史（{len(drifted)} 组校验和不一致、"
                    f"{len(vanished)} 组消失）⇒ 拒绝增量追加；需要一次人工裁定的全量对齐："
                    f"先 --mode full 重跑并复核 G-F，再由负责人确认基线", **detail), sig
    return ok("G_G", f"历史区间 {len(groups)} 组（票×年）校验和与基线一致，"
                     f"新增键 {len(added)} 组属新上市/新年度", **detail), sig


def write_drift_baseline(P: Paths, sig: dict, ctx: Context, *, enabled: bool,
                         log: Tee) -> dict:
    """基线只在门禁通过时更新，且走原子写（temp + os.replace）。"""
    doc = {"schema": DRIFT_SCHEMA, "recorded_at_bjt": iso_bjt(now_bjt()),
           "run_id": ctx.run_id, "mode": ctx.mode, "source": sig["source"],
           "source_path": sig["path"], "sample_permille": sig["permille"],
           "new_months_excluded": list(ctx.new_months),
           "group_count": len(sig["groups"]), "groups": sig["groups"]}
    atomic_write_json(P.drift_baseline, doc, enabled=enabled, log=log)
    return doc


# ---------------------------------------------------------------- 三层纪律与增量接线

def layer_discipline_violations(ctx: Context, nodes: list[Node], state: dict) -> list[dict]:
    """聚合层/验收层在任何模式下都不许被跳过或降格（「增量」不等于「少算」）。"""
    out: list[dict] = []
    by_node = {n.get("node"): n for n in state.get("nodes", [])}
    for n in nodes:
        if n.mode_policy not in NO_SKIP_POLICIES:
            continue
        rec = by_node.get(n.index, {})
        status = rec.get("status", "PLANNED")
        if status in ("SKIPPED_NOT_SELECTED",):
            out.append({"node": n.index, "key": n.key, "layer": n.layer,
                        "policy": n.mode_policy, "status": status,
                        "code": E_AGG_SKIP,
                        "why": "该节点属聚合层/验收层，增量模式也不许跳过"})
    return out


def incremental_env(ctx: Context) -> dict:
    """注入给事实层脚本的增量开关：脚本支持就只追加新流月，不支持就照旧整表重建
    （G-F 负责证明历史没被改写）。"""
    return {"MONTHLY_UPDATE_MODE": ctx.mode,
            "MONTHLY_UPDATE_NEW_MONTHS": ",".join(ctx.new_months),
            "MONTHLY_UPDATE_RUN_ID": ctx.run_id}


def seed_staging_from_live(P: Paths, ctx: Context, *, enabled: bool, log: Tee,
                           roles: tuple = ("main", "etf")) -> dict:
    """增量的「底」：以现网库为底复制出 staging_<run_id>.sqlite（sqlite backup API）。

    事实层脚本随后只往这个副本里追加新流月；正式库在 G-F 通过之前一字节不动。
    用 sqlite3 Backup API（stdlib）而不是文件复制 ⇒ 不会把 -wal/-journal 的半截状态带过去。
    """
    rec: dict = {"seeded": [], "skipped": []}
    all_roles = {"main": (P.main_db, P.local_db_dir / f"staging_{ctx.run_id}.sqlite"),
                 "etf": (P.etf_db, P.etf_v1 / "database" / f"staging_{ctx.run_id}.sqlite")}
    for role, (live, staging) in {k: v for k, v in all_roles.items() if k in roles}.items():
        if not is_readable_db(live):
            rec["skipped"].append({"role": role, "why": f"现网库不可读: {live}"})
            continue
        if staging.exists():
            rec["skipped"].append({"role": role, "why": f"staging 已存在，不覆盖: {staging}"})
            continue
        if not enabled:
            log(f"WOULD SEED (dry-run, 0 字节) {live} -> {staging}")
            rec["skipped"].append({"role": role, "why": "dry-run"})
            continue
        staging.parent.mkdir(parents=True, exist_ok=True)
        tmp = staging.with_name(staging.name + f".part.{os.getpid()}")
        dst = sqlite3.connect(str(tmp))
        try:
            with ro(live) as src:
                src.backup(dst, pages=1024)
        finally:
            dst.commit()
            dst.close()
        os.replace(tmp, staging)
        rec["seeded"].append({"role": role, "staging": str(staging),
                              "bytes": staging.stat().st_size,
                              "seeded_at_bjt": iso_bjt(now_bjt())})
        log(f"SEEDED {role} 现网库 -> {staging}（历史行为物理底本）")
    return rec


def apply_incremental_facts(P: Paths, ctx: Context, *, enabled: bool, log: Tee) -> dict:
    """事实层追加引擎：只对有同构来源且 wired=True 的表做 ATTACH + INSERT 新流月。

    当前生产接线为 0（理由逐条登记在 FACT_APPEND_SPECS），因此这里如实返回
    pending 清单而不是假装成功；G-F 仍然逐字节核对历史。
    """
    applied, pending = [], []
    for spec in FACT_APPEND_SPECS:
        if spec["wired"]:
            applied.append(spec)
        else:
            pending.append({k: spec[k] for k in ("role", "table", "source") } |
                           {"reason": spec["reason"]})
    if applied and enabled:
        dbs = role_dbs(P, ctx, staging=True)
        srcs = {"batch_data_sqlite": (Path(ctx.batch_dir) / "data.sqlite")
                if ctx.batch_dir else None,
                "shadow_run_sqlite": P.project_root / f"v13_shadow_fullmarket_{ctx.runday}"
                / "v13_full_shadow.sqlite"}
        for spec in applied:
            src = srcs.get(spec["source"])
            dst = dbs[spec["role"]]
            if not src or not is_readable_db(src):
                raise RuntimeError(f"{E_APPEND_SCOPE_EMPTY}: 追加来源不可读 {src}")
            with sqlite3.connect(str(dst)) as con:
                con.execute("ATTACH DATABASE ? AS src", (str(src),))
                ph = ",".join("?" for _ in ctx.new_months)
                con.execute(f'INSERT INTO main."{spec["table"]}" '
                            f'SELECT * FROM src."{spec["table"]}" '
                            f'WHERE "{spec["month_col"]}" IN ({ph})', list(ctx.new_months))
                con.commit()
                con.execute("DETACH DATABASE src")
            log(f"FACT APPEND {spec['role']}.{spec['table']} 新流月 {list(ctx.new_months)}")
    if pending:
        log(f"FACT APPEND PENDING_SCRIPT {len(pending)} 项（脚本侧尚无 append 分支，"
            f"见 INTEGRATION_TODOS）；本轮这些表按整表重建跑，由 G-F 证明历史未改写")
    return {"mode": ctx.mode, "applied": applied, "pending_script": pending}


def gate_layers(P: Paths, ctx: Context, nodes: list[Node], *, stage: str, log: Tee,
                enabled: bool) -> tuple[GateResult, GateResult]:
    """把 G-F/G-G 接到主闸门链上：G_F 只在 finalize（staging 已建好）才真判，
    其余阶段标 PENDING；G_G 任何时候都能算（只读批次）。"""
    if ctx.batch_dir or stage == "finalize":
        ctx.new_months = derive_new_months(P, ctx, log)
    elif not ctx.new_months:
        ctx.new_months = derive_new_months(P, ctx, log)
    if stage == "finalize":
        gf = gate_f(P, ctx, stage=stage, log=log)
    else:
        gf = pending("G_F", "PENDING_STAGING",
                     "计划/预检阶段没有 staging 可比，G_F 待定（不因此放行替换）")
    gg, sig = gate_g(P, ctx, log=log)
    ctx.drift_sig = sig                                    # 收尾只在门禁通过时写基线
    return gf, gg



# ---------------------------------------------------------------- 接受判定


def all_gates_pass(results: list[GateResult]) -> bool:
    return bool(results) and all(r.verdict == "PASS" for r in results)


def decide_accept(accept_flag: bool, execute: bool,
                  results: list[GateResult]) -> tuple[bool, str, list[str]]:
    """双向断言用：只有 --execute + --accept-gates + 七闸全 PASS 才允许 --accept。"""
    blockers = [f"{r.gate}:{r.verdict}:{r.code or r.message}" for r in results
                if r.verdict != "PASS"]
    if not execute:
        return False, "dry-run 永不接受，也永不传 --accept", blockers
    if not accept_flag:
        return False, "未显式 --accept-gates：只建 staging，不替换正式库、不传 --accept", blockers
    if blockers:
        return False, "七闸（G_A..G_G）未全过，拒绝 --accept", blockers
    return True, "七闸全 PASS 且 --accept-gates：允许原子替换并向扩展库传 --accept", []


# ---------------------------------------------------------------- 指纹 / 更新记录

def build_fingerprints(P: Paths, entries: list[dict], run_id: str, status: str,
                       *, enabled: bool, log: Tee) -> dict:
    """各库指纹（size / sha256 前 12 位 / built_at_bjt）。5GB 大库禁止每请求全量哈希，
    但流水线收尾算一次是允许的（就是这里）。"""
    dbs: dict[str, dict] = {}
    for ent in entries:
        key = ent["key"]
        path = Path(ent["path"])
        item = {"path": str(path), "role": ent.get("role", key),
                "exists": path.exists(), "size_bytes": None, "sha256_12": None,
                "mtime_bjt": None, "built_at_bjt": None,
                "built_at_bjt_source": "file mtime（脚本内无构建时刻，收尾统一取 mtime）"}
        if path.exists():
            st = path.stat()
            item["size_bytes"] = st.st_size
            item["sha256_12"] = sha256_file(path)[:12]
            mb = iso_bjt(datetime.fromtimestamp(st.st_mtime, BJT))
            item["mtime_bjt"] = mb
            item["built_at_bjt"] = mb
        dbs[key] = item
    doc = {
        "schema": FINGERPRINT_SCHEMA,
        "generated_at_bjt": iso_bjt(now_bjt()),
        "run_id": run_id,
        "pipeline_status": status,
        "databases": dbs,
        "cache_handoff": {
            "author_of_this_file": "T2 run_monthly_update.py",
            "consumer": "T1 app/update_scheduler.py（8737 服务内置调度器）",
            "reason": ("app/api.py 的缓存 (_META_CACHE/_CALENDAR_CACHE/_OPP_CACHE) 在"
                       "服务进程内；编排器是另一个进程，清了也没用，且不得占 8737 端口/"
                       "不得 kill 现有服务。"),
            "detection": "轮询本文件 databases.*.sha256_12 / mtime_bjt 变化（或主库 mtime）",
            "expected_action": "变化即调用 app/api.invalidate_caches()（T1 地盘），"
                               "否则页面会一直返回旧统计直到进程重启",
            "orchestrator_calls_invalidate_caches": False,
            "orchestrator_posts_http_to_8737": False,
        },
    }
    atomic_write_json(P.fingerprints, doc, enabled=enabled, log=log)
    return doc


UPDATE_LOG_HEADER = (
    "# 更新记录\n\n"
    "由 `tools/run_monthly_update.py` 在每次流水线结束时追加一行。\n\n"
    "| 北京时间 | trigger | status | 错误码 | 一句话 |\n"
    "|---|---|---|---|---|\n")


def append_update_log(P: Paths, run_id: str, trigger: str, status: str, code: str,
                      one_liner: str, *, enabled: bool, log: Tee) -> None:
    row = f"| {iso_bjt(now_bjt())} | {trigger} | {status} | {code or '-'} | {one_liner} |"
    if not enabled:
        log(f"WOULD APPEND (dry-run, 0 字节) {P.update_log_md}: {row}")
        return
    if not P.update_log_md.exists():
        atomic_write_text(P.update_log_md, UPDATE_LOG_HEADER, enabled=True, log=log)
    append_line(P.update_log_md, row, enabled=True, header=UPDATE_LOG_HEADER, log=log)


# ---------------------------------------------------------------- 计划：13 节点

STAGES = [
    "stage1_schema_profile.py", "stage2_facts_relations.py", "stage3_stats.py",
    "stage4_validate.py", "stage5_display_order.py", "stage6_reports.py",
    "stage7_acceptance_md.py",
]
ETF_BRANCH = ["00_bootstrap.sh", "01_fetch_source.py", "02_build_read_layer.py",
              "03c_full_manifest.py", "04f_build_full_facts.py",
              "05f_build_full_relations.py", "06f_build_full_db.py",
              "07f_validate_full.py", "09_build_etf_stats.py", "10_validate_etf_stats.py"]


@dataclass
class Context:
    run_id: str
    runday: str
    trigger: str
    as_of: str | None
    as_of_basis: str
    dry_run: bool
    execute: bool
    accept_gates: bool
    only: list[int]
    force_source_check: bool
    target: dict = field(default_factory=dict)
    publication: dict = field(default_factory=dict)
    batch_dir: Path | None = None
    relation_build_db: Path | None = None
    absent_at_plan: tuple = ()      # 计划阶段确认「当时不存在」的 new_only 目标
    mode: str = MODE_INCREMENTAL     # incremental（缺省）| full（季度互验）
    new_months: tuple = ()          # 本轮允许追加的新流月 month_id（增量语义的边界）
    force_drift_baseline: bool = False   # 人工裁定：首轮建立 G-G 基线
    compare_run: str | None = None       # --compare-run：只读对账，不跑流水线
    drift_sig: dict | None = None        # 本轮算出的按票×年历史校验和（门禁全过才写基线）



def node_runs(ctx: Context, index: int) -> bool:
    return not ctx.only or index in ctx.only


def resolve_golden_report(P: Paths, ctx: Context) -> Path:
    """--golden-acceptance 用「上一批」的 golden_report.json：优先本轮记录，
    否则退回 frozen_baseline.txt 里登记的冻结批次（不硬编码日期）。"""
    last = P.work / "last_batch.json"
    if last.exists():
        try:
            cand = Path(json.loads(last.read_text(encoding="utf-8"))["batch_dir"]) / "reports" \
                / "golden_report.json"
            if cand.exists():
                return cand
        except Exception:                                  # noqa: BLE001
            pass
    fb = P.local_v1 / "work" / "frozen_baseline.txt"
    if fb.exists():
        for line in fb.read_text(encoding="utf-8").splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and "/batches/" in parts[1] and parts[1].endswith("data.sqlite"):
                cand = Path(parts[1].strip()).parent / "reports" / "golden_report.json"
                if cand.exists():
                    return cand
    return Path("<上一批>/reports/golden_report.json（未定位，节点 4 需人工指定）")


def plan_nodes(P: Paths, ctx: Context) -> list[Node]:
    # 事实层策略随模式变：incremental=只写新流月；full=整表重建（节点表不许谎报层与策略）
    fp = POLICY_APPEND if ctx.mode == MODE_INCREMENTAL else POLICY_FULL_REBUILD
    run_work = P.work / "runs" / ctx.run_id
    staging_main = P.local_db_dir / f"staging_{ctx.run_id}.sqlite"
    staging_etf = P.etf_v1 / "database" / f"staging_{ctx.run_id}.sqlite"
    shadow_run = P.project_root / f"v13_shadow_fullmarket_{ctx.runday}"
    relation_out = P.relation_v1 / "out" / ctx.run_id
    batch_new = ctx.batch_dir or P.batches / "<scanner 生成的新目录>"
    as_of = ctx.as_of or "<侦测到的源末交易日>"
    nodes: list[Node] = []

    nodes.append(Node(
        1, "nas_mirror_fetch", "NAS 正式源只读镜像（远端清单驱动，默认关）",
        cmds=[[PY, str(P.scripts / "fetch_nas_source.py"), "--from-remote-list"]],
        env={"FETCH_FROM_REMOTE_LIST": "1"}, cwd=P.scripts,
        layer=LAYER_SOURCE, mode_policy=POLICY_READONLY,
        writes=[WriteTarget(P.mirror / "source_manifest.json", note="镜像+清单，按设计原地刷新")],
        note="默认不加开关时 fetch_nas_source.py 仍走硬编码 60 文件清单；"
             "本流水线用 --from-remote-list 让远端目录清单驱动，上游新增/改名不再静默漏拉。"))

    nodes.append(Node(
        2, "project_source_input", "投影 → source_input/nas_source.duckdb",
        cmds=[[PY, str(P.scripts / "build_source_input.py")]], cwd=P.scripts,
        layer=LAYER_SOURCE, mode_policy=POLICY_READONLY,
        incremental_note="投影现状是按年全量重建（1990-2027 全量包），增/改历史都会重投；"
                         "语义上属源层，不是库层 ⇒ 增量模式不改它，漂移由 G-G 侦测。",
        inputs=[P.mirror / "source_manifest.json"],
        writes=[WriteTarget(P.source_input / "nas_source.duckdb",
                            note="派生件，脚本内路径不可改（不在允许改动的两个脚本之内）"),
                WriteTarget(P.source_input / "build_stats.json")],
        note="需要 duckdb：系统 python3 无该模块 → 留给集成阶段用既有环境跑（TODO）。"))

    nodes.append(Node(
        3, "build_master", "身份主表 stock_master.csv（AS_OF 可覆盖，缺省不变）",
        cmds=[[PY, str(P.scripts / "build_master.py")]],
        env={"AS_OF": as_of, **incremental_env(ctx)}, cwd=P.scripts,
        layer=LAYER_SOURCE, mode_policy=POLICY_READONLY,
        inputs=[P.source_input / "nas_source.duckdb"],
        writes=[WriteTarget(P.source_input / "stock_master.csv"),
                WriteTarget(P.docs_root / "reports" / "universe_notes.json")],
        note="AS_OF 由 G_A 侦测到的源末交易日注入；不设置环境变量时脚本仍用 2026-10-15。"
             "同样依赖 duckdb（TODO：集成阶段）。"))

    nodes.append(Node(
        4, "market_scan", "全市场扫描 → batches/{新 run_id}",
        cmds=[[PY, str(scanner_cli()), "scan", "--config", str(P.docs_root / "config" / "config.local.yaml"),
              "--scope", "market", "--as-of", as_of,
              "--golden-acceptance", str(resolve_golden_report(P, ctx))]],
        cwd=P.docs_root, inputs=[P.source_input / "stock_master.csv"],
        layer=LAYER_FACT, mode_policy=fp,
        append_tables=(("batch", "monthly_facts"),),
        incremental_note="批次是「新流月事实」的唯一来源：增量模式按 --as-of 只关心新流月，"
                         "但扫描器仍会重扫全历史（这是好事：G-G 拿它的按票×年校验和查上游修订）。",
        writes=[WriteTarget(batch_new, new_only=True, note="永不覆盖旧批次（SKILL.md L73）")],
        note="扫描器自身分配批次目录名，节点执行后回填实际路径。"))

    rel_db = ctx.relation_build_db or P.relation_sealed_db
    nodes.append(Node(
        5, "relation_db_rebuild", "历史关系库重建 m4a→m4b→m5→m6（本轮受封印件阻断）",
        cmds=[
            [PY, str(P.relation_v1 / "tools" / "m4a_prepare_snapshot.py")],
            [PY, str(P.relation_v1 / "tools" / "m4b_generate.py"),
             "--out", str(relation_out / "history_stock_relations_build.sqlite3")],
            [PY, str(P.relation_v1 / "tools" / "m5_verify_gates.py")],
            [PY, str(P.relation_v1 / "tools" / "m6_publish.py")],
        ],
        cwd=P.relation_v1, inputs=[rel_db],
        layer=LAYER_FACT, mode_policy=fp,
        append_tables=(("relation", "pair 关系"),),
        incremental_note="关系库 pair 关系现状是 m4b 全量生成；增量语义要求「历史行不动、"
                         "只 INSERT 新月份」⇒ 需要 m4b/m6 的 append 开关（TODO，见 INTEGRATION_TODOS）。",
        writes=[
            WriteTarget(P.relation_v1 / "snapshot" / "period.jsonl", sealed_rebuild=True,
                        note="m4a 无路径开关：写回 snapshot/（封印血缘）"),
            WriteTarget(relation_out / "history_stock_relations_build.sqlite3", new_only=True),
            WriteTarget(P.relation_v1 / "out" / "run_a.sqlite3", sealed_rebuild=True,
                        note="m5 固定读此路径"),
            WriteTarget(P.relation_v1 / "evidence" / "acceptance_gates.json", sealed_rebuild=True),
            WriteTarget(P.relation_dist / "history_stock_relations_v1.sqlite", sealed_rebuild=True,
                        note="m6 直接 os.replace 到封印 dist/"),
        ],
        internal="",
        note="m4b 支持 --out（可落 run_id 新目录），但 m4a/m5/m6 的输出路径全硬编码，"
             "m6 更是 os.replace 进 dist/（带 SHA256SUMS.txt 封印）。G_B 的写入审计必然把该节点"
             "判为 SEALED_TARGET_CONFLICT ⇒ 本轮绝不执行节点 5，需要负责人在集成阶段裁定："
             "要么授权给 m4a/m5/m6 加同类环境变量覆盖，要么关系库继续人工重建、"
             "流水线只读消费现有封印 dist。"))

    nodes.append(Node(
        6, "bridge_export_staging", "桥接导出 staging CSV（只读源库）",
        cmds=[[PY, str(P.release_candidate / "tools" / "from_readonly_sqlite.py"),
              "--db", str(rel_db), "--output", str(run_work / "v13_staging_input_all.csv")]],
        cwd=P.release_candidate / "tools",
        layer=LAYER_FACT, mode_policy=fp,
        inputs=[rel_db],
        writes=[WriteTarget(run_work / "v13_staging_input_all.csv", new_only=True)],
        note="README_直接运行.md L73-80 的现成用法；--db 指向本轮 m4b 产物（若节点 5 被放行），"
             "否则退回只读现有封印 dist 库（内容仍是上一发布线）。"))

    nodes.append(Node(
        7, "shadow_full_run", "V1.3 全市场影子跑批 → v13_shadow_fullmarket_<runday>/",
        cmds=[[BASH, str(shadow_run / "scripts" / "run_full_shadow.sh")]],
        cwd=P.project_root,
        layer=LAYER_ACCEPT, mode_policy=POLICY_ACCEPTANCE, skip_allowed=False,
        incremental_note="影子跑批属验收层，增量模式照样全量跑，不许降格（46 分钟该花就花）。",
        inputs=[run_work / "v13_staging_input_all.csv"],
        writes=[WriteTarget(shadow_run, new_only=True, note="本轮专属目录"),
                WriteTarget(shadow_run / "v13_full_shadow.sqlite", new_only=True),
                WriteTarget(shadow_run / "v13_full_shadow_manifest.json", new_only=True),
                WriteTarget(shadow_run / "scripts" / "run_full_shadow.sh", new_only=True,
                            note="由编排器从 20261018 版生成（TASK/INPUT 改指本轮目录）")],
        internal="materialize_shadow_run_dir",
        note="原 run_full_shadow.sh 的 TASK/INPUT 是绝对硬编码，且 20261018 目录属封印件；"
             "编排器不改它，而是在新目录里生成一份指向本轮的副本再执行。"
             "实测阶段A 204s / contract_validate 181s 峰值内存 19.9GB / 整包约 45 分钟。"))

    stage_layers = (LAYER_FACT, LAYER_FACT, LAYER_AGG, LAYER_ACCEPT, LAYER_AGG,
                    LAYER_ACCEPT, LAYER_ACCEPT)
    stage_policies = (fp, fp, POLICY_FULL_RECOMPUTE, POLICY_ACCEPTANCE,
                      POLICY_FULL_RECOMPUTE, POLICY_ACCEPTANCE, POLICY_ACCEPTANCE)
    nodes.append(Node(
        8, "main_db_stage1_7",
        "主库 stage1..stage7 → database/staging_<run_id>.sqlite"
        f"（{'事实追加' if ctx.mode == MODE_INCREMENTAL else '整库重建'} + 聚合全量 + 验收全量）",
        cmds=[[PY, str(P.local_scripts / s)] for s in STAGES],
        env={"LOCAL_DB_OUT": str(staging_main), **incremental_env(ctx)}, cwd=P.local_v1,
        layer=LAYER_FACT, mode_policy=fp, skip_allowed=False,
        cmd_layers=stage_layers, cmd_policies=stage_policies,
        append_tables=tuple(("main", t) for t in MAIN_FACT_TABLES),
        internal="seed_then_build" if ctx.mode == MODE_INCREMENTAL else "",
        incremental_note="增量：先 seed（现网库副本 = 历史行的物理底本），stage2 只该写新流月；"
                         "stage3/stage5 聚合层每轮全量确定性重算；stage4/6/7 验收层保持全量。",
        inputs=[P.main_db, P.shadow_old / "v13_full_shadow.sqlite",
                P.relation_sealed_db],
        writes=[WriteTarget(staging_main, new_only=True),
                WriteTarget(P.local_v1 / "work" / "validation.json", note="派生报告件"),
                WriteTarget(P.local_v1 / "reports" / "数据来源与版本清单.json", note="派生报告件"),
                WriteTarget(P.local_v1 / "reports" / "全市场数据库建设验收.md", note="派生报告件")],
        rows_sql=(staging_main, "SELECT COUNT(*) FROM monthly_facts"),
        note="stage*.py 的输出路径已加 LOCAL_DB_OUT 覆盖，缺省值与今天完全一致；"
             "全程约 43 分钟（含一次 stage2 失败重跑的历史）。"))

    nodes.append(Node(
        9, "extensions_compare", "扩展联动比对（本节点不传 --accept）",
        cmds=[[BASH, str(P.extensions_sh)]], cwd=P.local_v1 / "extensions",
        layer=LAYER_FACT, mode_policy=fp,
        append_tables=(("cap_tier", "cap_tier_monthly"),),
        incremental_note="市值分档/pair 关系扩展：只该追加新流月的行；扩展库现在原地重建 ⇒ "
                         "G-F 暂时无法在替换前证明（enabled=False 已登记，见 INTEGRATION_TODOS）。",
        inputs=[P.main_db],
        writes=[],
        note="无参 = 只比对。--accept 只在节点 13 七闸全过、正式主库完成原子替换后才传，"
             "顺序上保证扩展库对齐的是新主库。"))

    nodes.append(Node(
        10, "etf_branch", "ETF 支路 00→01→02→03c→04f→05f→06f→07f→09→10 → staging_<run_id>.sqlite",
        cmds=[[BASH, str(P.etf_scripts / "00_bootstrap.sh")]] +
             [[PY, str(P.etf_scripts / s)] for s in ETF_BRANCH[1:]],
        env={"ETF_DB_OUT": str(staging_etf), **incremental_env(ctx)}, cwd=P.etf_v1,
        layer=LAYER_FACT, mode_policy=fp, skip_allowed=False,
        cmd_layers=(LAYER_SOURCE, LAYER_SOURCE, LAYER_SOURCE, LAYER_FACT, LAYER_FACT, LAYER_FACT,
                    LAYER_ACCEPT, LAYER_ACCEPT, LAYER_AGG, LAYER_AGG),
        cmd_policies=(POLICY_READONLY, POLICY_READONLY, POLICY_READONLY, fp, fp, fp,
                      POLICY_ACCEPTANCE, POLICY_ACCEPTANCE, POLICY_FULL_RECOMPUTE,
                      POLICY_FULL_RECOMPUTE),
        append_tables=tuple(("etf", t) for t in MAIN_FACT_TABLES),
        internal="seed_then_build_etf" if ctx.mode == MODE_INCREMENTAL else "",
        incremental_note="ETF 支路同 A 股：04f/05f 关系事实只该追加新流月，09 的 stats 每轮全量"
                         "重算（ETF 关系历史行不许改写，由 G-F 兜）。",
        inputs=[P.etf_db],
        writes=[WriteTarget(staging_etf, new_only=True),
                WriteTarget(P.etf_v1 / "work", note="work/ 下的派生 staging 件原地重建"),
                WriteTarget(P.etf_v1 / "reports", note="派生报告件原地重建")],
        rows_sql=(staging_etf, "SELECT COUNT(*) FROM monthly_facts"),
        note="07f 内 LAST_FINAL=\"2026-M08\" 等硬编码期望需要集成阶段确认；01 需 NAS ETF 镜像；"
             "02/04f/07f 需 duckdb（TODO）。整板实测约 21 分钟。"))

    nodes.append(Node(
        11, "independent_verify", "独立核验（09:30 锚点复算）→ 新批次 reports/ 内",
        cmds=[[PY, str(P.scripts / "independent_verify.py"), str(batch_new)]],
        cwd=P.scripts, inputs=[P.mirror / "source_manifest.json"],
        layer=LAYER_ACCEPT, mode_policy=POLICY_ACCEPTANCE, skip_allowed=False,
        incremental_note="独立核验属验收层：增量模式一样全量复算，不许降格。",
        writes=[WriteTarget(Path(str(batch_new) + "/reports/independent_recomputation.json"),
                            note="落在本轮新批次目录内")],
        note="输入只需本轮新批次 + 已哈希校验的源，故 G_C 在节点 13 定案时读它的报告；"
             "该脚本需 duckdb（TODO：集成阶段用既有环境跑）。"))

    nodes.append(Node(
        12, "stats_reports_v5_slice", "stats/reports/v5 切片重导（对 staging 库重跑 + 切片）",
        cmds=[[PY, str(P.local_scripts / "stage3_stats.py")],
              [PY, str(P.local_scripts / "stage6_reports.py")],
              [PY, str(P.project_root / "v5_lowpoint_ui_isolated" / "tools" / "export_v5_slice.py"),
               "--out", str(run_work / "v5_lowpoint_slice.sqlite")]],
        env={"LOCAL_DB_OUT": str(staging_main), **incremental_env(ctx)}, cwd=P.local_v1,
        layer=LAYER_AGG, mode_policy=POLICY_FULL_RECOMPUTE, skip_allowed=False,
        cmd_layers=(LAYER_AGG, LAYER_ACCEPT, LAYER_AGG),
        cmd_policies=(POLICY_FULL_RECOMPUTE, POLICY_ACCEPTANCE, POLICY_FULL_RECOMPUTE),
        incremental_note="stats/reports/页面五切片：聚合层每轮从事实表全量重扫重算，"
                         "近似/只更新受影响分组一律禁止（分位数与窗口排序对不上就是错）。",
        inputs=[staging_main],
        writes=[WriteTarget(P.local_v1 / "work" / "validation.json", note="stage3 后由 stage4 重导"),
                WriteTarget(P.local_v1 / "reports" / "数据来源与版本清单.json"),
                WriteTarget(run_work / "v5_lowpoint_slice.sqlite", new_only=True)],
        note="V5 预注册口径冻结，切片重导只在源确实推进后做；export_v5_slice 需 duckdb（TODO），"
             "落到 dev_root/data/ 的正式切片由 v5_lowpoint_ui_isolated/tools/merge_into_8737.py "
             "自行 --apply（本流水线不代替它覆盖 1.12GB 切片）。"))

    nodes.append(Node(
        13, "promote_and_finalize",
        "七闸终检（含 G-F 历史不变性 / G-G 历史漂移）→ 写前副本 → 原子替换 → "
        "扩展 --accept → 状态/指纹/记录",
        cmds=[[BASH, str(P.extensions_sh), "--accept"]],
        env={"LOCAL_DB_OUT": str(P.main_db)}, cwd=P.local_v1 / "extensions",
        layer=LAYER_DELIVER, mode_policy=POLICY_DELIVER, skip_allowed=False,
        inputs=[staging_main, staging_etf],
        writes=[WriteTarget(P.main_db, promotion=True, note="仅七闸全 PASS 才 os.replace"),
                WriteTarget(P.etf_db, promotion=True, note="仅七闸全 PASS 才 os.replace"),
                WriteTarget(P.work / "db_fingerprints.json"),
                WriteTarget(P.work / "update_state.json"),
                WriteTarget(P.layer_report_path(ctx.run_id),
                            note="三层对账报告（G-F/G-G 结论 + 每节点 sha/rows/耗时）"),
                WriteTarget(P.drift_baseline,
                            note="G-G 漂移基线：只在七闸全过、本轮 SUCCESS 时原子更新"),
                WriteTarget(P.baseline_file, note="成功后刷新发布态 baseline"),
                WriteTarget(P.update_log_md, note="追加一行")],
        internal="promote_and_finalize",
        note="内置动作；--accept 只在七闸全过时才附加。市值库的 G13_SOURCE_FRESHNESS 不放宽，"
             "G-G 检出历史漂移时这里就是终点：拒绝替换、拒绝增量，等人工裁定全量对齐。"))
    return nodes


# ---------------------------------------------------------------- 三层对账报告 / --compare-run

def build_layer_report(P: Paths, ctx: Context, state: dict, gates: list[GateResult]) -> dict:
    """把三层节点表 + G-F/G-G 结论压成一份可跨轮对账的紧凑报告（不含大表数据）。"""
    gf = next((g for g in gates if g.gate == "G_F"), None)
    gg = next((g for g in gates if g.gate == "G_G"), None)
    per_table = (gf.detail.get("per_table") if gf else []) or []
    digest_src = json.dumps(
        [{"role": t.get("role"), "table": t.get("table"),
          "overlap_digest": t.get("overlap_digest"),
          "old_rows": t.get("old_rows"),
          "new_rows": t.get("new_rows") or t.get("new_rows_total") or t.get("new_rows_overlap")}
         for t in per_table], ensure_ascii=False, sort_keys=True)
    months = sorted(set(ctx.new_months))
    return {
        "schema": LAYER_REPORT_SCHEMA,
        "generated_at_bjt": iso_bjt(now_bjt()),
        "run_id": ctx.run_id,
        "mode": ctx.mode,
        "trigger": ctx.trigger,
        "status": state.get("status"),
        "new_months": list(months),
        "target_month": ctx.target.get("month_id"),
        "as_of": ctx.as_of,
        "layers": {str(n["node"]): n.get("layer") for n in state.get("nodes", [])},
        "nodes": [{"node": n.get("node"), "key": n.get("key"), "status": n.get("status"),
                   "layer": n.get("layer"), "mode_policy": n.get("mode_policy"),
                   "started_bjt": n.get("started_bjt"), "finished_bjt": n.get("finished_bjt"),
                   "duration_s": n.get("duration_s"), "rows": n.get("rows"),
                   "input_sha256_12": n.get("input_sha256_12"),
                   "output_path": n.get("output_path")}
                  for n in state.get("nodes", [])],
        "gate_f": {"verdict": gf.verdict if gf else None, "code": gf.code if gf else None,
                   "message": gf.message if gf else None,
                   "checked_tables": gf.detail.get("checked_tables") if gf else None,
                   "unverified_tables": gf.detail.get("unverified_tables") if gf else None,
                   "hard_differences": gf.detail.get("hard_differences") if gf else None},
        "gate_g": {"verdict": gg.verdict if gg else None, "code": gg.code if gg else None,
                   "message": gg.message if gg else None,
                   "checked_groups": gg.detail.get("checked_groups") if gg else None,
                   "drifted_count": gg.detail.get("drifted_count") if gg else None,
                   "vanished_count": gg.detail.get("vanished_count") if gg else None,
                   "source": gg.detail.get("source") if gg else None},
        "layer_discipline": state.get("layer_discipline"),
        "incremental": {"seed": state.get("incremental_seed", []),
                        "append": state.get("incremental_append", {})},
        "history_digest": hashlib.sha256(digest_src.encode("utf-8")).hexdigest(),
        "per_table": [{k: v for k, v in t.items() if k != "differences"} for t in per_table],
    }


def load_layer_report(P: Paths, run_id: str) -> dict | None:
    path = P.layer_report_path(run_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:                                       # noqa: BLE001
        return None


def compare_layer_reports(a: dict, b: dict) -> dict:
    """同区间等值结论：两轮都覆盖过的历史区间必须给出同一份逐字节校验和。"""
    ta = {str(t.get("role")) + "." + str(t.get("table")): t for t in a.get("per_table", [])}
    tb = {str(t.get("role")) + "." + str(t.get("table")): t for t in b.get("per_table", [])}
    rows = []
    for key in sorted(set(ta) & set(tb)):
        x, y = ta[key], tb[key]
        da, db_ = x.get("overlap_digest"), y.get("overlap_digest")
        equal = bool(da) and da == db_
        rows.append({"table": key, "verdict": "EQUAL" if equal else "DIFFERENT",
                     "a_rows": x.get("old_rows"), "b_rows": y.get("old_rows"),
                     "a_overlap": str(da)[:16], "b_overlap": str(db_)[:16],
                     "a_interval": [x.get("overlap_month_min"), x.get("overlap_month_max")],
                     "b_interval": [y.get("overlap_month_min"), y.get("overlap_month_max")]})
    overlap = sorted(set(a.get("new_months", [])) & set(b.get("new_months", [])))
    only_a = sorted(set(ta) - set(tb))
    only_b = sorted(set(tb) - set(ta))
    verdict = "EQUAL" if rows and all(r["verdict"] == "EQUAL" for r in rows) else \
        ("NO_OVERLAP" if not rows else "DIFFERENT")
    return {"a_run_id": a.get("run_id"), "b_run_id": b.get("run_id"),
            "a_mode": a.get("mode"), "b_mode": b.get("mode"),
            "compared_tables": len(rows), "overlap_new_months": overlap,
            "tables_only_in_a": only_a, "tables_only_in_b": only_b,
            "per_table": rows, "verdict": verdict,
            "compared_at_bjt": iso_bjt(now_bjt())}


def run_compare(P: Paths, ctx: Context, log: Tee) -> int:
    """--compare-run <run_id>[,<另一个>]：只读对账，不跑任何节点、不写任何文件。"""
    wanted = [x.strip() for x in str(ctx.compare_run).split(",") if x.strip()]
    for w in wanted:
        if not RUN_ID_RE.match(w):
            print(f"BAD_RUN_ID {w}：--compare-run 只接受 MU-YYYYMMDDTHHMMSS（不许冒号）",
                  file=sys.stderr)
            return 2
    known = sorted(d.name for d in P.runs_dir.glob("MU-*")
                   if P.layer_report_path(d.name).exists()) if P.runs_dir.exists() else []
    if len(wanted) == 1:
        target = wanted[0]
        others = [k for k in known if k != target]
        if not others:
            print(f"{E_COMPARE_RUN}：找不到与 {target} 对账的另一轮（work/runs/*/layer_report.json "
                  f"只有 {known}）", file=sys.stderr)
            return 2
        pair = [target, others[-1]]
    elif len(wanted) >= 2:
        pair = wanted[:2]
    else:
        pair = known[-2:] if len(known) >= 2 else []
    if len(pair) < 2:
        print(f"{E_COMPARE_RUN}：可用对账轮次={known}", file=sys.stderr)
        return 2
    a = load_layer_report(P, pair[0])
    b = load_layer_report(P, pair[1])
    for rid, rep_ in zip(pair, (a, b)):
        if rep_ is None:
            print(f"{E_COMPARE_RUN}：{P.layer_report_path(rid)} 不存在（该轮没跑到收尾？）",
                  file=sys.stderr)
            return 2
    out = compare_layer_reports(a, b)
    log(f"COMPARE {pair[0]}({out['a_mode']}) vs {pair[1]}({out['b_mode']}) "
        f"verdict={out['verdict']} tables={out['compared_tables']}")
    for r in out["per_table"]:
        log(f"  {r['table']:<52} {r['verdict']:<10} a_rows={r['a_rows']} "
            f"b_rows={r['b_rows']} a_overlap={r['a_overlap']} b_overlap={r['b_overlap']} "
            f"区间A={r['a_interval']} 区间B={r['b_interval']}")
    log(f"  新增流月交集={out['overlap_new_months']} 仅A={out['tables_only_in_a']} "
        f"仅B={out['tables_only_in_b']}")
    log(f"COMPARE 结论：{'同区间逐字节等值（两种模式互验通过）' if out['verdict'] == 'EQUAL' else out['verdict']}"
        f" —— full 与 incremental 的对账只在 G-F 通过的轮次之间有意义")
    # 对账是只读动作：结果只打印。--execute 也不在这里落盘（compare 分支在 guard_roots
    # 之前就返回了，绝不能成为绕过人工哨兵写现网树的口子）。
    log(f"COMPARE_RESULT {json.dumps(out, ensure_ascii=False)}")
    return 0 if out["verdict"] == "EQUAL" else 1


# ---------------------------------------------------------------- 影子跑批本轮目录

def materialize_shadow_run_dir(P: Paths, ctx: Context, *, enabled: bool, log: Tee) -> Path | None:
    """在新 run 目录里生成指向本轮的 run_full_shadow.sh（不改 20261018 封印件）。"""
    src = P.shadow_old / "scripts" / "run_full_shadow.sh"
    dst_dir = P.project_root / f"v13_shadow_fullmarket_{ctx.runday}"
    dst = dst_dir / "scripts" / "run_full_shadow.sh"
    if not src.exists():
        log(f"SHADOW_LAUNCHER_MISSING {src}")
        return None
    text = src.read_text(encoding="utf-8")
    old_task = str(P.shadow_old)
    run_work = P.work / "runs" / ctx.run_id
    replaced = text.replace(f'TASK="{old_task}"', f'TASK="{dst_dir}"')
    replaced = replaced.replace(
        f'INPUT="{P.release_candidate.parent / "work" / "input" / "v13_staging_input_all_rev3.csv"}"',
        f'INPUT="{run_work / "v13_staging_input_all.csv"}"')
    if replaced == text:
        log("SHADOW_LAUNCHER_REWRITE_FAILED：未匹配到 TASK/INPUT 行，放弃生成")
        return None
    if not enabled:
        log(f"WOULD WRITE (dry-run, 0 字节) {dst}")
        return dst
    if dst.exists():
        log(f"SEALED_TARGET_CONFLICT 已存在，不覆盖: {dst}")
        return None
    dst.parent.mkdir(parents=True, exist_ok=True)
    (dst_dir / "work").mkdir(parents=True, exist_ok=True)
    atomic_write_text(dst, replaced, enabled=True, log=log)
    os.chmod(dst, 0o755)
    return dst


# ---------------------------------------------------------------- 原子替换

def promote_atomic(staging: Path, final: Path, *, enabled: bool, log: Tee) -> dict:
    """同卷 os.replace；失败/未放行时丢弃 staging，正式文件一字节不动。"""
    rec = {"staging": str(staging), "final": str(final), "promoted": False,
           "dry_run": not enabled}
    if not staging.exists():
        rec["skipped"] = "staging_absent"
        log(f"PROMOTE SKIP {staging} 不存在")
        return rec
    if not enabled:
        log(f"WOULD PROMOTE (dry-run) os.replace({staging} -> {final})")
        return rec
    os.replace(staging, final)
    rec["promoted"] = True
    rec["promoted_at_bjt"] = iso_bjt(now_bjt())
    rec["size_bytes"] = final.stat().st_size
    log(f"PROMOTED os.replace -> {final}")
    return rec


def discard_staging(staging: Path, *, enabled: bool, log: Tee) -> None:
    if not staging.exists():
        return
    if not enabled:
        log(f"WOULD DISCARD (dry-run) {staging}")
        return
    staging.unlink()
    log(f"DISCARDED {staging}")


# ---------------------------------------------------------------- 节点执行

def run_cmd(cmd: list[str], *, env: dict | None, cwd: Path | None, log: Tee) -> tuple[int, float]:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    disp = " ".join(shlex_quote(c) for c in cmd) + (f"  [cwd={cwd}]" if cwd else "") + \
           (f"  [env={env}]" if env else "")
    log(f"$ {disp}")
    started = now_bjt()
    t0 = time.monotonic()
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=full_env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
    dur = round(time.monotonic() - t0, 1)
    for line in (proc.stdout or "").splitlines():
        log(f"    | {line}")
    log(f"    exit={proc.returncode} duration_s={dur} started_bjt={iso_bjt(started)}")
    return proc.returncode, dur


def shlex_quote(s: str) -> str:
    import shlex
    return s if re.fullmatch(r"[\w@./:+,=\-]+", s) else shlex.quote(s)


def count_rows(spec, log: Tee) -> int | None:
    if not spec:
        return None
    db, sql = spec
    if not Path(db).exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return int(con.execute(sql).fetchone()[0])
        finally:
            con.close()
    except Exception as exc:                               # noqa: BLE001
        log(f"ROWCOUNT WARN {db}: {exc}")
        return None


# ---------------------------------------------------------------- 状态文件

def sync_scheduler_compat(state: dict) -> dict:
    """与 T1 调度器的字段别名对齐（它是这份文件的前任写者，读侧可能仍用它的键名）。

    T1 初态键：status/stage/run_id/trigger/started_at_bjt/stages[]/pid/error{code,message}。
    我只增不删：nodes 同时镜像到 stages，error_code 同时镜像成 error。
    """
    state["stages"] = state.get("nodes", [])
    state.setdefault("update_mode", state.get("update_mode"))
    cur = [n for n in state.get("nodes", []) if n.get("status") not in (None, "PLANNED",
                                                                        "SKIPPED_NOT_SELECTED")]
    state["stage"] = (cur[-1]["key"] if cur else state.get("status", "").lower())
    state.setdefault("started_at_bjt", state.get("created_at_bjt"))
    code = state.get("error_code")
    state["error"] = None if not code else {"code": code, "message": state.get("error_note", "")}
    state.setdefault("pid", os.getpid())
    return state


def new_state(P: Paths, ctx: Context, old: dict | None) -> dict:
    st = {
        "schema": STATE_SCHEMA,
        "run_id": ctx.run_id,
        "trigger": ctx.trigger,
        "mode": "dry-run" if ctx.dry_run else "execute",
        "update_mode": ctx.mode,
        "new_months": list(ctx.new_months),
        "layer_discipline": {"mode": ctx.mode, "violations": [],
                             "layers": {str(n.index): list(layers_of(n)) for n in
                                        plan_nodes(P, ctx)}},
        "incremental": {"seed": [], "append": {}},
        "accept_gates": ctx.accept_gates,
        "only_nodes": ctx.only,
        "force_source_check": ctx.force_source_check,
        "as_of": ctx.as_of,
        "as_of_basis": ctx.as_of_basis,
        "source_max_trade_date_detected": ctx.publication.get("max_trade_date"),
        "target_month": ctx.target,
        "created_at_bjt": iso_bjt(now_bjt()),
        "updated_at_bjt": iso_bjt(now_bjt()),
        "status": "PLANNING",
        "error_code": None,
        "gates": {},
        "promotion": {"main_db": None, "etf_db": None, "accepted": False},
        "planned_writes": [],
        "nodes": [],
    }
    if old:
        # 接管写者：保留调度器写入的非我属字段
        foreign = {k: v for k, v in old.items() if k not in st}
        if foreign:
            st["scheduler_initial"] = foreign
        st["taken_over_from"] = {"run_id": old.get("run_id"),
                                 "status": old.get("status"),
                                 "updated_at_bjt": old.get("updated_at_bjt"),
                                 "schema": old.get("schema")}
    return st


def write_state(P: Paths, state: dict, *, enabled: bool, log: Tee) -> None:
    sync_scheduler_compat(state)
    atomic_write_json(P.state_file, state, enabled=enabled, log=log)


def read_existing_state(P: Paths) -> dict | None:
    try:
        if P.state_file.exists():
            doc = json.loads(P.state_file.read_text(encoding="utf-8"))
            return doc if isinstance(doc, dict) else None
    except Exception:                              # noqa: BLE001 —— 读不动就当作没有
        return None
    return None


def write_orchestrator_terminal(P: Paths, state: dict | None, *, status: str, rc: int,
                                code: str | None, message: str, run_id: str | None = None,
                                trigger: str | None = None, dry_run: bool = True,
                                log: Tee | None = None) -> dict:
    """**每一轮的结束都必须落终态**（P1-2）：`status` + `finished_at_bjt` + `error{code,message}`。

    覆盖编排器自己的全部非零退出路径（参数被拒、根守卫拒绝、跑中途炸串）；
    rc=0 的成功计划轮走 `run_dry_plan` 里的 `DRY_RUN_PLAN`，同样带结束时刻。
    写的是白名单第 1 件 `work/update_state.json`，别的什么也不碰。
    """
    doc = dict(state) if isinstance(state, dict) else (read_existing_state(P) or {})
    doc.setdefault("schema", STATE_SCHEMA)
    doc["run_id"] = run_id or doc.get("run_id")
    doc["trigger"] = trigger or doc.get("trigger")
    doc["mode"] = doc.get("mode") or ("dry-run" if dry_run else "execute")
    doc["status"] = status
    doc["exit_rc"] = rc
    doc["error_code"] = code
    doc["error_note"] = (str(message or ""))[:400]
    doc["error"] = (None if not code
                    else {"code": str(code), "message": (str(message or ""))[:400]})
    doc.setdefault("started_at_bjt", doc.get("created_at_bjt") or iso_bjt(now_bjt()))
    doc["stages"] = doc.get("stages") or doc.get("nodes") or []
    doc["updated_at_bjt"] = iso_bjt(now_bjt())
    doc["finished_at_bjt"] = iso_bjt(now_bjt())
    doc["terminal_writer"] = "run_monthly_update"
    write_state(P, doc, enabled=True, log=log)
    return doc


def node_record(node: Node, status: str, *, started=None, finished=None, cmd: str = "",
                inputs_sha: dict | None = None, outputs: list[str] | None = None,
                rows=None, duration=None, note: str = "") -> dict:
    return {
        "node": node.index, "key": node.key, "title": node.title, "status": status,
        "layer": list(layers_of(node)), "mode_policy": list(policies_of(node)),
        "skip_allowed": node.skip_allowed, "incremental_note": node.incremental_note,
        "started_bjt": iso_bjt(started) if started else None,
        "finished_bjt": iso_bjt(finished) if finished else None,
        "cmd": cmd, "input_sha256_12": inputs_sha or {}, "output_path": outputs or [],
        "rows": rows, "duration_s": duration, "note": note,
    }


def upsert_node(state: dict, rec: dict) -> None:
    nodes = state["nodes"]
    for i, existing in enumerate(nodes):
        if existing["node"] == rec["node"]:
            nodes[i] = rec
            return
    nodes.append(rec)


# ---------------------------------------------------------------- baseline

def write_baseline(P: Paths, stats: dict, ctx: Context, *, enabled: bool, log: Tee) -> dict:
    doc = {
        "schema": BASELINE_SCHEMA,
        "recorded_at_bjt": iso_bjt(now_bjt()),
        "run_id": ctx.run_id,
        "max_trade_date": stats.get("max_trade_date"),
        "max_trade_date_source": stats.get("max_trade_date_source"),
        "source_version": stats.get("source_version"),
        "publication_files": stats.get("publication_files", {}),
        "file_count": stats.get("file_count"),
    }
    atomic_write_json(P.baseline_file, doc, enabled=enabled, log=log)
    return doc


# ---------------------------------------------------------------- CLI

def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="月度数据更新流水线编排器（默认 --dry-run，一个字节都不写）")
    ap.add_argument("--run-id", help="MU-YYYYMMDDTHHMMSS（不许有冒号）；缺省按当前北京时间生成")
    ap.add_argument("--trigger", choices=TRIGGERS, default="manual")
    ap.add_argument("--as-of", help="YYYY-MM-DD；缺省由侦测到的源末交易日决定（不硬编码日期）")
    ap.add_argument("--dry-run", action="store_true",
                    help="默认行为：不执行任何子进程；除 dry-run 白名单"
                         "（work/update_state.json + logs/update_<run_id>.log）外不写任何文件")
    ap.add_argument("--execute", action="store_true", help="真跑（仍不传 --accept，除非 --accept-gates）")
    ap.add_argument("--accept-gates", action="store_true",
                    help="五闸全 PASS 才允许原子替换正式库并向扩展库传 --accept")
    ap.add_argument("--only-node", help="逗号分隔节点号，便于分步联调，如 8 或 4,6,7")
    ap.add_argument("--force-source-check", action="store_true",
                    help="baseline 缺失时人工建档放行一次 / 强制刷新源态")
    ap.add_argument("--root", help="假树/单根模式：一次把 project/docs/dev 三个根都指过去"
                                   "（缺省无根，不给就报 NO_ROOT_CONFIGURED）")
    ap.add_argument("--project-root", help="显式注入项目根（无缺省值）")
    ap.add_argument("--docs-root", help="显式注入 docs 车间根（无缺省值，不再有 ~/Documents 回落）")
    ap.add_argument("--dev-root", help="显式注入 app 开发根（无缺省值）")
    ap.add_argument("--mode", choices=MODES, default=MODE_INCREMENTAL,
                    help="incremental（缺省：事实层只追加新流月 + 聚合层全量重算 + 两道历史门禁）"
                         " / full（整库重建，留作季度互验）")
    ap.add_argument("--force-drift-baseline", action="store_true",
                    help="人工裁定：G-G 漂移基线缺失时建档放行一次（与 --force-source-check 同性质）")
    ap.add_argument("--compare-run", metavar="RUN_ID[,RUN_ID]",
                    help="只读对账两轮（incremental vs full）的同区间等值结论，不跑任何节点")
    ap.add_argument("--print-plan", action="store_true", help="只打印 13 节点计划表后退出")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    execute = bool(args.execute) and not args.dry_run
    dry_run = not execute

    # ---- 缺省无根（第一优先项）：拿不到显式注入的三根就拒绝启动，绝不回落真实路径
    try:
        P = resolve_paths(project_root=args.project_root, docs_root=args.docs_root,
                          dev_root=args.dev_root, root=args.root)
    except NoRootConfigured as exc:
        # 三根都不知道 ⇒ `work/` 在哪都不确定，编排器**物理上无法**落终态。
        # 这一类硬失败（rc=4）的终态由调度器 `_synth_terminal_state` 按退出码合成
        # （P1-2 兜底半边），原因取自本行 stderr：`NO_ROOT_CONFIGURED` 从此在页面上可见。
        print(str(exc), file=sys.stderr)
        return 4

    def _reject(rc: int, code: str, message: str) -> int:
        """参数级拒绝：仍然给这一轮落一个终态（EXITED_FAILED + finished_at_bjt + error）。

        P1-2 之前这类退出只在 stderr 留一句话，页面永远停在「RUNNING · 阶段 init」。
        """
        print(f"{code} {message}", file=sys.stderr)
        write_orchestrator_terminal(P, None, status="EXITED_FAILED", rc=rc,
                                    code=code, message=message, run_id=run_id,
                                    trigger=args.trigger, dry_run=dry_run)
        return rc

    now = now_bjt()
    run_id = args.run_id or f"MU-{compact_bjt(now)}"
    if not RUN_ID_RE.match(run_id):
        # run_id 不合法 ⇒ 本轮**不享有**日志白名单（文件名不可控），只允许写状态件这一件
        set_dry_run_guard(P, None, active=dry_run)
        return _reject(2, "BAD_RUN_ID",
                       f"{run_id}：必须形如 MU-YYYYMMDDTHHMMSS（不许冒号）")
    runday = run_id[3:11]
    mode = args.mode or MODE_INCREMENTAL
    if args.as_of and not DATE_RE.match(args.as_of):
        set_dry_run_guard(P, run_id, active=dry_run)
        return _reject(2, "BAD_AS_OF", f"{args.as_of}：必须 YYYY-MM-DD")
    only = [int(x) for x in re.findall(r"\d+", args.only_node)] if args.only_node else []
    # dry-run 从这里开始受白名单保护：只有 work/update_state.json 与
    # logs/update_<run_id>.log 允许落盘，其余 enabled=True 的写入一律抛
    # DryRunWriteDenied（P1-2 白名单，`--print-plan` 会把这份名单打印出来）。
    set_dry_run_guard(P, run_id, active=dry_run)

    if args.compare_run:
        # 只读对账：不建日志文件、不跑节点、不写状态（--execute 才落一份对账结果）
        cmp_log = Tee(None, enabled=False)
        cmp_ctx = Context(run_id=run_id, runday=runday, trigger=args.trigger,
                          as_of=args.as_of, as_of_basis="compare", dry_run=True,
                          execute=execute, accept_gates=False, only=[],
                          force_source_check=False, mode=mode, compare_run=args.compare_run,
                          force_drift_baseline=bool(args.force_drift_baseline))
        rc = run_compare(P, cmp_ctx, cmp_log)
        cmp_log.close()
        return rc

    # 终态白名单（fixround3 / P1-2）：dry-run 也把自己的日志与状态文件落盘，其余目标仍 0 字节。
    log = Tee(P.logs / f"update_{run_id}.log", enabled=True)
    allowed_roots, why = guard_roots(P, execute, log)
    if not allowed_roots:
        # 根守卫拒绝（真跑没哨兵 / 改根没授权）同样是**一轮的结束**：落 EXITED_FAILED 终态，
        # 页面才知道这一轮结束了、为什么没跑（P1-2 之前这里只往 stderr 写一句话就退出）。
        print(why, file=sys.stderr)
        code = str(why).split("：")[0] or E_EXECUTE_NOT_CONFIRMED
        write_orchestrator_terminal(P, read_existing_state(P), status="EXITED_FAILED", rc=3,
                                    code=code, message=why, run_id=run_id,
                                    trigger=args.trigger, dry_run=dry_run)
        log.close()
        return 3
    log.open()
    log(f"run_id={run_id} trigger={args.trigger} mode={'dry-run' if dry_run else 'execute'} "
        f"update_mode={mode} accept_gates={args.accept_gates} only={only or 'ALL'}")
    log(f"project_root={P.project_root}")
    log(f"docs_root={P.docs_root}")
    log(f"dev_root={P.dev_root}")
    if dry_run:
        # 白名单在**每一条** dry-run 路径上都打印（含 --print-plan），见 run_dry_plan 与下面
        for line in whitelist_banner(P, run_id):
            log(line)

    old_state = None
    if P.state_file.exists():
        try:
            old_state = json.loads(P.state_file.read_text(encoding="utf-8"))
            log(f"TAKEOVER update_state.json 已有初态（run_id={old_state.get('run_id')}，"
                f"status={old_state.get('status')}）：本编排器接管续写")
        except Exception as exc:                           # noqa: BLE001
            log(f"TAKEOVER WARN 既有 update_state.json 解析失败：{exc}")

    ctx = Context(run_id=run_id, runday=runday, trigger=args.trigger, as_of=args.as_of,
                  as_of_basis="", dry_run=dry_run, execute=execute,
                  accept_gates=args.accept_gates, only=only,
                  force_source_check=args.force_source_check, mode=mode,
                  force_drift_baseline=bool(args.force_drift_baseline)
                  or os.environ.get("MONTHLY_UPDATE_FORCE_DRIFT_BASELINE", "") == "1")

    # ---- 源态侦测 + 目标流月（全部只读）
    stats = read_publication_stats(P)
    ctx.publication = stats
    if args.as_of:
        ctx.as_of_basis = "cli --as-of"
    else:
        ctx.as_of = stats.get("max_trade_date")
        ctx.as_of_basis = "detected_source_max_trade_date"
    ctx.target = resolve_target_month(P, args.as_of, now, resolve_calendar(P))
    log(f"publication: max_trade_date={stats.get('max_trade_date')} "
        f"files={stats.get('file_count')} available={stats.get('available')}")
    log(f"target_month: {json.dumps(ctx.target, ensure_ascii=False)}")
    if args.print_plan:
        # P1-2：白名单要在 `--print-plan` 里看得见 —— dry-run 分支已经把
        # `DRY_RUN_WRITE_WHITELIST` 四条原文打进本轮日志（stdout 同步可见），这里再点明一次。
        if dry_run:
            log("PRINT-PLAN 说明：以下是 13 节点计划表；本命令不执行任何子进程，"
                "落盘范围只限上面 DRY_RUN_WRITE_WHITELIST 列出的两件自有状态件，其余 0 字节。")
        else:
            log("PRINT-PLAN 说明：以下是 13 节点计划表；本命令不执行任何子进程"
                "（--execute 已授权，故不适用 dry-run 白名单口径）。")
        for n in plan_nodes(P, ctx):
            print_plan_node(n, log)
        log.close()
        return 0

    baseline = None
    if P.baseline_file.exists():
        baseline = json.loads(P.baseline_file.read_text(encoding="utf-8"))

    ctx.batch_dir = None
    ctx.relation_build_db = P.relation_v1 / "out" / run_id / \
        "history_stock_relations_build.sqlite3"
    nodes = plan_nodes(P, ctx)

    state = new_state(P, ctx, old_state)
    state["nodes"] = [node_record(n, "PLANNED", cmd=render_cmds(n),
                                 inputs_sha=inputs_sha(n, log),
                                 outputs=[str(w.path) for w in n.writes], note=n.note)
                      for n in nodes]

    try:
        if dry_run:
            return run_dry_plan(P, ctx, nodes, state, log, stats, baseline)
        return run_execute(P, ctx, nodes, state, log, stats, baseline)
    except Exception as exc:                            # noqa: BLE001
        # 非零退出的编排器侧终态：先落 EXITED_FAILED（含 rc 与异常摘要），再把异常原样抛出去，
        # 让调度器那侧的 rc 合成兜底继续可观测。绝不吞异常、绝不假装这一轮结束了却什么都没写。
        try:
            write_orchestrator_terminal(
                P, state, status="EXITED_FAILED", rc=1,
                code=f"ORCHESTRATOR_CRASHED_{type(exc).__name__}",
                message=f"{type(exc).__name__}: {exc}", run_id=run_id,
                trigger=args.trigger, dry_run=dry_run, log=log)
            log.close()
        except Exception:                               # noqa: BLE001 —— 落终态失败也别掩盖原异常
            pass
        raise


def render_cmds(node: Node) -> str:
    return " ;; ".join(" ".join(shlex_quote(c) for c in cmd) for cmd in node.cmds)


def inputs_sha(node: Node, log: Tee) -> dict:
    out: dict[str, dict] = {}
    for p in node.inputs:
        if not Path(p).exists():
            out[str(p)] = {"sha12": None, "mode": "missing"}
            continue
        out[str(p)] = digest_of(Path(p))
    return out


def print_plan_node(node: Node, log: Tee) -> None:
    log(f"NODE {node.index:>2} [{node.key}] {node.title}")
    log(f"     layer={' + '.join(layers_of(node))}  policy={','.join(policies_of(node))}"
        f"  skip_allowed={node.skip_allowed}")
    if node.internal:
        log(f"     internal: {node.internal}")
    for i, cmd in enumerate(node.cmds):
        env = f"  env={node.env}" if node.env else ""
        cwd = f"  cwd={node.cwd}" if node.cwd else ""
        sub = ""
        if node.cmd_layers and i < len(node.cmd_layers):
            sub = f"  [{node.cmd_layers[i]}|{node.cmd_policies[i]}]"
        log(f"     $ {' '.join(shlex_quote(c) for c in cmd)}{cwd}{env}{sub}")
    for w in node.writes:
        flag = "new_only" if w.new_only else ("promotion" if w.promotion else "in-place")
        log(f"     -> {w.path}  [{flag}] {w.note}")
    if node.note:
        log(f"     note: {node.note}")


# ---------------------------------------------------------------- dry-run

def run_dry_plan(P: Paths, ctx: Context, nodes: list[Node], state: dict, log: Tee,
                 stats: dict, baseline: dict | None) -> int:
    log("=== DRY-RUN：除自有状态件（work/update_state.json + logs/update_<run_id>.log）外，"
        "以下只打印，不写任何文件（含指纹/更新记录/baseline/任何库）===")
    for n in nodes:
        print_plan_node(n, log)
    gates = evaluate_gates(P, ctx, nodes, stats, baseline, stage="plan", log=log)
    for g in gates:
        log(f"GATE {g.gate} {g.verdict} {g.code} {g.message}")
    planned, conflicts = audit_write_targets(
        nodes, sealed=sealed_path_set(P), protected=protected_dirs(P),
        allow_inplace=derived_inplace_allowlist(P), run_id=ctx.run_id, runday=ctx.runday, P=P)
    state["gates"] = {g.gate: g.to_dict() for g in gates}
    state["planned_writes"] = planned
    state["write_conflicts"] = conflicts
    state["layer_discipline"]["violations"] = layer_discipline_violations(ctx, nodes, state)
    state["layer_report"] = build_layer_report(P, ctx, state, gates)
    # ---- 终态（fixround3 P1-2）：dry-run 也是**一轮的结束**，必须落 finished_at_bjt +
    # 状态 + 原因。rc 由 main() 决定（dry-run 走到这里恒 0）；闸门没过不是「失败轮」，
    # 但原因必须写进 error，页面上才看得见「为什么这一轮不能跑」。
    state["status"] = "DRY_RUN_PLAN"
    state["exit_rc"] = 0
    failing = [g for g in gates if g.verdict != "PASS"]
    if failing:
        state["error_code"] = "DRY_RUN_GATES_NOT_ALL_PASS"
        state["error_note"] = (
            f"闸门 {sum(1 for g in gates if g.verdict == 'PASS')}/{len(gates)} PASS，未过："
            + "、".join(f"{g.gate}({g.verdict}{':' + g.code if g.code else ''})" for g in failing))[:400]
    else:
        state["error_code"] = None
        state["error_note"] = ""
    state["updated_at_bjt"] = iso_bjt(now_bjt())
    state["finished_at_bjt"] = iso_bjt(now_bjt())
    write_state(P, state, enabled=True, log=log)
    log(f"DRY_RUN 结论：{len(nodes)} 节点、{len(planned)} 个写入目标、"
        f"写入冲突 {len(conflicts)} 个、闸门 {sum(1 for g in gates if g.verdict == 'PASS')}/"
        f"{len(gates)} PASS")
    log("未执行任何子进程；除上面的自有状态件外未写入任何文件。"
        "真跑请在集成阶段由负责人发起 --execute。")
    log.close()
    return 0


# ---------------------------------------------------------------- 闸门评估

def evaluate_gates(P: Paths, ctx: Context, nodes: list[Node], stats: dict,
                   baseline: dict | None, *, stage: str, log: Tee,
                   ran: dict | None = None) -> list[GateResult]:
    ran = ran or {}

    def _write_baseline(s):
        write_baseline(P, s, ctx, enabled=(ctx.execute and stage == "finalize"), log=log)

    ga = gate_a(stats, baseline, ctx.target, force_source_check=ctx.force_source_check,
                write_baseline=_write_baseline)
    gb = gate_b(P, nodes, ctx.run_id, ctx.runday, self_created=ctx.absent_at_plan)
    shadow_man = None
    shadow_dir = P.project_root / f"v13_shadow_fullmarket_{ctx.runday}"
    if (shadow_dir / "v13_full_shadow_manifest.json").exists():
        shadow_man = json.loads((shadow_dir / "v13_full_shadow_manifest.json")
                               .read_text(encoding="utf-8"))
    verify_rep = None
    if ctx.batch_dir and (Path(ctx.batch_dir) / "reports" /
                          "independent_recomputation.json").exists():
        verify_rep = json.loads((Path(ctx.batch_dir) / "reports" /
                                 "independent_recomputation.json").read_text(encoding="utf-8"))
    gc = gate_c(shadow_man, verify_rep, node7_ran=bool(ran.get(7)), node11_ran=bool(ran.get(11)))
    plan = [(P.main_db, P.backups / f"pre_monthly_{ctx.runday}" / "databases" / P.main_db.name),
            (P.etf_db, P.backups / f"pre_monthly_{ctx.runday}" / "databases" / P.etf_db.name)]
    gd = gate_d(plan, volume=P.project_root, min_free=min_free_bytes(),
                do_copy=(stage == "finalize"))
    ge = gate_e(P)
    gf, gg = gate_layers(P, ctx, nodes, stage=stage, log=log, enabled=(stage == "finalize"))
    gates = [ga, gb, gc, gd, ge, gf, gg]
    for g in gates:
        log(f"GATE[{stage}] {g.gate} {g.verdict} {g.code or '-'} :: {g.message}")
    return gates



# ---------------------------------------------------------------- execute

def run_execute(P: Paths, ctx: Context, nodes: list[Node], state: dict, log: Tee,
                stats: dict, baseline: dict | None) -> int:
    if P.disable_flag.exists():
        state["status"] = "REFUSED"
        state["error_code"] = E_DISABLED
        state["error_note"] = "work/update.disabled 停手绳存在"
        state["exit_rc"] = 3
        state["finished_at_bjt"] = iso_bjt(now_bjt())     # P1-2：拒绝也是一轮的结束
        log(f"REFUSED {E_DISABLED}：{P.disable_flag} 存在（停手绳），不执行任何节点")
        write_state(P, state, enabled=True, log=log)
        append_update_log(P, ctx.run_id, ctx.trigger, "REFUSED", E_DISABLED,
                          "停手绳存在，流水线未执行", enabled=True, log=log)
        log.close()
        return 3
    if P.lock_file.exists():
        try:
            other = json.loads(P.lock_file.read_text(encoding="utf-8"))
        except Exception:                                  # noqa: BLE001
            other = {}
        if other.get("pid") != os.getpid():
            state["status"] = "REFUSED"
            state["error_code"] = "PIPELINE_ALREADY_RUNNING"
            state["error_note"] = f"pipeline.lock 已被 pid={other.get('pid')} 占用"
            state["exit_rc"] = 3
            state["finished_at_bjt"] = iso_bjt(now_bjt())  # P1-2：拒绝也是一轮的结束
            log(f"REFUSED：{P.lock_file} 已被 pid={other.get('pid')} 占用")
            write_state(P, state, enabled=True, log=log)
            log.close()
            return 3
    atomic_write_json(P.lock_file, {"pid": os.getpid(), "run_id": ctx.run_id,
                                   "taken_bjt": iso_bjt(now_bjt())}, enabled=True, log=log)
    staging_main = P.local_db_dir / f"staging_{ctx.run_id}.sqlite"
    staging_etf = P.etf_v1 / "database" / f"staging_{ctx.run_id}.sqlite"
    batches_before = list_batches(P)
    state["status"] = "RUNNING"
    pre_gates = evaluate_gates(P, ctx, nodes, stats, baseline, stage="pre", log=log)
    state["gates"] = {g.gate: g.to_dict() for g in pre_gates}
    _planned, conflicts = audit_write_targets(
        nodes, sealed=sealed_path_set(P), protected=protected_dirs(P),
        allow_inplace=derived_inplace_allowlist(P), run_id=ctx.run_id,
        runday=ctx.runday, P=P)
    state["write_conflicts"] = conflicts
    write_state(P, state, enabled=True, log=log)

    ran: dict[int, bool] = {}
    failure: tuple[str, str] | None = None
    by_index = {n.index: n for n in nodes}
    for idx in sorted(by_index):
        node = by_index[idx]
        if idx == 13:
            continue
        if idx == 5:
            g = state["gates"].get("G_B", {})
            if g.get("code") == E_SEALED_TARGET or any(
                    c.get("node") == 5 for c in state.get("write_conflicts", [])):
                upsert_node(state, node_record(node, "BLOCKED_SEALED_TARGET",
                                               cmd=render_cmds(node),
                                               outputs=[str(w.path) for w in node.writes],
                                               note="G_B 判定该节点的写入目标是封印件/固定路径，"
                                                    "已阻止执行；需负责人裁定（见报告）。"
                                                    "节点 6 退回只读现有封印 dist 库。"))
                ctx.relation_build_db = None
                nodes = plan_nodes(P, ctx)
                by_index = {n.index: n for n in nodes}
                write_state(P, state, enabled=True, log=log)
                continue
        if not node_runs(ctx, idx):
            upsert_node(state, node_record(node, "SKIPPED_NOT_SELECTED", cmd=render_cmds(node),
                                           note="--only-node 未选择该节点"))
            write_state(P, state, enabled=True, log=log)
            continue
        started = now_bjt()
        if node.internal in ("seed_then_build", "seed_then_build_etf"):
            roles = ("main",) if node.internal == "seed_then_build" else ("etf",)
            rec_seed = seed_staging_from_live(P, ctx, enabled=True, log=log, roles=roles)
            node_seed = {"node": node.index, **rec_seed}
            state.setdefault("incremental_seed", []).append(node_seed)
            rec_append = apply_incremental_facts(P, ctx, enabled=True, log=log) \
                if roles == ("main",) else {"mode": ctx.mode, "applied": [],
                                             "pending_script": []}
            state["incremental_append"] = rec_append
            write_state(P, state, enabled=True, log=log)
        if node.internal == "materialize_shadow_run_dir":
            dst = materialize_shadow_run_dir(P, ctx, enabled=True, log=log)
            if dst is None:
                failure = (E_SEALED_TARGET, "影子跑批本轮启动器未能生成")
                upsert_node(state, node_record(node, "FAILED", started=started, finished=now_bjt(),
                                               cmd=render_cmds(node), note=failure[1]))
                break
        rc_total = 0
        for cmd in node.cmds:
            rc, dur = run_cmd(cmd, env=node.env, cwd=node.cwd, log=log)
            ran[idx] = True
            rc_total = rc
            if rc != 0:
                failure = (E_NODE_FAILED, f"节点 {idx} 命令失败 rc={rc}: "
                                          f"{' '.join(shlex_quote(c) for c in cmd)}")
                break
        finished = now_bjt()
        rows = count_rows(node.rows_sql, log) if idx in (8, 10) else None
        outputs = [str(w.path) for w in node.writes]
        if idx == 1 and rc_total == 0:
            stats = read_publication_stats(P)          # 镜像已刷新：G_A 用最新源态
            ctx.publication = stats
            ctx.as_of = ctx.as_of or stats.get("max_trade_date")
            log(f"PUBLICATION 刷新 max_trade_date={stats.get('max_trade_date')} "
                f"files={stats.get('file_count')}")
            nodes = plan_nodes(P, ctx)
            by_index = {n.index: n for n in nodes}
        if idx == 4 and rc_total == 0:
            found = discover_new_batch(P, batches_before, log)
            if found is not None:
                ctx.batch_dir = found
                outputs = [str(found)]
                # 批次目录已知：新流月集合按「批次有、现网主库没有」重算一次
                ctx.new_months = derive_new_months(P, ctx, log)
                # 批次目录已知：重建后续节点（11/12/13）的命令与写入目标
                nodes = plan_nodes(P, ctx)
                by_index = {n.index: n for n in nodes}
                log(f"REPLAN 后续节点已回填批次目录 {found}")
        status = "FAILED" if rc_total else "OK"
        if status == "OK":
            # 本节点声明的写入目标已在 pre 阶段按「run_id 新目录 / 明示允许的派生件」审过，
            # 本轮由自己创建出来的路径在收尾复评时不算越界覆盖。
            ctx.absent_at_plan = tuple(ctx.absent_at_plan) + tuple(
                str(w.path) for w in node.writes)
            if ctx.batch_dir:
                ctx.absent_at_plan = tuple(ctx.absent_at_plan) + (str(ctx.batch_dir),)
        upsert_node(state, node_record(node, status, started=started, finished=finished,
                                       cmd=render_cmds(node),
                                       inputs_sha=inputs_sha(node, log), outputs=outputs,
                                       rows=rows, duration=None,
                                       note=node.note + ("" if not failure else f" | {failure[1]}")))
        state["updated_at_bjt"] = iso_bjt(finished)
        write_state(P, state, enabled=True, log=log)
        if failure:
            break

    viol = layer_discipline_violations(ctx, nodes, state)
    state["layer_discipline"]["violations"] = viol
    for v in viol:
        log(f"LAYER VIOLATION {json.dumps(v, ensure_ascii=False)}")
    if viol and not failure:
        failure = (E_AGG_SKIP, "聚合层/验收层被跳过或降格：" +
                   ",".join(f"节点{v['node']}({v['policy']})" for v in viol))
    write_state(P, state, enabled=True, log=log)

    node13 = by_index[13]
    started = now_bjt()
    final_gates = evaluate_gates(P, ctx, nodes, stats, baseline, stage="finalize", log=log, ran=ran)
    state["gates"] = {g.gate: g.to_dict() for g in final_gates}
    allowed, reason, blockers = decide_accept(ctx.accept_gates, True, final_gates)
    log(f"ACCEPT 判定: allowed={allowed} :: {reason} blockers={blockers}")
    if failure and not allowed:
        status_code = f"{E_NODE_FAILED}/{E_GATE_FAILED}"[:64]
        promote = {"main_db": None, "etf_db": None, "accepted": False,
                   "reason": f"节点失败：{failure[1]}"}
        for node in nodes:
            if node.index in (8, 10, 12):
                discard_staging(staging_main if node.index in (8, 12) else staging_etf,
                                enabled=True, log=log)
        upsert_node(state, node_record(node13, "SKIPPED_DUE_TO_FAILURE", started=started,
                                       finished=now_bjt(), cmd=render_cmds(node13),
                                       note=failure[1]))
        state.update(status="FAILED", error_code=failure[0], error_note=failure[1],
                     promotion=promote)
    elif not allowed:
        hist_code = next((g.code for g in final_gates
                          if g.gate in ("G_F", "G_G") and g.verdict == "FAIL"), "GATED")
        upsert_node(state, node_record(node13, "GATED_NO_ACCEPT", started=started,
                                       finished=now_bjt(), cmd=render_cmds(node13),
                                       note=f"{reason}（{hist_code}）"))
        state.update(status="STAGED_ONLY", error_code=hist_code, error_note=reason,
                     promotion={
            "main_db": None, "etf_db": None, "accepted": False, "reason": reason,
            "staging_main": str(staging_main), "staging_etf": str(staging_etf)})
    else:
        gd = next(g for g in final_gates if g.gate == "G_D")
        if gd.verdict != "PASS":
            upsert_node(state, node_record(node13, "BLOCKED", started=started,
                                           finished=now_bjt(), cmd=render_cmds(node13),
                                           note=gd.message))
            state.update(status="BLOCKED", error_code=gd.code, error_note=gd.message,
                         promotion={"accepted": False, "reason": gd.message})
        else:
            rec_main = promote_atomic(staging_main, P.main_db, enabled=True, log=log)
            rec_etf = promote_atomic(staging_etf, P.etf_db, enabled=True, log=log)
            rc, _ = run_cmd([BASH, str(P.extensions_sh), "--accept"],
                            env={"LOCAL_DB_OUT": str(P.main_db)},
                            cwd=P.local_v1 / "extensions", log=log)
            upsert_node(state, node_record(node13, "OK" if rc == 0 else "FAILED",
                                           started=started, finished=now_bjt(),
                                           cmd=render_cmds(node13) + " --accept",
                                           outputs=[str(P.main_db), str(P.etf_db),
                                                    str(P.fingerprints), str(P.state_file)],
                                           note=f"扩展 --accept rc={rc}"))
            state.update(status="SUCCESS" if rc == 0 else "PROMOTED_EXTENSION_FAILED",
                         error_code=None if rc == 0 else E_NODE_FAILED,
                         promotion={"main_db": rec_main, "etf_db": rec_etf,
                                    "accepted": True, "extensions_rc": rc})
    state["updated_at_bjt"] = iso_bjt(now_bjt())
    state["finished_at_bjt"] = iso_bjt(now_bjt())
    build_fingerprints(P, [
        {"key": "ashare_main", "path": P.main_db, "role": "A 股主库"},
        {"key": "etf", "path": P.etf_db, "role": "ETF 库"},
        {"key": "staging_main", "path": staging_main, "role": "本轮 staging（未替换则残留）"},
        {"key": "gender_dayun",
         "path": P.local_v1 / "extensions" / "gender_dayun_v1" / "database" /
         "gender_dayun_v1.sqlite", "role": "大运扩展库"},
        {"key": "market_cap_tier",
         "path": P.local_v1 / "extensions" / "market_cap_tier_v1" / "database" /
         "market_cap_tier_v1.sqlite", "role": "市值分档扩展库"},
    ], ctx.run_id, state["status"], enabled=True, log=log)
    state["layer_report"] = build_layer_report(P, ctx, state, final_gates)
    if state["status"] == "SUCCESS":
        write_baseline(P, stats, ctx, enabled=True, log=log)
        # G-G 基线只在门禁全过时更新（原子写）：full 模式的全量对齐也算重建参考点
        if getattr(ctx, "drift_sig", None):
            doc = write_drift_baseline(P, ctx.drift_sig, ctx, enabled=True, log=log)
            state["drift_baseline_updated_at_bjt"] = doc["recorded_at_bjt"]
            state["drift_baseline_groups"] = doc["group_count"]
        if ctx.batch_dir:
            atomic_write_json(P.work / "last_batch.json",
                              {"batch_dir": str(ctx.batch_dir),
                               "recorded_at_bjt": iso_bjt(now_bjt()),
                               "run_id": ctx.run_id}, enabled=True, log=log)
    atomic_write_json(P.layer_report_path(ctx.run_id), state["layer_report"],
                      enabled=True, log=log)
    append_update_log(P, ctx.run_id, ctx.trigger, state["status"], state["error_code"] or "",
                      state.get("promotion", {}).get("reason") or reason,
                      enabled=True, log=log)
    write_state(P, state, enabled=True, log=log)
    try:
        P.lock_file.unlink()
    except OSError:
        pass
    log(f"FINAL status={state['status']} error={state['error_code']}")
    log.close()
    return 0 if state["status"] == "SUCCESS" else 1


def list_batches(P: Paths) -> list[str]:
    if not P.batches.exists():
        return []
    return sorted(str(p) for p in P.batches.iterdir())


def discover_new_batch(P: Paths, before: list[str], log: Tee) -> Path | None:
    """扫描器自分配批次目录名；执行后取唯一新增目录回填（不猜名字、不覆盖旧批）。"""
    cur = [p for p in list_batches(P) if p not in before]
    if len(cur) == 1:
        log(f"BATCH_DIR {cur[0]}")
        return Path(cur[0])
    log(f"BATCH_DIR AMBIGUOUS 新增 {len(cur)} 个: {cur}")
    return None


if __name__ == "__main__":
    raise SystemExit(main())
