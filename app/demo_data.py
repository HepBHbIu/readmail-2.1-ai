from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from .classifier import classify_email, load_buyer_rules
from .db import connect, load_buyer_identities, queue_control_events, save_case, upsert_buyer_identity, upsert_email, utcnow


def _hash(*parts: str) -> str:
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()


def _email(uid: str, subject: str, body: str, *, sender: str = "returns@sam-parts.ru", days_ago: int = 0, attachments: list[dict[str, Any]] | None = None, in_reply_to: str | None = None) -> dict[str, Any]:
    received = (datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(microsecond=0).isoformat()
    msg_id = f"<demo-{uid}@readmail.local>"
    return {
        "mailbox": "DEMO/Возвраты",
        "uid": f"demo-{uid}",
        "message_id": msg_id,
        "in_reply_to": in_reply_to,
        "references": [in_reply_to] if in_reply_to else [],
        "subject": subject,
        "from_addr": f"SAM Demo <{sender}>",
        "to_addr": "returns@company.local",
        "cc_addr": "",
        "received_at": received,
        "body_text": body,
        "body_html": "",
        "visible_text": body,
        "snippet": body[:300],
        "raw_hash": _hash(uid, subject, body),
        "attachments": attachments or [],
        "quote_markers": 0,
    }


def generate_demo_data(queue: bool = True) -> dict[str, Any]:
    """Create a tiny realistic dataset for acceptance testing without touching IMAP."""
    with connect() as con:
        from .classifier import norm as _norm, normalize_subject as _normalize_subject

        upsert_buyer_identity(
            con,
            identity_type="domain",
            identity_value="sam-parts.ru",
            buyer_code="sam_demo",
            buyer_name="SAM Demo",
            source="demo",
            confidence=0.99,
            confirmed=True,
        )
        buyer_rules = load_buyer_rules()
        learned = load_buyer_identities(con)
        samples = [
            _email(
                "shortage-ready",
                "Запрос на возврат УПД № 752341 от 05.05.2026",
                "Добрый день. Недовоз по УПД № 752341 от 05.05.2026. Артикул ABC-1234, количество 2 шт. Фото во вложении.",
                attachments=[{"filename":"photo_abc1234.jpg", "content_type":"image/jpeg", "size_bytes":123456}],
            ),
            _email(
                "defect-blocked",
                "Рекламация: брак УПД № 752342 от 05.05.2026",
                "Просим принять возврат. Брак, не работает после установки. Артикул DEF-777, количество 1 шт. Фото приложили, акт сервиса пока не готов.",
                attachments=[{"filename":"defect_photo.jpg", "content_type":"image/jpeg", "size_bytes":234567}],
            ),
            _email(
                "reminder",
                "Re: Запрос на возврат УПД № 752341 от 05.05.2026",
                "Повторно просьба дать ответ по возврату. УПД № 752341, артикул ABC-1234.\n\n----- Исходное сообщение -----\nНедовоз по УПД № 752341...",
                in_reply_to="<demo-shortage-ready@readmail.local>",
            ),
            _email(
                "new-unknown",
                "Возврат по документу № 991122 от 06.05.2026",
                "Здравствуйте. Некондиция: упаковка повреждена. Артикул XYZ-9000, количество 1 шт. Фото по ссылке https://returns.example/photo/991122",
                sender="newclient@mail.ru",
            ),
            _email(
                "supplier-decision",
                "Re: Рекламация: брак УПД № 752342 от 05.05.2026",
                "Решение: возврат согласовано, можете вернуть товар поставщику. УПД № 752342, артикул DEF-777.",
                in_reply_to="<demo-defect-blocked@readmail.local>",
            ),
        ]
        case_ids: list[int] = []
        inserted = 0

        # Собираем existing_cases контекст для детекции первого контакта
        existing_cases: list[dict[str, Any]] = []

        for sample in samples:
            raw_id, is_new = upsert_email(con, sample)
            inserted += 1 if is_new else 0
            # Передаём контекст уже обработанных писем для детекции первого контакта
            case = classify_email(
                sample, buyer_rules,
                learned_identities=learned,
                existing_cases=existing_cases,
            )
            case_id = save_case(con, raw_id, case)
            case_ids.append(case_id)

            # Сохраняем existing_cases контекст для последующих писем
            existing_cases.append({
                "from_addr": _norm(sample.get("from_addr")),
                "subject_template": _normalize_subject(sample.get("subject", "")),
                "event_type": case.get("event_type"),
            })

        queued = queue_control_events(con, limit=200) if queue else {"ok": True, "queued": 0, "skipped": 0}
    return {"ok": True, "inserted_emails": inserted, "case_ids": case_ids, "queued": queued, "generated_at": utcnow()}
