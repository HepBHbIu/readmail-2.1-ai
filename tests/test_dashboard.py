from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.config import settings


@pytest.fixture()
def fresh_db(monkeypatch):
    tmp = Path(tempfile.mkdtemp()) / "dash.sqlite3"
    monkeypatch.setattr(settings, "database_path", tmp, raising=False)
    from app.db import init_db
    init_db()
    return tmp


@pytest.fixture()
def empty_audit(monkeypatch, tmp_path):
    # пустой audit_out → проверяем, что overview не падает без snapshot'ов
    import app.dashboard as d
    monkeypatch.setattr(d, "AUDIT_DIR", tmp_path / "audit_out", raising=False)
    return tmp_path


# ── Ф3: overview не падает без snapshot ───────────────────────────────

def test_overview_works_without_snapshots(fresh_db, empty_audit):
    from app.dashboard import build_overview
    o = build_overview()
    assert o["ok"] is True
    for sec in ("server", "auth", "workers", "mail", "processing", "outbox", "ai", "recent"):
        assert sec in o
    # mail snapshot отсутствует → помечен stale/exists=False, но не падает
    assert o["mail"]["stale"] is True
    assert o["mail"].get("server_total") is None


def test_overview_outbox_explanation(fresh_db, empty_audit):
    from app.dashboard import build_overview
    o = build_overview()
    assert "explanation" in o["outbox"]
    assert "control_events" in o["outbox"] and "business_events" in o["outbox"]
    assert o["outbox"]["delivery_enabled"] in (True, False)


def test_overview_no_secrets(fresh_db, empty_audit, monkeypatch):
    monkeypatch.setattr(settings, "server_session_secret", "TOPSECRET", raising=False)
    from app.dashboard import build_overview
    import json
    blob = json.dumps(build_overview(), ensure_ascii=False, default=str)
    assert "TOPSECRET" not in blob
    assert "pbkdf2_sha256" not in blob


def test_snapshot_meta_stale(tmp_path):
    from app.dashboard import _snapshot_meta
    missing = _snapshot_meta(tmp_path / "nope.json")
    assert missing["exists"] is False and missing["stale"] is True
    p = tmp_path / "s.json"
    p.write_text("{}", encoding="utf-8")
    fresh = _snapshot_meta(p, stale_minutes=60)
    assert fresh["exists"] is True and fresh["stale"] is False and fresh["source"] == "snapshot"


def test_overview_does_not_change_outbox(fresh_db, empty_audit):
    from app.dashboard import build_overview
    from app.db import connect
    with connect() as con:
        before = con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
    build_overview()
    with connect() as con:
        after = con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
    assert before == after  # read-only


# ── Ф2: logout / endpoint (TestClient) ────────────────────────────────

@pytest.fixture()
def client(fresh_db, monkeypatch):
    monkeypatch.setattr(settings, "server_require_auth", True, raising=False)
    monkeypatch.setattr(settings, "server_allow_lan", False, raising=False)
    from fastapi.testclient import TestClient
    import app.auth as auth
    auth._SESSIONS.clear()
    import app.main as m
    return TestClient(m.app)


def test_overview_requires_auth_then_works(client):
    assert client.get("/api/dashboard/overview").status_code == 401
    client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    client.post("/api/auth/change-password", json={"old_password": "admin", "new_password": "newpass1"})
    r = client.get("/api/dashboard/overview")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_logout_revokes_session(client):
    client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    client.post("/api/auth/change-password", json={"old_password": "admin", "new_password": "newpass1"})
    assert client.get("/api/dashboard/overview").status_code == 200
    client.post("/api/auth/logout")
    client.cookies.clear()
    assert client.get("/api/dashboard/overview").status_code == 401


def test_login_page_public(client):
    assert client.get("/login").status_code == 200


# ── Ф8: developer-mode visibility (ui/mode) ───────────────────────────

def test_ui_mode_hides_engineering_for_operator(monkeypatch):
    import app.server_core as sc
    monkeypatch.setattr(settings, "developer_mode", True, raising=False)
    assert "ai_trace" not in sc.ui_mode("operator")["visible_tabs"]
    assert "ai_trace" in sc.ui_mode("admin")["visible_tabs"]
    monkeypatch.setattr(settings, "developer_mode", False, raising=False)
    assert "ai_trace" not in sc.ui_mode("admin")["visible_tabs"]
