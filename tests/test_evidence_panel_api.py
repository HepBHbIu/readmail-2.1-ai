from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app import evidence_panel
from app.main import app


def _jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _setup_files(tmp_path: Path, monkeypatch) -> Path:
    summary = tmp_path / "summary.json"
    suppliers = tmp_path / "suppliers.json"
    cases = tmp_path / "cases.jsonl"
    quick = tmp_path / "quick.jsonl"
    safe = tmp_path / "safe.jsonl"
    warning = tmp_path / "warning.jsonl"
    staging = tmp_path / "staging.jsonl"
    ledger = tmp_path / "learning.jsonl"
    actions = tmp_path / "actions.jsonl"
    final_sorting = tmp_path / "final_sorting.jsonl"
    ai_trace = tmp_path / "ai_trace.jsonl"
    defect_audit = tmp_path / "defect_audit.json"
    inbox_sorting = tmp_path / "inbox_sorting.jsonl"
    inbox_summary = tmp_path / "inbox_sorting_summary.json"
    raw_without_summary = tmp_path / "raw_without_cases.json"
    imap_reconcile_summary = tmp_path / "imap_reconcile_summary.json"
    database = tmp_path / "readmail.sqlite3"

    summary.write_text(
        json.dumps(
            {
                "created_at": "2026-06-10T03:00:00+03:00",
                "total_cases": 10,
                "eligible_return_cases": 6,
                "runtime_errors": 0,
                "by_final_dry_run_class": {
                    "auto_export_safe": 2,
                    "auto_export_with_warning": 3,
                    "suspicious_passed": 0,
                    "quick_review": 1,
                    "human_review": 0,
                    "blocked": 4,
                },
            }
        ),
        encoding="utf-8",
    )
    suppliers.write_text(
        json.dumps(
            {
                "suppliers": [
                    {
                        "buyer_code": "supplier_a",
                        "total_cases": 10,
                        "eligible_return_cases": 6,
                        "auto_export_safe": 2,
                        "auto_export_with_warning": 3,
                        "quick_review": 1,
                        "human_review": 0,
                        "blocked": 4,
                        "top_5_blocking_reasons": {"quantity:weak": 2},
                        "top_5_warnings": {"buyer:pattern_only": 1},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    quick_item = {
        "review_id": "7:quantity:weak",
        "review_type": "quantity_choice",
        "case_id": 7,
        "raw_email_id": 70,
        "buyer_code": "supplier_a",
        "field": "quantity",
        "reason": "quantity:weak",
        "current_value": 1,
        "candidates": [{"value": 2, "label": "2 шт.", "evidence": "Арт. A1, шт. 2"}],
        "source_snippet": "Арт. A1, шт. 2",
        "one_click": True,
    }
    _jsonl(quick, [quick_item])
    case = {
        "case_id": 7,
        "raw_email_id": 70,
        "subject": "Возврат",
        "current_state": "needs_review",
        "event_type": "new_return",
        "claim_kind": "quality_refusal",
        "second_gate": {"passed": True},
        "evidence_repair": {"changed": False, "case_data": {"payload": {"processing_source": "pattern"}}},
        "final_dry_run_class": "auto_export_safe",
        "safety": {"safety_class": "auto_export_safe"},
    }
    _jsonl(cases, [case])
    preview = {
        "case_id": 7,
        "raw_email_id": 70,
        "buyer_code": "supplier_a",
        "safety_class": "auto_export_safe",
        "export_allowed": True,
        "one_c_payload_preview": {"claim": {"kind": "quality_refusal"}, "items": [{"part_number": "A1", "quantity": 2}]},
    }
    _jsonl(safe, [preview])
    _jsonl(warning, [{**preview, "case_id": 8, "safety_class": "auto_export_with_warning"}])
    staged = {
        **preview,
        "idempotency_key": "key-7",
        "status": "staged",
        "staged_at": "2026-06-10T00:00:00+00:00",
    }
    _jsonl(staging, [staged])
    _jsonl(
        final_sorting,
        [
            {
                "case_id": 7,
                "raw_email_id": 70,
                "buyer_code": "supplier_a",
                "current_state": "needs_review",
                "event_type": "new_return",
                "final_bucket": "auto_safe_staged",
                "next_action": "await_staging_approval",
                "allowed_actions": ["view_case"],
                "forbidden_actions": ["send_to_1c"],
                "blocking_reasons": [],
                "warning_reasons": [],
                "evidence_summary": {"gate_passed": True},
                "review_tasks_count": 0,
                "review_tasks": [],
                "staged_status": {"staged": True},
                "outbox_status": {"present": False, "count": 0, "statuses": []},
                "learning_ledger_status": {"status": "no_decision", "decisions_count": 0},
            }
        ],
    )
    _jsonl(
        ai_trace,
        [{
            "case_id": 7, "buyer_code": "supplier_a", "mode": "sandbox_replay",
            "ai_provider": "mock", "ai_model": "mock-model",
            "field_diff": {"claim_kind": {"ai_changed": True}},
            "accepted_fields": [], "rejected_fields": ["claim_kind"],
            "ai_result": {"claim_kind": "defect"}, "final_result": {"claim_kind": "quality_refusal"},
        }],
    )
    defect_audit.write_text(json.dumps({
        "summary": {"total_defect_candidates": 1, "confirmed_defect": 1},
        "cases": [{"case_id": 7, "buyer_code": "supplier_a", "defect_class": "confirmed_defect", "has_photos": False, "explicit_reason": True}],
    }), encoding="utf-8")
    _jsonl(inbox_sorting, [
        {
            "raw_email_id": 70, "mailbox": "INBOX", "sender": "a@example.com",
            "sender_domain": "example.com", "subject": "Возврат",
            "inbox_bucket": "return_claim", "confidence": 93,
            "reasons": ["return evidence"], "matched_rules": ["return_terms"],
            "next_action": "process_return", "has_case": True,
        },
        {
            "raw_email_id": 71, "mailbox": "Reports", "sender": "report@example.com",
            "sender_domain": "example.com", "subject": "Отчёт",
            "inbox_bucket": "supplier_report", "confidence": 95,
            "reasons": ["report"], "matched_rules": ["report_attachment"],
            "next_action": "ignore_report", "has_case": False,
        },
    ])
    inbox_summary.write_text(json.dumps({
        "total_raw": 2, "raw_without_case": 1, "should_enter_return_pipeline": 1,
        "non_return_automatic": 1, "unknown_needs_review": 0,
    }), encoding="utf-8")
    raw_without_summary.write_text(json.dumps({
        "total_raw_without_case": 1, "by_inbox_bucket": {"supplier_report": 1},
    }), encoding="utf-8")
    imap_reconcile_summary.write_text(json.dumps({
        "checked_at": "2026-06-10T08:00:00+00:00",
        "server_total": 2,
        "local_raw_total": 2,
        "missing_local_total": 0,
        "duplicate_linked_total": 1,
        "fetch_failed_total": 0,
    }), encoding="utf-8")

    with sqlite3.connect(database) as con:
        con.execute(
            "CREATE TABLE outbox (id INTEGER PRIMARY KEY, case_id INTEGER, status TEXT, event_type TEXT, channel TEXT, created_at TEXT, sent_at TEXT, last_error TEXT)"
        )
        con.execute("INSERT INTO outbox(case_id,status) VALUES (99,'new')")

    for name, value in {
        "SUMMARY_PATH": summary,
        "SUPPLIER_MATRIX_PATH": suppliers,
        "CASES_PATH": cases,
        "QUICK_REVIEW_PATH": quick,
        "SAFE_PREVIEW_PATH": safe,
        "WARNING_PREVIEW_PATH": warning,
        "STAGING_PATH": staging,
        "ACTION_LOG_PATH": actions,
        "FINAL_SORTING_PATH": final_sorting,
        "AI_TRACE_PATH": ai_trace,
        "DEFECT_AUDIT_PATH": defect_audit,
        "INBOX_SORTING_PATH": inbox_sorting,
        "INBOX_SORTING_SUMMARY_PATH": inbox_summary,
        "RAW_WITHOUT_CASES_SUMMARY_PATH": raw_without_summary,
        "IMAP_RECONCILE_SUMMARY_PATH": imap_reconcile_summary,
    }.items():
        monkeypatch.setattr(evidence_panel, name, value)
    monkeypatch.setattr(evidence_panel.settings, "database_path", database)
    evidence_panel._CACHE.clear()
    return database


def test_evidence_read_only_endpoints_and_timeline(tmp_path, monkeypatch):
    database = _setup_files(tmp_path, monkeypatch)
    client = TestClient(app)

    summary = client.get("/api/evidence/summary").json()
    assert summary["auto_export_safe"] == 2
    assert summary["staging_count"] == 1
    assert summary["real_outbox_count"] == 1

    suppliers = client.get("/api/evidence/suppliers").json()
    assert suppliers["items"][0]["buyer_code"] == "supplier_a"
    assert suppliers["items"][0]["auto_percent"] == 83.33

    queue = client.get("/api/quick-review/queue?one_click_only=true").json()
    assert queue["total"] == 1
    assert queue["facets"]["buyer_codes"] == ["supplier_a"]

    staging = client.get("/api/outbox-staging").json()
    assert staging["total"] == 1
    assert staging["items"][0]["safety_class"] == "auto_export_safe"

    sorter = client.get("/api/control/final-sorting").json()
    assert sorter["total"] == 1
    assert sorter["items"][0]["final_bucket"] == "auto_safe_staged"
    sorter_summary = client.get("/api/control/final-sorting/summary").json()
    assert sorter_summary["by_bucket"]["auto_safe_staged"] == 1
    sorter_case = client.get("/api/control/final-sorting/case/7").json()
    assert sorter_case["item"]["next_action"] == "await_staging_approval"

    trace = client.get("/api/ai-trace?changed_field=claim_kind&rejected=true").json()
    assert trace["total"] == 1
    assert trace["items"][0]["ai_model"] == "mock-model"
    trace_case = client.get("/api/ai-trace/7").json()
    assert len(trace_case["items"]) == 1
    defect = client.get("/api/ai-trace/defect-audit?defect_class=confirmed_defect").json()
    assert defect["total"] == 1

    inbox = client.get("/api/inbox-sorting/items?has_case=false").json()
    assert inbox["total"] == 1
    assert inbox["items"][0]["inbox_bucket"] == "supplier_report"
    assert client.get("/api/inbox-sorting/item/70").json()["item"]["next_action"] == "process_return"
    assert client.get("/api/inbox-sorting/summary").json()["summary"]["total_raw"] == 2
    assert client.get("/api/raw-without-cases/summary").json()["summary"]["total_raw_without_case"] == 1
    assert client.get("/api/raw-without-cases/items").json()["total"] == 1
    reconcile = client.get("/api/import/reconcile-summary").json()
    assert reconcile["read_only"] is True
    assert reconcile["summary"]["server_total"] == 2

    timeline = client.get("/api/case/7/timeline").json()
    statuses = {row["stage"]: row["status"] for row in timeline["stages"]}
    assert statuses["evidence_gate"] == "passed"
    assert statuses["staging"] == "staged"
    assert statuses["1c"] == "not_called"

    with sqlite3.connect(database) as con:
        assert con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1


# v2.1 AI-only: тест learning_ledger удалён (ledger вырезан).


def test_staging_routes_are_read_only():
    methods = {
        method
        for route in app.routes
        if getattr(route, "path", "") in {"/api/outbox-staging", "/api/outbox-staging/item/{idempotency_key}"}
        for method in getattr(route, "methods", set())
    }
    assert methods == {"GET"}


def test_inbox_sorter_routes_are_read_only():
    paths = {
        "/api/inbox-sorting/summary",
        "/api/inbox-sorting/items",
        "/api/inbox-sorting/item/{raw_email_id}",
        "/api/raw-without-cases/summary",
        "/api/raw-without-cases/items",
        "/api/import/reconcile-summary",
    }
    methods = {
        method
        for route in app.routes
        if getattr(route, "path", "") in paths
        for method in getattr(route, "methods", set())
    }
    assert methods == {"GET"}
