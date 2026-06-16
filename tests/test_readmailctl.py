"""readmailctl terminal monitor / CLI search/trace/outbox (Фаза 9).

Все проверки: read-only по данным (кроме pause/resume → runtime_flags), без секретов,
AI/1С не вызываются, real outbox не меняется.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from app.config import settings

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = json.loads((ROOT / "tests" / "fixtures" / "search_cases.json").read_text("utf-8"))


def _load_rmctl():
    spec = importlib.util.spec_from_file_location("readmailctl", ROOT / "scripts" / "readmailctl.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_fixtures(con):
    from app.db import dumps
    for r in FIXTURES["raw_emails"]:
        con.execute(f"INSERT INTO raw_emails({','.join(r)}) VALUES ({','.join('?'*len(r))})", tuple(r.values()))
    for c in FIXTURES["cases"]:
        c = dict(c)
        c["fields_json"] = dumps(c.get("fields_json") or {})
        c["payload_json"] = dumps(c.get("payload_json") or {})
        con.execute(f"INSERT INTO cases({','.join(c)}) VALUES ({','.join('?'*len(c))})", tuple(c.values()))
    for o in FIXTURES["outbox"]:
        con.execute(f"INSERT INTO outbox({','.join(o)}) VALUES ({','.join('?'*len(o))})", tuple(o.values()))
    con.commit()


@pytest.fixture()
def rmctl(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "ctl.sqlite3", raising=False)
    from app.db import init_db, connect
    init_db()
    with connect() as c:
        _load_fixtures(c)
    mod = _load_rmctl()
    monkeypatch.setattr(mod, "_bootstrap_db", lambda: None)  # не трогать боевую БД
    return mod


def _outbox_snapshot():
    from app.db import connect
    with connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT id, status, payload_json, event_type FROM outbox ORDER BY id")]


# ── Фаза 2: aggregator + render ───────────────────────────────────────

def test_collect_status_keys(rmctl):
    st = rmctl.collect_terminal_status()
    for k in ("server", "auth", "mail", "processing", "workers", "outbox", "ai", "warnings", "next_actions"):
        assert k in st

def test_render_contains_monitor_and_patterns(rmctl):
    txt = rmctl.render_tui_status(rmctl.collect_terminal_status())
    assert "READMAIL MONITOR" in txt
    assert "0 токенов" in txt

def test_render_shows_delivery_off(rmctl):
    txt = rmctl.render_tui_status(rmctl.collect_terminal_status())
    assert "OFF" in txt or "off" in txt  # delivery выключена по умолчанию

def test_status_no_secrets(rmctl, monkeypatch):
    monkeypatch.setattr(settings, "server_session_secret", "TOPSECRET", raising=False)
    blob = json.dumps(rmctl.collect_terminal_status(), default=str)
    assert "TOPSECRET" not in blob and "pbkdf2" not in blob

def test_status_read_only(rmctl):
    before = _outbox_snapshot()
    rmctl.collect_terminal_status()
    assert _outbox_snapshot() == before


# ── Фаза 4: search ────────────────────────────────────────────────────

def test_search_case_id(rmctl):
    r = rmctl.do_search("2001")
    assert any(x["type"] == "case" and x["id"] == 2001 for x in r["results"])

def test_search_raw_email_id(rmctl):
    r = rmctl.do_search("1001")
    assert any(x["type"] == "raw_email" and x["id"] == 1001 for x in r["results"])

def test_search_outbox_id(rmctl):
    r = rmctl.do_search("3001")
    assert any(x["type"] == "outbox" and x["id"] == 3001 for x in r["results"])

def test_search_part_number(rmctl):
    r = rmctl.do_search("BR-500X")
    assert any(x["type"] == "case" and x["id"] == 2001 for x in r["results"])

def test_search_empty_no_crash(rmctl):
    r = rmctl.do_search("")
    assert r["ok"] is False

def test_search_read_only(rmctl):
    before = _outbox_snapshot()
    rmctl.do_search("2001")
    assert _outbox_snapshot() == before


# ── Фаза 5: trace ─────────────────────────────────────────────────────

def test_trace_case(rmctl):
    t = rmctl.do_trace("case", 2001)
    assert t["ok"] and t["raw_email"]["id"] == 1001
    assert 3001 in [o["outbox_id"] for o in t["outbox"]]

def test_trace_raw(rmctl):
    t = rmctl.do_trace("raw_email", 1001)
    assert {2001, 2002}.issubset({c["case_id"] for c in t["cases"]})

def test_trace_outbox(rmctl):
    t = rmctl.do_trace("outbox", 3001)
    assert t["raw_email"]["id"] == 1001 and 2001 in [c["case_id"] for c in t["cases"]]

def test_trace_no_secrets(rmctl):
    blob = json.dumps(rmctl.do_trace("case", 2001), default=str)
    assert "password" not in blob.lower() and "pbkdf2" not in blob


# ── Фаза 6: outbox summary / preview ──────────────────────────────────

def test_outbox_summary_read_only(rmctl):
    before = _outbox_snapshot()
    s = rmctl.outbox_summary()
    assert "by_status" in s and "control_events" in s and "business_events" in s
    assert _outbox_snapshot() == before

def test_outbox_preview_read_only(rmctl):
    before = _outbox_snapshot()
    p = rmctl.outbox_preview(limit=5)
    assert p["read_only"] is True and p["items"]
    assert _outbox_snapshot() == before

def test_outbox_preview_business_control(rmctl):
    p = rmctl.outbox_preview(ids=[3001])
    assert p["items"][0]["kind"] == "business"  # return_ready

def test_outbox_preview_debug_warning(rmctl):
    p = rmctl.outbox_preview(ids=[3001], profile="debug")
    assert "warning" in p

def test_outbox_preview_no_real_delivery(rmctl):
    p = rmctl.outbox_preview(limit=2)
    assert "1С НЕ вызывается" in p["delivery"]


# ── Фаза 7: pause/resume ──────────────────────────────────────────────

def test_pause_resume_all(rmctl):
    from app import runtime_control
    assert runtime_control.pause("all")["ok"]
    assert runtime_control.resume("all")["ok"]

def test_pause_import(rmctl):
    from app import runtime_control
    assert runtime_control.pause("import")["ok"]

def test_pause_unknown_worker(rmctl):
    from app import runtime_control
    assert runtime_control.pause("bogus")["ok"] is False

def test_render_runtime_unknown(rmctl):
    txt = rmctl._render_runtime({"ok": False, "error": "unknown worker: bogus", "workers": ["import"]})
    assert "❌" in txt and "bogus" in txt
