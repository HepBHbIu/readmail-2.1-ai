"""Read-only classification taxonomy mapping for live raw emails and cases."""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from typing import Any

from app.inbox_sorter import classify_inbox


RETURN_KINDS = {
    "quality_refusal",
    "defect",
    "wrong_item",
    "shortage",
    "overdelivery",
    "nonconforming",
    "incomplete_set",
    "marking_request",
    "correction_request",
    "number_replacement",
}
FOLLOWUP_EVENTS = {
    "followup_reminder",
    "followup_dialog",
    "supplier_decision",
    "correction_request",
    "ready_to_ship",
    "marking_request",
}
SERVICE_BRANDS = {
    "здравствуйте",
    "добрый день",
    "возврат",
    "уведомление",
    "письмо",
    "с уважением",
    "поставщик",
    "клиент",
    "неизвестно",
}
REQUIRED_RETURN_FIELDS = {
    "document_number",
    "document_date",
    "part_number",
    "quantity",
    "claim_kind",
    "buyer_code",
}


def _loads(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _text(raw: dict[str, Any]) -> str:
    return "\n".join(str(raw.get(key) or "") for key in (
        "subject", "visible_text", "body_text", "body_html", "snippet"
    ))


def _current_taxonomy(
    raw: dict[str, Any], case: dict[str, Any] | None
) -> tuple[str, str]:
    if raw.get("duplicate_of_raw_email_id") or str(raw.get("status") or "") == "duplicate":
        subtype = (
            "exact_message_id_duplicate"
            if raw.get("message_id")
            else "semantic_duplicate"
        )
        return "duplicate", subtype
    if case is None:
        if not _text(raw).strip():
            return "unknown", "no_body"
        return "unknown", "parser_failed"

    state = str(case.get("state") or "")
    event_type = str(case.get("event_type") or "")
    claim_kind = str(case.get("claim_kind") or "")
    payload = _loads(case.get("payload_json"), {})
    fields = _loads(case.get("fields_json"), {})

    if state == "linked_event" or event_type in FOLLOWUP_EVENTS and state != "needs_review":
        mapping = {
            "followup_reminder": "followup_reminder",
            "followup_dialog": "followup_dialog",
            "supplier_decision": "supplier_decision",
            "correction_request": "additional_documents",
            "ready_to_ship": "repeated_request",
            "marking_request": "clarification_request",
        }
        return "linked_event", mapping.get(event_type, "followup_dialog")
    if state == "needs_link":
        return "linked_event", "parent_missing"
    if event_type == "info_only" or state == "ignored_info_only":
        text = _text(raw).lower()
        filenames = " ".join(str(x.get("filename") or "") for x in raw.get("attachments", []))
        if re.search(r"прайс|price.?list", text + filenames, re.I):
            return "supplier_report", "price_list"
        if re.search(r"остатк|stock|наличи", text + filenames, re.I):
            return "supplier_report", "stock_report"
        if re.search(r"\.xlsx?\b|\.csv\b|\.zip\b", filenames, re.I):
            return "supplier_report", "excel_report"
        return "supplier_report", "daily_report"
    if event_type == "problem_notice" or state == "problem_notice":
        return "problem_notice", "defect_accepted_no_return"
    if event_type == "spam_promo" or state == "ignored_spam_promo":
        return "junk", "advertising"
    if event_type == "new_return":
        if payload.get("pre_delivery_refusal") or fields.get("pre_delivery_refusal"):
            return "return_claim", "pre_delivery_refusal"
        if claim_kind == "defect":
            defect = payload.get("defect_doc_flag") or {}
            defect_state = str(defect.get("state") or "")
            if defect_state in {"docs_complete", "complete"}:
                return "return_claim", "defect.documents_present"
            if defect_state in {"docs_incomplete", "present_unverified"}:
                return "return_claim", "defect.incomplete_documents"
            if defect_state in {"docs_missing", "missing"}:
                return "return_claim", "defect.no_documents"
            return "return_claim", "defect"
        return "return_claim", claim_kind if claim_kind in RETURN_KINDS else "unknown_return_reason"
    return "unknown", "needs_human_classification"


def _proposed_taxonomy(
    raw: dict[str, Any],
    case: dict[str, Any] | None,
    inbox: dict[str, Any],
) -> tuple[str, str]:
    current_category, current_subcategory = _current_taxonomy(raw, case)
    if current_category in {"duplicate", "linked_event"}:
        return current_category, current_subcategory
    bucket = str(inbox.get("inbox_bucket") or "")
    if bucket == "supplier_report":
        return _current_taxonomy(raw, {**(case or {}), "event_type": "info_only", "state": "ignored_info_only"})
    if bucket == "junk_or_noise":
        text = _text(raw).lower()
        return "junk", "delivery_notification" if "delivery status notification" in text else "auto_reply"
    if bucket == "info_only":
        text = _text(raw).lower()
        if re.search(r"реквизит", text):
            return "info_update", "requisites_changed"
        if re.search(r"контакт", text):
            return "info_update", "contacts_changed"
        if re.search(r"график|режим работ", text):
            return "info_update", "schedule_notice"
        return "info_update", "document_flow_notice"
    if bucket == "return_followup":
        return "linked_event", "followup_dialog"
    if bucket == "correction_doc" and case and case.get("state") in {"linked_event", "needs_link"}:
        return "linked_event", "additional_documents"
    if bucket in {"return_claim", "edo_marking", "correction_doc"}:
        if case and str(case.get("event_type") or "") == "new_return":
            return current_category, current_subcategory
        return "return_claim", (
            "marking_request" if bucket == "edo_marking"
            else "correction_request" if bucket == "correction_doc"
            else "unknown_return_reason"
        )
    if bucket == "import_error":
        return "unknown", "parser_failed"
    if not _text(raw).strip():
        return "unknown", "no_body"
    return "unknown", "needs_human_classification"


def _next_action(category: str, subcategory: str, case: dict[str, Any] | None) -> str:
    if category == "return_claim":
        if case and case.get("state") == "ready_to_1c" and case.get("ready_for_export"):
            return "ready_to_1c"
        return "quick_review" if subcategory != "unknown_return_reason" else "human_review"
    if category == "linked_event":
        return "link_to_parent" if subcategory == "parent_missing" else "terminal_no_action"
    if category == "supplier_report":
        return "supplier_report_archive"
    if category in {"duplicate", "junk", "info_update"}:
        return "terminal_no_action"
    return "human_review"


def _field_statuses(case: dict[str, Any] | None) -> dict[str, Any]:
    if not case:
        return {}
    payload = _loads(case.get("payload_json"), {})
    quality = payload.get("_quality") or {}
    gate = quality.get("evidence_gate") or payload.get("evidence_gate") or {}
    return gate.get("field_statuses") or {}


def audit_classifications(con: Any) -> dict[str, Any]:
    raw_rows = [dict(row) for row in con.execute(
        """
        SELECT id, mailbox, uid, uidvalidity, message_id, in_reply_to, references_json,
               subject, from_addr, received_at, body_text, body_html, visible_text, snippet,
               status, duplicate_of_raw_email_id
        FROM raw_emails ORDER BY id
        """
    )]
    attachments: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in con.execute("SELECT raw_email_id, filename, content_type, size_bytes FROM attachments"):
        attachments[int(row["raw_email_id"])].append(dict(row))
    cases_by_raw: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in con.execute("SELECT * FROM cases ORDER BY id"):
        cases_by_raw[int(row["raw_email_id"])].append(dict(row))

    items: list[dict[str, Any]] = []
    category_counts: Counter[str] = Counter()
    subcategory_counts: Counter[str] = Counter()
    buyer_counts: Counter[str] = Counter()
    mismatch_counts: Counter[str] = Counter()
    risks: Counter[str] = Counter()

    for raw in raw_rows:
        raw["attachments"] = attachments.get(int(raw["id"]), [])
        raw_cases = cases_by_raw.get(int(raw["id"]), [])
        case = raw_cases[0] if raw_cases else None
        inbox = classify_inbox(raw)
        current_category, current_subcategory = _current_taxonomy(raw, case)
        proposed_category, proposed_subcategory = _proposed_taxonomy(raw, case, inbox)
        fields = _loads(case.get("fields_json"), {}) if case else {}
        payload = _loads(case.get("payload_json"), {}) if case else {}
        field_statuses = _field_statuses(case)
        warnings: list[str] = []

        if (current_category, current_subcategory) != (proposed_category, proposed_subcategory):
            warnings.append("taxonomy_mismatch")
        if current_category == "return_claim" and inbox.get("inbox_bucket") == "supplier_report":
            warnings.append("supplier_report_misclassified_as_return")
        if (
            current_category == "return_claim"
            and inbox.get("inbox_bucket") == "return_followup"
        ):
            warnings.append("followup_misclassified_as_new_return")
        brand = str(fields.get("brand") or "").strip()
        if brand and (
            brand.lower() in SERVICE_BRANDS
            or "@" in brand
            or re.search(r"\b(ооо|уведомлен|письмо|возврат)\b", brand, re.I)
        ):
            warnings.append("brand_suspicious")
        missing_required = []
        if current_category == "return_claim":
            for field in REQUIRED_RETURN_FIELDS:
                value = case.get("buyer_code") if field == "buyer_code" and case else (
                    case.get("claim_kind") if field == "claim_kind" and case else fields.get(field)
                )
                if value in (None, "", []):
                    missing_required.append(field)
            if case and case.get("state") == "ready_to_1c" and missing_required:
                warnings.append("ready_to_1c_without_required_fields")
        weak_evidence = [
            key for key, status in field_statuses.items()
            if str(status) in {"weak_found", "not_found", "missing_processed"}
        ]
        if current_category == "return_claim" and not field_statuses:
            warnings.append("missing_evidence")
        elif current_category == "return_claim" and weak_evidence:
            warnings.append("weak_or_missing_evidence")

        for warning in warnings:
            risks[warning] += 1
        mismatch_reason = ", ".join(warnings) if warnings else None
        pattern_source = {
            "processing_source": payload.get("processing_source"),
            "processing_mode": payload.get("processing_mode"),
            "classifier": payload.get("classifier"),
            "reasons": payload.get("reasons") or {},
            "inbox_rules": inbox.get("matched_rules") or [],
        }
        item = {
            "raw_email_id": raw["id"],
            "case_id": case.get("id") if case else None,
            "buyer_code": case.get("buyer_code") if case else None,
            "subject": raw.get("subject"),
            "current_category": current_category,
            "current_subcategory": current_subcategory,
            "proposed_category": proposed_category,
            "proposed_subcategory": proposed_subcategory,
            "evidence": {
                "field_statuses": field_statuses,
                "inbox_reasons": inbox.get("reasons") or [],
                "missing_required": missing_required,
                "weak_fields": weak_evidence,
            },
            "pattern_source": pattern_source,
            "confidence": inbox.get("confidence"),
            "next_action": _next_action(proposed_category, proposed_subcategory, case),
            "mismatch_reason": mismatch_reason,
            "warnings": warnings,
            "current_state": case.get("state") if case else None,
            "event_type": case.get("event_type") if case else None,
            "claim_kind": case.get("claim_kind") if case else None,
            "ready_for_export": bool(case and case.get("ready_for_export")),
        }
        items.append(item)
        category_counts[proposed_category] += 1
        subcategory_counts[f"{proposed_category}.{proposed_subcategory}"] += 1
        buyer_counts[str(item.get("buyer_code") or "unknown")] += 1
        if mismatch_reason:
            mismatch_counts[mismatch_reason] += 1

    summary = {
        "schema": "readmail-classification-taxonomy-audit-v1",
        "read_only": True,
        "total_raw": len(raw_rows),
        "accounted": len(items),
        "unaccounted": len(raw_rows) - len(items),
        "by_category": dict(category_counts.most_common()),
        "by_subcategory": dict(subcategory_counts.most_common()),
        "by_buyer": dict(buyer_counts.most_common()),
        "mismatches": sum(1 for item in items if item["mismatch_reason"]),
        "missing_subcategory": sum(
            1 for item in items if not item["proposed_subcategory"]
        ),
        "missing_evidence": risks["missing_evidence"],
        "dangerous_misclassified": (
            risks["supplier_report_misclassified_as_return"]
            + risks["followup_misclassified_as_new_return"]
            + risks["ready_to_1c_without_required_fields"]
        ),
        "ready_to_1c_without_required_fields": risks["ready_to_1c_without_required_fields"],
        "supplier_report_misclassified_as_return": risks["supplier_report_misclassified_as_return"],
        "followup_misclassified_as_new_return": risks["followup_misclassified_as_new_return"],
        "brand_suspicious": risks["brand_suspicious"],
        "top_risks": dict(risks.most_common()),
        "top_mismatch_reasons": dict(mismatch_counts.most_common(20)),
    }
    return {"ok": True, "summary": summary, "items": items}
