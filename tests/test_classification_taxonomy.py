from __future__ import annotations

import sqlite3

from app.classification_taxonomy import audit_classifications


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE raw_emails (
            id INTEGER PRIMARY KEY, mailbox TEXT, uid TEXT, uidvalidity TEXT,
            message_id TEXT, in_reply_to TEXT, references_json TEXT,
            subject TEXT, from_addr TEXT, received_at TEXT, body_text TEXT,
            body_html TEXT, visible_text TEXT, snippet TEXT, status TEXT,
            duplicate_of_raw_email_id INTEGER
        );
        CREATE TABLE attachments (
            raw_email_id INTEGER, filename TEXT, content_type TEXT, size_bytes INTEGER
        );
        CREATE TABLE cases (
            id INTEGER PRIMARY KEY, raw_email_id INTEGER, buyer_code TEXT,
            event_type TEXT, claim_kind TEXT, status TEXT, state TEXT,
            needs_ai INTEGER DEFAULT 0, link_quarantine INTEGER DEFAULT 0,
            ready_for_export INTEGER DEFAULT 0, fields_json TEXT DEFAULT '{}',
            payload_json TEXT DEFAULT '{}'
        );
        """
    )
    rows = [
        (1, "Возврат", "Артикул A123 количество 1 накладная 55. Отказ клиента", None),
        (2, "Прайс-лист", "Остатки товара", None),
        (3, "Re: возврат", "Когда будет решение по возврату?", None),
        (4, "Дубль", "тот же текст", 1),
        (5, "", "", None),
    ]
    for rid, subject, body, duplicate in rows:
        con.execute(
            """
            INSERT INTO raw_emails VALUES(
              ?, 'INBOX', ?, '1', ?, NULL, '[]', ?, 'sender@test.ru',
              '2026-06-11', ?, '', ?, ?, 'imported', ?
            )
            """,
            (rid, str(rid), f"<{rid}@test>", subject, body, body, body, duplicate),
        )
    con.execute(
        "INSERT INTO attachments VALUES(2, 'price.xlsx', 'application/xlsx', 10)"
    )
    con.executemany(
        """
        INSERT INTO cases(
          id, raw_email_id, buyer_code, event_type, claim_kind, status, state,
          needs_ai, link_quarantine, ready_for_export, fields_json, payload_json
        ) VALUES (?, ?, 'buyer', ?, ?, NULL, ?, 0, 0, 0, ?, ?)
        """,
        [
            (11, 1, "new_return", "quality_refusal", "needs_review",
             '{"document_number":"55","document_date":"11.06.2026","part_number":"A123","quantity":1}',
             '{"processing_source":"pattern"}'),
            (12, 2, "info_only", None, "ignored_info_only", "{}", "{}"),
            (13, 3, "followup_dialog", None, "linked_event", "{}", "{}"),
        ],
    )
    con.commit()
    return con


def test_every_raw_gets_explicit_taxonomy():
    con = _db()
    before = (
        con.execute("SELECT COUNT(*) FROM raw_emails").fetchone()[0],
        con.execute("SELECT COUNT(*) FROM cases").fetchone()[0],
    )
    result = audit_classifications(con)
    after = (
        con.execute("SELECT COUNT(*) FROM raw_emails").fetchone()[0],
        con.execute("SELECT COUNT(*) FROM cases").fetchone()[0],
    )
    assert result["summary"]["total_raw"] == 5
    assert result["summary"]["accounted"] == 5
    assert result["summary"]["unaccounted"] == 0
    assert result["summary"]["missing_subcategory"] == 0
    assert all(item["proposed_category"] and item["proposed_subcategory"] for item in result["items"])
    assert after == before


def test_supplier_report_is_not_return_claim():
    result = audit_classifications(_db())
    item = next(x for x in result["items"] if x["raw_email_id"] == 2)
    assert item["proposed_category"] == "supplier_report"
    assert item["proposed_subcategory"] == "price_list"


def test_linked_followup_stays_linked_event():
    result = audit_classifications(_db())
    item = next(x for x in result["items"] if x["raw_email_id"] == 3)
    assert item["proposed_category"] == "linked_event"
    assert item["proposed_subcategory"] == "followup_dialog"


def test_duplicate_and_no_body_are_explicit():
    result = audit_classifications(_db())
    by_id = {item["raw_email_id"]: item for item in result["items"]}
    assert by_id[4]["proposed_category"] == "duplicate"
    assert by_id[5]["proposed_subcategory"] == "no_body"
