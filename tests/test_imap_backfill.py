from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from app.db import record_uid_failure, upsert_email

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import backfill_missing_imap_uids as bf  # noqa: E402


# ── фикстуры БД ──────────────────────────────────────────────────────

def _raw_db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE raw_emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mailbox TEXT NOT NULL, uid TEXT NOT NULL,
            folder_seen_json TEXT DEFAULT '[]', canonical_key TEXT,
            duplicate_of_raw_email_id INTEGER, status TEXT DEFAULT 'imported',
            message_id TEXT, in_reply_to TEXT, references_json TEXT DEFAULT '[]',
            subject TEXT, from_addr TEXT, to_addr TEXT, cc_addr TEXT, received_at TEXT,
            body_text TEXT, body_html TEXT, visible_text TEXT, snippet TEXT,
            raw_hash TEXT, raw_path TEXT, quote_markers INTEGER DEFAULT 0,
            imported_at TEXT NOT NULL, updated_at TEXT,
            UNIQUE(mailbox, uid)
        );
        CREATE TABLE attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, raw_email_id INTEGER,
            filename TEXT, content_type TEXT, size_bytes INTEGER, file_path TEXT
        );
        CREATE TABLE process_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, stage TEXT, level TEXT, message TEXT,
            case_id INTEGER, raw_email_id INTEGER, subject TEXT, details_json TEXT, created_at TEXT
        );
        CREATE TABLE outbox (id INTEGER PRIMARY KEY);
        """
    )
    return con


def _failures_db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE import_uid_failures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account TEXT, mailbox TEXT, uid TEXT, stage TEXT,
            error_type TEXT, error_message TEXT,
            attempts INTEGER DEFAULT 1, status TEXT DEFAULT 'failed',
            first_seen_at TEXT, last_seen_at TEXT, next_retry_at TEXT,
            uidvalidity TEXT, message_id TEXT, recoverable INTEGER,
            UNIQUE(account, mailbox, uid, stage)
        );
        """
    )
    return con


def _email(folder: str, uid: str, message_id: str | None, raw_hash: str, body: str = "body") -> dict:
    return {
        "mailbox": folder, "uid": uid, "message_id": message_id, "raw_hash": raw_hash,
        "subject": "Возврат товара", "body_text": body, "attachments": [],
    }


FOLDER = "&BCIENQRBBEI- Berru|avtoto.ru"


# ── 1. backfill missing UID создаёт raw ──────────────────────────────

def test_backfill_missing_uid_creates_raw():
    con = _raw_db()
    rid, created = upsert_email(con, _email(FOLDER, "1043", "<new@mail.ru>", "h1"))
    assert created is True
    row = con.execute("SELECT mailbox, uid, duplicate_of_raw_email_id FROM raw_emails WHERE id=?", (rid,)).fetchone()
    assert row["mailbox"] == FOLDER and row["uid"] == "1043"
    assert row["duplicate_of_raw_email_id"] is None  # imported_raw, не дубль


# ── 2. backfill existing UID не создаёт дубль ─────────────────────────

def test_backfill_existing_uid_no_duplicate():
    con = _raw_db()
    first, _ = upsert_email(con, _email(FOLDER, "1043", "<new@mail.ru>", "h1"))
    second, created = upsert_email(con, _email(FOLDER, "1043", "<new@mail.ru>", "h1"))
    assert created is False and second == first
    assert con.execute("SELECT COUNT(*) FROM raw_emails").fetchone()[0] == 1


# ── 3. semantic duplicate при backfill сохраняется и связывается ──────

def test_backfill_semantic_duplicate_linked():
    con = _raw_db()
    orig, _ = upsert_email(con, _email(FOLDER, "119", "<dup@y.ru>", "hash-a", "one"))
    dup, created = upsert_email(con, _email(FOLDER, "87", "<dup@y.ru>", "hash-b", "two"))
    assert created is True and dup != orig
    row = con.execute("SELECT duplicate_of_raw_email_id, status FROM raw_emails WHERE id=?", (dup,)).fetchone()
    assert row["duplicate_of_raw_email_id"] == orig
    assert row["status"] == "duplicate"
    assert con.execute("SELECT COUNT(*) FROM raw_emails").fetchone()[0] == 2  # ничего не потеряно


# ── 4. fetch_failed UID остаётся quarantine с clear reason ────────────

