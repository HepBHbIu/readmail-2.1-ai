from __future__ import annotations

import json

from app import ai_client, ai_trace
from app.ai_trace import build_field_diff, build_trace_entry, defect_evidence


def _email(text: str, attachments=None):
    return {
        "subject": "Претензия",
        "visible_text": text,
        "body_text": text,
        "attachments": attachments or [],
    }


def _case(claim_kind="quality_refusal"):
    return {
        "buyer_code": "supplier",
        "event_type": "new_return",
        "claim_kind": claim_kind,
        "fields": {
            "document_number": "100",
            "document_date": "10.06.2026",
            "part_number": "ABC123",
            "quantity": 1,
        },
        "payload": {"direction": "inbound_customer"},
        "missing": [],
        "quality": [],
    }


def test_field_diff_tracks_ai_before_after():
    pattern = _case()
    ai = {"claim_kind": "defect", "fields": {"quantity": 2}}
    final = {**pattern, "claim_kind": "defect", "fields": {**pattern["fields"], "quantity": 2}}
    diff = build_field_diff(pattern, ai, final)
    assert diff["claim_kind"]["before"] == "quality_refusal"
    assert diff["claim_kind"]["after"] == "defect"
    assert diff["claim_kind"]["changed"] is True
    assert diff["quantity"]["accepted"] is True


def test_ai_result_without_evidence_is_rejected():
    pattern = _case()
    ai = {"claim_kind": "defect", "fields": {}}
    final = {**pattern, "claim_kind": "defect"}
    entry = build_trace_entry(
        email_data=_email("Претензия. Есть фото."),
        pattern_result=pattern,
        ai_result=ai,
        final_result=final,
        provider="mock",
        model="mock",
        mode="overlay",
    )
    assert "claim_kind" in entry["rejected_fields"]
    assert "claim_kind" not in entry["accepted_fields"]


def test_defect_not_confirmed_by_photo_only():
    result = defect_evidence(
        claim_kind="defect",
        email_data=_email("Претензия. Фото во вложении.", [{"filename": "photo.jpg", "content_type": "image/jpeg"}]),
    )
    assert result["has_photos"] is True
    assert result["explicit_reason"] is False
    assert result["defect_class"] == "weak_defect"


def test_defect_confirmed_by_reason_column():
    result = defect_evidence(
        claim_kind="defect",
        email_data=_email("Артикул | Причина\nABC123 | Брак"),
    )
    assert result["explicit_reason"] is True
    assert result["defect_class"] == "confirmed_defect"


def test_defect_conflict_with_customer_refusal():
    result = defect_evidence(
        claim_kind="defect",
        email_data=_email("Причина возврата: отказ клиента. В комментарии также указано: брак."),
    )
    assert result["defect_class"] == "conflict_defect"


def test_mocked_ai_call_writes_trace(tmp_path, monkeypatch):
    trace_path = tmp_path / "ai_trace.jsonl"
    monkeypatch.setattr(ai_trace, "TRACE_PATH", trace_path)
    monkeypatch.setattr(ai_client.settings, "enable_ai", True)
    monkeypatch.setattr(ai_client.settings, "ai_cache_enabled", False)
    monkeypatch.setattr(
        ai_client,
        "_request_chat",
        lambda messages: (
            {"choices": [{"message": {"content": '{"claim_kind":"defect","fields":{}}'}}], "usage": {"prompt_tokens": 10, "completion_tokens": 3}},
            "mock",
            "mock-model",
            "mock://chat",
        ),
    )
    result = ai_client.run_ai_suggestion(
        _email("Причина: Брак"),
        _case(),
        case_id=5,
        purpose="manual_ai_suggest",
    )
    assert result["ok"] is True
    rows = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["case_id"] == 5
    assert rows[0]["ai_result"]["claim_kind"] == "defect"


def test_control_ai_trace_ui_does_not_trigger_ai():
    html = (ai_trace.TRACE_PATH.parent.parent / "app" / "web" / "index.html").read_text(encoding="utf-8")
    script = (ai_trace.TRACE_PATH.parent.parent / "app" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'data-tab="ai_trace"' in html
    assert 'data-tab="defect_audit"' in html
    assert "Sandbox replay</button>" in html
    assert "Sandbox replay" in html and "disabled" in html
    trace_section = script[script.index("async function loadAiTrace"):script.index("async function loadDefectAudit")]
    defect_section = script[script.index("async function loadDefectAudit"):]
    assert "/api/ai-trace" in trace_section
    assert "/api/ai-trace/defect-audit" in defect_section
    assert "method: 'POST'" not in trace_section
    assert "method: 'POST'" not in defect_section
