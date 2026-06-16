"""Единый control-plane воркеров: pause/resume + enabled-флаги, persist в БД.

Флаги хранятся в таблице `runtime_flags` (переживают рестарт). Воркеры (autopilot/scan/cli)
перед началом работы спрашивают `can_run(worker)`. Pause НЕ убивает backend/UI — только не даёт
начать новую работу. Доставка в 1С (`delivery`) по умолчанию на паузе (безопасный дефолт).
"""
from __future__ import annotations

from typing import Any

from .config import settings
from .db import connect, utcnow

# Логические воркеры конвейера.
WORKERS: tuple[str, ...] = ("import", "stage2", "ai", "outbox", "delivery", "telegram")

# Безопасные дефолты паузы: доставку в 1С не запускаем без явного resume.
_DEFAULT_PAUSED = {"delivery": True}


def _ensure(con: Any) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS runtime_flags (
            name TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        )
        """
    )


def _get_flag(con: Any, name: str) -> str | None:
    _ensure(con)
    row = con.execute("SELECT value FROM runtime_flags WHERE name=?", (name,)).fetchone()
    return row["value"] if row else None


def _set_flag(con: Any, name: str, value: str) -> None:
    _ensure(con)
    con.execute(
        """
        INSERT INTO runtime_flags(name, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (name, value, utcnow()),
    )


def _pause_key(worker: str) -> str:
    return f"pause:{worker}"


def worker_enabled(worker: str) -> bool:
    """Включён ли воркер по конфигурации (enabled != paused)."""
    mapping = {
        "import": True,
        "stage2": True,
        "ai": bool(getattr(settings, "enable_ai", False)),
        "outbox": True,
        "delivery": bool(getattr(settings, "auto_deliver_outbox", False)),
        "telegram": bool(getattr(settings, "telegram_enabled", False)),
    }
    return bool(mapping.get(worker, True))


def is_paused(worker: str) -> bool:
    """Глобальная пауза ИЛИ пауза конкретного воркера ИЛИ безопасный дефолт."""
    with connect() as con:
        if _get_flag(con, _pause_key("all")) == "1":
            return True
        flag = _get_flag(con, _pause_key(worker))
    if flag is None:
        return bool(_DEFAULT_PAUSED.get(worker, False))
    return flag == "1"


def can_run(worker: str) -> bool:
    """Можно ли воркеру начинать новую работу: enabled И не на паузе."""
    return worker_enabled(worker) and not is_paused(worker)


def pause(worker: str = "all") -> dict[str, Any]:
    worker = (worker or "all").strip().lower()
    if worker != "all" and worker not in WORKERS:
        return {"ok": False, "error": f"unknown worker: {worker}", "workers": list(WORKERS)}
    with connect() as con:
        _set_flag(con, _pause_key(worker), "1")
    return {"ok": True, "paused": worker, "status": get_runtime_status()}


def resume(worker: str = "all") -> dict[str, Any]:
    worker = (worker or "all").strip().lower()
    if worker != "all" and worker not in WORKERS:
        return {"ok": False, "error": f"unknown worker: {worker}", "workers": list(WORKERS)}
    with connect() as con:
        if worker == "all":
            # снять глобальную паузу и индивидуальные (кроме безопасных дефолтов)
            _set_flag(con, _pause_key("all"), "0")
            for w in WORKERS:
                _set_flag(con, _pause_key(w), "1" if _DEFAULT_PAUSED.get(w) else "0")
        else:
            _set_flag(con, _pause_key("all"), "0")
            _set_flag(con, _pause_key(worker), "0")
    return {"ok": True, "resumed": worker, "status": get_runtime_status()}


def get_runtime_status() -> dict[str, Any]:
    """Снимок состояния воркеров (для status/API). Не меняет БД."""
    workers: dict[str, Any] = {}
    with connect() as con:
        global_paused = _get_flag(con, _pause_key("all")) == "1"
        raw = {w: _get_flag(con, _pause_key(w)) for w in WORKERS}
    for w in WORKERS:
        flag = raw[w]
        paused = global_paused or (bool(_DEFAULT_PAUSED.get(w, False)) if flag is None else flag == "1")
        enabled = worker_enabled(w)
        if not enabled:
            state = "disabled"
        elif paused:
            state = "paused"
        else:
            state = "running"
        workers[w] = {"enabled": enabled, "paused": paused, "state": state}
    return {"global_paused": global_paused, "workers": workers}
