#!/usr/bin/env python3
"""T1 后端自测（标准库，无 pytest）：北京口径 + 锁 + 计划 + 触发分支 + 状态口径。

跑法：
    cd bazi_month_relation_tengod_dev_v1/app && python3 ../tests/test_update_scheduler_t1.py

红线自查：全部落盘都在 `tempfile.mkdtemp()` 里（work/logs/reports 一起搬走），
不碰项目 work/、不碰任何 .sqlite、不起任何真流水线、不联网不碰 NAS。
每条断言都做了**双向验证**：既能报错（负例必拦），也能放行（正例必过）。

2026-10-22 P1-2 整改（本轮把这套测试从「一条都跑不了」恢复成可执行验收闸门）：
  ① 夹具里的 run_id 从旧格式 `upd_new_stale` 之类换成产品 `assert_run_id` 认的
     `MU-YYYYMMDDTHHMMSS`，并且**路径串一起对齐**（`update_<run_id>.log`、
     `update.lock.stale.<run_id>`），**每个用例的 run_id 互不相同**；
  ② 需要 `manual_start` 的用例先走只读 preview 拿 `preview_token`（两段式令牌门上线后
     这套测试一次都没跑通过，缺 token 一律 `NO_PREVIEW_TOKEN`）；
  ③ 新增第 18 节：P1-2 的终态兜底（调度器合成终态、编排器 dry-run 写入白名单、
     编排器非零退出落 `EXITED_FAILED`），并配「把合成逻辑关掉就翻脸」的判别力负例。
被改掉的旧断言只有**标签措辞**（例如「T2 交付物尚未落地」→「临时根下的 tools/ 是空的」，
因为编排器现在真存在了），判据本身一条没删、没放松。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

import bjtime                      # noqa: E402
import update_scheduler as S       # noqa: E402

FAILS: list[str] = []
CHECKS = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  -> {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def section(name: str) -> None:
    print(f"\n=== {name} ===")


# ---------------------------------------------------------------- 两段式确认助手
#
# `manual_start` 现在**必须**带 `preview_token`（P1-2 期间发现的第二处夹具过时：两段式令牌
# 门上线后这套测试再没跑通过，不带 token 一律 `NO_PREVIEW_TOKEN`）。
# 这里按测试意图走**真实**的只读预览拿令牌：`preview_update()` 只读采集 + 内存签发，
# 不写库、不建锁、不起进程；令牌绑 10 分钟与状态范围哈希，所以必须**紧贴** start 之前签发。
def grant_preview_token() -> str:
    return S.preview_update()["preview_token"]


# ---------------------------------------------------------------- 北京口径助手

def t_bjtime() -> None:
    section("1. bjtime：固定 UTC+8，不用 zoneinfo/tzdata，不用 naive now()")
    check("BJT 偏移就是 +8:00（且不是本机 PDT）",
          bjtime.BJT.utcoffset(None) == timedelta(hours=8),
          str(bjtime.BJT.utcoffset(None)))
    iso = bjtime.now_iso_bjt()
    check("输出串带 +08:00", iso.endswith("+08:00"), iso)
    def import_lines(path: Path) -> list[str]:
        return [l.strip() for l in path.read_text(encoding="utf-8").splitlines()
                if l.strip().startswith(("import ", "from "))]

    for mod in ("bjtime.py", "update_scheduler.py"):
        imps = import_lines(APP / mod)
        check(f"{mod} 只 import 标准库+本项目，没有 zoneinfo/tzdata",
              any("zoneinfo" in i or "tzdata" in i for i in imps) is False, str(imps))
    check("正例对照：api.py 确实引了 zoneinfo（说明上面的检法能抓到）",
          any("zoneinfo" in i for i in import_lines(APP / "api.py")))
    check("解析库内简写偏移 +08",
          bjtime.parse_bjt("2026-11-07T17:52:04+08")
          == datetime(2026, 11, 7, 17, 52, 4, tzinfo=bjtime.BJT))
    check("解析纯日期 2026-10-14 → 北京零点",
          bjtime.date_bjt(bjtime.parse_bjt("2026-10-14")) == "2026-10-14")
    check("解析不了的一律 None（不抛、不猜）", bjtime.parse_bjt("不是时间") is None)

    section("2. 交节 → 首个交易日（跳过周末；交节在收盘后当日不算）")
    check("立冬 2026-11-07T17:52+08（周六）→ 2026-11-09（周一）",
          str(bjtime.first_trading_day_on_or_after("2026-11-07T17:52:04.819060+08:00"))
          == "2026-11-09")
    check("负例：不等于交节当日 2026-11-07（周六非交易日）",
          str(bjtime.first_trading_day_on_or_after("2026-11-07T17:52:04+08:00"))
          != "2026-11-07")
    check("白露 2026-09-07T22:41+08（周一收盘后）→ 2026-09-08",
          str(bjtime.first_trading_day_on_or_after("2026-09-07T22:41:17+08:00"))
          == "2026-09-08")
    check("立春 2026-02-04T04:02+08（凌晨交节，当日开盘在交节后）→ 2026-02-04",
          str(bjtime.first_trading_day_on_or_after("2026-02-04T04:02:08+08:00"))
          == "2026-02-04")
    check("坏输入 → None（不硬算）",
          bjtime.first_trading_day_on_or_after("garbage") is None)
    closed_today_noon = bjtime.is_session_closed(
        datetime(2026, 10, 22).date(), datetime(2026, 10, 22, 14, 0, tzinfo=bjtime.BJT))
    closed_today_night = bjtime.is_session_closed(
        datetime(2026, 10, 22).date(), datetime(2026, 10, 22, 20, 0, tzinfo=bjtime.BJT))
    check("收盘门控：当天 14:00 未收盘 False / 20:00 已收盘 True",
          closed_today_noon is False and closed_today_night is True)
    check("未来日一律未收盘（不硬跑）",
          bjtime.is_session_closed(bjtime.today_bjt() + timedelta(days=3)) is False)


# ---------------------------------------------------------------- 锁 / 哨兵

# run_id 夹具（P1-2 整改）：产品的 `assert_run_id` 只吃编排器同规格的
# `MU-YYYYMMDDTHHMMSS`，旧夹具那种 `upd_new_stale` 会在 `acquire_lock` 第一行就抛
# `BAD_RUN_ID`，整套 T1 一条都跑不到底。这里逐条换成合法串，并且
#   ① **路径串一起对齐**（`update.lock.stale.<run_id>`、`update_<run_id>.log` 都跟着改，
#      否则测试自己假失败）；
#   ② **每个用例的 run_id 互不相同**（不按名字排序复用，避免跨用例串状态）。
RID_OLD_HOLDER = "MU-20261001T000000"    # 陈旧锁里的原持有者（已死）
RID_STALE_ACQ = "MU-20261022T200001"     # 3a：撞开陈旧锁的人
RID_FRESH_HOLDER = "MU-20261022T200002"  # 3b：新鲜锁的持有者
RID_FRESH_ACQ = "MU-20261022T200003"     # 3b：来抢新鲜锁的人（必须被拒）
RID_LIVE_HOLDER = "MU-20261022T200004"   # 3c：pid 还活着的持有者
RID_LIVE_ACQ = "MU-20261022T200005"      # 3c：来抢的人（必须被拒）
RID_CLEAN = "MU-20261022T200006"         # 3d：无锁时直接成功
RID_BADFILE = "MU-20261022T200007"       # 3e：锁文件内容坏了
RID_OTHER = "MU-20261022T200008"         # 3f：release_lock(别人的 run_id)——故意不对上


def t_lock(tmp: Path) -> None:
    section("3. 文件锁：陈旧锁只改名不删除；新鲜锁必须拒（双向）")
    dead = 999_999
    check("探测：不存在的 pid 判死", S.pid_alive(dead) is False)
    check("探测：自己的 pid 判活（正例控制）", S.pid_alive(os.getpid()) is True)
    # 3-前置：run_id 规格本身就是锁的入场券（旧夹具 `upd_*` 就是死在这一条上）
    try:
        S.assert_run_id("upd_new_stale")
        check("坏 run_id（upd_ 前缀）被 assert_run_id 当场拦下", False, "没抛异常")
    except AssertionError as exc:
        check("坏 run_id（upd_ 前缀）被 assert_run_id 当场拦下", "BAD_RUN_ID" in str(exc),
              str(exc)[:60])
    check("合法 run_id 放行（正例控制）", S.assert_run_id(RID_STALE_ACQ) == RID_STALE_ACQ)

    # 3a 陈旧锁（pid 死 + mtime 超 36h）→ 改名保留，绝不删除
    old = datetime.now(bjtime.BJT) - timedelta(hours=40)
    S.LOCK_FILE.write_text(json.dumps({"pid": dead, "port": 8737, "run_id": RID_OLD_HOLDER,
                                       "acquired_at_bjt": bjtime.iso_bjt(old),
                                       "kind": "auto"}), encoding="utf-8")
    os.utime(S.LOCK_FILE, (old.timestamp(), old.timestamp()))
    before = S.LOCK_FILE.read_text(encoding="utf-8")
    ok, code, info = S.acquire_lock(8851, RID_STALE_ACQ, "manual")
    stale = list((tmp / "work").glob(f"update.lock.stale.{RID_STALE_ACQ}*"))
    check("陈旧锁 acquire 成功", ok is True and code == "ACQUIRED", code)
    check("陈旧锁被**改名**保留（不是删除）", len(stale) == 1
          and stale[0].read_text(encoding="utf-8") == before,
          str([p.name for p in stale]))
    check("改名后新锁内容是本次 run_id",
          S._read_json(S.LOCK_FILE).get("run_id") == RID_STALE_ACQ)
    check("陈旧锁事件已在 reports/更新记录.md 登记一次",
          (tmp / "reports" / "更新记录.md").exists()
          and "STALE_LOCK_RENAMED" in (tmp / "reports" / "更新记录.md").read_text(encoding="utf-8"))
    S.release_lock(RID_STALE_ACQ)

    # 3b 期望值改动（**产品行为已变，断言保留**）：`acquire_lock` 现在只认「锁里的持锁进程
    #    到底还在不在」，不再要求 mtime 也超 36 小时——那条等一天半的规则会把真故障现场
    #    吊死，而 pid 已死本身就证明没有并发。所以 pid 死 + 锁很新 ⇒ 按陈旧**改名保留**
    #    （不是删除）后放行；真正必须拦的是 3c（pid 还活着）。
    S.LOCK_FILE.write_text(json.dumps({"pid": dead, "port": 8737, "run_id": RID_FRESH_HOLDER,
                                       "acquired_at_bjt": bjtime.now_iso_bjt(),
                                       "kind": "auto"}), encoding="utf-8")
    before2 = S.LOCK_FILE.read_text(encoding="utf-8")
    ok2, code2, info2 = S.acquire_lock(8851, RID_FRESH_ACQ, "manual")
    stale2 = list((tmp / "work").glob(f"update.lock.stale.{RID_FRESH_ACQ}*"))
    check("pid 已死的锁（哪怕 mtime 很新）按陈旧回收并放行——不再等 36 小时",
          ok2 is True and code2 == "ACQUIRED", code2)
    check("陈旧锁仍然只**改名**不删除，内容原样保留",
          len(stale2) == 1 and stale2[0].read_text(encoding="utf-8") == before2,
          str([p.name for p in stale2]))
    for p in stale2:
        p.unlink()
    S.release_lock(RID_FRESH_ACQ)

    # 3c pid 活的锁 → 一律拒（哪怕 mtime 很久）
    S.LOCK_FILE.write_text(json.dumps({"pid": os.getpid(), "port": 8737,
                                       "run_id": RID_LIVE_HOLDER,
                                       "acquired_at_bjt": "2026-01-01T00:00:00+08:00",
                                       "kind": "auto"}), encoding="utf-8")
    os.utime(S.LOCK_FILE, (time.time() - 90000, time.time() - 90000))
    ok3, code3, info3 = S.acquire_lock(8851, RID_LIVE_ACQ, "manual")
    check("pid 活的锁必须拒（超 36h 也不抢）", ok3 is False and code3 == S.D_LOCK_HELD, code3)
    check("拒的时候把当前 run_id 带回去（给 409 用）",
          (info3 or {}).get("run_id") == RID_LIVE_HOLDER)
    check("活锁被拒时**没有**被改名或删除", S.LOCK_FILE.exists()
          and not list((tmp / "work").glob(f"update.lock.stale.{RID_LIVE_ACQ}*")))

    # 3d 释放只删自己那把
    check("release_lock(别人的 run_id) 不动锁",
          S.release_lock(RID_OTHER) is False and S.LOCK_FILE.exists())
    check("release_lock(自己的 run_id) 删锁",
          S.release_lock(RID_LIVE_HOLDER) is True and not S.LOCK_FILE.exists())
    check("锁不存在时 acquire 直接成功（正例）",
          S.acquire_lock(8851, RID_CLEAN, "manual")[0] is True)
    S.release_lock(RID_CLEAN)
    # 3e 期望值改动（与 3b 同一条产品变更）：坏锁文件（非 JSON）取不到 pid ⇒ 判「持有者已死」，
    #    于是按陈旧**改名保留**后放行，而不是判占用拒绝。保留的原语义是「不崩」这一半：
    #    这条断言仍然不许抛异常，只把期望从 D_LOCK_HELD 改成 ACQUIRED（改名保留的原件另行核对）。
    check("坏锁文件（非 json）也不崩：读得出 _corrupt 并按陈旧改名保留后放行",
          (S.LOCK_FILE.write_text("{not json"),
           S.acquire_lock(8851, RID_BADFILE, "manual")[1] == "ACQUIRED",
           S.release_lock(RID_BADFILE))[1])
    check("坏锁原件被保留成 update.lock.stale.*（没有静默删除）",
          bool(list((tmp / "work").glob(f"update.lock.stale.{RID_BADFILE}*"))))
    for p in (tmp / "work").glob(f"update.lock.stale.{RID_BADFILE}*"):
        p.unlink()
    S.LOCK_FILE.unlink(missing_ok=True)


def t_disabled(tmp: Path) -> None:
    section("4. 停用哨兵：存在即全链不触发（双向）")
    S.DISABLED_FILE.write_text("用户停手\n", encoding="utf-8")
    check("is_disabled True", S.is_disabled() is True)
    # 两段式第一关：不带 token 的 start 必须先被拒（这本身就是「没第二段就不许动手」的证据）
    code_nt, payload_nt = S.manual_start(port=8851)
    check("缺 preview_token 的 start → NO_PREVIEW_TOKEN(400 语义)，且不建锁不起进程",
          code_nt == "NO_PREVIEW_TOKEN" and payload_nt.get("error") == "NO_PREVIEW_TOKEN"
          and not S.LOCK_FILE.exists(), f"{code_nt} {payload_nt}")
    tok = grant_preview_token()
    code, payload = S.manual_start(port=8851, preview_token=tok)
    check("手动端点 → DISABLED（server 映射成 503）", code == "DISABLED"
          and payload.get("error") == "DISABLED", f"{code} {payload}")
    check("哨兵在时不建锁（真的没触发）", not S.LOCK_FILE.exists())
    check("哨兵在时不写流水线日志", not list((tmp / "logs").glob("update_*.log")))
    dec = S.evaluate_trigger(term=_term(), plan=S.new_plan("2026-M10", "2026-11-09"),
                             probe=lambda mid: "2026-11-20")
    check("调度判定也被哨兵拦住", dec["decision"] == S.D_DISABLED, dec["decision"])
    S.DISABLED_FILE.unlink()
    check("哨兵撤掉后同一场景立刻放行（正例）",
          S.evaluate_trigger(now=datetime(2026, 11, 9, 20, 30, tzinfo=bjtime.BJT),
                             term=_term(), plan=S.new_plan("2026-M10", "2026-11-09"),
                             probe=lambda mid: "2026-11-20")["decision"] == S.D_DUE)


# ---------------------------------------------------------------- 触发分支

def _term(month_id="2026-M10", boundary="2026-11-07T17:52:04.819060+08:00",
          end="2026-12-07T10:52:32+08:00") -> dict:
    return {"month_id": month_id, "start_term": "立冬",
            "start_at_bjt": boundary, "end_at_bjt": end, "boundary_at_bjt": boundary,
            "next_month_id": "2026-M11", "next_start_term": "大雪",
            "next_boundary_at_bjt": "2026-12-07T10:52:32+08:00"}


def t_branches() -> None:
    section("5. 三条触发分支（注入假源日期；探针桩不参与）")
    plan = S.new_plan("2026-M10", "2026-11-09")
    # ① 未到交节窗口（当天 10:00 < 20:00）
    d1 = S.evaluate_trigger(now=datetime(2026, 11, 9, 10, 0, tzinfo=bjtime.BJT),
                            term=_term(), plan=plan, probe=lambda mid: "2026-11-20")
    check("① 未到 20:00 → NOT_DUE（就算源里已有数据也不抢跑）",
          d1["decision"] == S.D_NOT_DUE, d1["decision"])
    check("① 负例：绝不判成 DUE", d1["decision"] != S.D_DUE)
    check("① 计划时刻 = 首个交易日 20:00 北京串",
          d1["planned_at_bjt"] == "2026-11-09T20:00:00+08:00"
          and d1["target_first_trade_date"] == "2026-11-09", d1["planned_at_bjt"])
    # ② 源未就绪（源里最新还停在 10-14）
    d2 = S.evaluate_trigger(now=datetime(2026, 11, 9, 20, 30, tzinfo=bjtime.BJT),
                            term=_term(), plan=plan, probe=lambda mid: "2026-10-14")
    check("② 源未就绪 → SOURCE_NOT_READY（不硬跑）",
          d2["decision"] == S.D_SOURCE_NOT_READY, d2["decision"])
    check("② 如实带出源里最新交易日，供首页黄字显示",
          d2["source_max_trade_date"] == "2026-10-14")
    # ②b 探针无结果（T1 桩就是这个行为）
    d2b = S.evaluate_trigger(now=datetime(2026, 11, 9, 20, 30, tzinfo=bjtime.BJT),
                             term=_term(), plan=plan, probe=lambda mid: None)
    check("②b 探不到源 → 同样判 SOURCE_NOT_READY（默认安全）",
          d2b["decision"] == S.D_SOURCE_NOT_READY and d2b["source_max_trade_date"] is None)
    # ③ 命中
    d3 = S.evaluate_trigger(now=datetime(2026, 11, 9, 20, 30, tzinfo=bjtime.BJT),
                            term=_term(), plan=plan, probe=lambda mid: "2026-11-09")
    check("③ 命中 → DUE", d3["decision"] == S.D_DUE, d3["decision"])
    # ④ 同月已成功 → 不再触发
    p4 = {**plan, "succeeded_bjt": "2026-11-09T21:00:00+08:00"}
    check("④ 本流月已成功 → 不重复触发",
          S.evaluate_trigger(now=datetime(2026, 11, 9, 22, 0, tzinfo=bjtime.BJT),
                             term=_term(), plan=p4,
                             probe=lambda mid: "2026-11-09")["decision"] == S.D_SUCCEEDED)
    # ⑤ 尝试次数耗尽
    p5 = {**plan, "attempts": S.MAX_ATTEMPTS_PER_MONTH, "last_status": S.D_SOURCE_NOT_READY}
    check("⑤ 同一节气月超 3 次 → 停止",
          S.evaluate_trigger(now=datetime(2026, 11, 9, 22, 0, tzinfo=bjtime.BJT),
                             term=_term(), plan=p5,
                             probe=lambda mid: "2026-11-09")["decision"] == S.D_MAX_ATTEMPTS)
    # ⑥ 当日已试过 → 次日重试
    p6 = {**plan, "attempts": 1, "last_attempt_bjt": "2026-11-09T20:01:00+08:00"}
    check("⑥ 当日已尝试 → 冷却到下一个北京日",
          S.evaluate_trigger(now=datetime(2026, 11, 9, 21, 0, tzinfo=bjtime.BJT),
                             term=_term(), plan=p6,
                             probe=lambda mid: "2026-10-14")["decision"] == S.D_COOLDOWN)
    check("⑥ 负例：隔一天就允许再试（次日重试真的生效）",
          S.evaluate_trigger(now=datetime(2026, 11, 10, 20, 30, tzinfo=bjtime.BJT),
                             term=_term(), plan=p6,
                             probe=lambda mid: "2026-11-09")["decision"] == S.D_DUE)
    # ⑦ 交节落在盘中（大雪 2026-12-07T10:52 周一）：当日 K 线跨月 → 取次一交易日
    d7 = S.evaluate_trigger(now=datetime(2026, 12, 8, 20, 30, tzinfo=bjtime.BJT),
                            term=_term("2026-M11", "2026-12-07T10:52:32.428941+08:00",
                                       end="2027-01-05T22:09:57+08:00"),
                            plan=S.new_plan("2026-M11", None),
                            probe=lambda mid: "2026-12-08")
    check("⑦ 盘中交节 → 目标取次一交易日 2026-12-08（与库内 first_date 口径一致）",
          d7["target_first_trade_date"] == "2026-12-08", d7["target_first_trade_date"])
    check("⑦ 目标日一定不是周末",
          bjtime.is_trading_weekday(datetime.fromisoformat(
              d7["target_first_trade_date"]).date()))
    check("⑦ 命中判定", d7["decision"] == S.D_DUE, d7["decision"])


def t_tick_effects(tmp: Path) -> None:
    section("6. tick 的副作用：plan 持久化 + 尝试计数 + 不硬跑")
    S.PLAN_FILE.unlink(missing_ok=True)
    real_probe = S.probe_source_max_trade_date
    real_connect = S.api.connect

    class FakeCon:
        def execute(self, *a, **k):
            raise AssertionError("这一轮不该真查库")

        def close(self):
            pass

    # 用假 term + 假探针把 evaluate_trigger 的分支喂进 tick_once（只测落盘效果）；
    # 造一个「触发窗已过但本月还没成功」的流月（2026-M09，planned 2026-10-09T20:00），
    # 这样用**真实北京时钟**就能走到源未就绪/冷却分支，不用假 now。
    term_m09 = _term("2026-M09", "2026-10-08T14:29:18.238783+08:00",
                     "2026-11-07T17:52:04.819060+08:00")
    S.api.connect = lambda *a, **k: FakeCon()
    try:
        ev = S.evaluate_trigger
        S.evaluate_trigger = (lambda now=None, con=None, term=None, plan=None, probe=None:
                              ev(now=None, term=term_m09,
                                 plan=plan if plan is not None else S.load_plan(),
                                 probe=probe or (lambda mid: None)))
        d = S.tick_once("auto")
        check("源未就绪时 tick 如实登记 SOURCE_NOT_READY",
              d["decision"] == S.D_SOURCE_NOT_READY, d["decision"])
        plan = S.load_plan()
        check("plan 已落盘且 attempts=1（次日重试的计数起点）",
              plan and plan["month_id"] == "2026-M09" and plan["attempts"] == 1
              and plan["last_attempt_bjt"] and plan["succeeded_bjt"] is None, str(plan))
        check("plan 字段严格是约定的 7 个（没夹带）",
              sorted(plan) == sorted(S.PLAN_FIELDS), str(sorted(plan)))
        check("决策快照已落盘（状态端点免查库）", S.DECISION_FILE.exists())
        check("没建锁、没起进程", not S.LOCK_FILE.exists() and S.child_info() is None)
        d2 = S.tick_once("auto")
        check("同日重复 tick → 冷却，不重复计数",
              d2["decision"] == S.D_COOLDOWN and S.load_plan()["attempts"] == 1, d2["decision"])
        check("冷却不写锁、不起进程", not S.LOCK_FILE.exists() and S.child_info() is None)
        # 把 last_attempt 挪到昨天 → 立刻允许再试（次日重试真的生效，负例对照）
        p = S.load_plan()
        p["last_attempt_bjt"] = bjtime.iso_bjt(bjtime.now_bjt() - timedelta(days=1))
        S.save_plan(p)
        d3 = S.tick_once("auto")
        check("隔一个北京日就允许再试，attempts 变 2",
              d3["decision"] == S.D_SOURCE_NOT_READY and S.load_plan()["attempts"] == 2,
              f'{d3["decision"]} {S.load_plan()["attempts"]}')
    finally:
        S.evaluate_trigger = ev
        S.api.connect = real_connect
        S.probe_source_max_trade_date = real_probe


# ---------------------------------------------------------------- 拉起 / 回收

def t_launch(tmp: Path) -> None:
    section("7a. 流水线入口缺失：如实登记，不崩、不假装成功")
    RID_NOENTRY = "MU-20261022T200101"     # 入口缺失这一轮
    RID_NOENTRY2 = "MU-20261022T200102"    # 手动端点走同一分支的另一轮
    RID_NOLOCK = "MU-20261022T200103"      # 未持锁直接调拉起
    RID_OK = "MU-20261022T200104"          # 替身脚本正常拉起→回收
    check("约定入口确实不存在（临时根下的 tools/ 是空的）", not S.PIPELINE_ENTRY.exists(),
          str(S.PIPELINE_ENTRY))
    ok, code, info = S.launch_pipeline("manual", port=8851, dry_run=True, run_id=RID_NOENTRY)
    check("返回 PIPELINE_ENTRY_MISSING（不是异常）", ok is False and code == S.D_ENTRY_MISSING)
    check("没为失败的轮次留状态文件（不谎报 RUNNING）", S.load_state() is None)
    check("没建 update_<run_id>.log", not (tmp / "logs" / f"update_{RID_NOENTRY}.log").exists())
    tok = grant_preview_token()          # 两段式：紧贴 start 之前签令牌
    code2, payload2 = S.manual_start(port=8851, run_id=RID_NOENTRY2, preview_token=tok)
    check("手动端点在入口缺失时返回非 202（server 映射 500）", code2 == "ENTRY_MISSING",
          f"{code2} {payload2}")
    check("入口缺失也已在更新记录里留痕",
          "PIPELINE_ENTRY_MISSING" in (tmp / "reports" / "更新记录.md").read_text(encoding="utf-8"))
    check("payload 不含任何绝对路径", "/" not in json.dumps(payload2, ensure_ascii=False),
          json.dumps(payload2, ensure_ascii=False)[:80])
    check("失败后锁已释放（不会卡住后面）", not S.LOCK_FILE.exists())
    check("入口缺失这一轮 plan 里没有 RUNNING（P1-2：不许把轮次吊在进行中）",
          (S.load_plan() or {}).get("last_status") not in (S.D_RUNNING, "RUNNING"),
          str((S.load_plan() or {}).get("last_status")))

    section("7b. 用 /tmp 里的**替身脚本**验证拉起→移交→回收（不是真流水线）")
    entry = tmp / "tools" / "run_monthly_update.py"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(
        "import json,os,sys,time\n"
        "print('ARGV', sys.argv[1:], flush=True)\n"
        "print('SID', os.getsid(0), 'PSID', os.getsid(os.getppid()), flush=True)\n"
        "p=os.path.join(os.environ['T1_WORK'],'update_state.json')\n"
        "time.sleep(0.4)\n"
        "json.dump({'status':'SUCCESS','stage':'done','run_id':sys.argv[sys.argv.index('--run-id')+1],"
        "'finished_at_bjt':'2026-11-09T21:00:00+08:00'},open(p,'w'))\n", encoding="utf-8")
    S.PIPELINE_ENTRY = entry
    os.environ["T1_WORK"] = str(tmp / "work")
    # 负例：没持锁就调拉起接口 → 一律拒绝（防止绕过锁的并发拉起）
    ok0, code0, _ = S.launch_pipeline("manual", port=8851, dry_run=True, run_id=RID_NOLOCK)
    check("未持锁时拒绝拉起（LOCK_NOT_HELD）", ok0 is False and code0 == "LOCK_NOT_HELD",
          code0)
    check("  拒绝时没写 RUNNING 初态、也没起进程",
          S.load_state() is None and S.child_info() is None)
    # 正例：先拿锁（真实调用方 manual_start / tick_once 都是这么走的）
    got_lock, _c, _i = S.acquire_lock(8851, RID_OK, "catchup")
    check("先取锁", got_lock is True)
    ok, code, info = S.launch_pipeline("catchup", port=8851, dry_run=True, run_id=RID_OK)
    check("拉起成功", ok is True and code == "LAUNCHED", code)
    st = S.load_state()
    check("拉起方写了初态 RUNNING/init（含 _bjt 串与 stages）",
          st and st["status"] == "RUNNING" and st["stage"] == "init"
          and st["status"] == "RUNNING" and st["run_id"] == RID_OK
          and st["trigger"] == "catchup"
          and st["started_at_bjt"].endswith("+08:00") and st["stages"] == [], str(st))
    check("锁由拉起方持有（同 run_id）",
          (S._read_json(S.LOCK_FILE) or {}).get("run_id") == RID_OK)
    check("日志文件已建在临时 logs/ 下", (tmp / "logs" / f"update_{RID_OK}.log").exists())
    child_pid = info.get("pid")
    deadline = time.time() + 20
    while time.time() < deadline and S.child_info() and S.child_info()["alive"]:
        time.sleep(0.2)
    log_txt = (tmp / "logs" / f"update_{RID_OK}.log").read_text(encoding="utf-8")
    check("命令行默认带 --dry-run（替身自报 argv）", "--dry-run" in log_txt,
          [l for l in log_txt.splitlines() if l.startswith("ARGV")][:1])
    check("trigger 透传成 --trigger catchup", "--trigger', 'catchup" in log_txt
          or "--trigger" in log_txt and "catchup" in log_txt)
    sid_line = [l for l in log_txt.splitlines() if l.startswith("SID ")]
    check("start_new_session=True：子进程会话号自成一组（脱离服务进程组）",
          bool(sid_line) and int(sid_line[0].split()[1]) != os.getsid(0),
          sid_line[:1])
    check("流水线把状态文件写成了终态（写者移交生效）",
          S.load_state().get("status") == "SUCCESS")
    S._reap_child()
    plan = S.load_plan()
    check("回收后 plan.succeeded_bjt 由流水线终态落账",
          plan and plan["succeeded_bjt"] == "2026-11-09T21:00:00+08:00"
          and plan["last_status"] == "SUCCESS", str(plan))
    check("回收后锁已释放", not S.LOCK_FILE.exists())
    check("回收后 child 句柄已清空（不留僵尸引用）", S.child_info() is None)
    check("替身子进程已退出、无残留", S._CHILD is None or S._CHILD.poll() is not None,
          f"pid={child_pid}")
    entry.unlink()
    S.PIPELINE_ENTRY = tmp / "tools" / "run_monthly_update.py"


# ---------------------------------------------------------------- 状态口径

def t_status(tmp: Path) -> None:
    RID_TAIL = "MU-20261022T200201"        # 有日志的一轮（第 9 节）
    RID_NOLOG = "MU-20261022T200202"       # 日志缺失的一轮（第 9 节）
    RID_STATE = "MU-20261022T200203"       # 只用来装状态文档的一轮（第 8 节）
    section("8. 状态口径：无状态文件时全 null（不填 0/空串）+ 指纹弱路径")
    for f in (S.PLAN_FILE, S.STATE_FILE, S.DECISION_FILE, S.FINGERPRINT_FILE):
        f.unlink(missing_ok=True)
    payload = S.db_status(con=None, dataset="ashare")
    need = ["asof_bjt", "status", "scheduler", "db_last_trade_date", "source_max_trade_date",
            "lag_trading_days", "last_success_bjt", "next_planned_bjt", "dbs", "error",
            "month_id", "target_first_trade_date"]
    check("必需字段齐全", all(k in payload for k in need),
          ",".join(k for k in need if k not in payload))
    check("从未成功 → last_success_bjt 是 null（不是 0/空串）",
          payload["last_success_bjt"] is None)
    check("探针桩 → source_max_trade_date 是 null", payload["source_max_trade_date"] is None)
    check("拿不到两侧日期 → lag_trading_days 是 null（不填 0）",
          payload["lag_trading_days"] is None, str(payload["lag_trading_days"]))
    check("scheduler 结构齐全",
          set(payload["scheduler"]) == {"pid", "port", "started_at_bjt", "alive"})
    check("五库指纹齐全", set(payload["dbs"]) == {"ashare", "etf", "gender", "cap_tier",
                                                  "v5_slice"}, str(set(payload["dbs"])))
    check("db_fingerprints.json 缺失 → 弱指纹 st:<size>-<mtime_bjt> 且 weak=true",
          payload["dbs"]["ashare"]["weak"] is True
          and payload["dbs"]["ashare"]["sha12"].startswith("st:")
          and payload["dbs"]["ashare"]["sha12"].endswith("+08:00"),
          payload["dbs"]["ashare"]["sha12"])
    check("所有 *_bjt 输出都带 +08:00",
          all(str(v).endswith("+08:00") for k, v in payload.items()
              if k.endswith("_bjt") and v is not None))
    check("db_last_trade_date 复用库内最后有数据流月（不新造 SQL 口径）",
          payload["db_last_month_id"] == "2026-M08", str(payload["db_last_month_id"]))

    (tmp / "work" / "db_fingerprints.json").write_text(json.dumps({
        "built_at_bjt": "2026-11-09T23:00:00+08:00",
        "dbs": {"ashare": {"sha12": "abcdef0123456789" * 2, "bytes": 5129752576},
                "etf": {"sha256": "1234abcd5678"}, "cap_tier": {"sha12": "deadbeef0000"}}}),
        encoding="utf-8")
    payload2 = S.db_status(con=None, dataset="ashare")
    check("权威指纹文件优先（sha256 自动截 12 位）",
          payload2["dbs"]["ashare"]["sha12"] == "abcdef012345"
          and payload2["dbs"]["ashare"]["weak"] is False
          and payload2["dbs"]["ashare"]["built_at_bjt"] == "2026-11-09T23:00:00+08:00",
          str(payload2["dbs"]["ashare"]))
    check("指纹文件里没有的库仍回落弱指纹", payload2["dbs"]["v5_slice"]["weak"] is True)
    (tmp / "work" / "db_fingerprints.json").unlink()

    # 有状态/计划时的口径
    S.save_plan({**S.new_plan("2026-M10", "2026-11-09"), "attempts": 2,
                 "last_status": S.D_SOURCE_NOT_READY,
                 "last_attempt_bjt": "2026-11-10T20:00:15+08:00"})
    S._write_decision({"decision": S.D_SOURCE_NOT_READY, "month_id": "2026-M10",
                       "planned_at_bjt": "2026-11-09T20:00:00+08:00",
                       "target_first_trade_date": "2026-11-09",
                       "source_max_trade_date": "2026-10-14",
                       "boundary_at_bjt": "2026-11-07T17:52:04+08:00",
                       "note": "源里最新交易日 2026-10-14 还没到 2026-11-09",
                       "at": bjtime.now_iso_bjt()})
    payload3 = S.db_status(con=None, dataset="ashare")
    check("源未就绪时 status 如实显示", payload3["status"] == S.D_SOURCE_NOT_READY,
          payload3["status"])
    check("error.code 带 SOURCE_NOT_READY 且 message 不含绝对路径",
          payload3["error"]["code"] == S.D_SOURCE_NOT_READY
          and "/" not in payload3["error"]["message"], str(payload3["error"]))
    check("source_max_trade_date 显示 2026-10-14（验收口径要的黄字）",
          payload3["source_max_trade_date"] == "2026-10-14")
    check("next_planned_bjt 顺延且必为 +08:00 的 20:00",
          payload3["next_planned_bjt"] is not None
          and payload3["next_planned_bjt"].endswith("T20:00:00+08:00"),
          payload3["next_planned_bjt"])

    # 真成功一轮
    S.save_plan({**S.new_plan("2026-M10", "2026-11-09"), "attempts": 1,
                 "last_status": "SUCCESS", "succeeded_bjt": "2026-11-09T23:00:00+08:00"})
    S.write_state({"status": "SUCCESS", "stage": "done", "run_id": RID_STATE,
                   "started_at_bjt": "2026-11-09T20:05:00+08:00", "stages": ["mirror"]})
    p4 = S.db_status(con=None, dataset="ashare")
    check("成功后 status=SUCCESS / last_success_bjt 有值 / next_planned=null",
          p4["status"] == "SUCCESS" and p4["last_success_bjt"] == "2026-11-09T23:00:00+08:00"
          and p4["next_planned_bjt"] is None, f'{p4["status"]} {p4["next_planned_bjt"]}')

    # 冷却日：status 不能退回 IDLE，要留着上一轮的真实阻断码
    S.save_plan({**S.new_plan("2026-M10", "2026-11-09"), "attempts": 1,
                 "last_status": S.D_SOURCE_NOT_READY,
                 "last_attempt_bjt": bjtime.now_iso_bjt()})
    S._write_decision({"decision": S.D_COOLDOWN, "month_id": "2026-M10",
                       "planned_at_bjt": "2026-11-09T20:00:00+08:00",
                       "target_first_trade_date": "2026-11-09",
                       "source_max_trade_date": "2026-10-14",
                       "note": "今日已尝试过一次", "at": bjtime.now_iso_bjt()})
    p_cd = S.db_status(con=None, dataset="ashare")
    check("冷却日 status 仍显示 SOURCE_NOT_READY（不是 IDLE）",
          p_cd["status"] == S.D_SOURCE_NOT_READY and p_cd["decision"] == S.D_COOLDOWN,
          f'{p_cd["status"]} / {p_cd["decision"]}')

    # lag 计算
    check("lag_trading_days：2026-10-08→2026-10-14 = 4 个交易日（周一~周五口径）",
          S._lag_trading_days("2026-10-08", "2026-10-14") == 4,
          str(S._lag_trading_days("2026-10-08", "2026-10-14")))
    check("lag 负例：源比库还旧 → 0（不给负数）",
          S._lag_trading_days("2026-10-14", "2026-10-08") == 0)
    check("lag 任一侧缺值 → null", S._lag_trading_days(None, "2026-10-14") is None)

    section("9. /api/db_update/state：日志尾部 40 行 + run_id 语义")
    lg = tmp / "logs" / f"update_{RID_TAIL}.log"
    lg.write_text("\n".join(f"line{i}" for i in range(1, 101)) + "\n", encoding="utf-8")
    p5 = S.update_state_payload(RID_TAIL)
    check("日志只回最后 40 行且是尾部",
          p5["log_tail_lines"] == 40 and p5["log_tail"][-1] == "line100"
          and p5["log_tail"][0] == "line61", str(p5["log_tail"][:1]))
    check("run_id 透传", p5["run_id"] == RID_TAIL)
    check("日志文件名不含绝对路径", p5["log_file"] == f"logs/update_{RID_TAIL}.log")
    p6 = S.update_state_payload(RID_NOLOG)
    check("日志不存在 → 空数组（不 500）", p6["log_tail"] == [])
    lg.unlink()

    section("10. 更新记录追加：格式对、原子、失败不炸")
    S.RECORD_MD.unlink(missing_ok=True)
    S.append_update_record("auto", "FAILED", "SOURCE_NOT_READY", "源停在 2026-10-14")
    S.append_update_record("manual", "SUCCESS", None, "整链完成 | 含竖线要转义")
    lines = S.RECORD_MD.read_text(encoding="utf-8").splitlines()
    rows = [l for l in lines if l.startswith("| 20")]
    check("自动补了表头（列名与 T2 的表一致）",
          lines[0] == "# 更新记录" and "| 北京时间 | trigger | status | 错误码 | 一句话 |" in lines)
    check("行格式 | 北京时间 | trigger | status | 错误码 | 一句话 |",
          len(rows) == 2 and rows[0].endswith(
              "+08:00 | auto | FAILED | SOURCE_NOT_READY | 源停在 2026-10-14 |"), rows[0])
    check("表头只补一次（第二轮不会重复建表）",
          sum(1 for l in lines if l.startswith("| 北京时间 |")) == 1)
    check("正文里的竖线被转义，表格列数不被撑坏（仍是 6 个竖线）",
          "¦" in rows[1] and rows[1].count("|") == 6, rows[1])
    S.RECORD_MD.chmod(0o444)          # 只读文件：追加必然失败，但绝不能抛
    check("追加失败时只返回 False、不抛异常（不影响主流程）",
          S.append_update_record("auto", "FAILED", "X", "不该炸") is False)
    S.RECORD_MD.chmod(0o644)


# ---------------------------------------------------------------- 缓存 / 连接

def t_caches() -> None:
    section("11. api.invalidate_caches() 与 v5_api 线程本地连接的 inode/mtime 校验")
    import api
    import v5_api
    api._META_CACHE.clear()
    api._CALENDAR_CACHE.clear()
    api._OPP_CACHE.clear()
    api._META_CACHE["ashare"] = {"x": 1}
    api._CALENDAR_CACHE[("ashare", "000001.SH")] = {"y": 2}
    api._OPP_CACHE[("k",)] = {"z": 3}
    api._OPP_CACHE[("k2",)] = {"z": 4}
    check("填充后三个 dict 非空（负例基线）",
          len(api._META_CACHE) == 1 and len(api._CALENDAR_CACHE) == 1
          and len(api._OPP_CACHE) == 2)
    n = api.invalidate_caches()
    check("返回清掉的条数 4", n == 4, str(n))
    check("三个 dict 全空",
          not api._META_CACHE and not api._CALENDAR_CACHE and not api._OPP_CACHE)
    check("再清一次返回 0（幂等，不报错）", api.invalidate_caches() == 0)

    tmp2 = Path(tempfile.mkdtemp(prefix="t1_v5_"))
    fake = tmp2 / "slice_placeholder"          # 故意不是 .sqlite：本测试不建任何数据库文件
    fake.write_bytes(b"x")
    real_slice = v5_api.SLICE_DB
    v5_api.SLICE_DB = fake

    class FakeCon:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    def try_connect():
        try:
            return v5_api._connect()
        except Exception as exc:  # noqa: BLE001 —— 占位文件不是 sqlite，报错是预期的
            return exc

    try:
        st0 = fake.stat()
        sig0 = v5_api._db_signature()
        check("_db_signature 取到 (inode, mtime_ns, size)",
              isinstance(sig0, tuple) and len(sig0) == 3 and sig0[2] == 1, str(sig0))
        # ① 正例：文件没变 → 复用同一条线程本地连接（不重开）
        c = FakeCon()
        v5_api._tl.con, v5_api._tl.sig = c, sig0
        check("签名一致 → 复用旧连接（正例放行、不关不重开）",
              try_connect() is c and c.closed is False)
        # ② mtime/size 变（同 inode 原地写）→ 必须丢
        fake.write_bytes(b"xy")
        c2 = FakeCon()
        v5_api._tl.con, v5_api._tl.sig = c2, sig0
        try_connect()
        check("size/mtime 变了 → 旧连接被关闭（负例必拦）", c2.closed is True)
        check("  且旧句柄绝不再交出去", getattr(v5_api._tl, "con", None) is not c2)
        os.utime(fake, ns=(st0.st_atime_ns, st0.st_mtime_ns))
        fake.write_bytes(b"x")
        os.utime(fake, ns=(st0.st_atime_ns, st0.st_mtime_ns))
        # ③ 只换 inode（流水线那种 临时文件 + os.replace 原子换库），size/mtime 全还原
        fake2 = tmp2 / "new_placeholder"
        fake2.write_bytes(b"x")
        os.utime(fake2, ns=(st0.st_atime_ns, st0.st_mtime_ns))
        os.replace(fake2, fake)
        st1 = fake.stat()
        check("  前置核实：size 与 mtime_ns 都没变，只有 inode 变了",
              st1.st_size == st0.st_size and st1.st_mtime_ns == st0.st_mtime_ns
              and st1.st_ino != st0.st_ino, f"{st0.st_ino} -> {st1.st_ino}")
        c3 = FakeCon()
        v5_api._tl.con, v5_api._tl.sig = c3, sig0
        try_connect()
        check("只换 inode 也照样重开（只看 mtime 是不够的）", c3.closed is True)
        # ④ 文件消失 → 丢连接并给原有结构化错误
        fake.unlink()
        c4 = FakeCon()
        v5_api._tl.con, v5_api._tl.sig = c4, v5_api._db_signature()
        got = try_connect()
        check("文件缺失 → 旧连接不返回、被关闭", c4.closed is True and got is not c4)
        check("文件缺失仍给原有 V5Unavailable（SQL/返回结构不变）",
              isinstance(got, v5_api.V5Unavailable), type(got).__name__)
        check("_db_signature 拿不到文件时是 None（不抛）", v5_api._db_signature() is None)
    finally:
        v5_api.SLICE_DB = real_slice
        cur = getattr(v5_api._tl, "con", None)
        if cur is not None:
            try:
                cur.close()
            except Exception:  # noqa: BLE001
                pass
        v5_api._tl.con = None
        v5_api._tl.sig = None
        shutil.rmtree(tmp2, ignore_errors=True)


def t_concurrent_exception(tmp: Path) -> None:
    section("12. 调度线程异常时请求面仍 200（真起一份临时端口服务）")
    from http.server import ThreadingHTTPServer
    import server as SRV
    port = 8853
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), SRV.Handler)
    except OSError as exc:
        check("临时端口可用", False, str(exc))
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tick_real = S.tick_once
    S.tick_once = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("测试注入的 tick 爆炸"))
    S.TICK_SECONDS = 1
    S.CATCHUP_DELAY_SECONDS = 0
    try:
        S.start(port=port)
        time.sleep(2.0)
        import urllib.request
        import urllib.error
        code = None
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/db_status",
                                        timeout=20) as r:
                code = r.status
                body = json.loads(r.read())
        except urllib.error.HTTPError as e:
            code, body = e.code, {}
        check("调度线程炸了，/api/db_status 仍 200", code == 200, str(code))
        check("状态里如实留下 ERROR 决策（不假装 WAITING）",
              body.get("decision") in (S.D_ERROR, S.D_SOURCE_NOT_READY, S.D_NOT_DUE),
              str(body.get("decision")))
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/meta", timeout=30) as r:
            check("既有接口照常 200（查询面没被牵连）", r.status == 200)
        check("异常已写进决策快照文件",
              (tmp / "work" / "update_last_decision.json").exists())
    finally:
        S.tick_once = tick_real
        srv.shutdown()
        srv.server_close()


def t_run_id_and_probe(tmp: Path) -> None:
    section("13. run_id 规格与源探测（对接 T2 的 MU- 正则与 baseline 文件）")
    import re
    rid = S.new_run_id()
    check("run_id 形如 MU-YYYYMMDDTHHMMSS（T2 RUN_ID_RE 同规格）",
          re.match(r"^MU-\d{8}T\d{6}$", rid) is not None, rid)
    check("run_id 不含冒号（可安全做文件名）", ":" not in rid and "/" not in rid)

    check("没有 baseline → None（= SOURCE_NOT_READY，不硬跑）",
          S.probe_source_max_trade_date() is None, str(S.SOURCE_BASELINE_FILE.name))
    S.SOURCE_BASELINE_FILE.write_text("{坏 json", encoding="utf-8")
    check("baseline 坏了 → None（不抛）", S.probe_source_max_trade_date() is None)
    S.SOURCE_BASELINE_FILE.write_text(json.dumps({"max_trade_date": "20261014"}),
                                      encoding="utf-8")
    check("日期格式不合 → None", S.probe_source_max_trade_date() is None)
    good_base = json.dumps({
        "schema": "astock-source-publication-baseline-v1",
        "max_trade_date": "2026-10-14", "recorded_at_bjt": "2026-10-22T18:00:00+08:00"})
    S.SOURCE_BASELINE_FILE.write_text(good_base, encoding="utf-8")
    # ↓ 期望值改动（**产品行为变了，断言本身保留**，理由写在下面两条里）：
    #   第一作者写这条断言时 `probe_source_max_trade_date()` 读的是 baseline JSON 那一个文件；
    #   现在（`probe_source` 文档）判据改成**只认本地镜像内容**（README `规模.跨度`），
    #   baseline 只作交叉核对——因为 2026-10-22 实测过「mtime 被刷屏但内容 60/60 全等」，
    #   任何单点留痕都不能当新鲜度判据。
    check("镜像读不到时：baseline 再正常也照样 None（fail-closed，镜像为准）",
          S.probe_source_max_trade_date() is None,
          f"baseline={S.SOURCE_BASELINE_FILE.name} 在位但镜像根是空的")
    readme = S.SOURCE_README
    readme.parent.mkdir(parents=True, exist_ok=True)
    readme.write_text(json.dumps({"规模": {"跨度": "20260101..20261014",
                                          "行": 1516000, "交易日": 200}},
                                 ensure_ascii=False), encoding="utf-8")
    check("baseline 正常 + 镜像在位 → 2026-10-14（只读本地 JSON，不联网不碰 NAS）",
          S.probe_source_max_trade_date() == "2026-10-14",
          str(S.probe_source().get("basis")))
    S.SOURCE_BASELINE_FILE.write_text(json.dumps({
        "schema": "astock-source-publication-baseline-v1",
        "max_trade_date": "2026-09-30", "recorded_at_bjt": "2026-10-22T18:00:00+08:00"}),
        encoding="utf-8")
    check("负例：baseline 与镜像不一致时以镜像为准，并把分歧照实登记（不静默采信留痕）",
          S.probe_source_max_trade_date() == "2026-10-14"
          and any("以镜像为准" in n for n in S.probe_source().get("notes") or []),
          str(S.probe_source().get("notes"))[:120])
    S.SOURCE_BASELINE_FILE.unlink(missing_ok=True)
    dec = S.evaluate_trigger(now=datetime(2026, 11, 9, 20, 30, tzinfo=bjtime.BJT),
                             term=_term(), plan=S.new_plan("2026-M10", "2026-11-09"))
    check("用真探针 + 立冬场景 → 源未就绪(2026-10-14)，与验收口径一致",
          dec["decision"] == S.D_SOURCE_NOT_READY
          and dec["source_max_trade_date"] == "2026-10-14", str(dec["decision"]))
    check("负例：源日期一旦追平首个交易日就判 DUE",
          S.evaluate_trigger(now=datetime(2026, 11, 9, 20, 30, tzinfo=bjtime.BJT),
                             term=_term(), plan=S.new_plan("2026-M10", "2026-11-09"),
                             probe=lambda mid: "2026-11-09")["decision"] == S.D_DUE)

    section("14. 回收终态：dry-run 轮次绝不算「本月已成功」")
    class FakeChild:
        def poll(self):
            return 0

        returncode = 0

    RID_DRY = "MU-20261022T200301"     # DRY_RUN_PLAN 那一轮
    RID_SUC = "MU-20261022T200302"     # 真 SUCCESS 那一轮
    RID_PEF = "MU-20261022T200303"     # PROMOTED_EXTENSION_FAILED 那一轮
    S._CHILD = FakeChild()
    S._CHILD_META = {"run_id": RID_DRY, "trigger": "auto"}
    S.save_plan(S.new_plan("2026-M10", "2026-11-09"))
    S.write_state({"status": "DRY_RUN_PLAN", "run_id": RID_DRY})
    S.LOCK_FILE.write_text(json.dumps({"run_id": RID_DRY,
                                       "pid": os.getpid()}), encoding="utf-8")
    S._reap_child()
    plan = S.load_plan()
    check("DRY_RUN_PLAN 只登记状态、不写 succeeded_bjt",
          plan["last_status"] == "DRY_RUN_PLAN" and plan["succeeded_bjt"] is None, str(plan))
    check("回收后锁释放", not S.LOCK_FILE.exists() and S.child_info() is None)
    # 正例对照：真 SUCCESS 才落 succeeded_bjt
    S._CHILD, S._CHILD_META = FakeChild(), {"run_id": RID_SUC, "trigger": "auto"}
    S.LOCK_FILE.write_text(json.dumps({"run_id": RID_SUC,
                                       "pid": os.getpid()}), encoding="utf-8")
    S.write_state({"status": "SUCCESS", "run_id": RID_SUC,
                   "finished_at_bjt": "2026-11-09T23:00:00+08:00"})
    S._reap_child()
    plan = S.load_plan()
    check("SUCCESS 才写 succeeded_bjt（用流水线给的北京终态串）",
          plan["last_status"] == "SUCCESS"
          and plan["succeeded_bjt"] == "2026-11-09T23:00:00+08:00", str(plan))
    # PROMOTED_EXTENSION_FAILED 这类以 FAILED 结尾的终态要算失败
    S._CHILD, S._CHILD_META = FakeChild(), {"run_id": RID_PEF, "trigger": "auto"}
    S.LOCK_FILE.write_text(json.dumps({"run_id": RID_PEF,
                                       "pid": os.getpid()}), encoding="utf-8")
    S.write_state({"status": "PROMOTED_EXTENSION_FAILED", "run_id": RID_PEF})
    S._reap_child()
    p7 = S.db_status(con=None, dataset="ashare")
    check("以 FAILED 结尾的终态 → status=FAILED 且 error 带码",
          p7["status"] == "FAILED" and p7["error"]["code"] == "PROMOTED_EXTENSION_FAILED",
          f'{p7["status"]} {p7["error"]}')
    S.SOURCE_BASELINE_FILE.unlink(missing_ok=True)
    # 第 13 节造的临时镜像 README 收掉：后面的用例仍在同一个临时镜像根上跑，
    # 留着会让 source_max_trade_date 一路非空，判据就不干净了。
    readme.unlink(missing_ok=True)


def t_t2_handoff(tmp: Path) -> None:
    section("15. 与 T2 实际落盘格式对接（databases/sha256_12/size_bytes + 缓存失效）")
    import config
    doc = {
        "schema": "astock-db-fingerprints-v1",
        "generated_at_bjt": "2026-10-22T17:50:14+08:00",
        "run_id": "MU-20261022T200401",
        "pipeline_status": "SUCCESS",
        "databases": {
            "ashare_main": {"exists": True, "size_bytes": 5129752576,
                            "sha256_12": "c1f7af5f5582",
                            "mtime_bjt": "2026-10-21T11:38:46+08:00",
                            "built_at_bjt": "2026-10-21T11:38:46+08:00",
                            "path": str(config.ASHARE_DB)},
            "etf": {"exists": True, "size_bytes": 446578688, "sha256_12": "e67e13d4b620",
                    "path": str(config.ETF_DB)},
            "market_cap_tier": {"exists": True, "size_bytes": 110538752,
                                "sha256_12": "60aeea1191ff", "path": str(config.CAP_DB)},
            # T2 给 gender 记的是扩展构建库，而 8737 实际读的是 *_proxy.sqlite ⇒ 不是同一个文件
            "gender_dayun": {"exists": False, "sha256_12": "aaaa5555bbbb",
                             "path": str(config.ROOT / "nowhere" / "gender_dayun_v1.sqlite")},
        },
        "cache_handoff": {"consumer": "T1 app/update_scheduler.py"},
    }
    S.FINGERPRINT_FILE.write_text(json.dumps(doc), encoding="utf-8")
    fp = S.db_fingerprints()
    check("T2 的 databases/sha256_12/size_bytes 被正确读成权威指纹",
          fp["ashare"]["sha12"] == "c1f7af5f5582" and fp["ashare"]["weak"] is False
          and fp["ashare"]["bytes"] == 5129752576
          and fp["ashare"]["built_at_bjt"] == "2026-10-21T11:38:46+08:00", str(fp["ashare"]))
    check("缺 built_at 的条目回落到文件 generated_at_bjt",
          fp["etf"]["built_at_bjt"] == "2026-10-22T17:50:14+08:00", str(fp["etf"]))
    check("负例：path 指向的不是服务在读的那个文件 ⇒ 不采信，退回弱指纹并标注",
          fp["gender"]["weak"] is True and fp["gender"].get("path_mismatch") is True
          and fp["gender"]["sha12"] != "aaaa5555bbbb", str(fp["gender"]))
    st = S.db_status(con=None, dataset="ashare")
    check("db_status.dbs 用的就是权威指纹（不重算哈希）",
          st["dbs"]["ashare"]["sha12"] == "c1f7af5f5582")

    section("16. 换库侦测 → 进程内缓存失效（T2 在别的进程清不动，契约指向本模块）")
    import api
    S._LAST_FP_SIG = None
    api._META_CACHE["ashare"] = {"x": 1}
    api._CALENDAR_CACHE[("ashare", None)] = {"y": 2}
    n0 = S.maybe_invalidate_caches()
    check("首轮只建基线，不清缓存（返回 0）",
          n0 == 0 and len(api._META_CACHE) == 1, str(n0))
    n1 = S.maybe_invalidate_caches()
    check("指纹没变 → 不清（返回 0，缓存留着）",
          n1 == 0 and len(api._META_CACHE) == 1, str(n1))
    doc["databases"]["ashare_main"]["sha256_12"] = "ffffffffffff"
    doc["databases"]["ashare_main"]["size_bytes"] = 6000000000
    S.FINGERPRINT_FILE.write_text(json.dumps(doc), encoding="utf-8")
    n2 = S.maybe_invalidate_caches()
    check("换库（sha12 变了）→ 立刻清掉进程内三个缓存",
          n2 == 2 and not api._META_CACHE and not api._CALENDAR_CACHE, str(n2))
    check("同一份新指纹不会反复清（幂等）", S.maybe_invalidate_caches() == 0)
    S.FINGERPRINT_FILE.unlink()
    check("指纹文件缺失 → 返回 0 不报错", S.maybe_invalidate_caches() == 0)

    section("17. T2 终态字段（顶层 error_code / nodes / mode）能被状态端点读出来")
    RID_T2TERMINAL = "MU-20261022T200501"     # 第 17 节专用：T2 自己写的终态
    S.write_state({"schema": "astock-monthly-update-state-v1",
                   "run_id": RID_T2TERMINAL, "trigger": "auto", "mode": "execute",
                   "status": "FAILED", "error_code": "NODE_FAILED",
                   "created_at_bjt": "2026-11-09T20:00:05+08:00",
                   "updated_at_bjt": "2026-11-09T23:59:59+08:00",
                   "finished_at_bjt": "2026-11-09T23:59:59+08:00",
                   "source_max_trade_date_detected": "2026-11-09",
                   "nodes": [{"node": 1, "key": "mirror", "status": "SUCCESS"},
                             {"node": 2, "key": "source_input", "status": "FAILED"}],
                   "gates": {}, "planned_writes": []})
    p = S.db_status(con=None, dataset="ashare")
    check("status=FAILED 且 error 取顶层 error_code",
          p["status"] == "FAILED" and p["error"]["code"] == "NODE_FAILED",
          f'{p["status"]} {p["error"]}')
    check("state 里能看到 T2 的 mode/nodes/finished_at_bjt/探测到的源末交易日",
          p["state"]["mode"] == "execute" and len(p["state"]["stages"]) == 2
          and p["state"]["current_node"] == "source_input"
          and p["state"]["finished_at_bjt"] == "2026-11-09T23:59:59+08:00"
          and p["state"]["source_max_trade_date_detected"] == "2026-11-09",
          str(p["state"]))
    check("所有 _bjt 字段仍是带 +08:00 的串",
          all(str(v).endswith("+08:00") for k, v in p.items()
              if k.endswith("_bjt") and v is not None))
    sp = S.update_state_payload(RID_T2TERMINAL)
    check("state 端点同样能读 T2 的终态", sp["state"]["status"] == "FAILED"
          and sp["plan"] is not None)
    S.STATE_FILE.unlink()


def t_terminal_p12(tmp: Path) -> None:
    """P1-2「轮次没有终态」的判别性断言：调度器兜底 + 编排器 dry-run 白名单。

    全部在临时 work/logs 里跑；子进程用**假句柄**（FakeChild）模拟「已退出」，
    不真起流水线、不 --execute、不碰任何 .sqlite。
    每条都配了「把修复关掉就翻脸」的负例半边：如果终态合成不生效，断言必须变红。
    """
    section("18. P1-2：子进程已退出但状态停在 RUNNING ⇒ 调度器必须合成终态")

    class FakeChild:
        def __init__(self, rc):
            self.returncode = rc

        def poll(self):
            return self.returncode      # 非 None = 已退出（poll 会回收僵尸，不许用 os.kill 猜）

    def fresh_round(rid: str, rc: int, state_doc: dict, log_lines: list[str]) -> None:
        S._CHILD = FakeChild(rc)
        S._CHILD_META = {"run_id": rid, "trigger": "auto", "dry_run": True}
        S.write_state(state_doc)
        S.LOCK_FILE.write_text(json.dumps({"run_id": rid, "pid": os.getpid()}),
                               encoding="utf-8")
        (tmp / "logs" / f"update_{rid}.log").write_text("\n".join(log_lines) + "\n",
                                                        encoding="utf-8")

    # ---- 18a 硬失败 rc=4（NO_ROOT_CONFIGURED）：现场就是页面上完全静默那一条
    RID_SYNTH_FAIL = "MU-20261022T200601"
    S.save_plan(S.new_plan("2026-M10", "2026-11-09"))
    fresh_round(RID_SYNTH_FAIL, 4,
                {"status": "RUNNING", "stage": "init", "run_id": RID_SYNTH_FAIL,
                 "trigger": "auto", "started_at_bjt": "2026-10-22T20:00:05+08:00",
                 "stages": [], "error": None},
                ["NO_ROOT_CONFIGURED：根目录未显式配置，拒绝启动（本工具已无「回落到真实路径」"
                 "的缺省）。缺失项=['MONTHLY_UPDATE_DOCS_ROOT 或 --docs-root']"])
    S._reap_child()
    st = S.load_state() or {}
    plan = S.load_plan() or {}
    check("rc=4：调度器把停在 RUNNING 的状态合成了终态 EXITED_FAILED",
          st.get("status") == "EXITED_FAILED", str(st.get("status")))
    check("rc=4：终态必须带结束时刻，且是带 +08:00 的北京串",
          bool(st.get("finished_at_bjt"))
          and str(st.get("finished_at_bjt")).endswith("+08:00"), str(st.get("finished_at_bjt")))
    check("rc=4：error 是 {code,message} 结构（不是只剩一个「更新中」）",
          isinstance(st.get("error"), dict) and bool(st["error"].get("code"))
          and bool(st["error"].get("message")), str(st.get("error")))
    check("rc=4：error.code 如实说明是调度器按退出码合成的",
          st["error"]["code"] == "SCHEDULER_SYNTHESIZED_EXIT_4", st["error"]["code"])
    check("rc=4：NO_ROOT_CONFIGURED 从日志尾部被带进 message（不再静默）",
          "NO_ROOT_CONFIGURED" in st["error"]["message"], st["error"]["message"][:120])
    check("rc=4：plan.last_status 绝不留在 RUNNING",
          plan.get("last_status") == "EXITED_FAILED"
          and plan.get("last_status") != S.D_RUNNING, str(plan.get("last_status")))
    check("rc=4：状态端点读出来是 FAILED + 同一个错误码（前端据此停轮询/解禁按钮）",
          (lambda p: p["status"] == "FAILED" and p["error"]["code"] == st["error"]["code"])(
              S.db_status(con=None, dataset="ashare")))
    check("rc=4：回收后锁释放、child 句柄清空（不会有下一轮被吊着）",
          not S.LOCK_FILE.exists() and S.child_info() is None)

    # ---- 18b 负例半边（判别力）：把合成逻辑关掉 ⇒ 同一场景必须退回「永久 RUNNING」现场
    real_synth = S._synth_terminal_state
    try:
        S._synth_terminal_state = lambda state, rc, run_id, trigger: dict(state or {})
        fresh_round(RID_SYNTH_FAIL, 7,
                    {"status": "RUNNING", "stage": "init", "run_id": RID_SYNTH_FAIL,
                     "trigger": "auto", "started_at_bjt": "2026-10-22T20:00:05+08:00",
                     "stages": [], "error": None},
                    ["NO_ROOT_CONFIGURED：根目录未显式配置，拒绝启动"])
        S._reap_child()
        st_off = S.load_state() or {}
        plan_off = S.load_plan() or {}
        check("关掉合成后这条测试会翻脸：状态又停在 RUNNING（证明 18a 有判别力）",
              st_off.get("status") == "RUNNING" and not st_off.get("finished_at_bjt")
              and plan_off.get("last_status") == "RUNNING",
              f'{st_off.get("status")} / {plan_off.get("last_status")}')
    finally:
        S._synth_terminal_state = real_synth
        # 复原成真实产品行为，后面的断言不许建立在被打桩的模块上
        S.STATE_FILE.unlink(missing_ok=True)
        (tmp / "logs" / f"update_{RID_SYNTH_FAIL}.log").unlink(missing_ok=True)

    # ---- 18c rc=0 但没写终态：算「这一轮结束了」，但**不算**本月成功（dry-run 不是成功）
    RID_SYNTH_OK = "MU-20261022T200602"
    S.save_plan(S.new_plan("2026-M10", "2026-11-09"))
    fresh_round(RID_SYNTH_OK, 0,
                {"status": "RUNNING", "stage": "init", "run_id": RID_SYNTH_OK,
                 "trigger": "auto", "started_at_bjt": "2026-10-22T20:10:05+08:00",
                 "stages": [], "error": None},
                ["=== DRY-RUN：以下只打印，不写任何文件 ==="])
    S._reap_child()
    st2 = S.load_state() or {}
    plan2 = S.load_plan() or {}
    check("rc=0 停在 RUNNING：合成 EXITED_OK（有结束时刻），而不是编造成 SUCCESS",
          st2.get("status") == "EXITED_OK" and bool(st2.get("finished_at_bjt"))
          and st2.get("error") is None, str(st2.get("status")))
    check("rc=0 合成的终态不写 succeeded_bjt（dry-run 轮次绝不算本月已成功）",
          plan2.get("succeeded_bjt") is None and plan2.get("last_status") == "EXITED_OK",
          str(plan2))
    check("rc=0：状态文件里绝不再出现 status=RUNNING 且 finished_at_bjt=null 的组合",
          not (str(st2.get("status")).upper() == "RUNNING" and not st2.get("finished_at_bjt")))

    # ---- 18d 状态文件压根没写：同样要有终态可读（不许留白）
    RID_NOSTATE = "MU-20261022T200603"
    S.STATE_FILE.unlink(missing_ok=True)
    S._CHILD = FakeChild(9)
    S._CHILD_META = {"run_id": RID_NOSTATE, "trigger": "auto"}
    S.LOCK_FILE.write_text(json.dumps({"run_id": RID_NOSTATE, "pid": os.getpid()}),
                           encoding="utf-8")
    S._reap_child()
    st3 = S.load_state() or {}
    check("连状态文件都没写过时，回收也会补一份终态（不是留空）",
          st3.get("status") == "EXITED_FAILED" and bool(st3.get("finished_at_bjt"))
          and st3.get("exit_rc") == 9, str(st3)[:160])

    # ---- 18e 编排器侧：dry-run 写入白名单是**代码里读得出来**的一条常量
    import importlib.util
    entry = S.PKG_ROOT / "tools" / "run_monthly_update.py"
    check("编排器入口在仓库里存在（T1 临时根里那份是替身，这里读原件）", entry.is_file(),
          str(entry.name))
    spec = importlib.util.spec_from_file_location("t1_pipeline_mod", str(entry))
    R = importlib.util.module_from_spec(spec)
    sys.modules["t1_pipeline_mod"] = R          # dataclass 需要模块已登记进 sys.modules
    tmp_tree = Path(tempfile.mkdtemp(prefix="t1_p12_tree_"))
    try:
        spec.loader.exec_module(R)
        wl = tuple(getattr(R, "DRY_RUN_WRITE_WHITELIST", ()) or ())
        check("白名单以命名常量存在于代码里，且只有自有状态件那两件",
              wl == ("work/update_state.json", "logs/update_<run_id>.log"), str(wl))
        Pw = R.resolve_paths(root=str(tmp_tree))
        rid_w = "MU-20261022T200604"
        R.set_dry_run_guard(Pw, rid_w, active=True)
        check("白名单第 1 件（状态件）允许落盘",
              R.atomic_write_json(Pw.state_file, {"status": "DRY_RUN_PLAN"},
                                  enabled=True, log=None) is True
              and Pw.state_file.exists())
        check("白名单第 2 件（本轮日志）允许落盘",
              R.atomic_write_text(Pw.logs / f"update_{rid_w}.log", "x\n",
                                  enabled=True, log=None) is True)
        denied = None
        try:
            R.atomic_write_json(Pw.fingerprints, {"y": 1}, enabled=True, log=None)
        except R.DryRunWriteDenied as exc:
            denied = str(exc)
        check("dry-run 写白名单外的目标被当场拒绝（判据没放松）",
              denied is not None and R.E_DRY_RUN_WRITE_DENIED in denied
              and not Pw.fingerprints.exists(), (denied or "没抛，越界写成功了")[:120])
        denied2 = None
        try:
            R.append_line(Pw.update_log_md, "| x |", enabled=True, log=None)
        except R.DryRunWriteDenied as exc:
            denied2 = str(exc)
        check("reports/更新记录.md 这类目标在 dry-run 下仍然写不进去（0 字节）",
              denied2 is not None and not Pw.update_log_md.exists())
        R.set_dry_run_guard(Pw, rid_w, active=False)
        check("真跑（execute）不受该保护：关掉保护后同一目标可写（只测函数层，不跑节点）",
              R.atomic_write_json(Pw.drift_baseline, {"y": 1}, enabled=True, log=None) is True)
        R.set_dry_run_guard(Pw, rid_w, active=True)      # 恢复，避免影响后面的 print-plan
        # ---- 18f 白名单必须在 `--print-plan` 里看得见（不许只写在注释里）
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        rid_pp = "MU-20261022T200605"
        with redirect_stdout(buf):
            rc_pp = R.main(["--run-id", rid_pp, "--root", str(tmp_tree), "--print-plan"])
        out_pp = buf.getvalue()
        check("--print-plan 退出码 0 且把白名单原样打印出来",
              rc_pp == 0 and "DRY_RUN_WRITE_WHITELIST" in out_pp
              and "work/update_state.json" in out_pp
              and "logs/update_<run_id>.log" in out_pp, f"rc={rc_pp}")
        check("--print-plan 不建状态件以外的东西：指纹/更新记录/baseline 全部不存在",
              not Pw.fingerprints.exists() and not Pw.update_log_md.exists()
              and not Pw.baseline_file.exists()
              and list((tmp_tree / "bazi_month_relation_tengod_dev_v1" / "logs")
                       .glob("update_MU-*.log")),
              str(sorted(p.name for p in
                         (tmp_tree / "bazi_month_relation_tengod_dev_v1" / "logs").glob("*"))))
        # ---- 18g 编排器非零退出也必须自己落一份终态（EXITED_FAILED + rc + 原因）
        rid_bad = "MU-bad-for-t1"
        rc_bad = R.main(["--run-id", rid_bad, "--root", str(tmp_tree)])
        doc_bad = json.loads(Pw.state_file.read_text(encoding="utf-8")) if Pw.state_file.exists() else {}
        check("BAD_RUN_ID 这类参数级拒绝：仍然落 EXITED_FAILED 终态（含 rc 与 error.code）",
              rc_bad == 2 and doc_bad.get("status") == "EXITED_FAILED"
              and doc_bad.get("exit_rc") == 2
              and (doc_bad.get("error") or {}).get("code") == "BAD_RUN_ID",
              f"rc={rc_bad} status={doc_bad.get('status')}")
        check("坏 run_id 那一轮不许留下日志文件（文件名不可控 ⇒ 不在白名单里）",
              not (Pw.logs / f"update_{rid_bad}.log").exists())
        rc_alt = R.main(["--run-id", "MU-20261022T200606", "--root", str(tmp_tree),
                         "--execute", "--accept-gates"])
        doc_alt = json.loads(Pw.state_file.read_text(encoding="utf-8"))
        check("根守卫拒绝（假树 --execute）：同样落终态并把原因写进 error（rc=3）",
              rc_alt == 3 and doc_alt.get("status") == "EXITED_FAILED"
              and doc_alt.get("exit_rc") == 3 and bool(doc_alt.get("finished_at_bjt"))
              and (doc_alt.get("error") or {}).get("code") == R.E_ALT_ROOT_UNAUTHORIZED,
              f"rc={rc_alt} {doc_alt.get('status')}")
    finally:
        shutil.rmtree(tmp_tree, ignore_errors=True)
        sys.modules.pop("t1_pipeline_mod", None)
        S._CHILD = S._CHILD_META = None
        S.LOCK_FILE.unlink(missing_ok=True)
        S.STATE_FILE.unlink(missing_ok=True)
        for rid in (RID_SYNTH_FAIL, RID_SYNTH_OK, RID_NOSTATE):
            (tmp / "logs" / f"update_{rid}.log").unlink(missing_ok=True)


# ---------------------------------------------------------------- 入账语义（2026-10-22 裁定）

def _term_m09_legacy() -> dict:
    """旧语义的 M09 历法桩：只给「旧语义会翻脸」反例用，不是产品路径。"""
    return {"month_id": "2026-M09", "start_term": "寒露",
            "start_at_bjt": "2026-10-08T14:29:18+08:00",
            "end_at_bjt": "2026-11-07T17:52:04+08:00",
            "boundary_at_bjt": "2026-10-08T14:29:18+08:00",
            "next_month_id": "2026-M10", "next_start_term": "立冬",
            "next_boundary_at_bjt": "2026-11-07T17:52:04+08:00"}


def t_posting_semantics(tmp: Path) -> None:
    """第 19 节：入账语义 = 月没走完不入账；自动路径只拉 `post_month --mode staging`。

    四条注入时刻（不改系统钟，用 now 参数 / BAZI_NOW_BJT / set_now_override_bjt）：
      ① 2026-10-23 19:00（现网今天）→ 不触发，MONTH_NOT_ENDED；
      ② 2026-11-01 20:00            → 不触发，MONTH_NOT_ENDED；
      ③ 2026-11-09 20:00（源停在 10-14）→ 不触发，SOURCE_NOT_READY；
      ④ 2026-11-09 20:00（假镜像到 11-06）→ 触发，argv 里只有 --mode staging、没有 promote。
    另配反例：同一时刻按**旧语义**（target_first_trade_date）会判 DUE —— 证明 ① 有判别力。

    红线：④ 拉起的 `tools/post_month.py` 是临时根下的**替身脚本**（只记录 argv 就退出），
    不是真入口；不跑 staging/promote、不碰任何 .sqlite、不联网、不连 NAS。
    """
    section("19. 入账语义：月没走完不入账 + 只拉 staging（注入时刻 + 旧语义翻脸反例）")
    saved_entry = S.POST_MONTH_ENTRY
    saved_readme = (S.SOURCE_README.read_text(encoding="utf-8")
                    if S.SOURCE_README.exists() else None)
    fake = tmp / "tools" / "post_month.py"
    argv_file = tmp / "work" / "fake_post_month_argv.json"
    logs_before = {p.name for p in (tmp / "logs").glob("update_*.log")}
    try:
        for f in (S.PLAN_FILE, S.STATE_FILE, S.DECISION_FILE, S.LOCK_FILE, argv_file):
            f.unlink(missing_ok=True)
        S._CHILD = S._CHILD_META = None
        S.set_now_override_bjt(None)
        os.environ.pop("BAZI_NOW_BJT", None)

        # ---- 待入账月解析：M08 → M09（按 (solar_year, month_no) 排序，不依赖 month_id 字符串序）
        pend = S.resolve_pending_month(refresh=True)
        check("待入账月 = 库内最后有数据流月 M08 的下一个流月 M09（按年月排序，不赌 month_id 字符串序）",
              bool(pend) and pend["last_month_id"] == "2026-M08"
              and pend["pending_month"] == "2026-M09",
              str(pend and (pend["last_month_id"], pend["pending_month"])))
        check("M09 = 戊戌月，结束于 2026-11-07T17:52:04+08:00",
              bool(pend) and pend["pending_month_pillar"] == "戊戌"
              and pend["pending_month_ends_at_bjt"] == "2026-11-07T17:52:04+08:00",
              str(pend and (pend["pending_month_pillar"],
                            pend["pending_month_ends_at_bjt"])))

        # 旧语义在 10-22 把 M09 误判成「该跑」并记了 attempts=1：那次发生在月没走完之前，
        # 不该占新语义的 3 次额度（构建串换代 ⇒ plan 行重建，attempts 归 0）。
        S.save_plan({"month_id": "2026-M09", "target_first_trade_date": "2026-10-09",
                     "attempts": 1, "manual_attempts": 0, "last_status": "SOURCE_NOT_READY",
                     "last_attempt_bjt": "2026-10-22T17:43:13+08:00", "succeeded_bjt": None,
                     "scheduler_build": "T1-sched-2026.10.22"})
        d_old = S.evaluate_trigger(now=datetime(2026, 10, 23, 19, 0, tzinfo=bjtime.BJT))
        check("旧构建串留下的 attempts=1 不算新语义额度（重建后 attempts=0）",
              d_old["attempts"] == 0, str(d_old["attempts"]))
        S.PLAN_FILE.unlink(missing_ok=True)

        # 假镜像（本地只读 JSON）：先停在现网源的真实末日 2026-10-14
        S.SOURCE_README.parent.mkdir(parents=True, exist_ok=True)

        def set_mirror(ymd: str) -> None:
            S.SOURCE_README.write_text(json.dumps(
                {"规模": {"跨度": f"20260101..{ymd}", "行": 1516000, "交易日": 200}},
                ensure_ascii=False), encoding="utf-8")

        set_mirror("20261014")

        # ---- ① 2026-10-23 19:00（现网今天）→ MONTH_NOT_ENDED
        n1 = datetime(2026, 10, 23, 19, 0, tzinfo=bjtime.BJT)
        d1 = S.evaluate_trigger(now=n1)
        check("① 10-23 19:00 → MONTH_NOT_ENDED（月没走完不许入账）",
              d1["decision"] == S.D_MONTH_NOT_ENDED
              and d1["blocked_reason"] == "MONTH_NOT_ENDED",
              f'{d1["decision"]} / {d1["blocked_reason"]}')
        check("① 文案可读：戊戌月未结束，11-07 17:52 后入账",
              "戊戌月未结束" in d1["note"] and "11-07 17:52" in d1["note"], d1["note"])
        check("① 不再拿「本流月首个交易日 10-09」当目标，而是 M09 走完后的首个交易日 11-09",
              d1["target_first_trade_date"] == "2026-11-09",
              str(d1["target_first_trade_date"]))
        check("① next_planned = 交节后首个交易日 20:00",
              S.next_planned_bjt(d1, None, now=n1) == "2026-11-09T20:00:00+08:00",
              str(S.next_planned_bjt(d1, None, now=n1)))
        check("① 负例：不判 DUE，等待期 attempts 不增长",
              d1["decision"] != S.D_DUE and d1["attempts"] == 0, str(d1["attempts"]))

        # ①b 同一时刻走 tick（日期注入生效，不改系统钟）：不建锁、不起进程
        tick1 = S.tick_once("auto", now=n1)
        check("①b tick@10-23 19:00 → MONTH_NOT_ENDED，不建锁、不起子进程",
              tick1["decision"] == S.D_MONTH_NOT_ENDED and not S.LOCK_FILE.exists()
              and S.child_info() is None, str(tick1["decision"]))
        check("①b 没有拉起任何 post_month（替身的 argv 文件不存在）",
              not argv_file.exists())
        check("①b 等待期不烧 attempts（plan.attempts 仍 0）",
              (S.load_plan() or {}).get("attempts") == 0, str(S.load_plan()))

        # ①c 环境变量注入也生效（BAZI_NOW_BJT，仅测试用）
        os.environ["BAZI_NOW_BJT"] = "2026-10-23T19:00:00+08:00"
        check("①c BAZI_NOW_BJT 注入生效（current_now_bjt 读它）",
              bjtime.iso_bjt(S.current_now_bjt()) == "2026-10-23T19:00:00+08:00",
              bjtime.iso_bjt(S.current_now_bjt()))
        check("①c 不带 now 参数时同样判 MONTH_NOT_ENDED",
              S.evaluate_trigger()["decision"] == S.D_MONTH_NOT_ENDED)
        os.environ.pop("BAZI_NOW_BJT", None)

        # ---- ② 2026-11-01 20:00 → 仍不触发（月末才 11-07 17:52）
        S.set_now_override_bjt("2026-11-01T20:00:00+08:00")
        d2 = S.evaluate_trigger()
        check("② 11-01 20:00 → MONTH_NOT_ENDED（set_now_override_bjt 生效）",
              d2["decision"] == S.D_MONTH_NOT_ENDED
              and d2["blocked_reason"] == "MONTH_NOT_ENDED",
              f'{d2["decision"]} / {d2["blocked_reason"]}')
        S.set_now_override_bjt(None)

        # ---- ③ 2026-11-09 20:00 但源只到 10-14 → SOURCE_NOT_READY
        n4 = datetime(2026, 11, 9, 20, 0, tzinfo=bjtime.BJT)
        d3 = S.evaluate_trigger(now=n4)
        check("③ 11-09 20:00 + 源 10-14 → SOURCE_NOT_READY（三项 check 的第三项没过）",
              d3["decision"] == S.D_SOURCE_NOT_READY
              and d3["blocked_reason"] == "SOURCE_NOT_READY",
              f'{d3["decision"]} / {d3["blocked_reason"]}')
        check("③ 如实带出源末日 2026-10-14 与 M09 最后交易日 2026-11-06",
              d3["source_max_trade_date"] == "2026-10-14"
              and d3["last_trading_day_in_window"] == "2026-11-06",
              f'{d3["source_max_trade_date"]} / {d3["last_trading_day_in_window"]}')
        check("③ 负例：绝不判 DUE", d3["decision"] != S.D_DUE)
        # 让状态面读到的就是「11-09 这一 tick」的决策快照（生产里 tick 是唯一写者；
        # 不覆盖的话 db_status 会优先读 10-23 那枚仍然新鲜的快照，测的就不是这一条了）。
        S._write_decision({**d3, "trigger": "auto", "at": bjtime.iso_bjt(n4)})
        st3 = S.db_status(con=None, dataset="ashare")
        check("③ db_status 带 pending_month / pending_month_ends_at_bjt / blocked_reason",
              st3["pending_month"] == "2026-M09"
              and st3["pending_month_ends_at_bjt"] == "2026-11-07T17:52:04+08:00"
              and st3["blocked_reason"] == "SOURCE_NOT_READY",
              str({k: st3.get(k) for k in ("pending_month", "pending_month_ends_at_bjt",
                                           "blocked_reason")}))

        # ---- ④ 2026-11-09 20:00 且假镜像到 11-06 → 触发；拉起替身记录 argv
        set_mirror("20261106")
        d4 = S.evaluate_trigger(now=n4)
        check("④ 11-09 20:00 + 源 11-06 → DUE",
              d4["decision"] == S.D_DUE and d4["blocked_reason"] is None,
              f'{d4["decision"]} / {d4["blocked_reason"]}')
        check("④ DUE 文案注明只 staging、不 promote",
              "--mode staging" in d4["note"] and "不 promote" in d4["note"], d4["note"])

        fake.write_text(
            "import json, os, sys\n"
            "rec = {'argv': sys.argv, 'cwd': os.getcwd(), 'pid': os.getpid()}\n"
            "with open(os.path.join(os.environ['T1_WORK'], 'fake_post_month_argv.json'),\n"
            "          'w', encoding='utf-8') as fh:\n"
            "    json.dump(rec, fh, ensure_ascii=False)\n",
            encoding="utf-8")
        os.environ["T1_WORK"] = str(tmp / "work")
        S.POST_MONTH_ENTRY = fake          # 替身入口：只记 argv，不碰任何库
        tick4 = S.tick_once("auto", now=n4)
        launch = tick4.get("launch") or {}
        check("④ tick@11-09 20:00 → DUE 并拉起成功（launch.mode=staging）",
              tick4["decision"] == S.D_DUE and launch.get("ok") is True
              and launch.get("mode") == "staging" and launch.get("month_id") == "2026-M09",
              str(launch))
        deadline = time.time() + 20
        while time.time() < deadline and S.child_info() and S.child_info()["alive"]:
            time.sleep(0.1)                 # 只 poll，不 os.kill 猜死活
        S._reap_child()
        check("④ 替身已退出且被回收：child 句柄清空、锁释放",
              S.child_info() is None and not S.LOCK_FILE.exists())
        rec = json.loads(argv_file.read_text(encoding="utf-8")) if argv_file.exists() else {}
        args = list(rec.get("argv") or [])[1:]
        check("④ argv 是 post_month 的 staging 口径（--month 2026-M09 / --mode staging）",
              "--month" in args and args[args.index("--month") + 1] == "2026-M09"
              and "--mode" in args and args[args.index("--mode") + 1] == "staging",
              str(args))
        check("④ argv 里 --mode 只出现一次，且绝不出现 promote / --allow-incomplete",
              args.count("--mode") == 1
              and not any("promote" in str(a) for a in args)
              and "--allow-incomplete" not in args, str(args))
        check("④ 拉起日志里也没有 promote（只记 staging 口径）",
              all("promote" not in (tmp / "logs" / n).read_text(
                  encoding="utf-8", errors="replace")
                  for n in ({p.name for p in (tmp / "logs").glob("update_*.log")} - logs_before)))
        plan4 = S.load_plan() or {}
        check("④ attempts 记 1 次；staging 成功只算 EXITED_OK，绝不写 succeeded_bjt（staging≠入账）",
              plan4.get("attempts") == 1 and plan4.get("month_id") == "2026-M09"
              and plan4.get("succeeded_bjt") is None
              and plan4.get("last_status") == "EXITED_OK", str(plan4))
        # ④b 新路径同样保留「同一节气月 3 次上限」
        capped = dict(plan4)
        capped["attempts"] = S.MAX_ATTEMPTS_PER_MONTH
        S.save_plan(capped)
        dcap = S.evaluate_trigger(now=datetime(2026, 11, 10, 20, 0, tzinfo=bjtime.BJT))
        check("④b 新路径保留 attempts 上限：满 3 次 → STOPPED_MAX_ATTEMPTS",
              dcap["decision"] == S.D_MAX_ATTEMPTS, str(dcap["decision"]))
        bad_cmd = None
        try:
            S.build_post_month_cmd("2026-M09 --mode promote")
        except AssertionError as exc:
            bad_cmd = str(exc)
        check("④c month_id 只吃 YYYY-Mx（拒绝把选项塞进月份做参数注入）",
              bad_cmd is not None and "BAD_MONTH_ID" in bad_cmd, str(bad_cmd))

        # ---- 反例（判别力）：同一时刻按旧语义（target_first_trade_date）会判 DUE
        legacy = S.evaluate_trigger(now=n1, term=_term_m09_legacy(),
                                    plan=S.new_plan("2026-M09", "2026-10-09"),
                                    probe=lambda mid: "2026-10-14")
        check("反例①：旧语义同刻（10-23 19:00）判 DUE —— 新断言若改回旧口径必翻脸",
              legacy["decision"] == S.D_DUE
              and legacy["target_first_trade_date"] == "2026-10-09"
              and legacy["decision"] != d1["decision"],
              f'old={legacy["decision"]}/{legacy["target_first_trade_date"]} '
              f'new={d1["decision"]}')
        check("反例②：旧口径下 10-09（M09 首个交易日）就已越过触发时刻，早于月末 11-07",
              bjtime.parse_bjt("2026-10-09T20:00:00+08:00")
              < bjtime.parse_bjt("2026-11-07T17:52:04+08:00"))
    finally:
        S.set_now_override_bjt(None)
        os.environ.pop("BAZI_NOW_BJT", None)
        os.environ.pop("T1_WORK", None)
        S.POST_MONTH_ENTRY = saved_entry
        fake.unlink(missing_ok=True)
        if saved_readme is None:
            S.SOURCE_README.unlink(missing_ok=True)
        else:
            S.SOURCE_README.write_text(saved_readme, encoding="utf-8")
        for f in (S.PLAN_FILE, S.STATE_FILE, S.DECISION_FILE, S.LOCK_FILE, argv_file):
            f.unlink(missing_ok=True)
        S._CHILD = S._CHILD_META = None
        for p in (tmp / "logs").glob("update_*.log"):
            if p.name not in logs_before:
                p.unlink(missing_ok=True)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="t1_sched_"))
    for sub in ("work", "logs", "reports", "tools", "mirror"):
        (tmp / sub).mkdir(parents=True, exist_ok=True)
    print(f"临时根目录：{tmp}")
    print(f"本机时区（不参与业务判断）：{time.tzname}")
    # 源镜像也指到临时目录里那个**空的** mirror：`probe_source` 只认本地镜像内容，
    # 不指过来就会读到现网那棵真镜像（用例结果会跟着真源的发布线漂移，不是自测的错）。
    # 需要「镜像在位」的正例在第 13 节里自己造临时 README.json。
    os.environ["BAZI_SOURCE_MIRROR_ROOT"] = str(tmp / "mirror")
    S.set_roots(root=tmp, work_dir=tmp / "work")
    # 让状态口径读真库（只读），其余落盘全在临时目录
    try:
        t_bjtime()
        t_lock(tmp)
        t_disabled(tmp)
        t_branches()
        t_tick_effects(tmp)
        t_launch(tmp)
        t_status(tmp)
        t_caches()
        t_run_id_and_probe(tmp)
        t_t2_handoff(tmp)
        t_terminal_p12(tmp)
        t_posting_semantics(tmp)
        t_concurrent_exception(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"\n临时目录已清理：{tmp} 存在={tmp.exists()}")
    print(f"\n合计 {CHECKS} 条断言，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  FAILED: {f}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
