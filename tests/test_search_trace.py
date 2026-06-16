"""Единый поиск и трассировка (Фаза 7-8). Read-only по данным, без AI/1С/outbox-изменений."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import settings

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "search_cases.json").read_text("utf-8"))


def _load(con):
    from app.db import dumps
    for r in FIXTURES["raw_emails"]:
        cols = ",".join(r.keys())
        con.execute(f"INSERT INTO raw_emails({cols}) VALUES ({','.join('?' * len(r))})", tuple(r.values()))
    for c in FIXTURES["cases"]:
        c = dict(c)
        c["fields_json"] = dumps(c.get("fields_json") or {})
        c["payload_json"] = dumps(c.get("payload_json") or {})
        cols = ",".join(c.keys())
        con.execute(f"INSERT INTO cases({cols}) VALUES ({','.join('?' * len(c))})", tuple(c.values()))
    for o in FIXTURES["outbox"]:
        cols = ",".join(o.keys())
        con.execute(f"INSERT INTO outbox({cols}) VALUES ({','.join('?' * len(o))})", tuple(o.values()))
    con.commit()


@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "search.sqlite3", raising=False)
    from app.db import init_db, connect
    init_db()
    with connect() as c:
        _load(c)
    with connect() as c:
        yield c


def _types(res):
    return {r["type"] for r in res["results"]}


def _ids(res, t):
    return {r["id"] for r in res["results"] if r["type"] == t}


# ── detect_query_type ─────────────────────────────────────────────────

def test_detect_types():
    from app.search import detect_query_type
    assert detect_query_type("12345") == "numeric"
    assert detect_query_type("<abc@mail.ru>") == "message_id"
    assert detect_query_type("user@mail.ru") in ("email", "message_id")
    assert detect_query_type("BR-500X") == "part_number"
    assert detect_query_type("брак диск") == "text"
    assert detect_query_type("") == "unknown"


# ── каждый fixture находится нужным способом ───────────────────────────

def test_case_id_search(con):
    from app.search import unified_search
    r = unified_search(con, "2001")
    assert 2001 in _ids(r, "case")
    case = next(x for x in r["results"] if x["type"] == "case" and x["id"] == 2001)
    assert case["open_tab"] == "review" and case["open_params"]["case_id"] == 2001


def test_raw_email_id_search(con):
    from app.search import unified_search
    r = unified_search(con, "1001")
    assert 1001 in _ids(r, "raw_email")
    raw = next(x for x in r["results"] if x["type"] == "raw_email" and x["id"] == 1001)
    assert raw["open_tab"] == "emails"


def test_outbox_id_search(con):
    from app.search import unified_search
    r = unified_search(con, "3001")
    assert 3001 in _ids(r, "outbox")
    ob = next(x for x in r["results"] if x["type"] == "outbox" and x["id"] == 3001)
    assert ob["open_tab"] == "onec" and ob["open_params"]["outbox_id"] == 3001


def test_document_number(con):
    from app.search import unified_search
    assert 2001 in _ids(unified_search(con, "РН-7788"), "case")

def test_numeric_document_number(con):
    from app.search import unified_search
    assert 2002 in _ids(unified_search(con, "778899"), "case")

def test_alphanumeric_part_number(con):
    from app.search import unified_search
    assert 2001 in _ids(unified_search(con, "BR-500X"), "case")

def test_numeric_part_number(con):
    from app.search import unified_search
    assert 2002 in _ids(unified_search(con, "500200"), "case")

def test_message_id(con):
    from app.search import unified_search
    r = unified_search(con, "abc123@mail.ru")
    assert 1001 in _ids(r, "raw_email")
    raw = next(x for x in r["results"] if x["type"] == "raw_email" and x["id"] == 1001)
    assert raw["score"] == 98

def test_text_search_subject(con):
    from app.search import unified_search
    assert 1001 in _ids(unified_search(con, "Брак"), "raw_email")

def test_buyer_search(con):
    from app.search import unified_search
    r = unified_search(con, "autoeuro")
    assert "client" in _types(r) or 2001 in _ids(r, "case")


# ── особые случаи ─────────────────────────────────────────────────────

def test_numeric_searches_more_than_id(con):
    # числовой запрос ищет не только id, но и document/part
    from app.search import unified_search
    r = unified_search(con, "778899")
    case = next(x for x in r["results"] if x["id"] == 2002 and x["type"] == "case")
    assert "document_number" in case["matched_fields"]


def test_numeric_claim_number_searches_json_not_id_only(con):
    from app.search import unified_search
    result = unified_search(con, "00000230135")
    case = next(x for x in result["results"] if x["type"] == "case" and x["id"] == 2006)
    assert case["matched_fields"] == ["claim_number"]
    assert case["score"] == 94


def test_client_request_number_search(con):
    from app.search import unified_search
    result = unified_search(con, "REQ-778")
    case = next(x for x in result["results"] if x["type"] == "case" and x["id"] == 2007)
    assert "client_request_number" in case["matched_fields"]


def test_return_number_search(con):
    from app.search import unified_search
    assert 2008 in _ids(unified_search(con, "RET-901"), "case")


def test_request_number_from_payload_search(con):
    from app.search import unified_search
    result = unified_search(con, "REQUEST-55")
    case = next(x for x in result["results"] if x["type"] == "case" and x["id"] == 2009)
    assert case["matched_fields"] == ["request_number"]


def test_claim_exact_scores_above_fuzzy(con):
    from app.search import unified_search
    result = unified_search(con, "00000230135")
    cases = [x for x in result["results"] if x["type"] == "case"]
    exact = next(x for x in cases if x["id"] == 2006)
    fuzzy = next(x for x in cases if x["id"] == 2010)
    assert exact["score"] > fuzzy["score"]

def test_exact_above_fuzzy(con):
    from app.search import unified_search
    r = unified_search(con, "2001")
    cases = [x for x in r["results"] if x["type"] == "case"]
    assert cases[0]["id"] == 2001  # exact id первым

def test_pre_delivery_refusal_findable_without_document(con):
    from app.search import unified_search
    # находится по part/order, document_number отсутствует
    assert 2003 in _ids(unified_search(con, "REF-9"), "case")

def test_supplier_report_not_marked_return(con):
    from app.search import unified_search
    r = unified_search(con, "2004")
    case = next(x for x in r["results"] if x["id"] == 2004 and x["type"] == "case")
    assert case["status"] == "closed"
    # event_type сохранён, не подменён на return
    assert "return" not in (case["subtitle"] or "").lower() or "supplier_report" in case["subtitle"]

def test_empty_query_no_crash(con):
    from app.search import unified_search
    r = unified_search(con, "")
    assert r["ok"] is False and r["total"] == 0

def test_no_secrets_in_results(con):
    from app.search import unified_search
    blob = json.dumps(unified_search(con, "autoeuro"), ensure_ascii=False, default=str)
    assert "password" not in blob.lower() and "pbkdf2" not in blob


# ── trace ─────────────────────────────────────────────────────────────

def test_trace_from_outbox(con):
    from app.search import trace
    t = trace(con, "outbox", 3001)
    assert t["ok"] and t["raw_email"]["id"] == 1001
    assert 2001 in [c["case_id"] for c in t["cases"]]
    assert 3001 in [o["outbox_id"] for o in t["outbox"]]

def test_trace_from_raw(con):
    from app.search import trace
    t = trace(con, "raw_email", 1001)
    # у письма 1001 несколько кейсов (2001,2002,2005)
    assert {2001, 2002, 2005}.issubset({c["case_id"] for c in t["cases"]})

def test_trace_outbox_error_shown(con):
    from app.search import trace
    t = trace(con, "case", 2002)
    ob = next(o for o in t["outbox"] if o["outbox_id"] == 3002)
    assert "connection refused" in ob["last_error"]

def test_trace_bad_type(con):
    from app.search import trace
    assert trace(con, "bogus", 1)["ok"] is False


# ── API endpoints (TestClient) ────────────────────────────────────────

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "api.sqlite3", raising=False)
    monkeypatch.setattr(settings, "server_require_auth", False, raising=False)
    from app.db import init_db, connect
    init_db()
    with connect() as c:
        _load(c)
    from fastapi.testclient import TestClient
    import app.main as m
    return TestClient(m.app)


def test_api_search_ok(client):
    r = client.get("/api/search?q=2001")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] and j["read_only"] and j["detected_type"] == "numeric"
    assert any(x["type"] == "case" and x["id"] == 2001 for x in j["results"])


def test_api_search_empty_no_422(client):
    r = client.get("/api/search?q=")
    assert r.status_code == 200 and r.json()["ok"] is False


def test_api_search_scope(client):
    r = client.get("/api/search?q=autoeuro&scope=clients")
    assert r.status_code == 200 and r.json()["scope"] == "clients"


def test_api_trace(client):
    r = client.get("/api/search/trace/outbox/3001")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] and j["raw_email"]["id"] == 1001 and j["read_only"]


# ── UI статические проверки ───────────────────────────────────────────

def _read(p):
    return (Path(__file__).resolve().parent.parent / p).read_text("utf-8")


def test_ui_global_search_present():
    html = _read("app/web/index.html")
    assert 'id="gsearch-input"' in html and 'id="gsearch-dropdown"' in html

def test_ui_global_search_js():
    js = _read("app/web/static/app.js")
    assert "runGlobalSearch" in js and '/api/search?q=' in js
    assert "openSearchResult" in js and "activateTab(tab" in js
    assert 'e.key === "k"' in js  # Ctrl+K
