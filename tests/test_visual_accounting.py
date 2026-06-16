"""Visual Accounting + Safety Router: каждое письмо учтено, сумма buckets == total raw.

Read-only: AI/1С не вызываются, outbox не меняется.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from app.config import settings
from app import visual_accounting as va

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = json.loads((ROOT / "tests" / "fixtures" / "search_cases.json").read_text("utf-8"))


def _case(**kw):
    base = {"id": 1, "raw_email_id": 1, "buyer_code": "b", "event_type": "new_return",
            "claim_kind": "quality_refusal", "state": "needs_review", "status": "needs_review",
            "ready_for_export": 0, "needs_ai": 0, "missing_json": "[]", "payload_json": "{}",
            "has_ai_suggestion": 0}
    base.update(kw)
    return base


def _raw(**kw):
    base = {"id": 1, "subject": "Тест", "from_addr": "x@y.ru", "status": "imported",
            "duplicate_of_raw_email_id": None}
    base.update(kw)
    return base


# ── visible_bucket: маршрутизация по типам ─────────────────────────────

def test_raw_without_case_is_technical():
    vb = va.visible_bucket(_raw(), None)
    assert vb["visible_bucket"] == va.TECH_RAW_WITHOUT_CASE
    assert vb["requires_action"] is True


def test_duplicate_bucket():
    vb = va.visible_bucket(_raw(status="duplicate", duplicate_of_raw_email_id=5), _case())
    assert vb["visible_bucket"] == va.TERMINAL_DUPLICATE
    assert vb["requires_action"] is False


def test_supplier_report_not_ready_to_1c():
    c = _case(event_type="info_only", state="ignored_info_only", claim_kind="price_list",
              payload_json=json.dumps({"classification_subcategory": "price_list"}))
    vb = va.visible_bucket(_raw(subject="Отчет об автоматической загрузке прайс-листа"), c)
    assert vb["visible_bucket"] == va.TERMINAL_SUPPLIER_REPORT
    assert vb["visible_bucket"] != va.ACTION_READY_1C
    assert vb["requires_action"] is False


def test_completed_followup_not_in_review():
    c = _case(event_type="followup_dialog", state="linked_event", claim_kind=None)
    vb = va.visible_bucket(_raw(), c)
    assert vb["visible_bucket"] == va.TERMINAL_LINKED_COMPLETED
    assert vb["visible_bucket"] != va.ACTION_REVIEW


def test_active_followup_in_linked_active_or_review():
    c = _case(event_type="followup_reminder", state="needs_link", claim_kind=None)
    vb = va.visible_bucket(_raw(), c)
    assert vb["visible_bucket"] in {va.TERMINAL_LINKED_ACTIVE, va.ACTION_REVIEW}
    assert vb["requires_action"] is True


def test_marking_gets_service_bucket():
    c = _case(event_type="marking_request", claim_kind="marking_request", state="needs_review")
    vb = va.visible_bucket(_raw(subject="Маркировка/ТНВЭД"), c)
    assert vb["visible_bucket"] == va.TERMINAL_SERVICE


def test_pre_delivery_refusal_does_not_require_document_number():
    c = _case(event_type="pre_delivery_refusal", claim_kind="quality_refusal", state="needs_review",
              payload_json=json.dumps({"pre_delivery_refusal": True, "document_required": False}))
    dec = va.route_case_for_operator(c)
    # не идёт в ready_to_1c из-за отсутствия документа; document не требуется
    assert dec["decision"] in {"manual_review", "ai_assist"}
    vb = va.visible_bucket(_raw(), c)
    assert vb["visible_bucket"] != va.ACTION_READY_1C


def test_weak_evidence_not_ready_to_1c():
    c = _case(state="needs_review", ready_for_export=0, missing_json=json.dumps(["document_number"]),
              payload_json=json.dumps({"evidence_gate": {"passed": False, "blocking_errors": ["x"]}}))
    vb = va.visible_bucket(_raw(), c)
    assert vb["visible_bucket"] in {va.ACTION_AI_ASSIST, va.ACTION_REVIEW}
    assert vb["visible_bucket"] != va.ACTION_READY_1C


def test_strong_evidence_ready_can_go_to_1c():
    c = _case(state="ready_to_1c", ready_for_export=1, missing_json="[]",
              payload_json=json.dumps({"evidence_gate": {"passed": True, "blocking_errors": []}}))
    dec = va.route_case_for_operator(c)
    assert dec["decision"] == "ready_to_1c"
    vb = va.visible_bucket(_raw(), c)
    assert vb["visible_bucket"] == va.ACTION_READY_1C


# ── build_visual_accounting на тестовой БД: сумма == total ─────────────

@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "va.sqlite3", raising=False)
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


def test_sum_equals_total_and_no_unaccounted(db):
    from app.db import connect
    with connect() as con:
        s = va.build_visual_accounting(con)
    assert s["total_raw"] == s["accounted"]
    assert s["unaccounted"] == 0
    assert sum(s["by_bucket"].values()) == s["total_raw"]


def test_every_raw_has_bucket(db):
    from app.db import connect
    with connect() as con:
        s = va.build_visual_accounting(con, include_items=True)
    assert all(it["visible_bucket"] in va.ALL_BUCKETS for it in s["items"])
    assert len(s["items"]) == s["total_raw"]


def test_build_does_not_change_outbox(db):
    from app.db import connect
    with connect() as con:
        before = con.execute("SELECT COUNT(*) c FROM outbox").fetchone()["c"]
        va.build_visual_accounting(con)
        va.decision_for_case(con, FIXTURES["cases"][0]["id"])
        after = con.execute("SELECT COUNT(*) c FROM outbox").fetchone()["c"]
    assert before == after


def test_decision_for_case_read_only(db):
    from app.db import connect
    with connect() as con:
        d = va.decision_for_case(con, FIXTURES["cases"][0]["id"])
    assert d["ok"] is True
    assert "visible_bucket" in d and "safety_router_result" in d


# ── shell /buckets и /decision read-only ───────────────────────────────

def _load_rmctl():
    spec = importlib.util.spec_from_file_location("readmailctl_va", ROOT / "scripts" / "readmailctl.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_shell_buckets_command(db, monkeypatch):
    m = _load_rmctl()
    monkeypatch.setattr("app.db.settings.database_path", settings.database_path, raising=False)
    res = m.dispatch_shell_command("/buckets")
    assert "TOTAL RAW" in res.text and "UNACCOUNTED" in res.text


def test_shell_decision_command(db):
    m = _load_rmctl()
    res = m.dispatch_shell_command(f"/decision case {FIXTURES['cases'][0]['id']}")
    assert "visible_bucket" in res.text
