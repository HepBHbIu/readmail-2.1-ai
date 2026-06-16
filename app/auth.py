"""Минимальная auth-модель: bootstrap admin/admin → обязательная смена, сессии, роли.

Хранилище admin — таблица app_settings (ключи ADMIN_*), пароль ТОЛЬКО хэшем (pbkdf2). Сессии —
in-memory (рестарт = разлогин, безопасно). Секреты/хэши не возвращаются наружу.
"""
from __future__ import annotations

import secrets
import time
from typing import Any

from .db import get_app_settings, set_app_settings
from .server_core import hash_password, verify_password

# token -> {username, role, must_change, created}
_SESSIONS: dict[str, dict[str, Any]] = {}
SESSION_TTL_SECONDS = 12 * 3600

BOOTSTRAP_USER = "admin"
BOOTSTRAP_PASS = "admin"


def _admin() -> dict[str, Any]:
    s = get_app_settings()
    return {
        "username": str(s.get("ADMIN_USERNAME") or ""),
        "password_hash": str(s.get("ADMIN_PASSWORD_HASH") or ""),
        "must_change": bool(s.get("ADMIN_MUST_CHANGE") or False),
    }


def admin_configured() -> bool:
    a = _admin()
    return bool(a["username"] and a["password_hash"])


def bootstrap_required() -> bool:
    """admin ещё не создан → разрешён первый вход admin/admin."""
    return not admin_configured()


def set_admin(username: str, password: str, *, must_change: bool = False) -> None:
    set_app_settings({
        "ADMIN_USERNAME": username,
        "ADMIN_PASSWORD_HASH": hash_password(password),
        "ADMIN_MUST_CHANGE": bool(must_change),
    })


def issue_session(username: str, role: str = "admin", *, must_change: bool = False) -> str:
    token = secrets.token_urlsafe(32)
    _SESSIONS[token] = {"username": username, "role": role, "must_change": must_change, "created": time.time()}
    return token


def get_session(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    sess = _SESSIONS.get(token)
    if not sess:
        return None
    if time.time() - float(sess.get("created") or 0) > SESSION_TTL_SECONDS:
        _SESSIONS.pop(token, None)
        return None
    return sess


def revoke(token: str | None) -> None:
    if token:
        _SESSIONS.pop(token, None)


def login(username: str, password: str) -> dict[str, Any]:
    """Вход. В bootstrap-режиме принимает только admin/admin (с флагом обязательной смены)."""
    if bootstrap_required():
        if username == BOOTSTRAP_USER and password == BOOTSTRAP_PASS:
            token = issue_session(BOOTSTRAP_USER, role="admin", must_change=True)
            return {"ok": True, "token": token, "role": "admin", "must_change": True,
                    "bootstrap": True, "message": "Смените пароль admin/admin перед использованием."}
        return {"ok": False, "error": "bootstrap_login_required", "hint": "Первый вход: admin/admin"}
    a = _admin()
    if username == a["username"] and verify_password(password, a["password_hash"]):
        token = issue_session(a["username"], role="admin", must_change=a["must_change"])
        return {"ok": True, "token": token, "role": "admin", "must_change": a["must_change"]}
    return {"ok": False, "error": "invalid_credentials"}


def change_password(token: str, old_password: str, new_password: str) -> dict[str, Any]:
    sess = get_session(token)
    if not sess:
        return {"ok": False, "error": "no_session"}
    if not new_password or len(new_password) < 6:
        return {"ok": False, "error": "weak_password", "hint": "Минимум 6 символов"}
    # В bootstrap-смене старый пароль = admin/admin или текущий must_change.
    if bootstrap_required():
        if old_password != BOOTSTRAP_PASS:
            return {"ok": False, "error": "old_password_mismatch"}
    else:
        a = _admin()
        if not verify_password(old_password, a["password_hash"]):
            return {"ok": False, "error": "old_password_mismatch"}
    set_admin(sess["username"] if sess["username"] != BOOTSTRAP_USER else BOOTSTRAP_USER,
              new_password, must_change=False)
    # все прежние сессии инвалидируем, текущую обновляем
    _SESSIONS.clear()
    new_token = issue_session(BOOTSTRAP_USER if sess["username"] == BOOTSTRAP_USER else sess["username"],
                              role="admin", must_change=False)
    return {"ok": True, "token": new_token, "role": "admin", "must_change": False}


def me(token: str | None) -> dict[str, Any]:
    sess = get_session(token)
    if not sess:
        return {"ok": False, "authenticated": False}
    return {"ok": True, "authenticated": True, "username": sess["username"],
            "role": sess["role"], "must_change": bool(sess.get("must_change"))}
