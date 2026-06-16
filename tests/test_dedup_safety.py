from __future__ import annotations

import sqlite3

from app.db import upsert_email


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE raw_emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mailbox TEXT NOT NULL,
            uid TEXT NOT NULL,
            folder_seen_json TEXT DEFAULT '[]',
            canonical_key TEXT,
            duplicate_of_raw_email_id INTEGER,
            status TEXT DEFAULT 'imported',
            message_id TEXT,
            in_reply_to TEXT,
            references_json TEXT DEFAULT '[]',
            subject TEXT,
            from_addr TEXT,
            to_addr TEXT,
            cc_addr TEXT,
            received_at TEXT,
            body_text TEXT,
            body_html TEXT,
            visible_text TEXT,
            snippet TEXT,
            raw_hash TEXT,
            raw_path TEXT,
            quote_markers INTEGER DEFAULT 0,
            imported_at TEXT NOT NULL,
            updated_at TEXT,
            UNIQUE(mailbox, uid)
        );
        CREATE TABLE attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            raw_email_id INTEGER,
            filename TEXT,
            content_type TEXT,
            size_bytes INTEGER,
            file_path TEXT
        );
        CREATE TABLE process_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stage TEXT,
            level TEXT,
            message TEXT,
            case_id INTEGER,
            raw_email_id INTEGER,
            subject TEXT,
            details_json TEXT,
            created_at TEXT
        );
        CREATE TABLE outbox (id INTEGER PRIMARY KEY);
        """
    )
    return con


def _email(uid: str, message_id: str | None, raw_hash: str, body: str = "body") -> dict:
    return {
        "mailbox": "INBOX",
        "uid": uid,
        "message_id": message_id,
        "raw_hash": raw_hash,
        "subject": "Возврат товара",
        "body_text": body,
        "attachments": [],
    }


def test_same_message_id_different_hash_is_linked_not_dropped():
    con = _db()
    first_id, first_created = upsert_email(con, _email("1", "<same@example>", "hash-a", "one"))
    second_id, second_created = upsert_email(con, _email("2", "<same@example>", "hash-b", "two"))
    assert first_created and second_created
    assert second_id != first_id
    row = con.execute("SELECT * FROM raw_emails WHERE id=?", (second_id,)).fetchone()
    assert row["duplicate_of_raw_email_id"] == first_id
    assert row["status"] == "duplicate"
    assert con.execute("SELECT COUNT(*) FROM process_events WHERE stage='dedup'").fetchone()[0] == 1


def test_same_message_id_same_hash_reuses_exact_duplicate():
    con = _db()
    first_id, _ = upsert_email(con, _email("1", "<same@example>", "hash-a"))
    second_id, created = upsert_email(con, _email("2", "<same@example>", "hash-a"))
    assert created is False
    assert second_id == first_id
    assert con.execute("SELECT COUNT(*) FROM raw_emails").fetchone()[0] == 1


def test_empty_message_ids_with_different_content_are_preserved():
    con = _db()
    first_id, _ = upsert_email(con, _email("1", None, "hash-a", "one"))
    second_id, created = upsert_email(con, _email("2", None, "hash-b", "two"))
    assert created is True
    assert second_id != first_id
    assert con.execute("SELECT COUNT(*) FROM raw_emails").fetchone()[0] == 2


def test_similar_reply_subject_is_not_raw_duplicate():
    con = _db()
    first_id, _ = upsert_email(con, _email("1", "<one@example>", "hash-a", "one"))
    reply = _email("2", "<two@example>", "hash-b", "two")
    reply["subject"] = "Re: Возврат товара"
    second_id, created = upsert_email(con, reply)
    assert created is True
    assert second_id != first_id
    assert con.execute("SELECT duplicate_of_raw_email_id FROM raw_emails WHERE id=?", (second_id,)).fetchone()[0] is None


def test_repeated_same_uid_reuses_server_object_and_outbox_unchanged():
    con = _db()
    first_id, _ = upsert_email(con, _email("1", "<one@example>", "hash-a"))
    second_id, created = upsert_email(con, _email("1", "<changed@example>", "hash-b"))
    assert created is False
    assert second_id == first_id
    assert con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
