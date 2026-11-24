"""模拟盘人工交易 HTTP 适配层：写路径与只读 paper_api 严格分离。"""
from __future__ import annotations

from paper_trade import manual


def capabilities() -> dict:
    return manual.capabilities()


def dispatch(path: str, payload: dict) -> tuple[int, dict]:
    try:
        if path == "/api/paper/manual/preview":
            return 200, manual.preview(payload)
        if path == "/api/paper/manual/order":
            out = manual.execute(payload)
            return (200 if out.get("ok") else 409), out
        return 404, {"ok": False, "error": "NOT_FOUND", "message": "未知人工交易接口"}
    except manual.ManualError as exc:
        return exc.http_status, exc.payload()
