"""Readmail Interactive Shell v1 (Фаза 10). Read-only кроме /pause /resume; 1С/AI не вызываются."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from app.config import settings

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = json.loads((ROOT / "tests" / "fixtures" / "search_cases.json").read_text("utf-8"))


def _load_rmctl():
    spec = importlib.util.spec_from_file_location("readmailctl_shell", ROOT / "scripts" / "readmailctl.py")
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
def sh(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "shell.sqlite3", raising=False)
    from app.db import init_db, connect
    init_db()
    with connect() as c:
        _load_fixtures(c)
    mod = _load_rmctl()
    monkeypatch.setattr(mod, "_bootstrap_db", lambda: None)
    return mod


def _outbox_snapshot():
    from app.db import connect
    with connect() as con:
        return [dict(r) for r in con.execute("SELECT id, status, payload_json FROM outbox ORDER BY id")]


def _run(sh, line):
    return sh.dispatch_shell_command(line)


# ── 1. argparse регистрирует shell/sh ─────────────────────────────────

def test_argparse_registers_shell():
    src = (ROOT / "scripts" / "readmailctl.py").read_text("utf-8")
    assert 'add_parser("shell"' in src and 'add_parser("sh"' in src
    assert '"--shell"' in src  # tui --shell


# ── 2. /help ──────────────────────────────────────────────────────────

def test_help(sh):
    for q in ("/", "/help"):
        r = _run(sh, q)
        assert "SYSTEM" in r.text and "DANGEROUS" in r.text

def test_unknown_command_hint(sh):
    r = _run(sh, "/bogus")
    assert "Неизвестная команда" in r.text

def test_non_slash_hint(sh):
    assert "начинаются с /" in _run(sh, "hello").text


# ── 3-6. read-only команды ────────────────────────────────────────────

def test_status_read_only(sh):
    before = _outbox_snapshot()
    r = _run(sh, "/status")
    assert "READMAIL MONITOR" in r.text
    assert _outbox_snapshot() == before

def test_search_read_only(sh):
    before = _outbox_snapshot()
    r = _run(sh, "/search 2001")
    assert "КЕЙСЫ" in r.text and "#2001" in r.text
    assert _outbox_snapshot() == before

def test_trace_read_only(sh):
    before = _outbox_snapshot()
    r = _run(sh, "/trace case 2001")
    assert "ПИСЬМО" in r.text and "OUTBOX" in r.text
    assert _outbox_snapshot() == before

def test_trace_raw_alias(sh):
    r = _run(sh, "/trace raw 1001")
    assert "КЕЙС" in r.text

def test_outbox_preview_read_only(sh):
    before = _outbox_snapshot()
    r = _run(sh, "/outbox preview 5")
    assert "PREVIEW" in r.text and "1С НЕ вызывается" in r.text
    assert _outbox_snapshot() == before

def test_outbox_summary(sh):
    r = _run(sh, "/outbox")
    assert "автодоставка" in r.text.lower() or "ВЫКЛЮЧЕНА" in r.text


# ── 7. /settings без секретов ─────────────────────────────────────────

def test_settings_no_secrets(sh, monkeypatch):
    monkeypatch.setattr(settings, "one_c_http_url", "https://1c.local/secret-token-abc", raising=False)
    monkeypatch.setattr(settings, "server_session_secret", "TOPSECRET", raising=False)
    for sec in ("", "runtime", "import", "workers", "ai", "onec", "auth", "paths", "env-safe"):
        txt = _run(sh, f"/settings {sec}").text
        low = txt.lower()
        assert "topsecret" not in low
        assert "secret-token-abc" not in txt  # полный URL не печатается
        assert "pbkdf2" not in low

def test_settings_onec_url_masked(sh, monkeypatch):
    monkeypatch.setattr(settings, "one_c_http_url", "https://1c.local/abc", raising=False)
    txt = _run(sh, "/settings onec").text
    assert "http_url_present" in txt and "https://1c.local" not in txt

def test_settings_ai_no_key(sh):
    txt = _run(sh, "/settings ai").text
    assert "api_key" in txt and "скрыто" in txt


# ── 8. /ai patterns=0 tokens ──────────────────────────────────────────

def test_ai_patterns_zero_tokens(sh):
    assert "0 tokens" in _run(sh, "/ai").text

def test_ai_disabled_no_crash(sh, monkeypatch):
    monkeypatch.setattr(settings, "enable_ai", False, raising=False)
    assert "AI BRAIN" in _run(sh, "/ai").text


# ── 9-10. опасные команды отключены ───────────────────────────────────

def test_deliver_disabled(sh):
    before = _outbox_snapshot()
    r = _run(sh, "/deliver")
    assert "отключена" in r.text
    assert _outbox_snapshot() == before

def test_ai_batch_disabled(sh):
    r = _run(sh, "/ai batch")
    assert "отключена" in r.text or "не запускается" in r.text

@pytest.mark.parametrize("cmd", ["/reset", "/cleanup", "/mass-import"])
def test_dangerous_disabled(sh, cmd):
    assert "отключена" in _run(sh, cmd).text


# ── 11-12. pause/resume меняют только runtime_flags ───────────────────

def test_pause_only_runtime_flags(sh):
    before = _outbox_snapshot()
    r = _run(sh, "/pause import")
    assert "import" in r.text
    assert _outbox_snapshot() == before  # outbox не тронут
    from app.db import connect
    with connect() as con:
        flags = con.execute("SELECT COUNT(*) FROM runtime_flags").fetchone()[0]
    assert flags >= 1

def test_resume_works(sh):
    assert "off" in _run(sh, "/resume all").text.lower() or "Global" in _run(sh, "/resume all").text

def test_pause_unknown_worker(sh):
    assert "❌" in _run(sh, "/pause bogus").text


# ── 13. /doctor не меняет БД ───────────────────────────────────────────

def test_doctor_read_only(sh):
    before = _outbox_snapshot()
    r = _run(sh, "/doctor")
    assert "DOCTOR" in r.text and "DB: OK" in r.text
    assert _outbox_snapshot() == before


# ── 14. exit ──────────────────────────────────────────────────────────

def test_exit(sh):
    for q in ("/quit", "/exit", "/q"):
        assert _run(sh, q).should_exit is True

def test_empty_input(sh):
    r = _run(sh, "")
    assert r.text == "" and r.should_exit is False


# ── header без секретов ───────────────────────────────────────────────

def test_header_no_secrets(sh, monkeypatch):
    monkeypatch.setattr(settings, "server_session_secret", "TOPSECRET", raising=False)
    assert "TOPSECRET" not in sh.render_shell_header()
    assert "READMAIL CONTROL SHELL" in sh.render_shell_header()
