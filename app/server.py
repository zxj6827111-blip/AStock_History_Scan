"""127.0.0.1-only local HTTP server for the bazi history query pages.

Standard library only (http.server + sqlite3). Nothing leaves this machine: the socket is bound
to loopback and every response is produced by a read-only query on the local database.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import socket
import sys
import threading
import traceback
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import api
import auth
import dayun_api
import n2_api
import seg_api
import today_api
import update_center
import update_scheduler
import v5_api

STATIC = Path(__file__).resolve().parent / "static"


def _clamped_int(raw, dflt: int, lo: int, hi: int) -> int:
    """把 URL 参数安全地变成 lo..hi 内的整数：非法值回默认，负数不会被当成「不限量」。"""
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        return dflt
    return min(max(n, lo), hi)


def dispatch(path: str, q: dict) -> object:
    g = lambda k, d="": (q.get(k, [d])[0] if q.get(k) else d)  # noqa: E731
    ds = g("dataset", api.DEFAULT_DATASET) or api.DEFAULT_DATASET
    if ds not in api.DATASETS:
        raise ValueError(f"未知数据集 {ds!r}；可用：{', '.join(api.DATASETS)}")
    con = api.connect(ds)
    try:
        if path == "/api/meta":
            return api.meta(con, ds)
        if path == "/api/search":
            return api.search(con, g("q"), _clamped_int(g("limit", "40") or 40, 40, 1, 200))
        if path == "/api/stock":
            out = api.stock(con, g("id"), ds)
            return out if out is not None else {"error": "未找到该证券身份"}
        if path == "/api/branch12":
            bflt = {k: g(k) for k in ("pillar", "stem_element", "branch_element",
                                      "sort", "min_n")}
            return api.branch12(con, g("id"), g("policy", "ALL"), bflt)
        if path == "/api/branch_detail":
            bflt = {k: g(k) for k in ("pillar", "stem_element", "branch_element")}
            return api.branch_detail(con, g("id"), g("branch"), g("policy", "ALL"), bflt)
        if path == "/api/pillar_options":
            return api.pillar_options(con)
        if path == "/api/calendar_now":
            return api.calendar_now(con, g("id") or None, ds)
        if path == "/api/pillar60":
            flt = {k: g(k) for k in ("stem_element", "branch_element", "pillar", "min_n",
                                     "sort", "day_master_element", "clash", "harmony",
                                     "punish", "harm", "broken", "both_clash_harmony",
                                     "dir", "rate_min", "rate_eq")}
            return api.pillar60(con, g("id"), flt)
        if path == "/api/window_layer_pool":
            pflt = {k: g(k) for k in ("stem_element", "branch_element", "pillar",
                                     "dir", "rate_min", "rate_eq", "sort")}
            return api.window_layer_pool(g("layer", "xun"), pflt, api.class_of_dataset(ds))
        if path == "/api/window_layer_stock":
            sflt = {k: g(k) for k in ("stem_element", "branch_element", "pillar", "min_n",
                                      "sort", "dir", "rate_min", "rate_eq", "clash", "harmony",
                                      "punish", "harm", "broken", "both_clash_harmony")}
            return api.window_layer_stock(con, g("layer", "xun"), g("id"), sflt)
        if path == "/api/stock_window_rules":
            return api.stock_window_rules(g("id"), g("pillar"))
        if path == "/api/window_cell_detail":
            return api.window_cell_detail(g("id"), g("pillar"), g("layer", "month"),
                                          _clamped_int(g("win"), 0, 0, 6))
        if path == "/api/window_path":
            return api.window_path(g("id"), g("pillar"), g("layer", "week"),
                                   _clamped_int(g("win"), 0, 0, 6), g("month") or None, ds)
        if path == "/api/window_rules_status":
            return api.window_rules_status(con)
        if path == "/api/pillar_detail":
            return api.pillar_detail(con, g("id"), g("pillar"))
        if path == "/api/stock_stability":
            return api.stock_stability(con, g("id"))
        if path == "/api/opportunities":
            oflt = {k: g(k) for k in ("months", "offset", "min_n", "policy", "sort", "msort", "dir",
                                       "cap", "page", "cap_tier", "rel", "grade", "rate_min", "rate_eq",
                                       "both_months", "next_rate", "stab", "cons",
                                       "clash", "harmony", "punish", "harm", "broken",
                                       "both_clash_harmony", "include_delisted",
                                       "include_index", "q", "layer", "window_no")}
            return api.opportunities(con, oflt, ds)
        if path == "/api/relation_types":
            return api.relation_types(con, g("id"), g("record_kind", "OFFICIAL"))
        if path == "/api/relation_pillars":
            return api.relation_pillars(con, g("id"), g("record_kind", "OFFICIAL"), g("type"))
        if path == "/api/relation_months":
            return api.relation_months(con, g("id"), g("record_kind", "OFFICIAL"), g("type"),
                                       g("pillar"))
        if path == "/api/month_detail":
            return api.month_detail(con, g("id"), g("month"))
        if path == "/api/ten_god_stats":
            return api.ten_god_stats(con, g("id"), g("sample_mode", "ALL") or "ALL",
                                     g("flow_branch"), g("relation_type"))
        if path == "/api/ten_god_months":
            return api.ten_god_months(con, g("id"), g("ten_god"),
                                      g("sample_mode", "ALL") or "ALL",
                                      g("flow_branch"), g("relation_type"))
        if path == "/api/market_relation_types":
            return api.market_relation_types(con)
        if path == "/api/db_status":
            # 首页那一行：全部字段已是带 +08:00 的北京串，前端不做时区换算
            return update_scheduler.db_status(con=con, dataset=ds)
        if path == "/api/update_center/status":
            # 客户机日更：只看本机更新器的状态文件/日志，不联网、不读写数据库
            return update_center.status()

        if path == "/api/db_update/state":
            # 含状态文件 + 计划 + 锁 + 日志尾部最后 40 行（run_id 缺省时取当前/最近一轮）
            return update_scheduler.update_state_payload(g("run_id") or None)
        if path == "/api/catalog":
            return api.catalog(con)
        if path == "/api/sources":
            return api.sources(con)
        if path.startswith("/api/dayun/"):
            return dayun_api.dispatch(con, path, g, ds)
        return {"error": f"未知接口 {path}"}
    finally:
        con.close()


MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8"}

BJT = timezone(timedelta(hours=8))

# ---------------- 客户机数据更新通道 ----------------
# 只读下载：/api/update/manifest（最新包元信息）、/api/update/package（包本体）、
# /api/update/ping（连通性自检）。走独立固定 token，不借用登录会话，因此放在
# 未认证拦截之前——客户机不需要、也拿不到网页登录密码。
# 包由 scripts/build_update_package.py 在日更成功后生成，latest.json 每请求现读，
# 换包不需要重启服务。
_UPDATE_PKG_RE = re.compile(r"^update_\d{8}\.zip$")


def _update_dir() -> Path:
    return Path(os.environ.get(
        "BAZI_UPDATE_DIR",
        str(Path(__file__).resolve().parents[2] / "exports" / "update_pkgs"))).resolve()


def _update_token_file() -> Path:
    return Path(os.environ.get(
        "BAZI_UPDATE_TOKEN_FILE", str(_update_dir() / ".token"))).resolve()


def _update_token() -> str:
    try:
        return _update_token_file().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _latest_manifest() -> dict | None:
    try:
        return json.loads((_update_dir() / "latest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write(f"[{datetime.now().strftime('%H:%M:%S')}] {self.address_string()} - {fmt % args}\n")
        sys.stderr.flush()

    def _send(self, code: int, body: bytes, ctype: str, cache_control: str = "no-store",
              extra_headers: list[tuple[str, str]] | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        if extra_headers:
            for k, v in extra_headers:
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, target_url: str, extra_headers: list[tuple[str, str]] | None = None):
        self.send_response(302)
        self.send_header("Location", target_url)
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        if extra_headers:
            for k, v in extra_headers:
                self.send_header(k, v)
        self.end_headers()

    def _send_file(self, path: Path, extra: dict | None = None) -> None:
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        for k, v in (extra or {}).items():
            if v:
                self.send_header(k, v)
        self.end_headers()
        try:
            with path.open("rb") as f:
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle_update(self, path: str, parsed) -> bool:
        """客户机更新通道。返回 True 表示已处理，调用方不再往下走。"""
        want = _update_token()
        if not want:
            return self._reject_update(503, "UPDATE_CHANNEL_NOT_CONFIGURED",
                                       "更新通道未配置：缺少 token 文件")
        qs = parse_qs(parsed.query)
        got = (self.headers.get("X-Update-Token") or (qs.get("token") or [""])[0]).strip()
        if not got or not hmac.compare_digest(got, want):
            return self._reject_update(401, "UNAUTHORIZED_UPDATE_TOKEN",
                                       "更新口令不正确")
        man = _latest_manifest()
        if path == "/api/update/ping":
            body = {"ok": True, "date": (man or {}).get("date"),
                    "file": (man or {}).get("file"),
                    "server_bjt": datetime.now(BJT).strftime("%F %T")}
            self._send(200, json.dumps(body, ensure_ascii=False).encode("utf-8"),
                       MIME[".json"])
            return True
        if man is None:
            return self._reject_update(503, "UPDATE_MANIFEST_MISSING",
                                       "还没有任何更新包（latest.json 不存在）")
        if path == "/api/update/manifest":
            self._send(200, json.dumps(man, ensure_ascii=False).encode("utf-8"),
                       MIME[".json"], cache_control="no-store")
            return True
        name = str(man.get("file") or "")
        if not _UPDATE_PKG_RE.match(name):
            return self._reject_update(500, "UPDATE_MANIFEST_INVALID_FILE",
                                       f"latest.json 里的文件名不合法：{name!r}")
        base = _update_dir()
        pkg = (base / name).resolve()
        if not pkg.is_relative_to(base) or not pkg.is_file():
            return self._reject_update(404, "UPDATE_PACKAGE_NOT_FOUND",
                                       f"包不存在：{name}")
        self._send_file(pkg, extra={"X-Package-Sha256": str(man.get("sha256") or "")})
        return True

    def _reject_update(self, code: int, err: str, message: str) -> bool:
        self._send(code, json.dumps({"error": err, "message": message},
                                    ensure_ascii=False).encode("utf-8"), MIME[".json"])
        return True

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        client_ip = auth.get_client_ip(self)
        user_agent = self.headers.get("User-Agent") or ""
        token = auth.extract_session_token(self.headers)
        is_authed = auth.session_manager.validate_session(token, client_ip, user_agent)

        # 1. 安全退出路由：销毁会话、清除 Cookie 并重定向至登录页
        if path in ("/logout", "/api/auth/logout"):
            auth.session_manager.destroy_session(token)
            return self._redirect("/login", [("Set-Cookie", auth.make_clear_cookie_header())])

        # 2. 会话状态探测接口（公开，供前端感知登录状态与锁定倒计时）
        if path == "/api/auth/status":
            is_allowed, rem_attempts, lock_secs = auth.rate_limiter.check_ip(client_ip)
            payload = {
                "authenticated": is_authed,
                "locked": not is_allowed,
                "lockout_seconds": lock_secs,
                "remaining_attempts": rem_attempts,
            }
            return self._send(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                              MIME[".json"])

        # 3. 登录页入口
        if path in ("/login", "/login.html"):
            if is_authed:
                qs = parse_qs(parsed.query)
                target = qs.get("redirect", ["/"])[0] or "/"
                return self._redirect(target)
            page = STATIC / "login.html"
            if not page.is_file():
                return self._send(404, b"login page not found", "text/plain")
            return self._send(200, page.read_bytes(), MIME[".html"], cache_control="no-cache")

        # 4. 登录页公开依赖静态资源（如 tailwindcss 脚本与图标）
        if path == "/static/tailwindcss.min.js":
            f = STATIC / "tailwindcss.min.js"
            if f.is_file():
                return self._send(200, f.read_bytes(), MIME[".js"], cache_control="public, max-age=86400")
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")

        # 4.6) 客户机数据更新通道：独立固定 token，不需要网页登录会话。
        #      必须放在下面的 fail-closed 拦截之前，否则客户机会被跳去登录页。
        if path in ("/api/update/manifest", "/api/update/package", "/api/update/ping"):
            return self._handle_update(path, parsed)

        # 5. 未认证拦截：Fail-Closed 架构，杜绝任何未授权越权访问
        if not is_authed:
            if path.startswith("/api/"):
                return self._send(401, json.dumps({
                    "error": "UNAUTHORIZED",
                    "message": "未登录或登录已失效，请重新登录"
                }, ensure_ascii=False).encode("utf-8"), MIME[".json"])
            # 访问 HTML 页面或其它受限页面统一重定向到登录页并记录跳转源
            target_url = f"/login?redirect={quote(self.path)}"
            return self._redirect(target_url)

        # --- 以下为已验证通过的业务路由 ---
        if path in ("/", "/index.html"):
            return self._send(200, (STATIC / "index.html").read_bytes(),
                              MIME[".html"], cache_control="no-cache")
        if path in ("/dayun", "/dayun.html"):
            page = STATIC / "dayun.html"
            if not page.is_file():
                return self._send(404, b"dayun page not found", "text/plain")
            return self._send(200, page.read_bytes(), MIME[".html"], cache_control="no-cache")
        if path in ("/seg", "/seg.html"):
            page = STATIC / "seg.html"
            if not page.is_file():
                return self._send(404, b"seg page not found", "text/plain")
            return self._send(200, page.read_bytes(), MIME[".html"], cache_control="no-cache")
        if path in ("/today", "/today.html"):
            # 页面内的 style.css/today.js 是相对路径，统一落到 /static/ 下
            return self._redirect("/static/today.html")
        if path in ("/n2", "/n2.html"):
            # 流日关系影子库预览页：同样走 /static/，复用现有 style.css
            return self._redirect("/static/n2.html")
        if path.startswith("/static/"):
            # 只允许读 static/ 目录内的文件：绝对路径、.. 与符号链接逃逸一律 404
            root = STATIC.resolve()
            f = (root / path[len("/static/"):].lstrip("/")).resolve()
            if not f.is_relative_to(root) or not f.is_file():
                return self._send(404, b"not found", "text/plain")
            # 静态资源缓存策略：第三方无状态核心库允许客户端缓存（如 tailwindcss、xlsx），业务代码开发与热更采用 no-cache，避免改动后用户普通刷新仍命中旧缓存
            cache_hdr = "public, max-age=86400" if ("tailwindcss" in f.name or "xlsx.full" in f.name) else "no-cache"
            return self._send(200, f.read_bytes(),
                              MIME.get(f.suffix, "application/octet-stream"),
                              cache_control=cache_hdr)
        if path.startswith("/api/v5/"):
            # V5 只读走标准库 sqlite3 + 只读切片，不打开任何生产库，也不建可写连接
            try:
                out = v5_api.dispatch(path, parse_qs(parsed.query))
                code, body = 200, json.dumps(out, ensure_ascii=False, default=str)
            except v5_api.V5Unavailable as exc:
                code, body = 503, json.dumps({"error": str(exc), "v5_available": False},
                                             ensure_ascii=False)
            except v5_api.V5Error as exc:
                code, body = 400, json.dumps({"error": str(exc)}, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                code, body = 500, json.dumps(
                    {"error": "V5 查询出错，完整错误见启动窗口"}, ensure_ascii=False)
            return self._send(code, body.encode("utf-8"), MIME[".json"])
        if path.startswith("/api/seg/"):
            # 100%段：只读 Parquet + DuckDB，不打开也不写任何 SQLite 业务库
            try:
                if path == "/api/seg/export":
                    data, filename = seg_api.export_csv(parse_qs(parsed.query))
                    return self._send(
                        200, data, "text/csv; charset=utf-8",
                        extra_headers=[("Content-Disposition",
                                        f'attachment; filename="{filename}"')])
                out = seg_api.dispatch(path, parse_qs(parsed.query))
                code = 200
                body = json.dumps(out, ensure_ascii=False, default=str)
            except seg_api.SegUnavailable as exc:
                code, body = 503, json.dumps({"error": str(exc)}, ensure_ascii=False)
            except seg_api.SegError as exc:
                code, body = 400, json.dumps({"error": str(exc)}, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                code, body = 500, json.dumps(
                    {"error": "100%段查询出错，完整错误见启动窗口"}, ensure_ascii=False)
            return self._send(code, body.encode("utf-8"), MIME[".json"])
        if path.startswith("/api/n2/"):
            # 流日关系影子库：只读 sqlite（mode=ro + query_only），不写任何文件
            try:
                out = n2_api.dispatch(path, parse_qs(parsed.query))
                code, body = 200, json.dumps(out, ensure_ascii=False, default=str)
            except n2_api.N2Unavailable as exc:
                code, body = 503, json.dumps({"error": str(exc)}, ensure_ascii=False)
            except n2_api.N2Error as exc:
                code, body = 400, json.dumps({"error": str(exc)}, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                code, body = 500, json.dumps(
                    {"error": "流日关系查询出错，完整错误见启动窗口"}, ensure_ascii=False)
            return self._send(code, body.encode("utf-8"), MIME[".json"])
        if path.startswith("/api/today/"):
            # 今日关注：只读 today_watch_v1 下的 JSON/CSV，不打开任何业务库
            try:
                if path == "/api/today/export":
                    data, filename = today_api.export_csv(parse_qs(parsed.query))
                    return self._send(
                        200, data, "text/csv; charset=utf-8",
                        extra_headers=[("Content-Disposition",
                                        f'attachment; filename="{filename}"')])
                out = today_api.dispatch(path, parse_qs(parsed.query))
                code = 200
                body = json.dumps(out, ensure_ascii=False, default=str)
            except today_api.TodayUnavailable as exc:
                code, body = 503, json.dumps({"error": str(exc)}, ensure_ascii=False)
            except today_api.TodayError as exc:
                code, body = 400, json.dumps({"error": str(exc)}, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                code, body = 500, json.dumps(
                    {"error": "今日关注查询出错，完整错误见启动窗口"}, ensure_ascii=False)
            return self._send(code, body.encode("utf-8"), MIME[".json"])
        if path.startswith("/api/"):
            try:
                out = dispatch(path, parse_qs(parsed.query))
                code, body = 200, json.dumps(out, ensure_ascii=False, default=str)
            except Exception as exc:  # noqa: BLE001
                # 异常文本常含本机绝对路径：打给启动窗口看，不回浏览器（页面仍不是空白）
                traceback.print_exc()
                code, body = 500, json.dumps(
                    {"error": f"{type(exc).__name__}：查询出错，完整错误见启动窗口"},
                    ensure_ascii=False)
            return self._send(code, body.encode("utf-8"), MIME[".json"])
        return self._send(404, b"not found", "text/plain")

    def _json_body(self) -> dict | None:
        """读 POST 的 JSON body；不合法/超大一律 None（超大不读，避免被灌满内存）。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return None
        if length <= 0 or length > 65536:
            return None
        try:
            raw = self.rfile.read(length)
            obj = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        return obj if isinstance(obj, dict) else None

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        client_ip = auth.get_client_ip(self)
        user_agent = self.headers.get("User-Agent") or ""
        token = auth.extract_session_token(self.headers)
        is_authed = auth.session_manager.validate_session(token, client_ip, user_agent)

        # 1. 登录验证接口（公开，带防爆破速率限制、OWASP 60万轮 PBKDF2 与恒定时间防时序攻击）
        if path == "/api/auth/login":
            is_allowed, rem_attempts, lock_secs = auth.rate_limiter.check_ip(client_ip)
            if not is_allowed:
                return self._send(429, json.dumps({
                    "success": False,
                    "error": "RATE_LIMITED",
                    "locked": True,
                    "lockout_seconds": lock_secs,
                    "message": f"密码错误过多，系统已锁定，请在 {lock_secs} 秒后再试"
                }, ensure_ascii=False).encode("utf-8"), MIME[".json"])

            body = self._json_body()
            if not body or not body.get("password"):
                return self._send(400, json.dumps({
                    "success": False,
                    "error": "MISSING_PASSWORD",
                    "message": "请输入系统访问密码"
                }, ensure_ascii=False).encode("utf-8"), MIME[".json"])

            input_pwd = str(body["password"])
            if not auth.verify_password(input_pwd):
                is_locked, remaining, lockout_remaining, penalty_delay = auth.rate_limiter.record_failure(client_ip)
                if penalty_delay > 0:
                    import time
                    time.sleep(penalty_delay)
                if is_locked:
                    return self._send(429, json.dumps({
                        "success": False,
                        "error": "RATE_LIMITED",
                        "locked": True,
                        "lockout_seconds": lockout_remaining,
                        "message": "连续 5 次密码错误，系统锁定 15 分钟"
                    }, ensure_ascii=False).encode("utf-8"), MIME[".json"])
                return self._send(401, json.dumps({
                    "success": False,
                    "error": "INVALID_PASSWORD",
                    "remaining_attempts": remaining,
                    "message": f"密码错误，还可尝试 {remaining} 次"
                }, ensure_ascii=False).encode("utf-8"), MIME[".json"])

            # 验证通过：重置失败计数，派发高熵会话凭证（HttpOnly + SameSite）
            auth.rate_limiter.record_success(client_ip)
            new_token = auth.session_manager.create_session(client_ip, user_agent)
            cookie_hdr = auth.make_set_cookie_header(new_token)
            return self._send(200, json.dumps({
                "success": True,
                "message": "登录成功"
            }, ensure_ascii=False).encode("utf-8"), MIME[".json"],
            extra_headers=[("Set-Cookie", cookie_hdr)])

        # 2. 其它所有 POST 接口均须持有有效登录凭证
        if not is_authed:
            return self._send(401, json.dumps({
                "error": "UNAUTHORIZED",
                "message": "未登录或登录已失效，请重新登录"
            }, ensure_ascii=False).encode("utf-8"), MIME[".json"])

        # 3. 退出登录 POST 路由
        if path == "/api/auth/logout":
            auth.session_manager.destroy_session(token)
            return self._send(200, json.dumps({"success": True}, ensure_ascii=False).encode("utf-8"),
                              MIME[".json"], extra_headers=[("Set-Cookie", auth.make_clear_cookie_header())])

        # 4. 修改密码接口
        if path == "/api/auth/change_password":
            body = self._json_body()
            if not body:
                return self._send(400, json.dumps({"success": False, "error": "请求参数不合法"}, ensure_ascii=False).encode("utf-8"), MIME[".json"])
            old_pwd = str(body.get("old_password") or "")
            new_pwd = str(body.get("new_password") or "")
            if not auth.verify_password(old_pwd):
                return self._send(400, json.dumps({"success": False, "error": "原密码验证失败，请重新确认"}, ensure_ascii=False).encode("utf-8"), MIME[".json"])
            if len(new_pwd) < 6:
                return self._send(400, json.dumps({"success": False, "error": "新密码长度不能少于 6 位"}, ensure_ascii=False).encode("utf-8"), MIME[".json"])
            if old_pwd == new_pwd:
                return self._send(400, json.dumps({"success": False, "error": "新密码不能与原密码相同"}, ensure_ascii=False).encode("utf-8"), MIME[".json"])

            try:
                auth.update_password(new_pwd)
                # 重新为当前请求者生成新会话凭证，避免登出
                new_token = auth.session_manager.create_session(client_ip, user_agent)
                cookie_hdr = auth.make_set_cookie_header(new_token)
                return self._send(200, json.dumps({
                    "success": True,
                    "message": "密码修改成功，其他在线设备已全部安全退出"
                }, ensure_ascii=False).encode("utf-8"), MIME[".json"],
                extra_headers=[("Set-Cookie", cookie_hdr)])
            except Exception as exc:
                return self._send(500, json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"), MIME[".json"])

        # 5. 客户机日更：拉起更新器，立即返回；结果由 GET /api/update_center/status 轮询
        if path == "/api/update_center/daily":
            try:
                code, payload = update_center.start_daily()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                return self._send(500, json.dumps(
                    {"error": "DAILY_UPDATE_FAILED",
                     "message": "日更未发起，完整错误见启动窗口"},
                    ensure_ascii=False).encode("utf-8"), MIME[".json"])
            if code == "STARTED":
                return self._send(202, json.dumps(payload, ensure_ascii=False)
                                  .encode("utf-8"), MIME[".json"])
            status = 409 if code in ("ALREADY_RUNNING", "NOT_INSTALLED",
                                     "UPDATER_OLD") else 500
            return self._send(status, json.dumps(payload, ensure_ascii=False)
                              .encode("utf-8"), MIME[".json"])

        # 6. 现存更新接口（受鉴权保护）
        if path == "/api/db_update/preview":
            try:
                out = update_scheduler.preview_update()
                body = json.dumps(out, ensure_ascii=False, default=str).encode("utf-8")
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                body = json.dumps({"error": "PREVIEW_FAILED",
                                   "message": "预览未完成，完整错误见启动窗口"},
                                  ensure_ascii=False).encode("utf-8")
                return self._send(500, body, MIME[".json"])
            return self._send(200, body, MIME[".json"])
        if path != "/api/db_update/start":
            return self._send(404, json.dumps({"error": "未知接口"}, ensure_ascii=False)
                              .encode("utf-8"), MIME[".json"])
        body = self._json_body()
        if body is None or body.get("confirm") is not True:
            return self._send(400, json.dumps({"error": "INVALID_CONFIRM"},
                                              ensure_ascii=False).encode("utf-8"),
                              MIME[".json"])
        try:
            port = self.server.server_address[1]
        except (AttributeError, IndexError, TypeError):
            port = None
        try:
            code, payload = update_scheduler.manual_start(
                port=port, preview_token=body.get("preview_token"))
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return self._send(500, json.dumps(
                {"error": "手动触发未完成，完整错误见启动窗口"},
                ensure_ascii=False).encode("utf-8"), MIME[".json"])
        if code == "NO_PREVIEW_TOKEN":
            return self._send(400, json.dumps(
                {"error": "NO_PREVIEW_TOKEN",
                 "message": "请先调用 POST /api/db_update/preview 取得 preview_token"},
                ensure_ascii=False).encode("utf-8"), MIME[".json"])
        if code == "PREVIEW_STALE":
            return self._send(401, json.dumps(
                {"error": "PREVIEW_STALE",
                 "message": "预览结论已过期或与当前状态不一致，请重新检查"
                            "（重新调用 /api/db_update/preview）"},
                ensure_ascii=False).encode("utf-8"), MIME[".json"])
        status = {"STARTED": 202, "HELD": 409, "DISABLED": 503}.get(code, 500)
        if status == 202:
            return self._send(202, json.dumps(payload, ensure_ascii=False)
                              .encode("utf-8"), MIME[".json"])
        if status == 409:
            return self._send(409, json.dumps(
                {"error": "ALREADY_RUNNING", "run_id": payload.get("run_id")},
                ensure_ascii=False).encode("utf-8"), MIME[".json"])
        if status == 503:
            return self._send(503, json.dumps({"error": "DISABLED"},
                                              ensure_ascii=False).encode("utf-8"),
                              MIME[".json"])
        return self._send(500, json.dumps(
            {"error": str(payload.get("error") or "LAUNCH_FAILED"),
             "run_id": payload.get("run_id")}, ensure_ascii=False).encode("utf-8"),
            MIME[".json"])


def free_port(host: str) -> int:
    s = socket.socket()
    s.bind((host, 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--set-password", nargs="?", const="", default=None,
                    help="设置或重置系统访问密码；可直接指定密码或交互式输入")
    args = ap.parse_args()

    # 若指定了 --set-password，则直接更新密码后退出
    if args.set_password is not None:
        new_pwd = args.set_password.strip()
        if not new_pwd:
            import getpass
            try:
                new_pwd = getpass.getpass("请输入新的访问密码（至少6位）：").strip()
                confirm = getpass.getpass("请再次输入确认密码：").strip()
                if new_pwd != confirm:
                    raise SystemExit("两次输入的密码不一致，操作中止。")
            except (KeyboardInterrupt, EOFError):
                raise SystemExit("\n操作已取消。")
        if len(new_pwd) < 6:
            raise SystemExit("密码长度不能少于 6 位。")
        auth.update_password(new_pwd)
        print("✅ 访问密码设置成功！(PBKDF2-HMAC-SHA256, 600,000 轮)")
        return

    # fail-closed：数据库缺失或十神规则与冻结映射不一致时拒绝启动，不带着错规则上线
    import config
    import ten_god_rules
    problems = config.check_databases()
    if problems:
        raise SystemExit("启动中止：\n  " + "\n  ".join(problems))
    mismatches = ten_god_rules.reference_mismatches()
    if mismatches:
        raise SystemExit("启动中止：十神推导与冻结映射不一致\n  " + "\n  ".join(mismatches[:5]))
    # V5 只读接入是软自检：不可用时原六页签照常工作，本栏目显示「尚未接通」而不是伪造数据
    try:
        st = v5_api.status()
        if not st.get("available"):
            raise RuntimeError(st.get("error") or "V5 状态不可用")
        v5_line = (f"V5 低点研究 {v5_api.RESEARCH_VERSION} 只读就绪 "
                   f"({st['driver']}, 切片 {st['slice_bytes']/1e6:.0f} MB, "
                   f"月事实 {st['row_counts']['v5_months']:,} 行, "
                   f"query_only={st['query_only']})")
    except Exception as exc:  # noqa: BLE001
        v5_line = f"V5 低点研究未接通：{type(exc).__name__}: {exc}"

    host = "127.0.0.1"
    port = args.port or free_port(host)
    url = f"http://127.0.0.1:{port}/"
    try:
        srv = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        raise SystemExit(f"启动失败：无法绑定 {host}:{port}（{exc}）。"
                         "请先确认端口未被占用，或改用 --port 0 自动选择空闲端口。")
    print("=" * 72)
    print(f"八字历史规律本地查询  ->  {url}")
    print("🔒 高安全身份认证已启用：PBKDF2-HMAC-SHA256 (600,000轮) · 防爆破锁定机制")
    sec_cfg = auth.init_security_config()
    if sec_cfg.get("initial_password_warning"):
        print("🔑 当前生效的是首次初始化口令（导入时已打印一次，或由 BAZI_AUTH_PASSWORD 指定）")
        print("💡 建议首次登录后点击右上角「修改密码」，或运行: python3 set_password.py")
    else:
        print("🔑 访问密码：已由管理员配置（若遗忘可运行 python3 set_password.py 重置）")
    print("=" * 72)
    print("只绑定 127.0.0.1，数据全部来自本机 SQLite，不访问互联网/NAS/云服务器。")
    for key, spec in api.DATASETS.items():
        print(f"  {key:6s} {spec['label']:>3s}  {spec['db']}")
    print("页头可切换 A股 / ETF；ETF 统计缺失时切过去会给出明确提示。")
    print(f"十神规则 {ten_god_rules.RULE_VERSION} · 藏干 {ten_god_rules.HIDDEN_RULE_VERSION}"
          f" · 100 格冻结映射校验 {len(mismatches)} 处不一致")
    print("数据库只读：mode=ro + PRAGMA query_only=ON；本副本不含 SQLite 文件。")
    print(v5_line)
    print("按 Ctrl+C 或关闭本终端窗口即可停止服务。")
    # 预览/演练实例（BAZI_NO_JOBS=1）：不启动任何会写盘或访问 NAS 的后台线程，
    # 避免第二个实例与 8737 抢调度锁、抢日更目录。
    if os.environ.get("BAZI_NO_JOBS") == "1":
        print("预览模式（BAZI_NO_JOBS=1）：跳过节气月自动更新与当月层日更线程。")
    else:
        # 节气月自动更新调度线程：只在 serve_forever 之前起这一次，daemon=True，
        # 整条循环自带 try/except（异常只进状态/日志，绝不拖垮请求面）。防重复触发靠文件锁。
        try:
            sched = update_scheduler.start(port=port, root=Path(__file__).resolve().parents[1])
            print(f"节气月自动更新调度：已启动（port={sched['port']}, "
                  f"started_at_bjt={sched['started_at_bjt']}）；本轮不跑任何流水线，拉起默认 --dry-run。")
        except Exception as exc:  # noqa: BLE001
            print(f"[提示] 自动更新调度线程未起来，查询服务不受影响："
                  f"{type(exc).__name__}: {exc}")
        # 当月层日更：NAS 17:00 跑链（约 18:10 齐），这里 19:05 起最多 4 次尝试
        try:
            import daily_live_job
            dl = daily_live_job.start(root=Path(__file__).resolve().parents[1])
            if dl.get("started"):
                print(f"当月层日更任务：已启动（{' / '.join(dl.get('slots', []))} 四档重试）")
            else:
                print(f"当月层日更任务：未启动（{dl.get('reason')}）")
        except Exception as exc:  # noqa: BLE001
            print(f"[提示] 当月层日更任务未启动，查询服务不受影响："
                  f"{type(exc).__name__}: {exc}")
            try:  # 同步写一份到数据目录，便于事后排查（启动终端会滚走）
                from datetime import datetime, timedelta, timezone as _tz
                _p = Path(__file__).resolve().parents[1] / "data" / "live_daily.log"
                _p.parent.mkdir(parents=True, exist_ok=True)
                with _p.open("a", encoding="utf-8") as _f:
                    _f.write(f"[{datetime.now(_tz(timedelta(hours=8))).strftime('%F %T')}] "
                             f"server: 日更任务启动异常 {type(exc).__name__}: {exc}\n")
            except Exception:  # noqa: BLE001
                pass
        # 客户机数据更新巡检：脚本随仓库走，配置在查询包根目录；发布端与旧版根脚本一律不启动。
        try:
            uc = update_center.start_auto_loop()
            print(f"客户机数据更新：{'已启动' if uc.get('started') else '未启动'}"
                  f"（{uc.get('reason')}）")
        except Exception as exc:  # noqa: BLE001
            print(f"[提示] 客户机数据更新巡检未启动，查询服务不受影响："
                  f"{type(exc).__name__}: {exc}")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
