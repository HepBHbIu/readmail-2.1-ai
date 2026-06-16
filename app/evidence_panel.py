from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from .config import settings
from .final_case_sorter import summarize_final_sorting


router = APIRouter()
PROJECT_DIR = Path(__file__).resolve().parent.parent
AUDIT_DIR = PROJECT_DIR / "audit_out"
DATA_DIR = PROJECT_DIR / "data"
SUMMARY_PATH = AUDIT_DIR / "full_dry_run_summary.json"
SUPPLIER_MATRIX_PATH = AUDIT_DIR / "supplier_matrix.json"
CASES_PATH = AUDIT_DIR / "full_dry_run_cases.jsonl"
QUICK_REVIEW_PATH = AUDIT_DIR / "quick_review_queue.jsonl"
SAFE_PREVIEW_PATH = AUDIT_DIR / "outbox_preview_safe.jsonl"
WARNING_PREVIEW_PATH = AUDIT_DIR / "outbox_preview_warning.jsonl"
STAGING_PATH = DATA_DIR / "outbox_staging.jsonl"
ACTION_LOG_PATH = DATA_DIR / "evidence_ui_actions.jsonl"
FINAL_SORTING_PATH = AUDIT_DIR / "final_case_sorting.jsonl"
AI_TRACE_PATH = DATA_DIR / "ai_trace.jsonl"
DEFECT_AUDIT_PATH = AUDIT_DIR / "defect_ai_audit.json"
INBOX_SORTING_PATH = AUDIT_DIR / "inbox_sorting.jsonl"
INBOX_SORTING_SUMMARY_PATH = AUDIT_DIR / "inbox_sorting_summary.json"
RAW_WITHOUT_CASES_SUMMARY_PATH = AUDIT_DIR / "raw_without_cases.json"
IMAP_RECONCILE_SUMMARY_PATH = AUDIT_DIR / "imap_reconcile_summary.json"

_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, tuple[int, Any]] = {}


class QuickReviewDecision(BaseModel):
    review_id: str
    selected_value: Any
    operator: str = "manual/ui"
    comment: str | None = None


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    stamp = path.stat().st_mtime_ns
    key = str(path)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and cached[0] == stamp:
            return cached[1]
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    with _CACHE_LOCK:
        _CACHE[key] = (stamp, rows)
    return rows


def _append_action(action: str, details: dict[str, Any]) -> None:
    ACTION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "action": action,
        "details": details,
    }
    with ACTION_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def _database_path() -> Path:
    configured = Path(settings.database_path)
    if configured.exists():
        return configured
    local = DATA_DIR / "readmail.sqlite3"
    return local


def _read_outbox(case_id: int | None = None) -> dict[str, Any]:
    path = _database_path()
    result: dict[str, Any] = {"count": 0, "items": [], "database": str(path)}
    if not path.exists():
        return result
    try:
        uri = f"file:{path}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=2) as con:
            con.row_factory = sqlite3.Row
            if case_id is None:
                row = con.execute("SELECT COUNT(*) AS c FROM outbox").fetchone()
                result["count"] = int(row["c"] if row else 0)
            else:
                rows = con.execute(
                    """
                    SELECT id, case_id, status, event_type, channel, created_at, sent_at, last_error
                    FROM outbox WHERE case_id=? ORDER BY id
                    """,
                    (case_id,),
                ).fetchall()
                result["items"] = [dict(row) for row in rows]
                result["count"] = len(rows)
    except sqlite3.Error as exc:
        result["error"] = str(exc)
    return result


def _case_id(row: dict[str, Any]) -> str:
    return str(row.get("case_id") or "")


def _find_case(case_id: str) -> dict[str, Any] | None:
    return next((row for row in _read_jsonl(CASES_PATH) if _case_id(row) == case_id), None)


def _find_preview(case_id: str) -> tuple[str | None, dict[str, Any] | None]:
    for preview_class, path in (
        ("auto_export_safe", SAFE_PREVIEW_PATH),
        ("auto_export_with_warning", WARNING_PREVIEW_PATH),
    ):
        item = next((row for row in _read_jsonl(path) if _case_id(row) == case_id), None)
        if item:
            return preview_class, item
    return None, None


def _short_counter(value: Any, limit: int = 5) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    return [{"reason": key, "count": count} for key, count in list(value.items())[:limit]]


@router.get("/api/inbox-sorting/summary")
def inbox_sorting_summary() -> dict[str, Any]:
    return {
        "ok": True,
        "read_only": True,
        "summary": _read_json(INBOX_SORTING_SUMMARY_PATH, {}),
    }