def test_fetch_failed_uid_quarantined_with_clear_reason():
    con = _failures_db()
    for _ in range(3):  # max_attempts=3
        out = record_uid_failure(
            con, account="acc", mailbox=FOLDER, uid="87", stage="fetch_single",
            error_type="abort", error_message="TLS EOF",
            uidvalidity="1777467890", message_id="<x@y.ru>",
            recoverable=True, next_retry_at="2026-06-10T18:00:00+00:00",
        )
    assert out["quarantined"] is True
    row = con.execute("SELECT * FROM import_uid_failures WHERE uid='87'").fetchone()
    assert row["status"] == "quarantined"
    assert row["attempts"] == 3
    assert row["recoverable"] == 1
    assert row["uidvalidity"] == "1777467890"
    assert row["message_id"] == "<x@y.ru>"
    assert row["next_retry_at"] == "2026-06-10T18:00:00+00:00"
    assert "TLS EOF" in row["error_message"]


# ── 4b. COALESCE: повтор без enrich-полей не затирает прежние ─────────

def test_record_uid_failure_coalesce_keeps_existing():
    con = _failures_db()
    record_uid_failure(con, account="a", mailbox=FOLDER, uid="9", stage="fetch_single",
                       error_type="abort", error_message="e1",
                       uidvalidity="111", message_id="<m@y>", recoverable=True)
    record_uid_failure(con, account="a", mailbox=FOLDER, uid="9", stage="fetch_single",
                       error_type="abort", error_message="e2")  # без enrich
    row = con.execute("SELECT * FROM import_uid_failures WHERE uid='9'").fetchone()
    assert row["uidvalidity"] == "111" and row["message_id"] == "<m@y>" and row["recoverable"] == 1


# ── 5. BODY.PEEK / readonly: скрипт не ставит Seen (source-level guard) ─

def test_backfill_uses_peek_and_readonly_only():
    src = (ROOT / "scripts" / "backfill_missing_imap_uids.py").read_text(encoding="utf-8")
    assert "BODY.PEEK[]" in src
    assert "readonly=True" in src
    # не должно быть небезопасного полного BODY[] / RFC822 (снимают \Seen)
    import re
    assert not re.search(r"BODY\[\](?!.*PEEK)", src.replace("BODY.PEEK[]", ""))
    assert "RFC822\\b" not in src and "\"RFC822\"" not in src


# ── 6. targeted parsing folder:uid (folder содержит '|') ─────────────

def test_parse_target_handles_folder_with_pipe():
    folder, uid = bf._parse_target(f"{FOLDER}:1043")
    assert folder == FOLDER and uid == "1043"
    with pytest.raises(ValueError):
        bf._parse_target("no-colon-token")


# ── 7. from-missing фильтрует quarantine без флага ───────────────────

def test_load_targets_filters_quarantine(tmp_path):
    import json
    p = tmp_path / "missing.jsonl"
    p.write_text(
        json.dumps({"folder": FOLDER, "uid": "1043", "local_status": "missing_local"}) + "\n" +
        json.dumps({"folder": FOLDER, "uid": "87", "local_status": "fetch_failed"}) + "\n",
        encoding="utf-8",
    )

    class A:  # имитация argparse.Namespace
        uid = None
        from_missing = str(p)
        include_quarantine = False

    targets = bf._load_targets(A())
    uids = {t["uid"] for t in targets}
    assert uids == {"1043"}  # 87 (fetch_failed) исключён без --include-quarantine

    A.include_quarantine = True
    uids2 = {t["uid"] for t in bf._load_targets(A())}
    assert uids2 == {"1043", "87"}


# ── 8. real outbox не меняется при backfill-upsert ───────────────────

def test_outbox_unchanged_during_backfill():
    con = _raw_db()
    upsert_email(con, _email(FOLDER, "1043", "<new@mail.ru>", "h1"))
    upsert_email(con, _email(FOLDER, "87", "<dup@y.ru>", "hash-b"))
    assert con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


# ── 9. AI/1С не вызываются (source-level guard) ──────────────────────

def test_backfill_does_not_call_ai_or_1c():
    src = (ROOT / "scripts" / "backfill_missing_imap_uids.py").read_text(encoding="utf-8")
    for forbidden in ("run_ai_suggestion", "run_vision", "ai_client", "deliver_outbox",
                      "send_to_1c", "process_imported_emails", "classify_email"):
        assert forbidden not in src, f"backfill must not reference {forbidden}"
