"""archiver.py — управление жизненным циклом писем в БД.

Политика хранения (настраивается в .env / UI):
  - archive_full_days (по умолчанию 90):
      полное тело письма + вложения хранятся N дней
  - archive_meta_days (по умолчанию 365):
      после этого срока хранятся только JSON-метаданные

Почта на сервере НЕ трогается. Внутренний учёт — по message_id в БД.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import settings
from .db import connect, utcnow

logger = logging.getLogger(__name__)


def run_archive_cleanup() -> dict[str, Any]:
    """Запустить чистку по политике хранения. Вызывается при старте и по расписанию."""
    result: dict[str, Any] = {
        "ok": True,
        "cleared_body_count": 0,
        "cleared_meta_count": 0,
        "errors": [],
        "ran_at": utcnow(),
    }
    now = datetime.now(timezone.utc)

    try:
        with connect() as con:
            # --- Шаг 1: очистить тело писем старше archive_full_days ---
            cutoff_full = (now - timedelta(days=int(settings.archive_full_days))).isoformat()
            rows = con.execute(
                """
                SELECT id FROM raw_emails
                WHERE received_at < ?
                  AND (body_text IS NOT NULL OR body_html IS NOT NULL)
                  AND archived_body = 0
                LIMIT 1000
                """,
                (cutoff_full,),
            ).fetchall()
            if rows:
                ids = [r["id"] for r in rows]
                con.execute(
                    f"UPDATE raw_emails SET body_text = NULL, body_html = NULL, archived_body = 1 "
                    f"WHERE id IN ({','.join('?' * len(ids))})",
                    ids,
                )
                result["cleared_body_count"] = len(ids)
                logger.info("Archived body for %d emails (older than %d days)", len(ids), settings.archive_full_days)

            # --- Шаг 2: очистить метаданные старше archive_meta_days ---
            cutoff_meta = (now - timedelta(days=int(settings.archive_meta_days))).isoformat()
            rows2 = con.execute(
                """
                SELECT id FROM raw_emails
                WHERE received_at < ?
                  AND archived_meta = 0
                LIMIT 500
                """,
                (cutoff_meta,),
            ).fetchall()
            if rows2:
                ids2 = [r["id"] for r in rows2]
                # Оставляем только snippet + from_addr + subject + received_at + message_id
                con.execute(
                    f"""UPDATE raw_emails
                    SET body_text = NULL, body_html = NULL,
                        raw_path = NULL, archived_body = 1, archived_meta = 1
                    WHERE id IN ({','.join('?' * len(ids2))})""",
                    ids2,
                )
                result["cleared_meta_count"] = len(ids2)
                logger.info("Archived meta for %d emails (older than %d days)", len(ids2), settings.archive_meta_days)

    except Exception as e:
        result["ok"] = False
        result["errors"].append(str(e))
        logger.exception("Archive cleanup error: %s", e)

    return result


def get_archive_stats() -> dict[str, Any]:
    """Статистика по архиву для UI."""
    try:
        with connect() as con:
            total = con.execute("SELECT COUNT(*) FROM raw_emails").fetchone()[0]
            archived_body = con.execute("SELECT COUNT(*) FROM raw_emails WHERE archived_body = 1").fetchone()[0]
            archived_meta = con.execute("SELECT COUNT(*) FROM raw_emails WHERE archived_meta = 1").fetchone()[0]
            oldest = con.execute("SELECT MIN(received_at) FROM raw_emails").fetchone()[0]
            newest = con.execute("SELECT MAX(received_at) FROM raw_emails").fetchone()[0]
            return {
                "total_emails": total,
                "full_stored": total - archived_body,
                "body_archived": archived_body - archived_meta,
                "meta_only": archived_meta,
                "oldest_email": oldest,
                "newest_email": newest,
                "archive_full_days": settings.archive_full_days,
                "archive_meta_days": settings.archive_meta_days,
            }
    except Exception as e:
        return {"error": str(e)}
