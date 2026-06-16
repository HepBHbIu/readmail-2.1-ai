from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from fastapi.testclient import TestClient

from app.bucket_accounting import build_bucket_accounting


def _connection() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:", check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE raw_emails (
            id INTEGER PRIMARY KEY,
            mailbox TEXT,
            uid TEXT,
            uidvalidity TEXT,
            subject TEXT,
            from_addr TEXT,
            message_id TEXT,
            status TEXT,
            duplicate_of_raw_email_id INTEGER,
            received_at TEXT
        );
        CREATE TABLE cases (
            id INTEGER PRIMARY KEY,
            raw_email_id INTEGER,
            buyer_code TEXT,
            event_type TEXT,
            claim_kind TEXT,
            status TEXT,
            state TEXT,
            needs_ai INTEGER DEFAULT 0,
            link_quarantine INTEGER DEFAULT 0
        );
        CREATE TABLE ai_suggestions (id INTEGER PRIMARY KEY, case_id INTEGER);
        CREATE TABLE outbox (
            id INTEGER PRIMARY KEY,
            case_id INTEGER,
            status TEXT,
            event_type TEXT
        );
        """
    )
    con.executemany(
        """
        INSERT INTO raw_emails(
            id, mailbox, uid, uidvalidity, subject, from_addr, message_id,
            status, duplicate_of_raw_email_id, received_at
        ) VALUES (?, 'INBOX', ?, '1', ?, 'sender@example.test', ?, ?, ?, '2026-06-11')
        """,
        [
            (1, "1", "return", "<1@test>", "imported", None),
            (2, "2", "report", "<2@test>", "imported", None),
            (3, "3", "followup", "<3@test>", "imported", None),
            (4, "4", "duplicate", "<4@test>", "duplicate", 1),
            (5, "5", "no case", "<5@test>", "imported", None),
        ],
    )
    con.executemany(
        """
        INSERT INTO cases(
            id, raw_email_id, buyer_code, event_type, claim_kind, status,
            state, needs_ai, link_quarantine
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        [
            (11, 1, "buyer", "new_return", "defect", "needs_review", "needs_review", 1),
            (12, 2, "buyer", "info_only", None, None, "ignored_info_only", 0),
            (13, 3, "buyer", "followup_dialog", None, None, "linked_event", 0),
        ],
    )
    con.commit()
    return con


def test_bucket_accounting_accounts_for_every_raw_once():
    con = _connection()
    result = build_bucket_accounting(con, include_items=True)
    summary = result["summary"]
    assert summary["total_raw"] == 5
    assert summary["accounted_raw"] == 5
    assert summary["accounting_gap"] == 0
    assert sum(summary["by_bucket"].values()) == 5


def test_supplier_report_is_terminal_not_return():
    con = _connection()
    item = next(
        row for row in build_bucket_accounting(con, include_items=True)["items"]
        if row["raw_email_id"] == 2
    )
    assert item["bucket"] == "terminal_non_export"
    assert item["event_type"] == "info_only"
    assert "review" not in item["ui_tabs"]


def test_linked_followup_is_terminal_not_duplicate():
    con = _connection()
    item = next(
        row for row in build_bucket_accounting(con, include_items=True)["items"]
        if row["raw_email_id"] == 3
    )
    assert item["bucket"] == "terminal_linked"
    assert item["bucket"] != "duplicate"
    assert item["not_displayed_in_operational_tabs"] is True


def test_duplicate_and_no_case_are_separate_buckets():
    con = _connection()
    summary = build_bucket_accounting(con)["summary"]
    assert summary["by_bucket"]["duplicate"] == 1
    assert summary["by_bucket"]["no_case"] == 1


def test_bucket_accounting_endpoint(monkeypatch):
    con = _connection()

    @contextmanager
    def fake_connect():
        yield con

    import app.main as main

    monkeypatch.setattr(main, "connect", fake_connect)
    response = TestClient(main.app).get("/api/stats/bucket-accounting")
    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["total_raw"] == 5
    assert payload["summary"]["accounting_gap"] == 0


def test_service_links_are_separate_from_completed_links():
    con = _connection()
    con.executemany(
        """
        INSERT INTO raw_emails(
            id, mailbox, uid, uidvalidity, subject, from_addr, message_id,
            status, duplicate_of_raw_email_id, received_at
        ) VALUES (?, 'INBOX', ?, '1', ?, 'sender@example.test', ?, 'imported', NULL, '2026-06-11')
        """,
        [
            (6, "6", "TNVED", "<6@test>"),
            (7, "7", "active", "<7@test>"),
        ],
    )
    con.executemany(
        """
        INSERT INTO cases(
            id, raw_email_id, buyer_code, event_type, claim_kind, status,
            state, needs_ai, link_quarantine
        ) VALUES (?, ?, 'buyer', ?, ?, NULL, ?, 0, 0)
        """,
        [
            (16, 6, "marking_request", "marking_request", "linked_event"),
            (17, 7, "followup_reminder", None, "needs_link"),
        ],
    )
    result = build_bucket_accounting(con, include_items=True)
    summary = result["summary"]
    assert summary["by_link_group"]["service"] == 1
    assert summary["by_link_group"]["active"] == 1
    assert summary["by_link_group"]["completed"] == 1
    marking = next(item for item in result["items"] if item["raw_email_id"] == 6)
    assert marking["bucket"] == "service_marking"
