from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fcntl

from .claim_kind_evidence import evaluate_claim_kind_evidence
from .quality_gate import build_evidence_gate


TRACE_PATH = Path(__file__).resolve().parent.parent / "data" / "ai_trace.jsonl"
TRACE_FIELDS = (
    "buyer_code", "event_type", "claim_kind", "document_number", "document_date",
    "part_number", "brand", "product_name", "quantity", "comment",
)
CONFIRMED_PREFIX = "confirmed_"


def _value(result: dict[str, Any], field: str) -> Any:
    if field in {"buyer_code", "event_type", "claim_kind"}:
        return result.get(field)
    fields = result.get("fields") if isinstance(result.get("fields"), dict) else {}
    return fields.get(field)


def build_field_diff(
    pattern_result: dict[str, Any],
    ai_result: dict[str, Any],
    final_result: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    diff = {}
    for field in TRACE_FIELDS:
        before = _value(pattern_result, field)
        proposed = _value(ai_result, field)
        final = _value(final_result, field)
        if before != proposed or proposed != final:
            diff[field] = {
                "before": before,
                "ai_proposed": proposed,
                "after": final,
                "changed": before != final,
                "ai_changed": before != proposed,
                "accepted": proposed not in (None, "") and proposed == final,
            }
    return diff


def _original_text(email_data: dict[str, Any]) -> str:
    return "\n".join(
        str(email_data.get(key) or "")
        for key in ("subject", "visible_text", "body_text", "body_html", "snippet")
    )


def build_trace_entry(
    *,
    email_data: dict[str, Any],
    pattern_result: dict[str, Any],
    ai_result: dict[str, Any],
    final_result: dict[str, Any],
    provider: str,
    model: str,
    mode: str,
    prompt_hash: str = "",
    case_id: Any = None,
    raw_email_id: Any = None,
    usage: dict[str, Any] | None = None,
    error: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = dict(metadata or {})
    metadata.setdefault("raw_email", email_data)
    gate = build_evidence_gate(_original_text(email_data), final_result, metadata)
    field_diff = build_field_diff(pattern_result, ai_result, final_result)
    statuses = gate.get("field_statuses") or {}
    accepted_fields = []
    rejected_fields = []
    for field, change in field_diff.items():
        if not change.get("ai_changed") or change.get("ai_proposed") in (None, ""):
            continue
        status = str(statuses.get(field) or "")
        if change.get("accepted") and status.startswith(CONFIRMED_PREFIX):
            accepted_fields.append(field)
        else:
            rejected_fields.append(field)
    attachments = email_data.get("attachments") or []
    has_images = any(
        str(item.get("content_type") or "").startswith("image/")
        or str(item.get("filename") or "").lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".heic"))
        for item in attachments if isinstance(item, dict)
    )
    usage = usage or {}
    return {
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "case_id": case_id,
        "raw_email_id": raw_email_id,
        "buyer_code": final_result.get("buyer_code") or pattern_result.get("buyer_code"),
        "ai_provider": provider,
        "ai_model": model,
        "mode": mode,
        "prompt_hash": prompt_hash,
        "input_summary": {
            "subject": str(email_data.get("subject") or "")[:300],
            "body_chars": len(_original_text(email_data)),
            "attachments_count": len(attachments),
            "has_images": has_images,
        },
        "pattern_result": pattern_result,
        "ai_result": ai_result,
        "final_result": final_result,
        "field_diff": field_diff,
        "accepted_fields": accepted_fields,
        "rejected_fields": rejected_fields,
        "evidence_gate_result": gate,
        "cost_tokens": {
            "input": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
            "output": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        },
        "error": error,
    }


def append_ai_trace(entry: dict[str, Any], path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path or TRACE_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
    with target.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return entry


def defect_evidence(
    *,
    claim_kind: Any,
    email_data: dict[str, Any],
    ai_proposed_defect: bool = False,
) -> dict[str, Any]:
    audit = evaluate_claim_kind_evidence(
        claim_kind or "defect",
        None,
        raw_email=email_data,
        original_text=_original_text(email_data),
    )
    attachments = email_data.get("attachments") or []
    has_photos = any(
        str(item.get("content_type") or "").startswith("image/")
        or str(item.get("filename") or "").lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".heic"))
        for item in attachments if isinstance(item, dict)
    )
    status = str(audit.get("status") or "")
    explicit = status in {
        "confirmed_by_explicit_reason",
        "confirmed_by_reason_label",
        "confirmed_by_supplier_contract",
        "confirmed_by_table_reason_column",
    }
    conflict = status == "conflict_reason_detected" or "claim_kind_conflict" in (audit.get("warnings") or [])
    if conflict:
        defect_class = "conflict_defect"
    elif explicit and str(claim_kind) in {"defect", "nonconforming"}:
        defect_class = "confirmed_defect"
    elif ai_proposed_defect and not explicit:
        defect_class = "defect_rejected_by_evidence"
    else:
        defect_class = "weak_defect"
    return {
        "defect_class": defect_class,
        "has_photos": has_photos,
        "explicit_reason": explicit,
        "status": status,
        "source": audit.get("source"),
        "evidence_snippet": audit.get("evidence_snippet") or "",
        "warnings": audit.get("warnings") or [],
    }