@router.get("/api/import/reconcile-summary")
def import_reconcile_summary() -> dict[str, Any]:
    summary = _read_json(IMAP_RECONCILE_SUMMARY_PATH, {})
    return {
        "ok": bool(summary),
        "read_only": True,
        "summary": summary,
        "report_path": str(AUDIT_DIR / "imap_reconcile_summary.md"),
    }


@router.get("/api/inbox-sorting/items")
def inbox_sorting_items(
    inbox_bucket: str | None = None,
    sender_domain: str | None = None,
    has_case: bool | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    all_rows = _read_jsonl(INBOX_SORTING_PATH)
    rows = all_rows
    if inbox_bucket:
        rows = [row for row in rows if row.get("inbox_bucket") == inbox_bucket]
    if sender_domain:
        rows = [row for row in rows if row.get("sender_domain") == sender_domain]
    if has_case is not None:
        rows = [row for row in rows if bool(row.get("has_case")) is has_case]
    return {
        "ok": True,
        "read_only": True,
        "total": len(rows),
        "limit": limit,
        "offset": offset,
        "items": rows[offset : offset + limit],
        "facets": {
            "buckets": sorted({str(row.get("inbox_bucket")) for row in all_rows if row.get("inbox_bucket")}),
            "sender_domains": sorted({str(row.get("sender_domain")) for row in all_rows if row.get("sender_domain")}),
        },
    }


@router.get("/api/inbox-sorting/item/{raw_email_id}")
def inbox_sorting_item(raw_email_id: int) -> dict[str, Any]:
    item = next(
        (row for row in _read_jsonl(INBOX_SORTING_PATH) if int(row.get("raw_email_id") or 0) == raw_email_id),
        None,
    )
    if not item:
        raise HTTPException(404, "Inbox sorting item not found")
    return {"ok": True, "read_only": True, "item": item}


@router.get("/api/raw-without-cases/summary")
def raw_without_cases_summary() -> dict[str, Any]:
    return {
        "ok": True,
        "read_only": True,
        "summary": _read_json(RAW_WITHOUT_CASES_SUMMARY_PATH, {}),
    }


@router.get("/api/raw-without-cases/items")
def raw_without_cases_items(
    inbox_bucket: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    rows = [row for row in _read_jsonl(INBOX_SORTING_PATH) if not row.get("has_case")]
    if inbox_bucket:
        rows = [row for row in rows if row.get("inbox_bucket") == inbox_bucket]
    return {
        "ok": True,
        "read_only": True,
        "total": len(rows),
        "limit": limit,
        "offset": offset,
        "items": rows[offset : offset + limit],
    }


@router.get("/api/evidence/summary")
def evidence_summary() -> dict[str, Any]:
    summary = _read_json(SUMMARY_PATH, {})
    classes = summary.get("by_final_dry_run_class") or {}
    outbox = _read_outbox()
    return {
        "ok": True,
        "total_cases": int(summary.get("total_cases") or 0),
        "eligible_return_cases": int(summary.get("eligible_return_cases") or 0),
        "auto_export_safe": int(classes.get("auto_export_safe") or 0),
        "auto_export_with_warning": int(classes.get("auto_export_with_warning") or 0),
        "suspicious": int(classes.get("suspicious_passed") or 0),
        "quick_review": int(classes.get("quick_review") or summary.get("quick_review") or 0),
        "human_review": int(classes.get("human_review") or summary.get("human_review") or 0),
        "blocked": int(classes.get("blocked") or summary.get("blocked") or 0),
        "staging_count": len(_read_jsonl(STAGING_PATH)),
        "real_outbox_count": int(outbox.get("count") or 0),
        "runtime_errors": int(summary.get("runtime_errors") or 0),
        "last_run_at": summary.get("created_at"),
        "source": summary.get("source"),
        "read_only": True,
    }


@router.get("/api/control/final-sorting")
def final_sorting_list(
    final_bucket: str | None = None,
    buyer_code: str | None = None,
    next_action: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    all_rows = _read_jsonl(FINAL_SORTING_PATH)
    rows = all_rows
    if final_bucket:
        rows = [row for row in rows if row.get("final_bucket") == final_bucket]
    if buyer_code:
        rows = [row for row in rows if row.get("buyer_code") == buyer_code]
    if next_action:
        rows = [row for row in rows if row.get("next_action") == next_action]
    facets = {
        "final_buckets": sorted({str(row.get("final_bucket")) for row in all_rows if row.get("final_bucket")}),
        "buyer_codes": sorted({str(row.get("buyer_code")) for row in all_rows if row.get("buyer_code")}),
        "next_actions": sorted({str(row.get("next_action")) for row in all_rows if row.get("next_action")}),
    }
    return {
        "ok": True,
        "total": len(rows),
        "limit": limit,
        "offset": offset,
        "items": rows[offset : offset + limit],
        "facets": facets,
        "read_only": True,
    }


@router.get("/api/control/final-sorting/summary")
def final_sorting_summary() -> dict[str, Any]:
    rows = _read_jsonl(FINAL_SORTING_PATH)
    summary = summarize_final_sorting(rows)
    generated_at = None
    if FINAL_SORTING_PATH.exists():
        generated_at = datetime.fromtimestamp(
            FINAL_SORTING_PATH.stat().st_mtime, tz=timezone.utc
        ).replace(microsecond=0).isoformat()
    return {"ok": True, **summary, "generated_at": generated_at, "read_only": True}


@router.get("/api/control/final-sorting/case/{case_id}")
def final_sorting_case(case_id: str) -> dict[str, Any]:
    item = next(
        (row for row in _read_jsonl(FINAL_SORTING_PATH) if _case_id(row) == case_id),
        None,
    )
    if not item:
        raise HTTPException(404, "Final sorting case not found")
    return {"ok": True, "item": item, "read_only": True}


@router.get("/api/ai-trace")
def ai_trace_list(
    case_id: str | None = None,
    buyer_code: str | None = None,
    mode: str | None = None,
    ai_provider: str | None = None,
    changed_field: str | None = None,
    claim_kind: str | None = None,
    accepted: bool | None = None,
    rejected: bool | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    all_rows = list(reversed(_read_jsonl(AI_TRACE_PATH)))
    rows = all_rows
    if case_id:
        rows = [row for row in rows if _case_id(row) == case_id]
    if buyer_code:
        rows = [row for row in rows if row.get("buyer_code") == buyer_code]
    if mode:
        rows = [row for row in rows if row.get("mode") == mode]
    if ai_provider:
        rows = [row for row in rows if row.get("ai_provider") == ai_provider]
    if changed_field:
        rows = [
            row for row in rows
            if (row.get("field_diff") or {}).get(changed_field, {}).get("ai_changed")
        ]
    if claim_kind:
        rows = [
            row for row in rows
            if (row.get("ai_result") or {}).get("claim_kind") == claim_kind
            or (row.get("final_result") or {}).get("claim_kind") == claim_kind
        ]
    if accepted is True:
        rows = [row for row in rows if row.get("accepted_fields")]
    if accepted is False:
        rows = [row for row in rows if not row.get("accepted_fields")]
    if rejected is True:
        rows = [row for row in rows if row.get("rejected_fields")]
    if rejected is False:
        rows = [row for row in rows if not row.get("rejected_fields")]
    facets = {
        "buyer_codes": sorted({str(row.get("buyer_code")) for row in all_rows if row.get("buyer_code")}),
        "modes": sorted({str(row.get("mode")) for row in all_rows if row.get("mode")}),
        "providers": sorted({str(row.get("ai_provider")) for row in all_rows if row.get("ai_provider")}),
        "changed_fields": sorted({
            field for row in all_rows for field, diff in (row.get("field_diff") or {}).items()
            if diff.get("ai_changed")
        }),
    }
    return {"ok": True, "total": len(rows), "items": rows[offset:offset + limit], "limit": limit, "offset": offset, "facets": facets, "read_only": True}


@router.get("/api/ai-trace/defect-audit")
def ai_trace_defect_audit(
    defect_class: str | None = None,
    ai_proposed_defect: bool | None = None,
    ai_changed_to_defect: bool | None = None,
    pattern_disagreed: bool | None = None,
    has_photos: bool | None = None,
    no_explicit_reason: bool | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    audit = _read_json(DEFECT_AUDIT_PATH, {"summary": {}, "cases": []})
    rows = list(audit.get("cases") or [])
    if defect_class:
        rows = [row for row in rows if row.get("defect_class") == defect_class]
    if ai_proposed_defect is not None:
        rows = [row for row in rows if bool(row.get("ai_proposed_defect")) == ai_proposed_defect]
    if ai_changed_to_defect is not None:
        rows = [row for row in rows if bool(row.get("ai_changed_to_defect")) == ai_changed_to_defect]
    if pattern_disagreed is not None:
        rows = [row for row in rows if bool(row.get("pattern_disagreed")) == pattern_disagreed]
    if has_photos is not None:
        rows = [row for row in rows if bool(row.get("has_photos")) == has_photos]
    if no_explicit_reason is not None:
        rows = [row for row in rows if bool(not row.get("explicit_reason")) == no_explicit_reason]
    return {
        "ok": True,
        "summary": audit.get("summary") or {},
        "total": len(rows),
        "items": rows[offset:offset + limit],
        "limit": limit,
        "offset": offset,
        "read_only": True,
    }


@router.get("/api/ai-trace/{case_id}")
def ai_trace_case(case_id: str) -> dict[str, Any]:
    items = [row for row in _read_jsonl(AI_TRACE_PATH) if _case_id(row) == case_id]
    if not items:
        raise HTTPException(404, "AI trace not found")
    return {"ok": True, "case_id": case_id, "items": items, "read_only": True}


@router.get("/api/evidence/suppliers")
def evidence_suppliers() -> dict[str, Any]:
    matrix = _read_json(SUPPLIER_MATRIX_PATH, {})
    items = []
    for row in matrix.get("suppliers") or []:
        returns = int(row.get("eligible_return_cases") or 0)
        safe = int(row.get("auto_export_safe") or 0)
        warning = int(row.get("auto_export_with_warning") or 0)
        items.append(
            {
                "buyer_code": row.get("buyer_code") or "unknown",
                "total": int(row.get("total_cases") or 0),
                "returns": returns,
                "safe": safe,
                "warning": warning,
                "quick": int(row.get("quick_review") or 0),
                "human": int(row.get("human_review") or 0),
                "blocked": int(row.get("blocked") or 0),
                "auto_percent": round((safe + warning) * 100 / returns, 2) if returns else 0,
                "top_blocking": _short_counter(row.get("top_5_blocking_reasons")),
                "top_warnings": _short_counter(row.get("top_5_warnings")),
            }
        )
    return {"ok": True, "total": len(items), "items": items}


@router.get("/api/quick-review/queue")
def quick_review_queue(
    buyer_code: str | None = None,
    review_type: str | None = None,
    field: str | None = None,
    reason: str | None = None,
    one_click_only: bool = False,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    all_rows = _read_jsonl(QUICK_REVIEW_PATH)
    facets = {
        "buyer_codes": sorted({str(row.get("buyer_code")) for row in all_rows if row.get("buyer_code")}),
        "review_types": sorted({str(row.get("review_type")) for row in all_rows if row.get("review_type")}),
        "fields": sorted({str(row.get("field")) for row in all_rows if row.get("field")}),
    }
    rows = all_rows
    if buyer_code:
        rows = [row for row in rows if row.get("buyer_code") == buyer_code]
    if review_type:
        rows = [row for row in rows if row.get("review_type") == review_type]
    if field:
        rows = [row for row in rows if row.get("field") == field]
    if reason:
        rows = [row for row in rows if reason.lower() in str(row.get("reason") or "").lower()]
    if one_click_only:
        rows = [row for row in rows if bool(row.get("one_click"))]
    return {"ok": True, "total": len(rows), "limit": limit, "offset": offset, "items": rows[offset : offset + limit], "facets": facets}


@router.get("/api/quick-review/item/{review_id:path}")
def quick_review_item(review_id: str) -> dict[str, Any]:
    item = next((row for row in _read_jsonl(QUICK_REVIEW_PATH) if row.get("review_id") == review_id), None)
    if not item:
        raise HTTPException(404, "Quick review item not found")
    result = dict(item)
    result["evidence_snippet"] = next(
        (candidate.get("evidence") for candidate in result.get("candidates") or [] if candidate.get("evidence")),
        result.get("source_snippet") or "",
    )
    return {"ok": True, "item": result}


@router.post("/api/quick-review/decision")
def quick_review_decision(decision: QuickReviewDecision) -> dict[str, Any]:
    item = next(
        (row for row in _read_jsonl(QUICK_REVIEW_PATH) if row.get("review_id") == decision.review_id),
        None,
    )
    if not item:
        raise HTTPException(404, "Quick review item not found")
    allowed = {str(candidate.get("value")) for candidate in item.get("candidates") or []}
    allowed.update({"not_return", "human_review"})
    if str(decision.selected_value) not in allowed:
        raise HTTPException(422, "Selected value is not one of the review candidates")
    # v2.1 AI-only: learning_ledger убран — решение оператора не пишем в журнал обучения.
    row = {
        "case_id": item.get("case_id"),
        "raw_email_id": item.get("raw_email_id"),
        "buyer_code": item.get("buyer_code") or "unknown",
        "field": item.get("field"),
        "old_value": item.get("current_value"),
        "new_value": decision.selected_value,
        "decision_type": "quick_review_choice",
        "review_id": decision.review_id,
    }
    _append_action(
        "quick_review_decision",
        {"review_id": decision.review_id, "case_id": item.get("case_id"), "operator": decision.operator},
    )
    return {"ok": True, "decision": row, "side_effects": []}


@router.get("/api/outbox-staging")
def outbox_staging(
    buyer_code: str | None = None,
    claim_kind: str | None = None,
    status: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    all_rows = _read_jsonl(STAGING_PATH)
    facets = {
        "buyer_codes": sorted({str(row.get("buyer_code")) for row in all_rows if row.get("buyer_code")}),
        "claim_kinds": sorted(
            {
                str(((row.get("one_c_payload_preview") or {}).get("claim") or {}).get("kind"))
                for row in all_rows
                if ((row.get("one_c_payload_preview") or {}).get("claim") or {}).get("kind")
            }
        ),
    }
    rows = all_rows
    if buyer_code:
        rows = [row for row in rows if row.get("buyer_code") == buyer_code]
    if claim_kind:
        rows = [
            row
            for row in rows
            if ((row.get("one_c_payload_preview") or {}).get("claim") or {}).get("kind") == claim_kind
        ]
    if status:
        rows = [row for row in rows if row.get("status") == status]
    return {"ok": True, "total": len(rows), "limit": limit, "offset": offset, "items": rows[offset : offset + limit], "facets": facets}


@router.get("/api/outbox-staging/item/{idempotency_key}")
def outbox_staging_item(idempotency_key: str) -> dict[str, Any]:
    item = next(
        (row for row in _read_jsonl(STAGING_PATH) if row.get("idempotency_key") == idempotency_key),
        None,
    )
    if not item:
        raise HTTPException(404, "Staging item not found")
    return {"ok": True, "item": item}


@router.get("/api/case/{case_id}/timeline")
def case_timeline(case_id: str) -> dict[str, Any]:
    case = _find_case(case_id)
    if not case:
        raise HTTPException(404, "Audit case not found")
    preview_class, preview = _find_preview(case_id)
    staging = next((row for row in _read_jsonl(STAGING_PATH) if _case_id(row) == case_id), None)
    outbox = _read_outbox(int(case_id)) if case_id.isdigit() else {"count": 0, "items": []}
    repair = case.get("evidence_repair") or {}
    repaired_case = repair.get("case_data") or {}
    payload = repaired_case.get("payload") or {}
    processing_source = payload.get("processing_source")
    stages = [
        {"stage": "original", "status": "available", "details": {"raw_email_id": case.get("raw_email_id"), "subject": case.get("subject")}},
        {"stage": "pattern", "status": "used" if processing_source == "pattern" else "not_recorded", "details": {"processing_source": processing_source}},
        {"stage": "ai", "status": "used" if processing_source == "ai" or payload.get("ai_overlay") else "not_used", "details": {"ai_overlay": bool(payload.get("ai_overlay"))}},
        {"stage": "final", "status": case.get("current_state"), "details": {"event_type": case.get("event_type"), "claim_kind": case.get("claim_kind")}},
        {"stage": "evidence_gate", "status": "passed" if (case.get("second_gate") or {}).get("passed") else "blocked", "details": case.get("second_gate") or case.get("initial_gate") or {}},
        {"stage": "repair", "status": "changed" if repair.get("changed") else "not_needed", "details": {"methods": case.get("repair_methods") or [], "warnings": repair.get("warnings") or []}},
        {"stage": "safety", "status": case.get("final_dry_run_class"), "details": case.get("safety") or {}},
        {"stage": "preview", "status": preview_class or "not_in_preview", "details": {"export_allowed": bool((preview or {}).get("export_allowed"))}},
        {"stage": "staging", "status": (staging or {}).get("status") or "not_staged", "details": staging or {}},
        {"stage": "real_outbox", "status": "present" if outbox.get("count") else "not_present", "details": outbox},
        {"stage": "1c", "status": "sent" if any(row.get("status") == "sent" for row in outbox.get("items") or []) else "not_called", "details": {"read_only_panel": True}},
    ]
    return {"ok": True, "case_id": case_id, "raw_email_id": case.get("raw_email_id"), "stages": stages}
