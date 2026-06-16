"""Import window: импорт только писем после заданной даты/времени старта.

Письма раньше границы НЕ качаются (нет BODY.PEEK), помечаются skipped_before_start и НЕ считаются
missing_local в reconcile. Чистые helper'ы — тестируются без IMAP.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .config import settings

_MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_MONTH_IDX = {m.lower(): i for i, m in enumerate(_MONTHS) if m}


def window_from_dt() -> datetime | None:
    """Граница импорта (UTC) или None, если окно выключено/не задано."""
    if not bool(getattr(settings, "import_window_enabled", False)):
        return None
    s = str(getattr(settings, "import_from_datetime", "") or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def imap_since_value(dt: datetime) -> str:
    """IMAP SINCE значение (по дате, гранулярность — день): '01-Jun-2026'."""
    dt = dt.astimezone(timezone.utc)
    return f"{dt.day:02d}-{_MONTHS[dt.month]}-{dt.year}"


def parse_internaldate(value: str | None) -> datetime | None:
    """Распарсить IMAP INTERNALDATE: '10-Jun-2026 08:35:16 +0000'."""
    if not value:
        return None
    s = str(value).strip().strip('"')
    try:
        datepart, timepart, tz = s.split(" ")
        day, mon, year = datepart.split("-")
        hh, mm, ss = timepart.split(":")
        sign = 1 if tz[0] == "+" else -1
        off_h, off_m = int(tz[1:3]), int(tz[3:5])
        from datetime import timedelta
        dt = datetime(int(year), _MONTH_IDX[mon.lower()], int(day), int(hh), int(mm), int(ss),
                      tzinfo=timezone.utc)
        dt = dt - sign * timedelta(hours=off_h, minutes=off_m)
        return dt
    except Exception:
        return None


def partition_uids(uid_to_internaldate: dict[str, str], from_dt: datetime | None) -> tuple[list[str], list[str]]:
    """Разделить UID на (keep_after_start, skip_before_start) по точному времени INTERNALDATE.

    Если from_dt is None — ничего не пропускаем (окно выключено). Если дата письма неизвестна —
    НЕ пропускаем (безопасно: лучше скачать, чем потерять).
    """
    if from_dt is None:
        return list(uid_to_internaldate.keys()), []
    keep: list[str] = []
    skip: list[str] = []
    for uid, idate in uid_to_internaldate.items():
        dt = parse_internaldate(idate)
        if dt is not None and dt < from_dt:
            skip.append(uid)
        else:
            keep.append(uid)
    return keep, skip
