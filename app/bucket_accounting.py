"""Read-only accounting of raw emails, cases, UI views, and terminal buckets."""
from __future__ import annotations

from collections import Counter, defaultdict
import json
from typing import Any, Iterable


TERMINAL_STATES = {
    "linked_event",
    "ignored_info_only",
    "ignored_spam_promo",
    "ignored_internal",
    "ignored_ready_to_ship",
    "context_sent",
    "closed",
    "delivered",
    "exported",
}


def _ui_tabs(case: dict[str, Any], *, has_ai: bool, outbox_ids: list[int]) -> list[str]:
    tabs = ["emails"]
    state = str(case.get("state") or "")
    event_type = str(case.get("event_type") or "")
    needs_ai = bool(case.get("needs_ai"))
    link_quarantine = bool(case.get("link_quarantine"))

    if event_type == "new_return" and state in {"ready_to_1c", "needs_review"}:
        tabs.append("review")
    if event_type == "new_return" and state == "ready_to_1c" and not has_ai:
        tabs.append("patterns")
    if (
        state in {"needs_review", "needs_link"}
        and event_type in {"new_return", "unknown"}
        and (needs_ai or event_type == "unknown")
        and not has_ai
    ):
        tabs.append("ai_review")
    if (
        (state == "needs_link" or link_quarantine)
        and event_type not in {"info_only", "spam_promo", "unknown"}
    ):
        tabs.append("links")
    if (
        state in {"needs_review", "unknown"}
        and event_type not in {"followup_reminder", "followup_dialog", "supplier_decision"}
        and (
            has_ai
            or (not case.get("buyer_code") and not case.get("claim_kind"))
            or event_type == "unknown"
        )
    ):
        tabs.append("unprocessed")
    if state in {"ignored_info_only", "ignored_spam_promo"} or event_type in {
        "info_only",
        "spam_promo",
    }:
        tabs.append("offtopic")
    if outbox_ids:
        tabs.append("onec")
    return tabs


def _top_bucket(
    raw: dict[str, Any], case: dict[str, Any] | None, *, has_ai: bool
) -> tuple[str, bool, str]:
    if raw.get("duplicate_of_raw_email_id") or str(raw.get("status") or "") == "duplicate":
        return "duplicate", True, "duplicate_of_raw_email_id/status=duplicate"
    if case is None:
        return "no_case", False, "raw email has no case"

    state = str(case.get("state") or "")
    event_type = str(case.get("event_type") or "")
    claim_kind = str(case.get("claim_kind") or "")
    payload = case.get("payload") if isinstance(case.get("payload"), dict) else {}
    subcategory = str(payload.get("classification_subcategory") or "")
    if event_type == "marking_request" or claim_kind == "marking_request":
        return "service_marking", state == "linked_event", "marking/TNVED service request"
    if event_type == "correction_request" or claim_kind == "correction_request":
        return "service_correction", state == "linked_event", "document correction service request"
    if event_type == "pre_delivery_refusal" or case.get("pre_delivery_refusal"):
        return "visible_pre_delivery_refusal", False, "customer refusal before shipment"
    if subcategory == "shortage.link_only":
        return "visible_shortage_link", False, "shortage accepted through trusted external link"
    if state == "linked_event":
        return "terminal_linked", True, "linked successfully; hidden from pending Links tab"
    if state.startswith("ignored_") or event_type in {"info_only", "spam_promo"}:
        return "terminal_non_export", True, "information/noise terminal case"
    if state == "needs_link" or case.get("link_quarantine"):
        return "visible_needs_link", False, "pending link/follow-up resolution"
    if state == "ready_to_1c" and event_type == "new_return":
        return "visible_ready_to_1c", False, "ready return case"
    if state == "needs_review" and event_type == "new_return":
        if case.get("needs_ai") and not has_ai:
            return "visible_ai_review", False, "return case waiting for AI review"
        return "visible_review", False, "return case waiting for operator review"
    if event_type == "unknown" or state == "unknown":
        return "visible_unprocessed", False, "unknown/unclassified case"
    if state in TERMINAL_STATES:
        return "terminal_other", True, f"terminal state={state}"
    return "hidden_with_reason", False, f"no operational UI mapping for state={state}, event={event_type}"


