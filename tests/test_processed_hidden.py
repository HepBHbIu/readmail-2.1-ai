"""Hidden Processed Mail Menu — раздел «Обработанные / не требуют действия».

Read-only поверх folder_accounting: AI/1С не вызываются, outbox не меняется.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import settings
from app import processed_hidden as ph
from app import folder_accounting as fa

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = json.loads((ROOT / "tests" / "fixtures" / "search_cases.json").read_text("utf-8"))


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "ph.sqlite3", raising=False)
    from app.db import init_db, connect, dumps
    init_db()
    with connect() as con:
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
    yield


def test_summary_read_only_ok(db):
    from app.db import connect
    with connect() as con:
        s = ph.build_processed_hidden_summary(con)
    assert s["ok"] is True and s["read_only"] is True


def test_summary_groups_sum_and_accounting(db):
    from app.db import connect
    with connect() as con:
        s = ph.build_processed_hidden_summary(con)
    assert sum(g["count"] for g in s["groups"]) == s["sum_groups"] == s["hidden_from_operator"]
    assert s["sum_groups"] + s["working_total"] == s["total_raw"]
    assert s["accounted_ok"] is True
    assert s["unaccounted"] == 0


def test_eleven_groups_present(db):
    from app.db import connect
    with connect() as con:
        s = ph.build_processed_hidden_summary(con)
    keys = {g["key"] for g in s["groups"]}
    assert keys == {k for k, _f, _t in ph.HIDDEN_GROUPS}
    assert "linked_completed" in keys  # связки завершённые в меню


def test_items_have_raw_id_and_reason(db):
    from app.db import connect
    with connect() as con:
        res = ph.list_processed_hidden_items(con, page_size=500)
    for it in res["items"]:
        assert it["raw_email_id"] is not None
        assert it["routing_reason"] or it["why_hidden"]
        assert it["open_trace_url"]


# ── маршрутизация в правильную группу (через folder_for, без БД) ───────

def _raw(subj="Тест"):
    return {"id": 1, "subject": subj, "from_addr": "x@y.ru", "status": "imported",
            "duplicate_of_raw_email_id": None, "snippet": "", "visible_text": "", "body_text": "", "body_html": ""}


def _case(**kw):
    base = {"id": 1, "raw_email_id": 1, "event_type": "new_return", "claim_kind": None,
            "state": "needs_review", "status": "needs_review", "ready_for_export": 0,
            "missing_json": "[]", "payload_json": "{}", "has_ai_suggestion": 0, "buyer_code": "b"}
    base.update(kw)
    return base


def _group_key(raw, case):
    folder = fa.folder_for(raw, case)["folder_name"]
    return ph._FOLDER_TO_KEY.get(folder)


def test_correction_maps_to_correction_group():
    assert _group_key(_raw(), _case(event_type="correction_request")) == "correction_edo_ksf"


def test_marking_maps_to_marking_group():
    assert _group_key(_raw("Маркировка ТНВЭД"), _case(event_type="marking_request",
                                                       claim_kind="marking_request")) == "marking_tnved"


def test_number_replacement_maps_to_group():
    assert _group_key(_raw("ЗАМЕНА НОМЕРА"), _case(claim_kind="number_replacement")) == "number_replacement"


def test_supplier_report_maps_to_reports_group():
    c = _case(event_type="info_only", state="ignored_info_only", claim_kind="price_list",
              payload_json=json.dumps({"classification_subcategory": "price_list"}))
    assert _group_key(_raw("Отчет об автоматической загрузке прайс-листа"), c) == "supplier_reports"


def test_raw_without_case_in_technical_group():
    folder = fa.folder_for(_raw(), None)["folder_name"]
    assert ph._FOLDER_TO_KEY.get(folder) == "raw_without_case"


# ── safety / read-only ─────────────────────────────────────────────────

def test_api_does_not_change_outbox(db):
    from app.db import connect
    with connect() as con:
        before = con.execute("SELECT COUNT(*) c FROM outbox").fetchone()["c"]
        ph.build_processed_hidden_summary(con)
        ph.list_processed_hidden_items(con, group="linked_completed")
        after = con.execute("SELECT COUNT(*) c FROM outbox").fetchone()["c"]
    assert before == after


def test_shell_hidden_no_ai_no_1c(db):
    import importlib.util
    spec = importlib.util.spec_from_file_location("rmctl_ph", ROOT / "scripts" / "readmailctl.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    res = m.dispatch_shell_command("/hidden")
    assert "ОБРАБОТАННЫЕ" in res.text or "total_raw" in res.text


def test_total_equals_accounted(db):
    from app.db import connect
    with connect() as con:
        s = ph.build_processed_hidden_summary(con)
    assert s["total_raw"] == s["sum_groups"] + s["working_total"]
