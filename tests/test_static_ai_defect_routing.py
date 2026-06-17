from __future__ import annotations

import json

from app.ai_client import SYSTEM_PROMPT, _chat_payload
from app.classifier import classify_email, defect_documents_flag, detect_event_type
from app.config import settings
from app.db import build_defect_flags


EXPECTED_EVENT_TYPES = {
    "new_return",
    "pre_delivery_refusal",
    "followup_reminder",
    "followup_dialog",
    "supplier_decision",
    "correction_request",
    "marking_request",
    "number_replacement",
    "shortage_link_event",
    "ready_to_ship",
    "supplier_report",
    "info_only",
    "unknown",
}


def test_supplier_report_has_terminal_event_type():
    event_type, is_followup, reasons = detect_event_type(
        {"subject": "Прайс-лист и остатки.xlsx", "from_addr": "price@supplier.test"},
        "Актуальный прайс-лист и остатки товара во вложении",
        None,
        {},
        "inbound_customer",
    )
    assert event_type == "supplier_report"
    assert is_followup is False
    assert "service_report" in reasons


def test_shortage_link_only_has_dedicated_event_type():
    case = classify_email(
        {
            "subject": "Недопоставка",
            "body_text": "Недовоз. Позиции и количество по ссылке https://www.avtoto.ru/nondelivery/382422/details",
            "visible_text": "Недовоз. Позиции и количество по ссылке https://www.avtoto.ru/nondelivery/382422/details",
            "from_addr": "claims@avtoto.ru",
            "to_addr": "returns@example.test",
            "attachments": [],
        },
        buyer_rules=[],
    )
    # event_type-подсказка скелета сохраняется (для prompt skeleton_guess)…
    assert case["event_type"] == "shortage_link_event"
    # …но в v2.1 ai_only скелет НЕ маршрутизирует: всё «ожидает ИИ» (needs_review+needs_ai),
    # финальный маршрут ставит ИИ. Это и есть «обезврежен скелет».
    assert case["state"] == "needs_review"
    assert case["ready_for_export"] is False
    assert case["needs_ai"] is True


def test_ai_prompts_share_full_event_contract():
    for event_type in EXPECTED_EVENT_TYPES:
        assert event_type in SYSTEM_PROMPT
    email = {"subject": "test", "body_text": "test", "visible_text": "test"}
    case = {"event_type": "unknown", "claim_kind": None, "fields": {}}

    # v2.1: lean user-payload — схема в ключе return_json (правила/примеры в SYSTEM).
    normal_messages, _, _ = _chat_payload(email, case)
    normal_shape = json.loads(normal_messages[1]["content"])["return_json"]
    full_messages, _, _ = _chat_payload(email, case, purpose="manual_full_ai")
    full_shape = json.loads(full_messages[1]["content"])["return_json"]

    for shape in (normal_shape, full_shape):
        for event_type in EXPECTED_EVENT_TYPES:
            assert event_type in shape["event_type"]
        assert "requires_action" in shape
        assert "next_action" in shape
        assert "cannot_export_reason" in shape
        assert "defect_documents_status" in shape


def test_defect_metadata_only_when_vision_disabled(monkeypatch):
    monkeypatch.setattr(settings, "defect_doc_ai_read", False)
    monkeypatch.setattr(settings, "defect_vision_enabled", False)
    monkeypatch.setattr(settings, "ai_vision_enabled", False)
    attachments = [{"filename": "damage.jpg", "content_type": "image/jpeg"}]

    classifier_flags = defect_documents_flag(attachments)
    assert classifier_flags["defect_documents_status"] == "metadata_only"
    assert classifier_flags["has_attachments"] is True
    assert classifier_flags["has_images"] is True
    assert classifier_flags["operator_attention"] is True
    assert classifier_flags["needs_ai_vision"] is False


def test_one_c_defect_flags_respect_metadata_strategy(monkeypatch):
    monkeypatch.setattr(settings, "defect_doc_ai_read", False)
    monkeypatch.setattr(settings, "defect_vision_enabled", False)
    monkeypatch.setattr(settings, "ai_vision_enabled", False)
    monkeypatch.setattr(settings, "max_defect_images_per_case", 2)
    payload = {
        "return": {"claim_kind": "defect"},
        "quality": {
            "evidence": {
                "attachments_count": 1,
                "has_photo": True,
                "photos": [{"filename": "damage.jpg", "content_type": "image/jpeg"}],
            }
        },
    }
    flags = build_defect_flags(payload)
    assert flags["defect_documents_status"] == "metadata_only"
    assert flags["operator_attention"] is True
    assert flags["max_images"] == 2
