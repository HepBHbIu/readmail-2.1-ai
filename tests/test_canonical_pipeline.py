"""Canonical Pipeline — 6 routes × reason_group. Read-only: AI/1С не вызываются, outbox не меняется."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import settings
from app import canonical_pipeline as cp
from app import folder_accounting as fa

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = json.loads((ROOT / "tests" / "fixtures" / "search_cases.json").read_text("utf-8"))


def _raw(subj="Тест", **kw):
    base = {"id": 1, "subject": subj, "from_addr": "x@y.ru", "status": "imported",
            "duplicate_of_raw_email_id": None, "snippet": "", "visible_text": "", "body_text": "", "body_html": ""}
    base.update(kw); return base


def _case(**kw):
    base = {"id": 10, "raw_email_id": 1, "event_type": "new_return", "claim_kind": "quality_refusal",
            "state": "needs_review", "status": "needs_review", "ready_for_export": 0, "needs_ai": 0,
            "missing_json": "[]", "buyer_code": "b", "thread_key": None, "has_ai_suggestion": 0,
            "fields_json": json.dumps({"part_number": "ABC123", "document_number": "82412"}),
            "payload_json": json.dumps({"evidence_gate": {"passed": True, "blocking_errors": [],
                                        "field_statuses": {"part_number": "confirmed_by_table_column",
                                                           "document_number": "confirmed_by_document_label",
                                                           "claim_kind": "confirmed"}}})}
    base.update(kw); return base


# ── pure routing ──────────────────────────────────────────────────────

def test_strong_evidence_ready_for_operator():
    c = cp.canonical_for(_raw(), _case())
    assert c["canonical_route"] == cp.READY_FOR_OPERATOR
    assert c["reason_group"] == "quality_refusal"
    assert c["can_send_to_1c"] is True


def test_weak_evidence_ai_assist():
    c = cp.canonical_for(_raw(), _case(
        missing_json=json.dumps(["document_number"]),
        fields_json=json.dumps({"part_number": "ABC123"}),
        payload_json=json.dumps({"evidence_gate": {"passed": False, "blocking_errors": [],
                                 "field_statuses": {"document_number": "missing_processed"}}})))
    assert c["canonical_route"] == cp.AI_ASSIST
    assert c["needs_ai"] is True


def test_conflict_or_missing_goes_manual_or_ai():
    # совсем нет полей + нет evidence → manual_review
    c = cp.canonical_for(_raw(), _case(
        missing_json=json.dumps(["document_number", "part_number", "quantity"]),
        fields_json=json.dumps({}),
        payload_json=json.dumps({"evidence_gate": {"passed": False, "blocking_errors": ["x"]}})))
    assert c["canonical_route"] in (cp.MANUAL_REVIEW, cp.AI_ASSIST)


def test_supplier_report_archive():
    c = _case(event_type="info_only", state="ignored_info_only", claim_kind="price_list",
              payload_json=json.dumps({"classification_subcategory": "price_list"}))
    out = cp.canonical_for(_raw("Отчет об автоматической загрузке прайс-листа"), c)
    assert out["canonical_route"] == cp.NO_ACTION_ARCHIVE
    assert out["reason_group"] == "supplier_report"


def test_duplicate_archive():
    out = cp.canonical_for(_raw(status="duplicate", duplicate_of_raw_email_id=5), _case())
    assert out["canonical_route"] == cp.NO_ACTION_ARCHIVE
    assert out["reason_group"] == "duplicate"


def test_completed_linked_archive():
    out = cp.canonical_for(_raw(), _case(event_type="followup_dialog", state="linked_event", claim_kind=None))
    assert out["canonical_route"] == cp.NO_ACTION_ARCHIVE
    assert out["reason_group"] == "linked_completed"


def test_reminder_without_parent_manual_find_parent():
    out = cp.canonical_for(_raw("напоминаю, когда ответ?"),
                           _case(event_type="followup_reminder", state="needs_link", claim_kind=None, thread_key="T1"),
                           parents={})
    assert out["canonical_route"] == cp.MANUAL_REVIEW
    assert out["link_type"] == "followup_reminder"
    assert out["parent_case_id"] is None
    assert out["priority_flag"] is True


def test_reminder_with_parent_update_route():
    parents = {"T1": {"id": 999, "raw_email_id": 1, "event_type": "new_return"}}
    out = cp.canonical_for(_raw(), _case(id=11, event_type="followup_reminder", state="needs_link",
                                         claim_kind=None, thread_key="T1"), parents=parents)
    assert out["canonical_route"] == cp.MANUAL_REVIEW
    assert out["parent_case_id"] == 999


def test_marking_is_reason_not_service():
    out = cp.canonical_for(_raw("Маркировка ТНВЭД код"),
                           _case(event_type="marking_request", claim_kind="marking_request",
                                 fields_json=json.dumps({"part_number": "ABC123"})))
    assert out["reason_group"] == "marking"
    assert out["canonical_route"] in (cp.READY_FOR_OPERATOR, cp.AI_ASSIST, cp.MANUAL_REVIEW)
    assert out["canonical_route"] != cp.NO_ACTION_ARCHIVE


def test_number_replacement_is_reason_not_service():
    out = cp.canonical_for(_raw("ЗАМЕНА НОМЕРА"),
                           _case(claim_kind="number_replacement",
                                 fields_json=json.dumps({"part_number": "ABC123"})))
    assert out["reason_group"] == "number_replacement"
    assert out["canonical_route"] != cp.NO_ACTION_ARCHIVE


def test_pre_delivery_does_not_require_document():
    ok, missing = cp.required_fields_ok("pre_delivery_refusal",
                                        {"part_number": "ABC123"})
    assert ok is True and "document_number" not in missing
    out = cp.canonical_for(_raw("Запрос на снятие. Отказ клиента"),
                           _case(event_type="pre_delivery_refusal",
                                 fields_json=json.dumps({"part_number": "ABC123"}),
                                 payload_json=json.dumps({"pre_delivery_refusal": True,
                                                          "evidence_gate": {"passed": True, "blocking_errors": []}})))
    assert out["reason_group"] == "pre_delivery_refusal"
    assert out["canonical_route"] in (cp.READY_FOR_OPERATOR, cp.AI_ASSIST)


def test_defect_vision_off_metadata_flags(monkeypatch):
    monkeypatch.setattr(settings, "ai_vision_enabled", False, raising=False)
    payload = {"defect_doc_flag": {"state": "present_unverified"},
               "quality": {"evidence": {"attachments_count": 1, "has_photo": True, "has_service_document": False}}}
    out = cp.canonical_for(_raw(), _case(claim_kind="defect", payload_json=json.dumps(payload),
                                         fields_json=json.dumps({"part_number": "ABC123"})))
    assert out["reason_group"] == "defect"
    assert out["defect_documents_status"] in ("metadata_only", "unknown_not_read")
    assert out["operator_attention"] is True


def test_multi_item_detection():
    payload = {"multi_item_count": 3, "table_items": [1, 2, 3]}
    out = cp.canonical_for(_raw(), _case(payload_json=json.dumps(payload)))
    assert out["is_multi_item"] is True
    assert out["needs_split"] is True
    assert out["item_count_estimate"] == 3


def test_static_uplift_autoeuro_phrase():
    up = cp.static_uplift(_raw("Авто-Евро: Отказ покупателя"),
                          {"buyer_code": "autoeuro_ru"}, {"part_number": "ABC123"}, "quality_refusal")
    assert up == "strong"


# ── DB-backed: sum invariants ──────────────────────────────────────────

@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "cp.sqlite3", raising=False)
    from app.db import init_db, connect, dumps
    init_db()
    with connect() as con:
        for r in FIXTURES["raw_emails"]:
            con.execute(f"INSERT INTO raw_emails({','.join(r)}) VALUES ({','.join('?'*len(r))})", tuple(r.values()))
        for c in FIXTURES["cases"]:
            c = dict(c); c["fields_json"] = dumps(c.get("fields_json") or {})
            c["payload_json"] = dumps(c.get("payload_json") or {})
            con.execute(f"INSERT INTO cases({','.join(c)}) VALUES ({','.join('?'*len(c))})", tuple(c.values()))
        for o in FIXTURES["outbox"]:
            con.execute(f"INSERT INTO outbox({','.join(o)}) VALUES ({','.join('?'*len(o))})", tuple(o.values()))
        con.commit()
    yield


def test_sum_route_equals_total(db):
    from app.db import connect
    with connect() as con:
        acc = cp.build_pipeline_accounting(con)
    assert acc["total_raw"] == acc["accounted"] == sum(acc["by_route"].values())
    assert acc["unaccounted"] == 0


def test_every_item_has_route_and_reason(db):
    from app.db import connect
    with connect() as con:
        acc = cp.build_pipeline_accounting(con, include_items=True)
    for it in acc["items"]:
        assert it["canonical_route"] in cp.ALL_ROUTES
        assert it["reason_group"]


def test_reason_sum_inside_route(db):
    from app.db import connect
    with connect() as con:
        acc = cp.build_pipeline_accounting(con)
    for r in cp.ALL_ROUTES:
        assert sum(acc["reason_in_route"][r].values()) == acc["by_route"][r]


def test_reports_read_only_outbox_unchanged(db):
    from app.db import connect
    with connect() as con:
        before = con.execute("SELECT COUNT(*) c FROM outbox").fetchone()["c"]
        cp.build_pipeline_accounting(con, include_items=True)
        cp.list_pipeline_items(con, route="ai_assist")
        after = con.execute("SELECT COUNT(*) c FROM outbox").fetchone()["c"]
    assert before == after
