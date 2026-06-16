"""Серверное ядро: bind host/port, LAN-баннер, минимальный auth, ui-mode, processing-метрики.

Безопасные дефолты: без allow_lan слушаем только 127.0.0.1. В LAN — auth обязателен.
Секреты (хэш пароля, session secret) НИКОГДА не попадают в status/баннер.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import socket
from typing import Any

from .config import settings


# ── bind / LAN ────────────────────────────────────────────────────────

def resolve_bind() -> tuple[str, int]:
    """Вернуть (host, port). allow_lan=false → 127.0.0.1; true → 0.0.0.0 (если host не задан явно)."""
    host = str(getattr(settings, "server_host", "") or "").strip()
    if not host:
        host = "0.0.0.0" if bool(getattr(settings, "server_allow_lan", False)) else "127.0.0.1"
    port = int(getattr(settings, "server_port", 8765) or 8765)
    return host, port


def detect_lan_ip() -> str | None:
    """Best-effort определить LAN-IP (без отправки данных). None, если не вышло."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))  # не шлёт пакетов, только выбирает интерфейс
            ip = s.getsockname()[0]
        finally:
            s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return None


def auth_required() -> bool:
    """LAN-режим ТРЕБУЕТ auth. Иначе — по настройке server_require_auth."""
    if bool(getattr(settings, "server_allow_lan", False)):
        return True
    return bool(getattr(settings, "server_require_auth", False))


def startup_banner(worker_status: dict[str, Any] | None = None) -> str:
    """Текст баннера запуска. БЕЗ секретов."""
    host, port = resolve_bind()
    lines = ["Readmail Server started"]
    local_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    lines.append(f"Local: http://{local_host}:{port}")
    if bool(getattr(settings, "server_allow_lan", False)):
        ip = detect_lan_ip()
        if ip:
            lines.append(f"LAN:   http://{ip}:{port}")
        else:
            lines.append("LAN:   (включён, IP не определён)")
    lines.append(f"Auth: {'enabled' if auth_required() else 'disabled'}")
    workers = (worker_status or {}).get("workers") or {}
    if (worker_status or {}).get("global_paused"):
        lines.append("Workers: paused (global)")
    elif workers:
        running = [w for w, v in workers.items() if v.get("state") == "running"]
        lines.append(f"Workers: {', '.join(running) if running else 'all paused/disabled'}")
    else:
        lines.append("Workers: n/a")
    lines.append(f"Developer mode: {'on' if bool(getattr(settings, 'developer_mode', False)) else 'off'}")
    return "\n".join(lines)


# ── auth (минимальная модель) ─────────────────────────────────────────

_PBKDF2_ITER = 200_000


def hash_password(password: str, *, salt: str | None = None) -> str:
    """pbkdf2_sha256$iter$salt$hex. Никогда не хранит plain text."""
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt.encode("utf-8"), _PBKDF2_ITER)
    return f"pbkdf2_sha256${_PBKDF2_ITER}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, hexhash = str(stored).split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt.encode("utf-8"), int(iters))
        return secrets.compare_digest(dk.hex(), hexhash)
    except Exception:
        return False


def admin_configured() -> bool:
    return bool(getattr(settings, "admin_username", "") and getattr(settings, "admin_password_hash", ""))


def session_secret() -> str:
    """Только из env/настроек; если пусто — эфемерный (не печатать!)."""
    sec = str(getattr(settings, "server_session_secret", "") or os.environ.get("SERVER_SESSION_SECRET", "") or "")
    return sec or secrets.token_hex(32)


# ── developer mode / ui visibility ────────────────────────────────────

OPERATOR_TABS = ("emails", "review", "onec", "errors", "stats", "settings")
DEVELOPER_TABS = (
    "evidence", "ai_trace", "cost_ledger", "worker_settings", "raw_json",
    "payload_debug", "dry_run", "snapshots", "quarantine", "outbox_internals",
)


def ui_mode(role: str = "operator") -> dict[str, Any]:
    """Какие вкладки показывать. Developer-вкладки — только при developer_mode И роли admin/developer."""
    dev_on = bool(getattr(settings, "developer_mode", False)) and role in ("admin", "developer")
    visible = list(OPERATOR_TABS) + (list(DEVELOPER_TABS) if dev_on else [])
    return {
        "role": role,
        "developer_mode": dev_on,
        "operator_tabs": list(OPERATOR_TABS),
        "developer_tabs": list(DEVELOPER_TABS),
        "visible_tabs": visible,
        "auth_required": auth_required(),
    }


def public_status() -> dict[str, Any]:
    """Безопасный статус для status-вывода: НЕ содержит секретов."""
    host, port = resolve_bind()
    try:
        from . import auth as _auth
        admin_ok = _auth.admin_configured()
    except Exception:
        admin_ok = admin_configured()
    return {
        "host": host,
        "port": port,
        "allow_lan": bool(getattr(settings, "server_allow_lan", False)),
        "auth_required": auth_required(),
        "admin_configured": admin_ok,
        "developer_mode": bool(getattr(settings, "developer_mode", False)),
        "public_base_url": getattr(settings, "server_public_base_url", "") or None,
        "lan_ip": detect_lan_ip() if bool(getattr(settings, "server_allow_lan", False)) else None,
    }
