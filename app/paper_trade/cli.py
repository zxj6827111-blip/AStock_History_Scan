"""模拟盘手动入口（在 app/ 目录下运行）：

    python3 -m paper_trade.cli status
    python3 -m paper_trade.cli verify                 # 检查 easytrader 依赖
    python3 -m paper_trade.cli plan --report <归档.json>
    python3 -m paper_trade.cli dry-run                # = plan（别名）
    python3 -m paper_trade.cli execute [--date YYYY-MM-DD]
    python3 -m paper_trade.cli sync
    python3 -m paper_trade.cli eod
    python3 -m paper_trade.cli reconcile --plan-id <id>
    python3 -m paper_trade.cli baseline --show | --adopt
    python3 -m paper_trade.cli feishu-send --card <cards/xxx.json>

真实执行（execute）需要三重开关全开：mode=live + BAZI_PAPER_TRADE_ENABLE=1
+ 已确认基线；否则被 blocked_* 拦下并如实输出原因。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta

from . import config, engine, report, store
from . import xueqiu as xq

BJT = timezone(timedelta(hours=8))


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cmd_status(_args) -> int:
    ok, msg = xq.verify_easytrader()
    s = config.settings()
    con = store.connect()
    try:
        sync = store.latest_sync(con)
        plans = store.plans_recent(con, 5)
        base = store.baseline(con)
    finally:
        con.close()
    _print({
        "mode": config.mode(), "disabled": config.disabled(),
        "execution_backend": config.settings().get("execution_backend"),
        "live_arming": config.live_arming_ready(),
        "easytrader": {"ok": ok, "message": msg},
        "xueqiu_config": config.xueqiu_config_path().is_file(),
        "baseline_confirmed": base is not None,
        "last_sync": {k: sync.get(k) for k in
                      ("synced_at", "kind", "ok", "drift", "error")}
        if sync else None,
        "plans_recent": plans,
        "execute_window": f"{s['execute_time']}–{s['catchup_until']}",
    })
    return 0


def cmd_verify(_args) -> int:
    ok, msg = xq.verify_easytrader()
    _print({"ok": ok, "message": msg})
    return 0 if ok else 1


def cmd_plan(args) -> int:
    res = engine.finalize_from_report({"json": args.report})
    if res.get("status") == "error":
        _print(res)
        return 2
    _print({k: v for k, v in res.items() if k != "plan"})
    return 0


def cmd_execute(args) -> int:
    now = datetime.now(BJT)
    if args.date:
        y, m, d = (int(x) for x in args.date.split("-"))
        now = now.replace(year=y, month=m, day=d, hour=9, minute=40)
    res = engine.execute_due(now=now)
    _print(res)
    status = res.get("status")
    if status in ("confirmed", "dry_run"):
        return 0
    if status in ("blocked_mode", "blocked_arming", "blocked_baseline",
                  "no_due_plan", "not_trade_day", "out_of_window", "disabled"):
        return 0   # 拦下是设计行为，如实输出原因即可
    return 1


def cmd_sync(_args) -> int:
    _print(engine.sync_portfolio(kind="read"))
    return 0


def cmd_eod(_args) -> int:
    _print(engine.eod_snapshot())
    return 0


def cmd_reconcile(args) -> int:
    _print(engine.reconcile_plan(args.plan_id))
    return 0


def cmd_baseline(args) -> int:
    con = store.connect()
    try:
        base = store.baseline(con)
    finally:
        con.close()
    if args.show or not args.adopt:
        _print(base or {"baseline": None})
        return 0
    _print(engine.adopt_baseline())
    return 0


def cmd_feishu_send(args) -> int:
    from . import feishu
    _print(feishu.send(args.card))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="paper_trade.cli",
                                  description="生存者报告前向模拟盘（雪球组合）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("verify").set_defaults(fn=cmd_verify)

    p = sub.add_parser("plan", help="盘后定稿：从归档报告生成次日计划")
    p.add_argument("--report", required=True, help="归档报告 JSON 路径")
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("dry-run", help="= plan（别名）")
    p.add_argument("--report", required=True)
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("execute", help="执行到期计划（受三重开关保护）")
    p.add_argument("--date", default="", help="执行日 YYYY-MM-DD（默认今天）")
    p.set_defaults(fn=cmd_execute)

    sub.add_parser("sync").set_defaults(fn=cmd_sync)
    sub.add_parser("eod").set_defaults(fn=cmd_eod)

    p = sub.add_parser("reconcile", help="人工对账：按当前组合核对计划")
    p.add_argument("--plan-id", required=True)
    p.set_defaults(fn=cmd_reconcile)

    p = sub.add_parser("baseline", help="基线管理（--adopt 采纳当前组合）")
    p.add_argument("--adopt", action="store_true")
    p.add_argument("--show", action="store_true")
    p.set_defaults(fn=cmd_baseline)

    p = sub.add_parser("feishu-send", help="手动发送 dry-run 卡片（先预览再发群）")
    p.add_argument("--card", required=True)
    p.set_defaults(fn=cmd_feishu_send)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
