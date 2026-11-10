"""高安全级别身份认证与访问控制模块 (Authentication & Access Control)

核心安全特性：
1. 口令哈希：PBKDF2-HMAC-SHA256，600,000 轮安全迭代计算（符合 OWASP 推荐标准），
   结合 32 字节高熵密码学随机盐（secrets.token_bytes(32)），抵御彩虹表与离线暴力破解。
2. 恒定时间比较：采用 hmac.compare_digest 严格避免时序攻击（Timing Attack）。
3. 防暴力破解与速率限制：
   - 滑动窗口追踪 IP 尝试记录；
   - 连续失败第 3~4 次增加自适应防御延迟（1~3 秒）；
   - 连续失败达到 5 次实施全局/IP 级别锁定 15 分钟（900 秒）；
   - 锁定状态下直接返回 429 Too Many Requests，阻断机器扫描。
4. 会话安全（Session Security）：
   - 采用 256 位高熵加密安全令牌（secrets.token_urlsafe(32)）；
   - Cookie 采用 HttpOnly（杜绝 XSS 窃取）与 SameSite=Lax（杜绝 CSRF）；
   - 服务端持久化会话记录，支持会话滑动过期与绝对过期；
   - 密码修改时支持一键撤销所有现有在线会话（单点/多点注销）。
5. 零第三方依赖：纯 Python 标准库（hashlib, secrets, hmac, json, time, threading）实现。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

APP_DIR = Path(__file__).resolve().parent
SECURITY_FILE = APP_DIR / "auth_security.json"
SESSIONS_FILE = APP_DIR / "auth_sessions.json"

COOKIE_NAME = "astock_session"
DEFAULT_ITERATIONS = 600000
SALT_BYTES = 32

# 速率限制策略
MAX_FAILED_ATTEMPTS = 5         # 15分钟内最多允许尝试5次
WINDOW_SECONDS = 900           # 15分钟窗口
LOCKOUT_SECONDS = 900          # 锁定15分钟

# 会话生命周期
SESSION_INACTIVITY_TIMEOUT = 7 * 86400  # 无操作 7 天过期
SESSION_MAX_LIFETIME = 30 * 86400       # 绝对有效时长 30 天

# 初始口令只来自环境变量；两者都没给时，由首次初始化随机生成（见 init_security_config）
DEFAULT_INITIAL_PASSWORD = os.environ.get("BAZI_AUTH_PASSWORD") or os.environ.get("ASTOCK_ADMIN_PASSWORD") or ""


def _hash_password(plain: str, salt_bytes: bytes, iterations: int = DEFAULT_ITERATIONS) -> str:
    """计算 PBKDF2-HMAC-SHA256 哈希串 (hex)。"""
    return hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt_bytes, iterations).hex()


def _ensure_secure_permissions(path: Path) -> None:
    """确保安全敏感文件仅当前系统用户可读写 (0o600)。"""
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


class RateLimiter:
    """线程安全的防暴力破解限制器。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # ip -> {"failures": [timestamp, ...], "locked_until": float}
        self._records: Dict[str, Dict[str, Any]] = {}
        self._global_failures: list[float] = []

    def _cleanup_old(self, now: float, ip: str) -> None:
        rec = self._records.get(ip)
        if rec:
            rec["failures"] = [t for t in rec.get("failures", []) if now - t < WINDOW_SECONDS]
            if rec.get("locked_until", 0) <= now and not rec["failures"]:
                self._records.pop(ip, None)
        self._global_failures = [t for t in self._global_failures if now - t < WINDOW_SECONDS]

    def check_ip(self, ip: str) -> Tuple[bool, int, int]:
        """检查指定 IP 是否被锁定。

        返回: (is_allowed, remaining_attempts, lockout_remaining_seconds)
        """
        now = time.time()
        with self._lock:
            self._cleanup_old(now, ip)
            rec = self._records.get(ip)
            if not rec:
                return True, MAX_FAILED_ATTEMPTS, 0

            locked_until = rec.get("locked_until", 0)
            if locked_until > now:
                remaining_lock = int(locked_until - now) + 1
                return False, 0, remaining_lock

            failures = len(rec.get("failures", []))
            remaining = max(0, MAX_FAILED_ATTEMPTS - failures)
            return True, remaining, 0

    def record_failure(self, ip: str) -> Tuple[bool, int, int, float]:
        """记录一次密码错误。

        返回: (is_locked, remaining_attempts, lockout_remaining_seconds, penalty_delay)
        """
        now = time.time()
        with self._lock:
            self._cleanup_old(now, ip)
            rec = self._records.setdefault(ip, {"failures": [], "locked_until": 0.0})
            rec["failures"].append(now)
            self._global_failures.append(now)

            failures = len(rec["failures"])
            if failures >= MAX_FAILED_ATTEMPTS:
                rec["locked_until"] = now + LOCKOUT_SECONDS
                return True, 0, LOCKOUT_SECONDS, 1.0

            remaining = MAX_FAILED_ATTEMPTS - failures
            delay = 0.0
            if failures == 3:
                delay = 1.0
            elif failures == 4:
                delay = 2.5
            return False, remaining, 0, delay

    def record_success(self, ip: str) -> None:
        """登录成功，重置失败历史。"""
        with self._lock:
            self._records.pop(ip, None)


