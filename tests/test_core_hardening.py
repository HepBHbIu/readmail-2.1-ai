from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.config import settings


@pytest.fixture()
def fresh_db(monkeypatch):
    tmp = Path(tempfile.mkdtemp()) / "core.sqlite3"
    monkeypatch.setattr(settings, "database_path", tmp, raising=False)
    from app.db import init_db
    init_db()
    return tmp


# ── Ф3: UIDVALIDITY в raw identity ────────────────────────────────────

def _raw_db_with_uv() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE raw_emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT, mailbox TEXT NOT NULL, uid TEXT NOT NULL,
            folder_seen_json TEXT DEFAULT '[]', canonical_key TEXT, duplicate_of_raw_email_id INTEGER,
            status TEXT DEFAULT 'imported', message_id TEXT, in_reply_to TEXT,
            references_json TEXT DEFAULT '[]', subject TEXT, from_addr TEXT, to_addr TEXT, cc_addr TEXT,
            received_at TEXT, body_text TEXT, body_html TEXT, visible_text TEXT, snippet TEXT,
            raw_hash TEXT, raw_path TEXT, quote_markers INTEGER DEFAULT 0, imported_at TEXT NOT NULL,
            updated_at TEXT, uidvalidity TEXT
        );
        CREATE UNIQUE INDEX idx_raw_identity ON raw_emails(mailbox, uid, uidvalidity);
        CREATE TABLE attachments (id INTEGER PRIMARY KEY AUTOINCREMENT, raw_email_id INTEGER,
            filename TEXT, content_type TEXT, size_bytes INTEGER, file_path TEXT);
        CREATE TABLE process_events (id INTEGER PRIMARY KEY AUTOINCREMENT, stage TEXT, level TEXT,
            message TEXT, case_id INTEGER, raw_email_id INTEGER, subject TEXT, details_json TEXT, created_at TEXT);
        CREATE TABLE outbox (id INTEGER PRIMARY KEY);
        """
    )
    return con


def _email(uid, mid, h, uv=None, body="b"):
    return {"mailbox": "F", "uid": uid, "message_id": mid, "raw_hash": h, "uidvalidity": uv,
            "subject": "Возврат", "body_text": body, "attachments": []}


def test_same_uid_same_uidvalidity_same_identity():
    from app.db import upsert_email
    con = _raw_db_with_uv()
    a, c1 = upsert_email(con, _email("5", "<a>", "h1", uv="111"))
    b, c2 = upsert_email(con, _email("5", "<a>", "h1", uv="111"))
    assert c1 is True and c2 is False and a == b


def test_same_uid_different_uidvalidity_new_row():
    from app.db import upsert_email
    con = _raw_db_with_uv()
    a, _ = upsert_email(con, _email("5", "<a>", "h1", uv="111", body="one"))
    b, created = upsert_email(con, _email("5", "<b>", "h2", uv="222", body="two"))
    assert created is True and a != b
    assert con.execute("SELECT COUNT(*) FROM raw_emails WHERE mailbox='F' AND uid='5'").fetchone()[0] == 2


def test_migration_idempotent(fresh_db):
    from app.db import connect, migrate_raw_emails_uidvalidity
    with connect() as con:
        # после init_db миграция уже прошла → второй вызов не перестраивает
        assert migrate_raw_emails_uidvalidity(con) is False
        cols = {r["name"] for r in con.execute("PRAGMA table_info(raw_emails)")}
        assert "uidvalidity" in cols


# ── Ф2: import window ─────────────────────────────────────────────────

def test_import_window_partition(monkeypatch):
    from app import import_window as iw
    monkeypatch.setattr(settings, "import_window_enabled", True, raising=False)
    monkeypatch.setattr(settings, "import_from_datetime", "2026-06-01T00:00:00+00:00", raising=False)
    fd = iw.window_from_dt()
    assert fd == datetime(2026, 6, 1, tzinfo=timezone.utc)
    keep, skip = iw.partition_uids(
        {"1": "10-May-2026 08:00:00 +0000", "2": "10-Jun-2026 08:00:00 +0000", "3": "bad"}, fd)
    assert "1" in skip and "2" in keep and "3" in keep  # before→skip, after→keep, unknown→keep


def test_import_window_disabled_keeps_all(monkeypatch):
    from app import import_window as iw
    monkeypatch.setattr(settings, "import_window_enabled", False, raising=False)
    assert iw.window_from_dt() is None
    keep, skip = iw.partition_uids({"1": "10-May-2026 08:00:00 +0000"}, None)
    assert keep == ["1"] and skip == []


def test_record_uid_skipped_is_skipped_status(fresh_db):
    from app.db import connect, record_uid_skipped
    with connect() as con:
        record_uid_skipped(con, mailbox="F", uid="9", uidvalidity="111")
        row = con.execute("SELECT status, stage FROM import_uid_failures WHERE uid='9'").fetchone()
        assert row["status"] == "skipped" and row["stage"] == "before_start"


# ── Ф5: job locks ─────────────────────────────────────────────────────

# (job_locks удалён в v2.1 — тесты блокировок убраны)


# ── Ф4: worker test harness ───────────────────────────────────────────

def test_worker_test_read_only(fresh_db):
    from app.worker_test import run_worker_test
    rep = run_worker_test(stage="import", limit=5, db=fresh_db)
    assert rep["ok"] and rep["read_only"] is True and rep["dry_run"] is True
    assert "import" in rep["stages"]


# ── Ф1: auth enforcement (TestClient) ─────────────────────────────────

@pytest.fixture()
def client(fresh_db, monkeypatch):
    monkeypatch.setattr(settings, "server_require_auth", True, raising=False)
    monkeypatch.setattr(settings, "server_allow_lan", False, raising=False)
    from fastapi.testclient import TestClient
    import app.auth as auth
    auth._SESSIONS.clear()
    import app.main as m
    return TestClient(m.app)


def test_health_public_without_auth(client):
    assert client.get("/api/health").status_code == 200
    assert client.get("/login").status_code == 200


def test_protected_endpoint_blocked(client):
    assert client.get("/api/runtime/status").status_code == 401


def test_bootstrap_admin_then_forced_change(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    assert r.status_code == 200 and r.json()["must_change"] is True
    # must_change блокирует обычные endpoints
    assert client.get("/api/runtime/status").status_code == 403
    ch = client.post("/api/auth/change-password",
                     json={"old_password": "admin", "new_password": "newpass1"})
    assert ch.status_code == 200 and ch.json()["ok"] is True
    assert client.get("/api/runtime/status").status_code == 200
    # старый admin/admin больше не работает
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "admin", "password": "newpass1"}).status_code == 200


def test_password_not_plain_text(client):
    client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    client.post("/api/auth/change-password", json={"old_password": "admin", "new_password": "secret9"})
    from app.db import get_app_settings
    stored = str(get_app_settings().get("ADMIN_PASSWORD_HASH") or "")
    assert "secret9" not in stored and stored.startswith("pbkdf2_sha256$")


def test_runtime_status_no_secrets(client):
    client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    client.post("/api/auth/change-password", json={"old_password": "admin", "new_password": "secret9"})
    body = client.get("/api/runtime/status").text
    assert "secret9" not in body and "pbkdf2_sha256" not in body
    assert "import_window" in body and "uidvalidity_in_identity" in body
