"""模拟盘账本（SQLite 事务 + 唯一约束幂等 + flock 单实例）。

状态机：
  plans.status: planned → dry_run（dry-run 执行；不动 position_meta）
                      → submitting → confirmed / unknown / needs_review / rejected
                      → missed（迟到窗口外不追单）/ superseded（新修订顶掉未执行旧计划）
                      → conflict（同执行日已有已执行计划后再修订，需人工处理）
  position_meta.status: open → closed（同月同票不重复开仓靠 PK+closed 记录）

写入口约定：本模块函数不自行 commit；调用方用 `with con:` 包住一个业务单元。
连接用 store.connect() 打开、显式 close；所有入口先抢 paper.lock（acquire_lock）。
"""
from __future__ import annotations

import fcntl
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
  plan_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT 'eod',
  month_id TEXT NOT NULL,
  signal_date TEXT NOT NULL,
  execute_trade_date TEXT NOT NULL,
  source_report TEXT NOT NULL,
  source_built_at TEXT,
  config_hash TEXT NOT NULL,
  signal_hash TEXT NOT NULL,
  regime TEXT, segment_id TEXT, total_weight_cap REAL,
  intents_json TEXT NOT NULL,
  target_weights_json TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  executed_at TEXT,
  result_json TEXT
);
CREATE TABLE IF NOT EXISTS submissions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  request_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  request_json TEXT NOT NULL,
  response_summary TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (plan_id, attempt)
);
CREATE TABLE IF NOT EXISTS syncs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  synced_at TEXT NOT NULL,
  kind TEXT NOT NULL,
  ok INTEGER NOT NULL,
  cash REAL, total REAL, market_value REAL,
  positions_json TEXT NOT NULL,
  drift TEXT,
  error TEXT
);
CREATE TABLE IF NOT EXISTS position_meta (
  code TEXT NOT NULL,
  strategy TEXT NOT NULL,
  month_id TEXT NOT NULL,
  entry_date TEXT NOT NULL,
  entry_ref REAL,
  approx_entry INTEGER NOT NULL DEFAULT 0,
  is_probe INTEGER NOT NULL DEFAULT 0,
  stop REAL, target REAL, status TEXT NOT NULL DEFAULT 'open',
  opened_at TEXT NOT NULL,
  closed_at TEXT, exit_reason TEXT,
  PRIMARY KEY (code, strategy, month_id)
);
CREATE TABLE IF NOT EXISTS baseline (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  positions_json TEXT NOT NULL,
  confirmed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nav (
  trade_date TEXT PRIMARY KEY,
  total REAL, cash REAL, market_value REAL, nav REAL,
  day_pnl REAL, cum_pnl REAL, synced_at TEXT NOT NULL
);
-- 本地撮合账本（execution_backend=local）
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  cash REAL NOT NULL,
  initial_cash REAL NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS holdings (
  code TEXT PRIMARY KEY,
  name TEXT,
  qty INTEGER NOT NULL,
  available_qty INTEGER NOT NULL DEFAULT 0,
  avg_cost REAL NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trade_date TEXT NOT NULL,
  slot TEXT,
  code TEXT NOT NULL,
  name TEXT,
  side TEXT NOT NULL,
  qty INTEGER NOT NULL,
  price REAL NOT NULL,
  fee REAL NOT NULL DEFAULT 0,
  tax REAL NOT NULL DEFAULT 0,
  transfer_fee REAL NOT NULL DEFAULT 0,
  plan_id TEXT,
  reason TEXT,
  created_at TEXT NOT NULL
);
"""

# flock 句柄必须一直被引用：局部变量被回收时文件关闭、锁随即释放（同 daily_live_job 惯例）
_lock_handles: list = []


def connect(path=None) -> sqlite3.Connection:
    p = Path(path or config.db_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    # 旧库迁移（盘中试探 2026-11-23 新增 is_probe 列）
    cols = {r[1] for r in con.execute("PRAGMA table_info(position_meta)")}
    if "is_probe" not in cols:
        con.execute("ALTER TABLE position_meta ADD COLUMN is_probe INTEGER NOT NULL DEFAULT 0")
        con.commit()
    pcols = {r[1] for r in con.execute("PRAGMA table_info(plans)")}
    if "kind" not in pcols:
        con.execute("ALTER TABLE plans ADD COLUMN kind TEXT NOT NULL DEFAULT 'eod'")
        con.commit()
    tcols = {r[1] for r in con.execute("PRAGMA table_info(trades)")}
    if "name" not in tcols:
        con.execute("ALTER TABLE trades ADD COLUMN name TEXT")
        con.commit()
    return con


def acquire_lock(blocking: bool = False):
    """非阻塞抢 paper.lock；抢不到说明另一实例在跑（返回 None 由调用方跳过）。"""
    config.private_dir().mkdir(parents=True, exist_ok=True)
    fh = config.lock_path().open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except OSError:
        fh.close()
        return None
    _lock_handles.append(fh)
    return fh


def release_lock(fh) -> None:
    if fh is None:
        return
    try:
        fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        pass
    fh.close()
    if fh in _lock_handles:
        _lock_handles.remove(fh)


# ---------------- plans ----------------

def _plan_row(row) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for k, jk in (("intents", "intents_json"),
                  ("target_weights", "target_weights_json"),
                  ("result", "result_json")):
        try:
            d[k] = json.loads(d.get(jk) or "null")
        except ValueError:
            d[k] = None
    return d


def upsert_plan(con, plan: dict) -> str:
    """新计划入库。返回 created / exists（同 plan_id 幂等）/ conflict（同执行日已执行过）。

    同执行日存在未执行旧计划（planned/dry_run）→ 旧计划 superseded；
    同执行日已有已执行计划（submitting/confirmed/unknown/needs_review）→ 新计划
    标记 conflict，绝不自动执行（修订与执行冲突需人工处理）。
    """
    now = config.iso_bjt()
    pid = plan["plan_id"]
    if con.execute("SELECT 1 FROM plans WHERE plan_id=?", (pid,)).fetchone():
        return "exists"
    intents = {k: plan.get(k) for k in
               ("buys", "exits", "holds", "excluded", "warnings")}
    kind = str(plan.get("kind") or "eod")
    conflict = None
    row = None
    if kind == "eod":
        # 同执行日的 EOD 修订才互斥；盘中试探是独立一次性计划（同日可多次、
        # 绝不顶掉/冲突于 EOD 计划）
        row = con.execute(
            "SELECT plan_id, status FROM plans WHERE execute_trade_date=? "
            "AND kind='eod' AND status NOT IN ('superseded','missed') "
            "ORDER BY created_at DESC, plan_id DESC LIMIT 1",
            (plan["execute_trade_date"],)).fetchone()
        if row is not None:
            if row["status"] in ("planned", "dry_run"):
                con.execute("UPDATE plans SET status='superseded', updated_at=? "
                            "WHERE plan_id=?", (now, row["plan_id"]))
            else:
                conflict = row["status"]
    status = "conflict" if conflict else "planned"
    con.execute(
        "INSERT INTO plans (plan_id, kind, month_id, signal_date, execute_trade_date,"
        " source_report, source_built_at, config_hash, signal_hash, regime,"
        " segment_id, total_weight_cap, intents_json, target_weights_json,"
        " status, created_at, updated_at, result_json)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, plan.get("kind") or "eod", plan.get("month_id"),
         plan.get("signal_date"),
         plan.get("execute_trade_date"), plan.get("source_report"),
         plan.get("source_built_at"), plan.get("config_hash"),
         plan.get("signal_hash"), plan.get("regime"), plan.get("segment_id"),
         plan.get("total_weight_cap"),
         json.dumps(intents, ensure_ascii=False),
         json.dumps(plan.get("target_weights") or {}, ensure_ascii=False),
         status, plan.get("created_at_bjt") or now, now,
         json.dumps({"conflict_with": row["plan_id"]},
                    ensure_ascii=False) if conflict else None))
    return "conflict" if conflict else "created"


def get_plan(con, plan_id: str) -> dict | None:
    return _plan_row(con.execute(
        "SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone())


def due_plan(con, execute_date: str) -> dict | None:
    """当日待执行计划（最新一份 planned；missed/superseded/conflict 不取）。"""
    return _plan_row(con.execute(
        "SELECT * FROM plans WHERE execute_trade_date=? AND status='planned' "
        "ORDER BY created_at DESC, plan_id DESC LIMIT 1",
        (execute_date,)).fetchone())


def plans_recent(con, limit: int = 14) -> list[dict]:
    out = []
    for r in con.execute(
            "SELECT * FROM plans ORDER BY created_at DESC, plan_id DESC LIMIT ?",
            (int(limit),)):
        d = _plan_row(r)
        d.pop("intents", None)
        d.pop("target_weights", None)
        out.append(d)
    return out


def mark_plan(con, plan_id: str, status: str, result: dict | None = None) -> None:
    con.execute(
        "UPDATE plans SET status=?, updated_at=?, executed_at=COALESCE(executed_at,?),"
        " result_json=CASE WHEN ? IS NULL THEN result_json ELSE ? END WHERE plan_id=?",
        (status, config.iso_bjt(), config.iso_bjt() if status != "superseded" else None,
         json.dumps(result, ensure_ascii=False) if result else None,
         json.dumps(result, ensure_ascii=False) if result else None,
         plan_id))


# ---------------- submissions ----------------

def next_attempt(con, plan_id: str) -> int:
    r = con.execute("SELECT MAX(attempt) AS a FROM submissions WHERE plan_id=?",
                    (plan_id,)).fetchone()
    return int(r["a"] or 0) + 1


def record_submission(con, plan_id: str, attempt: int, request_hash: str,
                      request: dict, status: str, summary: str | None = None,
                      error: str | None = None) -> None:
    now = config.iso_bjt()
    con.execute(
        "INSERT OR REPLACE INTO submissions (plan_id, attempt, request_hash,"
        " status, request_json, response_summary, error, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (plan_id, int(attempt), request_hash, status,
         json.dumps(request, ensure_ascii=False), summary, error, now, now))


def latest_submission(con, plan_id: str) -> dict | None:
    r = con.execute("SELECT * FROM submissions WHERE plan_id=? "
                    "ORDER BY attempt DESC LIMIT 1", (plan_id,)).fetchone()
    if r is None:
        return None
    d = dict(r)
    try:
        d["request"] = json.loads(d.pop("request_json") or "null")
    except ValueError:
        d["request"] = None
    return d


# ---------------- syncs ----------------

def save_sync(con, kind: str, ok: int, positions: list, cash=None,
              total=None, market_value=None, drift=None, error=None) -> int:
    cur = con.execute(
        "INSERT INTO syncs (synced_at, kind, ok, cash, total, market_value,"
        " positions_json, drift, error) VALUES (?,?,?,?,?,?,?,?,?)",
        (config.iso_bjt(), kind, int(ok), cash, total, market_value,
         json.dumps(positions, ensure_ascii=False), drift, error))
    return int(cur.lastrowid)


def latest_sync(con) -> dict | None:
    r = con.execute("SELECT * FROM syncs ORDER BY id DESC LIMIT 1").fetchone()
    if r is None:
        return None
    d = dict(r)
    try:
        d["positions"] = json.loads(d.pop("positions_json") or "[]")
    except ValueError:
        d["positions"] = []
    return d


def latest_sync_ok(con) -> dict | None:
    r = con.execute("SELECT * FROM syncs WHERE ok=1 ORDER BY id DESC LIMIT 1").fetchone()
    if r is None:
        return None
    d = dict(r)
    try:
        d["positions"] = json.loads(d.pop("positions_json") or "[]")
    except ValueError:
        d["positions"] = []
    return d


# ---------------- baseline ----------------

def set_baseline(con, positions: list) -> None:
    con.execute("INSERT OR REPLACE INTO baseline (id, positions_json, confirmed_at)"
                " VALUES (1,?,?)",
                (json.dumps(positions, ensure_ascii=False), config.iso_bjt()))


def baseline(con) -> dict | None:
    r = con.execute("SELECT * FROM baseline WHERE id=1").fetchone()
    if r is None:
        return None
    try:
        pos = json.loads(r["positions_json"] or "[]")
    except ValueError:
        pos = []
    return {"confirmed_at": r["confirmed_at"], "positions": pos}


# ---------------- position_meta ----------------

def position_meta_open(con, code: str, strategy: str, month_id: str,
                       entry_date: str, entry_ref=None, approx: int = 0,
                       stop=None, target=None, is_probe: int = 0) -> None:
    con.execute(
        "INSERT INTO position_meta (code, strategy, month_id, entry_date,"
        " entry_ref, approx_entry, is_probe, stop, target, status, opened_at)"
        " VALUES (?,?,?,?,?,?,?,?,?, 'open', ?)"
        " ON CONFLICT(code, strategy, month_id) DO UPDATE SET"
        " entry_ref=COALESCE(excluded.entry_ref, entry_ref),"
        " approx_entry=excluded.approx_entry, is_probe=excluded.is_probe,"
        " stop=excluded.stop,"
        " target=excluded.target, status='open', closed_at=NULL, exit_reason=NULL",
        (code, strategy, month_id, entry_date, entry_ref, int(approx),
         int(is_probe), stop, target, config.iso_bjt()))


def position_meta_confirm_probe(con, code: str, strategy: str, month_id: str) -> None:
    """收盘确认后把试探标记转为正式持仓（不再参与补仓/撤回判断）。"""
    con.execute("UPDATE position_meta SET is_probe=0 WHERE code=? AND strategy=? "
                "AND month_id=? AND status='open'", (code, strategy, month_id))


def position_meta_update_stop(con, code: str, strategy: str, month_id: str,
                              stop) -> None:
    """S4+ 移动止损回写（signals 已保证只上移）。"""
    con.execute("UPDATE position_meta SET stop=? WHERE code=? AND strategy=? "
                "AND month_id=? AND status='open'", (stop, code, strategy, month_id))


def close_position(con, code: str, strategy: str, month_id: str,
                   reason: str, date: str) -> None:
    con.execute("UPDATE position_meta SET status='closed', closed_at=?, exit_reason=? "
                "WHERE code=? AND strategy=? AND month_id=? AND status='open'",
                (date, reason, code, strategy, month_id))


def month_positions(con, month_id: str) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM position_meta WHERE month_id=?", (month_id,))]


def active_position_meta(con) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM position_meta WHERE status='open' "
        "ORDER BY code, strategy")]


def active_held(con) -> list[dict]:
    """活跃持仓 × 最近成功回读的实际权重（无回读时权重 0，退出检查照跑）。"""
    sync = latest_sync_ok(con)
    by_code = {p.get("code"): p for p in (sync or {}).get("positions") or []}
    out = []
    for m in active_position_meta(con):
        p = by_code.get(m["code"]) or {}
        out.append({**m,
                    "weight": float(p.get("weight") or 0.0),
                    "name": p.get("name") or "",
                    "market_value": p.get("market_value")})
    return out


# ---------------- nav ----------------

def upsert_nav(con, trade_date: str, total: float, cash, market_value,
               nav: float, synced_at: str) -> dict:
    prev = con.execute("SELECT total FROM nav WHERE trade_date<? ORDER BY "
                       "trade_date DESC LIMIT 1", (trade_date,)).fetchone()
    prev_total = float(prev["total"]) if prev else None
    first = con.execute("SELECT total FROM nav ORDER BY trade_date ASC LIMIT 1").fetchone()
    base_total = float(first["total"]) if first else total
    day_pnl = None if prev_total is None else round(total - prev_total, 2)
    cum_pnl = round(total - base_total, 2)
    con.execute("INSERT OR REPLACE INTO nav (trade_date, total, cash, market_value,"
                " nav, day_pnl, cum_pnl, synced_at) VALUES (?,?,?,?,?,?,?,?)",
                (trade_date, total, cash, market_value, nav, day_pnl, cum_pnl,
                 synced_at))
    return {"trade_date": trade_date, "total": total, "nav": nav,
            "day_pnl": day_pnl, "cum_pnl": cum_pnl}


def nav_rows(con) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM nav ORDER BY trade_date ASC")]


# ---------------- 本地撮合账本 ----------------

def get_account(con) -> dict | None:
    r = con.execute("SELECT * FROM accounts WHERE id=1").fetchone()
    return dict(r) if r else None


def create_account(con, cash: float) -> None:
    con.execute("INSERT OR IGNORE INTO accounts (id, cash, initial_cash, updated_at)"
                " VALUES (1,?,?,?)",
                (float(cash), float(cash), config.iso_bjt()))


def update_account_cash(con, cash: float) -> None:
    con.execute("UPDATE accounts SET cash=?, updated_at=? WHERE id=1",
                (round(float(cash), 2), config.iso_bjt()))


def holdings_list(con) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM holdings WHERE qty > 0 ORDER BY code")]


def holding_get(con, code: str) -> dict | None:
    r = con.execute("SELECT * FROM holdings WHERE code=?", (code,)).fetchone()
    return dict(r) if r else None


def holding_set(con, code: str, name: str, qty: int,
                avg_cost: float) -> None:
    """成交后更新持仓；qty<=0 直接删除（清仓）。"""
    if qty <= 0:
        con.execute("DELETE FROM holdings WHERE code=?", (code,))
        return
    con.execute(
        "INSERT INTO holdings (code, name, qty, available_qty, avg_cost, updated_at)"
        " VALUES (?,?,?,0,?,?)"
        " ON CONFLICT(code) DO UPDATE SET name=COALESCE(excluded.name, name),"
        " qty=excluded.qty, avg_cost=excluded.avg_cost, updated_at=excluded.updated_at",
        (code, name or None, int(qty), round(float(avg_cost), 4), config.iso_bjt()))


def trade_insert(con, trade_date: str, slot: str, code: str, side: str,
                 qty: int, price: float, fee: float, tax: float,
                 transfer_fee: float, plan_id: str, reason: str,
                 name: str = "") -> None:
    con.execute(
        "INSERT INTO trades (trade_date, slot, code, name, side, qty, price, fee,"
        " tax, transfer_fee, plan_id, reason, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trade_date, slot or "", code, name or None, side, int(qty), float(price),
         float(fee), float(tax), float(transfer_fee), plan_id or "",
         reason or "", config.iso_bjt()))


def bought_today(con, code: str, trade_date: str) -> int:
    r = con.execute("SELECT COALESCE(SUM(qty),0) AS q FROM trades "
                    "WHERE code=? AND trade_date=? AND side='buy'",
                    (code, trade_date)).fetchone()
    return int(r["q"] or 0)


def trades_recent(con, limit: int = 100) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (int(limit),))]
