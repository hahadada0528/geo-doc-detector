"""简单登录：本地固定账号 + 角色 + 签名 Cookie 会话。"""
from __future__ import annotations

import base64
import contextvars
import hashlib
import hmac
import json
import time
from typing import Any, Literal

from starlette.requests import Request
from starlette.responses import Response

from app.config import settings

Role = Literal["user", "admin"]

# 内置账号：密码 + 角色
USERS: dict[str, dict[str, str]] = {
    "123": {"password": "123456", "role": "user"},
    "admin": {"password": "admin", "role": "admin"},
}

COOKIE_NAME = "geo_session"
SESSION_DAYS = 14

_current_username: contextvars.ContextVar[str] = contextvars.ContextVar("current_username", default="")
_current_role: contextvars.ContextVar[str] = contextvars.ContextVar("current_role", default="")


def set_request_user(username: str, role: str = "") -> None:
    u = (username or "").strip()
    _current_username.set(u)
    if role:
        _current_role.set(role)
    elif u:
        _current_role.set(get_role(u))
    else:
        _current_role.set("")


def get_request_user() -> str:
    return _current_username.get() or ""


def get_request_role() -> str:
    return _current_role.get() or ""


def get_role(username: str) -> Role:
    info = USERS.get((username or "").strip())
    if not info:
        return "user"
    role = info.get("role") or "user"
    return "admin" if role == "admin" else "user"


def is_admin(username: str | None = None) -> bool:
    u = username if username is not None else get_request_user()
    return get_role(u) == "admin"


def verify_user(username: str, password: str) -> bool:
    u = (username or "").strip()
    p = password or ""
    info = USERS.get(u)
    if not info:
        return False
    expected = info.get("password") or ""
    return hmac.compare_digest(expected, p)


def is_public_path(path: str) -> bool:
    if path.startswith("/static/"):
        return True
    if path in ("/login", "/logout"):
        return True
    return False


# 用户功能路径前缀（管理员不可进）
USER_ONLY_PREFIXES = (
    "/collect",
    "/analyze",
    "/knowledge",
    "/brands",
    "/questions",
    "/detect",
    "/runs",
    "/generate",
    "/exports",
)

# 管理员功能路径前缀（普通用户不可进）
ADMIN_ONLY_PREFIXES = (
    "/agent",
)


def path_allowed_for_role(path: str, role: str) -> bool:
    """首页 /usage /settings /api 双方可用（api 内再细分）。"""
    if path == "/" or path.startswith("/usage") or path.startswith("/settings"):
        return True
    if path.startswith("/api/"):
        if path.startswith("/api/agent"):
            return role == "admin"
        if path.startswith("/api/status"):
            return True
        # 采集/分析相关 API：仅用户
        return role == "user"
    if any(path == p or path.startswith(p + "/") for p in ADMIN_ONLY_PREFIXES):
        return role == "admin"
    if any(path == p or path.startswith(p + "/") for p in USER_ONLY_PREFIXES):
        return role == "user"
    # 未列路径默认登录即可
    return True


def _sign(payload_b64: str) -> str:
    secret = (settings.session_secret or "geo-local-session-secret").encode("utf-8")
    return hmac.new(secret, payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()


def encode_session(data: dict[str, Any]) -> str:
    body = dict(data)
    body["_exp"] = int(time.time()) + SESSION_DAYS * 24 * 3600
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    payload = base64.urlsafe_b64encode(raw).decode("ascii")
    return f"{payload}.{_sign(payload)}"


def decode_session(token: str | None) -> dict[str, Any]:
    if not token or "." not in token:
        return {}
    payload, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(_sign(payload), sig):
        return {}
    try:
        raw = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    exp = int(data.get("_exp") or 0)
    if exp and exp < int(time.time()):
        return {}
    data.pop("_exp", None)
    return data


def read_session(request: Request) -> dict[str, Any]:
    return decode_session(request.cookies.get(COOKIE_NAME))


def set_session_cookie(response: Response, data: dict[str, Any]) -> None:
    response.set_cookie(
        COOKIE_NAME,
        encode_session(data),
        max_age=SESSION_DAYS * 24 * 3600,
        httponly=True,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")