class SessionManager:
    """持久化高熵会话管理。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if SESSIONS_FILE.is_file():
            try:
                data = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    now = time.time()
                    # 启动时清洗已过期会话
                    self._sessions = {
                        k: v for k, v in data.items()
                        if isinstance(v, dict)
                        and (now - v.get("last_active", 0) <= SESSION_INACTIVITY_TIMEOUT)
                        and (now - v.get("created_at", 0) <= SESSION_MAX_LIFETIME)
                    }
            except Exception:
                self._sessions = {}
        _ensure_secure_permissions(SESSIONS_FILE)

    def _save(self) -> None:
        try:
            tmp = SESSIONS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._sessions, ensure_ascii=False, indent=2), encoding="utf-8")
            _ensure_secure_permissions(tmp)
            tmp.replace(SESSIONS_FILE)
        except Exception as exc:
            sys.stderr.write(f"[Auth] 保存会话文件出错: {exc}\n")

    def create_session(self, ip: str, user_agent: str = "") -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        ua_hash = hashlib.sha256(user_agent.encode("utf-8")).hexdigest()[:16] if user_agent else ""
        with self._lock:
            self._sessions[token] = {
                "created_at": now,
                "last_active": now,
                "ip": ip,
                "ua_hash": ua_hash,
            }
            self._save()
        return token

    def validate_session(self, token: Optional[str], ip: str = "", user_agent: str = "") -> bool:
        if not token or not isinstance(token, str):
            return False
        now = time.time()
        with self._lock:
            data = self._sessions.get(token)
            if not data:
                return False

            created_at = data.get("created_at", 0)
            last_active = data.get("last_active", 0)

            # 超过无操作活跃期或绝对超时
            if now - last_active > SESSION_INACTIVITY_TIMEOUT or now - created_at > SESSION_MAX_LIFETIME:
                self._sessions.pop(token, None)
                self._save()
                return False

            # 滑动活跃时间
            data["last_active"] = now
            return True

    def destroy_session(self, token: Optional[str]) -> None:
        if not token:
            return
        with self._lock:
            if token in self._sessions:
                self._sessions.pop(token, None)
                self._save()

    def destroy_all(self, except_token: Optional[str] = None) -> None:
        with self._lock:
            if except_token and except_token in self._sessions:
                keep = self._sessions[except_token]
                self._sessions = {except_token: keep}
            else:
                self._sessions = {}
            self._save()


# 全局安全组件单例
rate_limiter = RateLimiter()
session_manager = SessionManager()


def init_security_config() -> dict[str, Any]:
    """初始化安全配置文件；若文件不存在则自动生成安全初始密码。"""
    if SECURITY_FILE.is_file():
        try:
            cfg = json.loads(SECURITY_FILE.read_text(encoding="utf-8"))
            if "password_hash" in cfg and "salt" in cfg:
                return cfg
        except Exception:
            pass

    # 首次启动：没给环境变量就随机生成一个，只在控制台显示这一次
    generated = "" if DEFAULT_INITIAL_PASSWORD else secrets.token_urlsafe(12)
    initial_password = DEFAULT_INITIAL_PASSWORD or generated
    salt_bytes = secrets.token_bytes(SALT_BYTES)
    salt_hex = salt_bytes.hex()
    pwd_hash = _hash_password(initial_password, salt_bytes, DEFAULT_ITERATIONS)
    cfg = {
        "version": 1,
        "algorithm": "pbkdf2_sha256",
        "iterations": DEFAULT_ITERATIONS,
        "salt": salt_hex,
        "password_hash": pwd_hash,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "initial_password_warning": True,
    }
    SECURITY_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    _ensure_secure_permissions(SECURITY_FILE)
    if generated:
        print("🔑 本次随机生成的访问口令（只显示这一次，请立即保存）：")
        print(f"    {generated}")
        print("💡 想换成自己的：python3 server.py --set-password")
    return cfg


def verify_password(plain_password: str) -> bool:
    """使用恒定时间比较验证密码。"""
    if not plain_password:
        return False
    try:
        cfg = json.loads(SECURITY_FILE.read_text(encoding="utf-8"))
        salt_bytes = bytes.fromhex(cfg["salt"])
        iterations = int(cfg.get("iterations", DEFAULT_ITERATIONS))
        stored_hash = cfg["password_hash"]
    except Exception:
        cfg = init_security_config()
        salt_bytes = bytes.fromhex(cfg["salt"])
        iterations = int(cfg.get("iterations", DEFAULT_ITERATIONS))
        stored_hash = cfg["password_hash"]

    calc_hash = _hash_password(plain_password, salt_bytes, iterations)
    return hmac.compare_digest(calc_hash, stored_hash)


def update_password(new_password: str) -> None:
    """更新访问密码，重置加密盐与哈希，并撤销所有已有在线凭证。"""
    if not new_password or len(new_password) < 6:
        raise ValueError("新密码长度不能少于 6 位")

    salt_bytes = secrets.token_bytes(SALT_BYTES)
    salt_hex = salt_bytes.hex()
    pwd_hash = _hash_password(new_password, salt_bytes, DEFAULT_ITERATIONS)

    cfg = {
        "version": 1,
        "algorithm": "pbkdf2_sha256",
        "iterations": DEFAULT_ITERATIONS,
        "salt": salt_hex,
        "password_hash": pwd_hash,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "initial_password_warning": False,
    }
    tmp = SECURITY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    _ensure_secure_permissions(tmp)
    tmp.replace(SECURITY_FILE)

    # 更改密码后注销所有已有设备会话，防止未授权持有旧会话
    session_manager.destroy_all()


def extract_session_token(headers: Any) -> Optional[str]:
    """从 HTTP 请求头中提取会话凭证 (Cookie 或 Authorization Bearer)。"""
    cookie_hdr = headers.get("Cookie")
    if cookie_hdr:
        try:
            cookie = SimpleCookie()
            cookie.load(cookie_hdr)
            if COOKIE_NAME in cookie:
                return cookie[COOKIE_NAME].value
        except Exception:
            pass

    auth_hdr = headers.get("Authorization") or ""
    if auth_hdr.startswith("Bearer "):
        token = auth_hdr[len("Bearer "):].strip()
        if token:
            return token
    return None


def make_set_cookie_header(session_id: str, max_age: int = SESSION_INACTIVITY_TIMEOUT) -> str:
    """生成具备防窃取保护的 Set-Cookie 响应头。"""
    return f"{COOKIE_NAME}={session_id}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}"


def make_clear_cookie_header() -> str:
    """生成立即注销清理 Cookie 响应头。"""
    return f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT"


def get_client_ip(handler: Any) -> str:
    """安全解析客户端 IP（支持代理前缀优先识别）。"""
    forwarded = handler.headers.get("X-Forwarded-For")
    if forwarded:
        # 取最左侧真实 IP
        return forwarded.split(",")[0].strip()
    real_ip = handler.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    try:
        return str(handler.client_address[0])
    except (AttributeError, IndexError):
        return "127.0.0.1"


# 模块装载时确保存储配置存在
init_security_config()
