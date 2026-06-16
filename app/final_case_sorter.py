from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable


BUCKETS = (
    "auto_safe_staged",
    "auto_safe_preview_not_staged",
    "auto_warning_candidate",
    "quick_review_one_click",
    "quick_review_choice",
    "human_review",
    "blocked_needs_rule",
    "needs_link",
    "terminal_non_export",
    "duplicate_or_followup",
    "unknown_error",
)
TERMINAL_STATES = {"ignored_info_only", "ignore_info_only", "info_only"}
FOLLOWUP_STATES = {"linked_event", "linking_event"}
FOLLOWUP_EVENTS = {"followup_reminder", "followup_dialog", "supplier_decision"}
REPEAT_BLOCKER_MIN = 5

ACTION_POLICY = {
    "auto_safe_staged": (
        "await_staging_approval",
        ["view_case", "open_timeline", "view_staging_payload"],
        ["send_to_1c", "write_real_outbox", "run_ai", "edit_processed_case"],
    ),
    "auto_safe_preview_not_staged": (
        "stage_safe",
        ["view_case", "open_timeline", "view_preview", "stage_safe"],
        ["send_to_1c", "write_real_outbox", "run_ai", "stage_warning"],
    ),
    "auto_warning_candidate": (
        "manual_sample_or_warning_policy",
        ["view_case", "open_timeline", "view_preview", "sample_manually"],
        ["send_to_1c", "write_real_outbox", "stage_automatically", "run_ai"],
    ),
    "quick_review_one_click": (
        "operator_one_click_decision",
        ["view_case", "open_timeline", "quick_review_decision", "send_to_human_review"],
        ["send_to_1c", "write_real_outbox", "stage_safe", "run_ai"],
    ),
    "quick_review_choice": (
        "operator_choice",
        ["view_case", "open_timeline", "quick_review_decision", "send_to_human_review"],
        ["send_to_1c", "write_real_outbox", "stage_safe", "run_ai"],
    ),
    "human_review": (
        "operator_full_review",
        ["view_case", "open_timeline", "send_to_human_review"],
        ["send_to_1c", "write_real_outbox", "stage_safe", "run_ai"],
    ),
    "blocked_needs_rule": (
        "create_supplier_rule_or_parser_fix",
        ["view_case", "open_timeline", "inspect_blocking_reason"],
        ["send_to_1c", "write_real_outbox", "stage_safe", "quick_auto_apply", "run_ai"],
    ),
    "needs_link": (
        "link_to_parent_case",
        ["view_case", "open_timeline", "inspect_link_candidates"],
        ["send_to_1c", "write_real_outbox", "stage_safe", "run_ai"],
    ),
    "terminal_non_export": (
        "no_action_terminal",
        ["view_case", "open_timeline"],
        ["send_to_1c", "write_real_outbox", "stage_safe", "quick_review_decision", "run_ai"],
    ),
    "duplicate_or_followup": (
        "keep_linked_no_export",
        ["view_case", "open_timeline", "view_parent_link"],
        ["send_to_1c", "write_real_outbox", "stage_safe", "quick_review_decision", "run_ai"],
    ),
    "unknown_error": (
        "inspect_runtime_or_data_error",
        ["view_case", "open_timeline", "inspect_raw_artifacts"],
        ["send_to_1c", "write_real_outbox", "stage_safe", "quick_auto_apply", "run_ai"],
    ),
}


def _case_key(value: Any) -> str:
    return str(value) if value not in (None, "") else ""


def _index(rows: Iterable[dict[str, Any]], key: str = "case_id") -> dict[str, dict[str, Any]]:
    return {
        _case_key(row.get(key)): row
        for row in rows
        if _case_key(row.get(key))
    }


def _group(rows: Iterable[dict[str, Any]], key: str = "case_id") -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        case_id = _case_key(row.get(key))
        if case_id:
            grouped[case_id].append(row)
    return grouped


def _evidence_summary(case: dict[str, Any]) -> dict[str, Any]:
    gate = case.get("second_gate") or case.get("initial_gate") or {}
    field_audit = gate.get("field_audit") or {}
    fields = {}
    for field in (
        "document_number",
        "document_date",
        "part_number",
        "quantity",
        "claim_kind",
        "buyer_code",
    ):
        audit = field_audit.get(field) or {}
        fields[field] = {
            "value": audit.get("value"),
            "status": audit.get("status") or (gate.get("field_statuses") or {}).get(field),
            "source": audit.get("source"),
            "method": audit.get("evidence_method") or audit.get("repair_method"),
        }
    return {
        "gate_passed": bool(gate.get("passed")),
        "safety_class": case.get("final_dry_run_class"),
        "risk_score": case.get("risk_score"),
        "fields": fields,
    }


