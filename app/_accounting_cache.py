"""Лёгкий процессный кэш для тяжёлых accounting-сборок (visual/folder/canonical).

Ключ инвалидации — дешёвая сигнатура состояния БД (кол-во raw/cases + max updated_at).
Пока данные не изменились (и не истёк TTL) — отдаём прошлый результат. Это убирает 25-секундные
пересчёты на каждый вызов UI/счётчиков. Read-only.
"""
from __future__ import annotations

import time
from typing import Any, Callable

_CACHE: dict[str, tuple[Any, Any, float]] = {}
_TTL_SECONDS = 45.0


def _signature(con: Any) -> tuple:
    # Только счётчики + max id (без MAX(updated_at)): кэш держится весь TTL даже при stage2-churn.
    # Для UI-accounting допустима свежесть в пределах TTL; новые письма (изменение count) кэш сбрасывают.
    try:
        row = con.execute(
            "SELECT (SELECT COUNT(*) FROM raw_emails), (SELECT COUNT(*) FROM cases), "
            "(SELECT MAX(id) FROM raw_emails), (SELECT MAX(id) FROM cases)"
        ).fetchone()
        return tuple(row)
    except Exception:
        return (time.time(),)  # не кэшируем при ошибке сигнатуры


def cached(con: Any, key: str, builder: Callable[[], Any], *, ttl: float = _TTL_SECONDS) -> Any:
    sig = _signature(con)
    now = time.time()
    ent = _CACHE.get(key)
    if ent is not None and ent[0] == sig and (now - ent[2]) < ttl:
        return ent[1]
    result = builder()
    _CACHE[key] = (sig, result, now)
    return result


def invalidate() -> None:
    _CACHE.clear()
