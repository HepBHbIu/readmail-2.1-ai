from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from fastapi.testclient import TestClient


def _review_connection() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:", check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE raw_emails (
            id INTEGER PRIMARY KEY,
            subject TEXT,
            from_addr TEXT,
            received_at TEXT,
            snippet TEXT
        );
        CREATE TABLE cases (
            id INTEGER PRIMARY KEY,
            raw_email_id INTEGER,
            buyer_code TEXT,
            buyer_name TEXT,
            event_type TEXT,
            claim_kind TEXT,
            state TEXT,
            priority TEXT,
            confidence REAL,
            ready_for_export INTEGER,
            fields_json TEXT,
            missing_json TEXT,
            quality_json TEXT,
            payload_json TEXT
        );
        """
    )
    for value in range(1, 61):
        con.execute(
            "INSERT INTO raw_emails VALUES (?, ?, 'sender@test', '2026-06-11', '')",
            (value, f"Return {value}"),
        )
        con.execute(
            """
            INSERT INTO cases VALUES (
                ?, ?, 'buyer', 'Buyer', 'new_return', 'quality_refusal',
                'needs_review', 'normal', 0.5, 0, '{}', '[]', '[]', '{}'
            )
            """,
            (value, value),
        )
    return con


def test_review_endpoint_explains_total_vs_shown(monkeypatch):
    con = _review_connection()

    @contextmanager
    def fake_connect():
        yield con

    import app.main as main

    monkeypatch.setattr(main, "connect", fake_connect)
    response = TestClient(main.app).get("/api/review/cases?page=1&limit=50")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total_count"] == 60
    assert payload["shown_count"] == 50
    assert payload["page"] == 1
    assert payload["page_size"] == 50
    assert payload["has_more"] is True


def test_review_endpoint_shows_hidden_cases_by_folders(monkeypatch):
    con = _review_connection()
    rows = [
        (61, "correction_request", "linked_event"),
        (62, "ready_to_ship", "linked_event"),
        (63, "info_only", "ignored_info_only"),
    ]
    for raw_id, event_type, state in rows:
        con.execute(
            "INSERT INTO raw_emails VALUES (?, ?, 'sender@test', '2026-06-11', '')",
            (raw_id, f"Message {raw_id}"),
        )
        con.execute(
            """
            INSERT INTO cases VALUES (
                ?, ?, 'buyer', 'Buyer', ?, NULL,
                ?, 'normal', 0.5, 0, '{}', '[]', '[]', '{}'
            )
            """,
            (raw_id, raw_id, event_type, state),
        )

    @contextmanager
    def fake_connect():
        yield con

    import app.main as main

    monkeypatch.setattr(main, "connect", fake_connect)
    client = TestClient(main.app)

    all_payload = client.get("/api/review/cases?folder=all&limit=100").json()
    assert all_payload["total_count"] == 63
    assert all_payload["folder_counts"]["corrections"] == 1
    assert all_payload["folder_counts"]["ready_to_ship"] == 1
    assert all_payload["folder_counts"]["information"] == 1
    assert sum(all_payload["folder_counts"].values()) - all_payload["folder_counts"]["all"] == 63

    correction = client.get("/api/review/cases?folder=corrections").json()
    assert correction["total_count"] == 1
    assert correction["cases"][0]["folder_key"] == "corrections"
    assert correction["cases"][0]["can_export"] is False