def _ledger_status(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    if not decisions:
        return {"status": "no_decision", "decisions_count": 0, "review_ids": []}
    latest = decisions[-1]
    return {
        "status": "decision_recorded_awaiting_rebuild",
        "decisions_count": len(decisions),
        "review_ids": [row.get("review_id") for row in decisions if row.get("review_id")],
        "latest": {
            "timestamp": latest.get("timestamp"),
            "field": latest.get("field"),
            "new_value": latest.get("new_value"),
            "operator": latest.get("operator"),
        },
    }


def _choose_bucket(
    case: dict[str, Any],
    tasks: list[dict[str, Any]],
    staged: dict[str, Any] | None,
    safe_preview: dict[str, Any] | None,
    warning_preview: dict[str, Any] | None,
    repeated_reasons: set[str],
) -> str:
    state = str(case.get("current_state") or "")
    event_type = str(case.get("event_type") or "")
    dry_class = str(case.get("final_dry_run_class") or "")

    if state in FOLLOWUP_STATES:
        return "duplicate_or_followup"
    if state in TERMINAL_STATES or case.get("disposition") == "terminal_non_export":
        return "terminal_non_export"
    if state == "needs_link" or case.get("disposition") == "link_required":
        return "needs_link"
    if event_type in FOLLOWUP_EVENTS:
        return "duplicate_or_followup"
    if staged and staged.get("safety_class") == "auto_export_safe":
        return "auto_safe_staged"
    if safe_preview or dry_class == "auto_export_safe":
        return "auto_safe_preview_not_staged"
    if warning_preview or dry_class == "auto_export_with_warning":
        return "auto_warning_candidate"
    if any(task.get("review_type") == "send_to_human_review" for task in tasks):
        return "human_review"
    if tasks:
        if all(bool(task.get("one_click")) for task in tasks):
            return "quick_review_one_click"
        return "quick_review_choice"
    if dry_class in {"human_review", "suspicious_passed"}:
        return "human_review"
    blockers = set(str(reason) for reason in case.get("blocking_reasons") or [])
    if dry_class == "blocked" and blockers & repeated_reasons:
        return "blocked_needs_rule"
    if dry_class == "blocked":
        return "human_review"
    return "unknown_error"


def build_final_sorting(
    cases: Iterable[dict[str, Any]],
    quick_review_tasks: Iterable[dict[str, Any]] = (),
    safe_previews: Iterable[dict[str, Any]] = (),
    warning_previews: Iterable[dict[str, Any]] = (),
    staging_items: Iterable[dict[str, Any]] = (),
    learning_decisions: Iterable[dict[str, Any]] = (),
    outbox_by_case: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    case_rows = list(cases)
    tasks_by_case = _group(quick_review_tasks)
    safe_by_case = _index(safe_previews)
    warning_by_case = _index(warning_previews)
    staging_by_case = _index(staging_items)
    ledger_by_case = _group(learning_decisions)
    outbox_by_case = outbox_by_case or {}
    blocker_counts = Counter(
        str(reason)
        for case in case_rows
        for reason in case.get("blocking_reasons") or []
        if reason
    )
    repeated_reasons = {
        reason for reason, count in blocker_counts.items() if count >= REPEAT_BLOCKER_MIN
    }
    result = []
    for case in case_rows:
        case_id = _case_key(case.get("case_id"))
        tasks = tasks_by_case.get(case_id, [])
        staged = staging_by_case.get(case_id)
        safe_preview = safe_by_case.get(case_id)
        warning_preview = warning_by_case.get(case_id)
        decisions = ledger_by_case.get(case_id, [])
        bucket = _choose_bucket(
            case,
            tasks,
            staged,
            safe_preview,
            warning_preview,
            repeated_reasons,
        )
        next_action, allowed, forbidden = ACTION_POLICY[bucket]
        ledger_status = _ledger_status(decisions)
        if decisions and bucket in {"quick_review_one_click", "quick_review_choice"}:
            next_action = "decision_recorded_awaiting_rebuild"
        outbox_rows = outbox_by_case.get(case_id, [])
        outbox_status = {
            "present": bool(outbox_rows),
            "count": len(outbox_rows),
            "statuses": sorted({str(row.get("status") or "unknown") for row in outbox_rows}),
        }
        result.append(
            {
                "case_id": case.get("case_id"),
                "raw_email_id": case.get("raw_email_id"),
                "buyer_code": case.get("buyer_code") or "unknown",
                "subject": case.get("subject") or "",
                "current_state": case.get("current_state"),
                "event_type": case.get("event_type"),
                "claim_kind": case.get("claim_kind"),
                "final_bucket": bucket,
                "next_action": next_action,
                "allowed_actions": list(allowed),
                "forbidden_actions": list(forbidden),
                "blocking_reasons": list(case.get("blocking_reasons") or []),
                "warning_reasons": list(case.get("warning_reasons") or []),
                "evidence_summary": _evidence_summary(case),
                "review_tasks_count": len(tasks),
                "review_tasks": [
                    {
                        "review_id": task.get("review_id"),
                        "review_type": task.get("review_type"),
                        "field": task.get("field"),
                        "reason": task.get("reason"),
                        "one_click": bool(task.get("one_click")),
                    }
                    for task in tasks
                ],
                "staged_status": {
                    "staged": bool(staged),
                    "status": (staged or {}).get("status"),
                    "idempotency_key": (staged or {}).get("idempotency_key"),
                },
                "preview_status": {
                    "safe": bool(safe_preview),
                    "warning": bool(warning_preview),
                },
                "outbox_status": outbox_status,
                "learning_ledger_status": ledger_status,
            }
        )
    return result


def summarize_final_sorting(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    items = list(rows)
    buckets = Counter(str(row.get("final_bucket") or "unknown_error") for row in items)
    suppliers: dict[str, Counter[str]] = defaultdict(Counter)
    for row in items:
        suppliers[str(row.get("buyer_code") or "unknown")][str(row.get("final_bucket"))] += 1
    return {
        "total_cases": len(items),
        "return_cases": sum(
            1
            for row in items
            if row.get("event_type") == "new_return"
            and row.get("final_bucket") not in {"terminal_non_export", "duplicate_or_followup"}
        ),
        "by_bucket": {bucket: buckets.get(bucket, 0) for bucket in BUCKETS},
        "top_next_actions": dict(Counter(str(row.get("next_action")) for row in items).most_common(20)),
        "by_supplier": {
            buyer: {"total": sum(counter.values()), **{bucket: counter.get(bucket, 0) for bucket in BUCKETS}}
            for buyer, counter in sorted(suppliers.items(), key=lambda item: sum(item[1].values()), reverse=True)
        },
    }