def build_bucket_accounting(con: Any, *, include_items: bool = False) -> dict[str, Any]:
    """Build a complete, mutually exclusive accounting for every raw email."""
    raw_rows = [dict(row) for row in con.execute(
        """
        SELECT id, mailbox, uid, uidvalidity, subject, from_addr, message_id,
               status, duplicate_of_raw_email_id, received_at
        FROM raw_emails
        ORDER BY id
        """
    )]
    case_rows = [dict(row) for row in con.execute(
        """
        SELECT c.*, EXISTS(
            SELECT 1 FROM ai_suggestions s WHERE s.case_id=c.id
        ) AS has_ai_suggestion
        FROM cases c
        ORDER BY c.id
        """
    )]
    outbox_rows = [dict(row) for row in con.execute(
        "SELECT id, case_id, status, event_type FROM outbox ORDER BY id"
    )]

    cases_by_raw: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for case in case_rows:
        cases_by_raw[int(case["raw_email_id"])].append(case)
    outbox_by_case: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for outbox in outbox_rows:
        outbox_by_case[int(outbox["case_id"])].append(outbox)

    top_buckets: Counter[str] = Counter()
    states: Counter[str] = Counter()
    events: Counter[str] = Counter()
    ui_tabs: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    link_groups: Counter[str] = Counter()
    service_subcategories: Counter[str] = Counter()
    items: list[dict[str, Any]] = []

    for raw in raw_rows:
        raw_cases = cases_by_raw.get(int(raw["id"]), [])
        case = raw_cases[0] if raw_cases else None
        if case:
            try:
                case["payload"] = json.loads(case.get("payload_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                case["payload"] = {}
        outbox = [
            row
            for current_case in raw_cases
            for row in outbox_by_case.get(int(current_case["id"]), [])
        ]
        has_ai = bool(case and case.get("has_ai_suggestion"))
        tabs = _ui_tabs(case, has_ai=has_ai, outbox_ids=[int(x["id"]) for x in outbox]) if case else ["emails"]
        bucket, terminal, reason = _top_bucket(raw, case, has_ai=has_ai)
        operational_tabs = [tab for tab in tabs if tab != "emails"]
        not_displayed = not operational_tabs

        top_buckets[bucket] += 1
        reasons[reason] += 1
        for tab in tabs:
            ui_tabs[tab] += 1
        if case:
            states[str(case.get("state") or "NULL")] += 1
            events[str(case.get("event_type") or "NULL")] += 1
            event_type = str(case.get("event_type") or "")
            claim_kind = str(case.get("claim_kind") or "")
            state = str(case.get("state") or "")
            payload = case.get("payload") or {}
            subcategory = str(payload.get("classification_subcategory") or "")
            if event_type in {"marking_request", "correction_request", "info_update", "document_flow_notice"} or claim_kind in {"marking_request", "correction_request"}:
                link_groups["service"] += 1
                service_subcategories[subcategory or claim_kind or event_type] += 1
            elif state == "needs_link" or case.get("link_quarantine"):
                link_groups["active"] += 1
            elif state == "linked_event":
                link_groups["completed"] += 1

        items.append({
            "raw_email_id": raw["id"],
            "subject": raw.get("subject"),
            "from": raw.get("from_addr"),
            "message_id": raw.get("message_id"),
            "mailbox": raw.get("mailbox"),
            "uid": raw.get("uid"),
            "uidvalidity": raw.get("uidvalidity"),
            "raw_status": raw.get("status"),
            "duplicate_of_raw_email_id": raw.get("duplicate_of_raw_email_id"),
            "has_case": bool(raw_cases),
            "case_id": case.get("id") if case else None,
            "case_ids": [current["id"] for current in raw_cases],
            "case_state": case.get("state") if case else None,
            "case_status": case.get("status") if case else None,
            "event_type": case.get("event_type") if case else None,
            "claim_kind": case.get("claim_kind") if case else None,
            "classification_subcategory": (
                (case.get("payload") or {}).get("classification_subcategory")
                if case else None
            ),
            "bucket": bucket,
            "ui_tabs": tabs,
            "terminal": terminal,
            "outbox_id": outbox[0]["id"] if outbox else None,
            "outbox_ids": [current["id"] for current in outbox],
            "not_displayed_in_operational_tabs": not_displayed,
            "reason_why_not_visible": reason if not_displayed else None,
        })

    total = len(items)
    raw_ids = {int(raw["id"]) for raw in raw_rows}
    accounted = sum(top_buckets.values())
    hidden = sum(1 for item in items if item["not_displayed_in_operational_tabs"])
    summary = {
        "schema": "readmail-bucket-accounting-v1",
        "read_only": True,
        "total_raw": total,
        "accounted_raw": accounted,
        "accounting_gap": total - accounted,
        "raw_with_case": sum(1 for item in items if item["has_case"]),
        "raw_without_case": sum(1 for item in items if not item["has_case"]),
        "cases_total": len(case_rows),
        "cases_without_raw": sum(
            1 for case in case_rows if int(case["raw_email_id"]) not in raw_ids
        ),
        "duplicate_raw": top_buckets["duplicate"],
        "outbox_total": len(outbox_rows),
        "hidden_from_operational_tabs": hidden,
        "by_bucket": dict(top_buckets.most_common()),
        "by_case_state": dict(states.most_common()),
        "by_event_type": dict(events.most_common()),
        "by_ui_tab": dict(ui_tabs.most_common()),
        "by_link_group": dict(link_groups.most_common()),
        "by_service_subcategory": dict(service_subcategories.most_common()),
        "top_visibility_reasons": dict(reasons.most_common(20)),
        "ui_counts_are_overlapping": True,
    }
    result = {"ok": True, "summary": summary}
    if include_items:
        result["items"] = items
    return result


def markdown_summary(summary: dict[str, Any]) -> str:
    def table(title: str, values: dict[str, int]) -> list[str]:
        lines = [f"## {title}", "", "| Категория | Количество |", "|---|---:|"]
        lines.extend(f"| `{key}` | {value} |" for key, value in values.items())
        lines.append("")
        return lines

    lines = [
        "# Bucket Accounting Matrix Summary",
        "",
        f"- Total raw: **{summary['total_raw']}**",
        f"- Accounted raw: **{summary['accounted_raw']}**",
        f"- Accounting gap: **{summary['accounting_gap']}**",
        f"- Raw with case: **{summary['raw_with_case']}**",
        f"- Raw without case: **{summary['raw_without_case']}**",
        f"- Cases: **{summary['cases_total']}**",
        f"- Outbox: **{summary['outbox_total']}**",
        f"- Hidden from operational tabs: **{summary['hidden_from_operational_tabs']}**",
        "",
        "UI tab counts overlap by design and must not be added together.",
        "",
    ]
    lines.extend(table("Mutually Exclusive Buckets", summary["by_bucket"]))
    lines.extend(table("Case States", summary["by_case_state"]))
    lines.extend(table("Event Types", summary["by_event_type"]))
    lines.extend(table("UI Views (Overlapping)", summary["by_ui_tab"]))
    lines.extend(table("Link Groups", summary.get("by_link_group") or {}))
    lines.extend(table("Service Subcategories", summary.get("by_service_subcategory") or {}))
    return "\n".join(lines)


def select_not_visible(items: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw_items = [
        item for item in items
        if item.get("not_displayed_in_operational_tabs") or not item.get("has_case")
    ]
    case_items = [item for item in raw_items if item.get("has_case")]
    return raw_items, case_items
