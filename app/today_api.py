"""今日关注只读 API：读 today_watch_v1 下的 JSON/CSV。

市值档是页面上的展示/筛选口径，按 api.cap_band_sql() 现算自市值扩展库（只读）；
扩展库缺失时回落到名单里冻结的 tier 标签。名单本身、模型用的 tier 特征都不在这里改。
"""
from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import config
from api import CAP_BAND_LABELS, cap_band_sql

BJT = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[2]
TODAY_DIR = Path(os.environ.get("BAZI_TODAY_DIR", str(ROOT / "today_watch_v1")))

MONTH_TERM = {
    "M01": "立春", "M02": "惊蛰", "M03": "清明", "M04": "立夏",
    "M05": "芒种", "M06": "小暑", "M07": "立秋", "M08": "白露",
    "M09": "寒露", "M10": "立冬", "M11": "大雪", "M12": "小寒",
}


class TodayUnavailable(Exception):
    """数据缺失或最新名单过旧（503）。"""


class TodayError(Exception):
    """参数错误（400）。"""


def _base() -> Path:
    return Path(TODAY_DIR)


def _picks_dir() -> Path:
    return _base() / "ledger" / "picks"


def _scores_dir() -> Path:
    return _base() / "ledger" / "scores"


def _read_json(path: Path):
    if not path.is_file():
        raise TodayUnavailable(f"缺少数据文件：{path.name}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TodayUnavailable(f"数据文件无法读取：{path.name}") from exc


def _tiers() -> list[str]:
    """名单里冻结的档位标签（模型的输入特征口径），只在市值扩展库读不到时兜底。"""
    params = _read_json(_base() / "config" / "params.json")
    return list(params["features"]["tiers"])


_BAND_CACHE: dict[str, tuple[dict[str, str], str | None]] = {}


def _cap_bands(month_id: str | None) -> tuple[dict[str, str], str | None]:
    """按 ≤ 该流月的最近一次月末快照，现算 {代码: 市值档}，口径与机会表同一份 cap_band_sql。

    扩展库缺失或没有可用快照时返回 ({}, None)，页面回落到名单里冻结的档位。
    """
    key = month_id or ""
    if key in _BAND_CACHE:
        return _BAND_CACHE[key]
    out: tuple[dict[str, str], str | None] = ({}, None)
    if config.CAP_DB.is_file():
        try:
            con = sqlite3.connect(config.CAP_DB.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                where, args = ("WHERE month_id <= ?", (key,)) if key else ("", ())
                snap = con.execute(
                    "SELECT MAX(month_id) FROM cap_tier_monthly " + where, args).fetchone()[0]
                if snap:
                    rows = con.execute(
                        "SELECT identity_code, " + cap_band_sql("free_mv_yi") +
                        " AS band FROM cap_tier_monthly WHERE month_id = ?", (snap,)).fetchall()
                    out = ({c: b for c, b in rows}, snap)
            finally:
                con.close()
        except sqlite3.Error:
            out = ({}, None)
    _BAND_CACHE[key] = out
    return out


def _pick_files() -> dict[str, Path]:
    out: dict[str, Path] = {}
    directory = _picks_dir()
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.json")):
        name = path.name
        asof = name.split(".")[0]
        suffix = 0
        if ".r" in name:
            try:
                suffix = int(name.split(".r")[1].split(".")[0])
            except ValueError:
                suffix = 0
        previous = out.get(asof)
        if previous is None:
            out[asof] = path
            continue
        prev_suffix = 0
        if ".r" in previous.name:
            try:
                prev_suffix = int(previous.name.split(".r")[1].split(".")[0])
            except ValueError:
                prev_suffix = 0
        if suffix > prev_suffix:
            out[asof] = path
    return out


def _load_picks(asof: str | None) -> tuple[dict, Path]:
    files = _pick_files()
    if not files:
        raise TodayUnavailable("今日关注暂无名单（ledger/picks 为空）")
    if asof:
        if asof not in files:
            raise TodayError(f"没有 {asof} 的名单")
        path = files[asof]
    else:
        asof = max(files)
        path = files[asof]
    return _read_json(path), path


def _q(q: dict, key: str, default: str = "") -> str:
    values = q.get(key)
    return values[0] if values else default


def latest(q: dict) -> dict:
    list_name = _q(q, "list", "L1")
    if list_name not in ("L1", "L2", "L3"):
        raise TodayError("list 只能是 L1、L2、L3")
    tier = _q(q, "tier", "all")
    asof = _q(q, "asof") or None
    payload, path = _load_picks(asof)
    try:
        stale_days = max(0, (datetime.now(BJT).date() - date.fromisoformat(payload["asof"])).days)
    except (KeyError, ValueError):
        stale_days = None
    rows = payload.get("lists", {}).get(list_name)
    if rows is None:
        raise TodayUnavailable(f"{payload.get('asof')} 没有 {list_name} 名单")
    calendar = dict(payload.get("calendar") or {})
    month_id = calendar.get("month_id") or ""
    calendar["month_term"] = MONTH_TERM.get(month_id[-3:], "")
    bands, band_month = _cap_bands(month_id)
    tier_options = list(CAP_BAND_LABELS) if bands else _tiers()
    if tier != "all" and tier not in tier_options:
        raise TodayError(f"未知市值档：{tier}")
    for r in rows:
        r["cap_band"] = bands.get(r.get("code")) if bands else r.get("tier")
    total = len(rows)
    if tier != "all":
        rows = [r for r in rows if r.get("cap_band") == tier]
    return {
        "schema": "today_watch_latest_v1",
        "asof": payload.get("asof"),
        "file": path.name,
        "entry": payload.get("entry", "下一交易日开盘"),
        "entry_date": payload.get("entry_date"),
        "calendar": calendar,
        "labels": payload.get("labels") or {},
        "release_id": payload.get("release_id"),
        "release_sha256": payload.get("release_sha256"),
        "data": payload.get("data") or {},
        "n_universe": payload.get("n_universe"),
        "n_pool": payload.get("n_pool"),
        "backfill": payload.get("backfill", False),
        "stale": bool(stale_days is not None and stale_days > 3),
        "stale_days": stale_days,
        "list": list_name,
        "tier": tier,
        "tier_options": tier_options,
        "tier_asof_month": band_month,
        "count": len(rows),
        "list_total": total,
        "rows": rows,
    }


def backtest(q: dict | None = None) -> dict:
    return _read_json(_base() / "data" / "backtest_summary.json")


def _score_day(path: Path) -> dict:
    score = _read_json(path)
    block = {"asof": score.get("asof"), "backfill": bool(score.get("backfill")),
             "horizons": {}}
    for h, hb in (score.get("horizons") or {}).items():
        if not hb.get("available"):
            continue
        lists = {}
        for name in ("L1", "C3"):
            rows = hb.get("lists", {}).get(name, [])
            valid = [r for r in rows if not r.get("void")]
            wins = sum(1 for r in valid if r.get("Y") == 1)
            lists[name] = {"n": len(valid), "wins": wins, "void": len(rows) - len(valid),
                           "beat_rate": (wins / len(valid) if valid else None)}
        block["horizons"][h] = {"target_date": hb.get("target_date"),
                                "med": hb.get("med"), "lists": lists}
    return block


def ledger(q: dict) -> dict:
    summary = _read_json(_base() / "ledger" / "summary.json")
    days = _q(q, "days", "20")
    if days not in ("20", "60", "all"):
        raise TodayError("days 只能是 20、60 或 all")
    files = sorted(_scores_dir().glob("*.json")) if _scores_dir().is_dir() else []
    if days != "all":
        files = files[-int(days):]
    return {
        "summary": summary,
        "days_requested": days,
        "days": [_score_day(p) for p in files],
    }


def dates(q: dict | None = None) -> dict:
    files = _pick_files()
    scored = {p.name.split(".")[0] for p in _scores_dir().glob("*.json")} \
        if _scores_dir().is_dir() else set()
    rows = []
    for asof in sorted(files, reverse=True):
        try:
            payload = _read_json(files[asof])
            backfill = bool(payload.get("backfill"))
        except TodayUnavailable:
            backfill = None
        rows.append({"asof": asof, "file": files[asof].name,
                     "backfill": backfill, "scored": asof in scored})
    return {"dates": rows}


def export_csv(q: dict) -> tuple[bytes, str]:
    data = latest(q)
    headers = ["排名", "代码", "名称", "市值档", "5 日跑赢概率", "10 日跑赢概率", "共振层数",
               "三层历史", "前5日涨跌", "20日波动", "5日预期区间", "失效线", "观察窗口"]
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(headers)
    for row in data["rows"]:
        m, x, w = row.get("m", [0, 0]), row.get("x", [0, 0]), row.get("w", [0, 0])
        writer.writerow([
            row.get("rank"),
            row.get("code"),
            row.get("name"),
            row.get("cap_band") or row.get("tier"),
            f"{float(row.get('p5_cal', 0)) * 100:.1f}%",
            f"{float(row.get('p10_cal', 0)) * 100:.1f}%",
            row.get("res_up"),
            f"月 {m[0]}/{m[1]} · 旬 {x[0]}/{x[1]} · 周 {w[0]}/{w[1]}",
            f"{float(row.get('r5', 0)) * 100:+.2f}%",
            f"{float(row.get('vol20', 0)) * 100:.2f}%",
            f"±{float(row.get('band5', 0)) * 100:.1f}%",
            f"{float(row.get('stop_price', 0)):.2f}",
            "5–10 个交易日",
        ])
    filename = f"today_watch_{data['asof']}_{data['list']}.csv"
    return buf.getvalue().encode("utf-8-sig"), filename


def dispatch(path: str, q: dict) -> dict:
    if path == "/api/today/latest":
        return latest(q)
    if path == "/api/today/backtest":
        return backtest(q)
    if path == "/api/today/ledger":
        return ledger(q)
    if path == "/api/today/dates":
        return dates(q)
    raise TodayError("未知的今日关注接口")
