#!/usr/bin/env python3
"""Offline evidence audit for processed email cases.

The script reads JSONL exports only. It never imports the application, opens
SQLite, calls AI, or writes outside the requested output directory.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


AUDITED_FIELDS = (
    "document_number",
    "document_date",
    "claim_number",
    "part_number",
    "brand",
    "product_name",
    "quantity",
    "claim_kind",
    "event_type",
    "buyer_code",
    "state",
    "ready_for_export",
)

TEXT_FIELDS = ("subject", "body_text", "visible_text", "body_html")
FOLLOWUP_EVENT_TYPES = {"followup_reminder", "followup_dialog", "supplier_decision"}
INFO_STATES = {"ignored_info_only", "ignored_internal", "offtopic"}

DOCUMENT_POSITIVE = (
    "документ",
    "упд",
    "реализац",
    "накладн",
    "счет-фактур",
    "счёт-фактур",
)
DOCUMENT_NEGATIVE = (
    "претензи",
    "заявк",
    "обращени",
    "claim",
    "рекламаци",
)
QUANTITY_LABELS = ("количество", "кол-во", "кол во", "к-во", "qty", "шт", "штук", "претензи")
PART_LABELS = ("артикул", "арт.", "арт ", "код", "номенклатур", "каталожн", "oem", "p/n", "номер детали")

CLAIM_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("quality_refusal", ("отказ клиента", "клиент отказался", "отказался клиент", "отказ")),
    ("defect", ("брак", "дефект", "неисправен", "неисправна", "неисправно")),
    ("shortage", ("недовоз", "не довезли", "не поступило", "не поставлено", "недопостав")),
    ("wrong_item", ("пересорт", "не тот товар", "другой товар", "не та деталь")),
    ("marking_request", ("маркировк", "честный знак", "чз", "эдо")),
)

CLAIM_EQUIVALENTS = {
    "customer_refusal": "quality_refusal",
    "refusal": "quality_refusal",
    "nonconforming": "defect",
    "wrong_product": "wrong_item",
    "under_delivery": "shortage",
}

STATUS_WEIGHT = {
    "confirmed_exact": 0,
    "confirmed_normalized": 2,
    "not_applicable": 0,
    "weak_found": 8,
    "missing_processed": 6,
    "not_found": 16,
}


def compact(value: Any) -> str:
    return " ".join(str(value or "").split())


def strip_html(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return compact(text)


def normalize_text(value: Any) -> str:
    text = html.unescape(str(value or "")).lower().replace("ё", "е")
    text = re.sub(r"[№#]", " n ", text)
    text = re.sub(r"\bno\.?\b", " n ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_compact(value: Any) -> str:
    return re.sub(r"[^0-9a-zа-я]+", "", normalize_text(value))


def normalize_identifier(value: Any) -> str:
    text = normalize_text(value)
    text = re.sub(r"^(?:n|номер)\s*", "", text)
    return re.sub(r"[\s\-_/.,:;]+", "", text)


def date_variants(value: Any) -> set[str]:
    raw = compact(value)
    variants = {raw, normalize_text(raw), normalize_compact(raw)}
    match = re.search(r"(?<!\d)(\d{1,2})[./\-](\d{1,2})[./\-](\d{2,4})(?!\d)", raw)
    if match:
        day, month, year = match.groups()
        year = ("20" + year) if len(year) == 2 else year
        day2, month2 = day.zfill(2), month.zfill(2)
        variants.update(
            {
                f"{day2}.{month2}.{year}",
                f"{day2}/{month2}/{year}",
                f"{day2}-{month2}-{year}",
                f"{year}-{month2}-{day2}",
                f"{day2}{month2}{year}",
            }
        )
    return {v for v in variants if v}


def load_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    rows.append(obj)
                else:
                    errors.append({"line": line_no, "error": "JSON value is not an object"})
            except Exception as exc:
                errors.append({"line": line_no, "error": str(exc)[:300]})
    return rows, errors


def raw_id(row: dict[str, Any]) -> str:
    value = row.get("raw_email_id")
    if value not in (None, ""):
        return str(value)
    export_id = row.get("parent_export_id") or row.get("etalon_id") or row.get("export_id")
    match = re.search(r"(\d+)", str(export_id or ""))
    return match.group(1) if match else ""


def case_value(case: dict[str, Any], field: str) -> Any:
    fields = case.get("fields") if isinstance(case.get("fields"), dict) else {}
    if field in fields:
        return fields.get(field)
    if field in case:
        return case.get(field)
    final_case = case.get("final_case") if isinstance(case.get("final_case"), dict) else {}
    if field in final_case:
        return final_case.get(field)
    final_fields = final_case.get("fields") if isinstance(final_case.get("fields"), dict) else {}
    if field in final_fields:
        return final_fields.get(field)
    export = case.get("export_json") if isinstance(case.get("export_json"), dict) else {}
    if field in export:
        return export.get(field)
    return None


def source_documents(original: dict[str, Any]) -> list[tuple[str, str, bool]]:
    docs: list[tuple[str, str, bool]] = []
    for field in TEXT_FIELDS:
        value = original.get(field)
        if not value:
            continue
        text = strip_html(value) if field == "body_html" else str(value)
        source = "html" if field == "body_html" else ("body" if field == "body_text" else field)
        docs.append((source, text, field == "body_html"))
    for attachment in original.get("attachments") or []:
        if not isinstance(attachment, dict):
            continue
        filename = compact(attachment.get("filename"))
        extracted = compact(attachment.get("extracted_text"))
        if filename:
            docs.append(("attachment", filename, False))
        if extracted:
            docs.append(("attachment", extracted, True))
    return docs


def snippet(text: str, start: int, length: int, radius: int = 90) -> str:
    left = max(0, start - radius)
    right = min(len(text), start + max(1, length) + radius)
    return compact(text[left:right])[:360]


def find_raw(value: Any, docs: Iterable[tuple[str, str, bool]]) -> tuple[str, str] | None:
    needle = compact(value)
    if not needle:
        return None
    needle_lower = needle.lower()
    for source, text, _table_like in docs:
        pos = text.lower().find(needle_lower)
        if pos >= 0:
            return source, snippet(text, pos, len(needle))
    return None


def find_normalized(
    value: Any,
    docs: Iterable[tuple[str, str, bool]],
    *,
    field: str,
) -> tuple[str, str] | None:
    variants = date_variants(value) if field == "document_date" else {
        normalize_text(value),
        normalize_compact(value),
        normalize_identifier(value),
    }
    variants.discard("")
    for source, text, _table_like in docs:
        normalized_options = (normalize_text(text), normalize_compact(text), normalize_identifier(text))
        if any(v in candidate for v in variants for candidate in normalized_options if len(v) >= 2):
            # Locate a readable approximation for the report.
            tokens = [t for t in re.split(r"\W+", compact(value)) if len(t) > 1]
            pos = next((text.lower().find(t.lower()) for t in tokens if text.lower().find(t.lower()) >= 0), 0)
            return source, snippet(text, max(0, pos), len(compact(value)))
    return None


def contexts_for(value: Any, docs: Iterable[tuple[str, str, bool]], radius: int = 90) -> list[tuple[str, str, bool]]:
    needle = compact(value).lower()
    if not needle:
        return []
    result = []
    for source, text, table_like in docs:
        low = text.lower()
        start = 0
        while True:
            pos = low.find(needle, start)
            if pos < 0:
                break
            result.append((source, snippet(text, pos, len(needle), radius), table_like))
            start = pos + max(1, len(needle))
            if len(result) >= 12:
                return result
    return result


def empty_check(value: Any, status: str = "missing_processed") -> dict[str, Any]:
    return {
        "value": value,
        "status": status,
        "source": "none",
        "evidence_snippet": "",
        "warnings": [],
    }


def base_evidence(field: str, value: Any, docs: list[tuple[str, str, bool]]) -> dict[str, Any]:
    if value in (None, "", [], {}):
        return empty_check(value)
    exact = find_raw(value, docs)
    if exact:
        return {
            "value": value,
            "status": "confirmed_exact",
            "source": exact[0],
            "evidence_snippet": exact[1],
            "warnings": [],
        }
    normalized = find_normalized(value, docs, field=field)
    if normalized:
        return {
            "value": value,
            "status": "confirmed_normalized",
            "source": normalized[0],
            "evidence_snippet": normalized[1],
            "warnings": [],
        }
    return {
        "value": value,
        "status": "not_found",
        "source": "none",
        "evidence_snippet": "",
        "warnings": [],
    }


def mark_warning(check: dict[str, Any], warning: str) -> None:
    if warning not in check["warnings"]:
        check["warnings"].append(warning)


def audit_document_number(value: Any, docs: list[tuple[str, str, bool]]) -> dict[str, Any]:
    check = base_evidence("document_number", value, docs)
    if check["status"] in {"missing_processed", "not_found"}:
        return check
    contexts = contexts_for(value, docs)
    joined = " ".join(ctx.lower() for _src, ctx, _table in contexts)
    has_positive = any(word in joined for word in DOCUMENT_POSITIVE)
    has_negative = any(word in joined for word in DOCUMENT_NEGATIVE)
    if has_negative and not has_positive:
        check["status"] = "weak_found"
        mark_warning(check, "possible_claim_number_not_document_number")
    elif not has_positive:
        check["status"] = "weak_found"
        mark_warning(check, "document_number_without_document_context")
    return check


def audit_quantity(value: Any, docs: list[tuple[str, str, bool]]) -> dict[str, Any]:
    if value in (None, ""):
        return empty_check(value)
    raw = compact(value).replace(",", ".")
    try:
        numeric = float(raw)
        rendered = str(int(numeric)) if numeric.is_integer() else str(numeric)
        token_re = re.compile(
            rf"(?<![\d.,]){re.escape(rendered)}(?:[.,]0+)?(?![\d.,])",
            re.IGNORECASE,
        )
    except Exception:
        numeric = None
        token_re = re.compile(rf"(?<!\w){re.escape(raw)}(?!\w)", re.IGNORECASE)

    contexts: list[tuple[str, str, bool]] = []
    for source, text, table_like in docs:
        for match in token_re.finditer(text):
            contexts.append((source, snippet(text, match.start(), len(match.group()), 55), table_like))
            if len(contexts) >= 100:
                break
        if len(contexts) >= 100:
            break
    if not contexts:
        return {
            "value": value,
            "status": "not_found",
            "source": "none",
            "evidence_snippet": "",
            "warnings": [],
        }
    strong = any(
        table_like or any(label in ctx.lower() for label in QUANTITY_LABELS)
        for _source, ctx, table_like in contexts
    )
    preferred = next(
        (
            item
            for item in contexts
            if item[2] or any(label in item[1].lower() for label in QUANTITY_LABELS)
        ),
        contexts[0],
    )
    check = {
        "value": value,
        "status": "confirmed_exact",
        "source": preferred[0],
        "evidence_snippet": preferred[1],
        "warnings": [],
    }
    if not strong:
        check["status"] = "weak_found"
        mark_warning(
            check,
            "quantity_1_without_context" if numeric == 1 else "quantity_without_context",
        )
    return check


def suspicious_part_number(value: Any) -> str | None:
    raw = compact(value)
    digits = re.sub(r"\D", "", raw)
    if re.fullmatch(r"\+?\d[\d ()\-]{8,}\d", raw) and len(digits) in range(10, 16):
        return "part_number_looks_like_phone"
    if re.fullmatch(r"\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4}", raw):
        return "part_number_looks_like_date"
    if "@" in raw:
        return "part_number_looks_like_email"
    return None


def audit_part_number(value: Any, docs: list[tuple[str, str, bool]]) -> dict[str, Any]:
    check = base_evidence("part_number", value, docs)
    if check["status"] == "missing_processed":
        return check
    suspicious = suspicious_part_number(value)
    if suspicious:
        check["status"] = "weak_found" if check["status"] != "not_found" else "not_found"
        mark_warning(check, suspicious)
    if check["status"] == "not_found":
        return check
    contexts = contexts_for(value, docs)
    strong = any(
        table_like or any(label in ctx.lower() for label in PART_LABELS)
        for _source, ctx, table_like in contexts
    )
    raw = compact(value)
    if re.fullmatch(r"\d{4,8}", raw):
        has_document_context = any(
            any(label in ctx.lower() for label in DOCUMENT_POSITIVE)
            for _source, ctx, _table_like in contexts
        )
        if has_document_context and not strong:
            check["status"] = "weak_found"
            mark_warning(check, "part_number_looks_like_document_number")
    if not strong:
        check["status"] = "weak_found"
        mark_warning(check, "part_number_without_product_context")
    return check


def detect_claim_kind(text: str) -> tuple[str | None, str]:
    normalized = normalize_text(text)
    for kind, hints in CLAIM_HINTS:
        for hint in hints:
            if re.search(rf"(?<![a-zа-я0-9]){re.escape(hint)}", normalized):
                pos = normalized.find(hint)
                return kind, snippet(text, max(0, pos), len(hint))
    return None, ""


def audit_claim_kind(value: Any, combined_text: str) -> dict[str, Any]:
    if value in (None, ""):
        return empty_check(value)
    detected, evidence = detect_claim_kind(combined_text)
    normalized_value = CLAIM_EQUIVALENTS.get(str(value), str(value))
    if not detected:
        return {
            "value": value,
            "status": "weak_found",
            "source": "body",
            "evidence_snippet": "",
            "warnings": ["claim_kind_without_explicit_evidence"],
        }
    if normalized_value != detected:
        return {
            "value": value,
            "status": "weak_found",
            "source": "body",
            "evidence_snippet": evidence,
            "warnings": ["claim_kind_conflict"],
        }
    return {
        "value": value,
        "status": "confirmed_normalized",
        "source": "body",
        "evidence_snippet": evidence,
        "warnings": [],
    }


def audit_event_type(value: Any, original: dict[str, Any], combined_text: str) -> dict[str, Any]:
    if value in (None, ""):
        return empty_check(value)
    event = str(value)
    subject = normalize_text(original.get("subject"))
    if event in FOLLOWUP_EVENT_TYPES:
        signals = []
        if original.get("in_reply_to"):
            signals.append("in_reply_to")
        if original.get("references"):
            signals.append("references")
        if re.match(r"^(re|fw|fwd)\s*:", subject):
            signals.append("reply_subject")
        if signals:
            return {
                "value": value,
                "status": "confirmed_normalized",
                "source": "subject" if "reply_subject" in signals else "none",
                "evidence_snippet": ", ".join(signals),
                "warnings": [],
            }
        return {
            "value": value,
            "status": "weak_found",
            "source": "none",
            "evidence_snippet": "",
            "warnings": ["followup_event_without_header_evidence"],
        }
    if event == "new_return":
        detected, evidence = detect_claim_kind(combined_text)
        if detected:
            return {
                "value": value,
                "status": "confirmed_normalized",
                "source": "body",
                "evidence_snippet": evidence,
                "warnings": [],
            }
    return empty_check(value, "not_applicable")


def audit_buyer(value: Any, original: dict[str, Any], docs: list[tuple[str, str, bool]]) -> dict[str, Any]:
    if value in (None, ""):
        return empty_check(value)
    candidates = {
        str(value),
        str(value).replace("_", "."),
        str(value).replace("_", "-"),
        str(value).replace("_", " "),
    }
    address_text = " ".join(
        str(original.get(k) or "") for k in ("from_addr", "to_addr", "cc_addr", "mailbox")
    )
    all_docs = [("headers", address_text, False), *docs]
    for candidate in candidates:
        found = find_normalized(candidate, all_docs, field="buyer_code")
        if found:
            return {
                "value": value,
                "status": "confirmed_normalized",
                "source": found[0] if found[0] != "headers" else "none",
                "evidence_snippet": found[1],
                "warnings": [],
            }
    return {
        "value": value,
        "status": "weak_found",
        "source": "none",
        "evidence_snippet": compact(address_text)[:360],
        "warnings": ["buyer_code_not_directly_confirmed"],
    }


def not_applicable(field: str, case: dict[str, Any]) -> bool:
    event_type = str(case_value(case, "event_type") or "")
    state = str(case_value(case, "state") or "")
    if field in {"state", "ready_for_export"}:
        return True
    if event_type in FOLLOWUP_EVENT_TYPES and field in {"brand", "product_name", "quantity"}:
        return True
    if state in INFO_STATES and field in {
        "document_number",
        "document_date",
        "claim_number",
        "part_number",
        "brand",
        "product_name",
        "quantity",
        "claim_kind",
    }:
        return True
    return False


def strong_followup_link(
    case: dict[str, Any],
    original: dict[str, Any],
    field_audit: dict[str, dict[str, Any]],
) -> tuple[bool, list[str]]:
    signals: list[str] = []
    if original.get("in_reply_to"):
        signals.append("in_reply_to")
    if original.get("references"):
        signals.append("references")
    for field in ("document_number", "claim_number", "part_number"):
        if field_audit.get(field, {}).get("status") in {"confirmed_exact", "confirmed_normalized"}:
            signals.append(field)
    subject = normalize_text(original.get("subject"))
    if re.match(r"^(re|fw|fwd)\s*:", subject):
        signals.append("normalized_reply_subject")
    return bool(signals), signals


def audit_case(case: dict[str, Any], original: dict[str, Any] | None) -> dict[str, Any]:
    rid = raw_id(case)
    result = {
        "raw_email_id": case.get("raw_email_id") if case.get("raw_email_id") is not None else rid,
        "case_id": case.get("case_id") or case.get("id"),
        "buyer_code": case_value(case, "buyer_code"),
        "state": case_value(case, "state"),
        "event_type": case_value(case, "event_type"),
        "claim_kind": case_value(case, "claim_kind"),
        "field_audit": {},
        "errors": [],
        "warnings": [],
        "risk_score": 0,
        "recommended_state": "needs_review",
    }
    if original is None:
        result["errors"].append("original_email_not_found")
        for field in AUDITED_FIELDS:
            result["field_audit"][field] = {
                **empty_check(case_value(case, field), "not_found"),
                "warnings": ["original_email_not_found"],
            }
        result["risk_score"] = 100
        return result

    docs = source_documents(original)
    combined_text = "\n".join(text for _source, text, _table in docs)
    for field in AUDITED_FIELDS:
        value = case_value(case, field)
        if not_applicable(field, case):
            check = empty_check(value, "not_applicable")
        elif field == "document_number":
            check = audit_document_number(value, docs)
        elif field == "quantity":
            check = audit_quantity(value, docs)
        elif field == "part_number":
            check = audit_part_number(value, docs)
        elif field == "claim_kind":
            check = audit_claim_kind(value, combined_text)
        elif field == "event_type":
            check = audit_event_type(value, original, combined_text)
        elif field == "buyer_code":
            check = audit_buyer(value, original, docs)
        else:
            check = base_evidence(field, value, docs)
        result["field_audit"][field] = check
        result["warnings"].extend(check.get("warnings") or [])
        if check["status"] == "not_found":
            result["errors"].append(f"{field}_not_found")

    state = str(result["state"] or "")
    event_type = str(result["event_type"] or "")
    if state == "linked_event" or event_type in FOLLOWUP_EVENT_TYPES:
        linked, signals = strong_followup_link(case, original, result["field_audit"])
        result["followup_link_signals"] = signals
        if not linked:
            result["errors"].append("weak_followup_link")

    result["errors"] = list(dict.fromkeys(result["errors"]))
    result["warnings"] = list(dict.fromkeys(result["warnings"]))
    risk = sum(STATUS_WEIGHT.get(check["status"], 0) for check in result["field_audit"].values())
    risk += 18 * len(result["errors"])
    risk += 5 * len(result["warnings"])
    if "weak_followup_link" in result["errors"]:
        risk += 25
    if "claim_kind_conflict" in result["warnings"]:
        risk += 15
    result["risk_score"] = min(100, risk)

    if state in INFO_STATES:
        result["recommended_state"] = "ignored_info_only"
    elif state == "linked_event" or event_type in FOLLOWUP_EVENT_TYPES:
        result["recommended_state"] = "linked_event" if "weak_followup_link" not in result["errors"] else "needs_review"
    elif result["risk_score"] <= 20 and not result["errors"]:
        result["recommended_state"] = "ready_to_1c"
    else:
        result["recommended_state"] = "needs_review"
    return result


def risk_bucket(score: int) -> str:
    if score <= 20:
        return "0-20"
    if score <= 50:
        return "21-50"
    if score <= 80:
        return "51-80"
    return "81-100"


def counter_dict(counter: Counter[Any]) -> dict[str, int]:
    return {str(key): int(value) for key, value in counter.most_common()}


def build_summary(
    results: list[dict[str, Any]],
    original_rows: int,
    processed_rows: int,
    skipped_without_case: int,
    original_load_errors: list[dict[str, Any]],
    processed_load_errors: list[dict[str, Any]],
) -> dict[str, Any]:
    by_state: Counter[str] = Counter()
    by_buyer: Counter[str] = Counter()
    by_event: Counter[str] = Counter()
    top_errors: Counter[str] = Counter()
    top_warnings: Counter[str] = Counter()
    risk_buckets: Counter[str] = Counter()
    field_status: dict[str, Counter[str]] = defaultdict(Counter)

    for item in results:
        by_state[str(item.get("state") or "(empty)")] += 1
        by_buyer[str(item.get("buyer_code") or "(empty)")] += 1
        by_event[str(item.get("event_type") or "(empty)")] += 1
        top_errors.update(item.get("errors") or [])
        top_warnings.update(item.get("warnings") or [])
        risk_buckets[risk_bucket(int(item.get("risk_score") or 0))] += 1
        for field, check in (item.get("field_audit") or {}).items():
            field_status[field][str(check.get("status") or "unknown")] += 1

    return {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "total_cases": len(results),
        "input": {
            "original_rows": original_rows,
            "processed_rows": processed_rows,
            "skipped_without_case": skipped_without_case,
            "original_json_errors": len(original_load_errors),
            "processed_json_errors": len(processed_load_errors),
        },
        "by_state": counter_dict(by_state),
        "by_buyer_code": counter_dict(by_buyer),
        "by_event_type": counter_dict(by_event),
        "top_errors": counter_dict(top_errors),
        "top_warnings": counter_dict(top_warnings),
        "field_evidence_status": {field: counter_dict(counts) for field, counts in field_status.items()},
        "risk_buckets": {
            bucket: int(risk_buckets.get(bucket, 0))
            for bucket in ("0-20", "21-50", "51-80", "81-100")
        },
    }


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(compact(cell).replace("|", "\\|") for cell in row) + " |")
    return "\n".join(lines)


def recommendations(summary: dict[str, Any]) -> list[str]:
    errors = summary.get("top_errors") or {}
    warnings = summary.get("top_warnings") or {}
    items: list[str] = []
    if any(key.startswith("document_number") for key in errors) or warnings.get("possible_claim_number_not_document_number"):
        items.append("Усилить извлечение document_number: принимать номер только рядом с УПД/накладной/реализацией и отделять от претензии/заявки.")
    if any(key.startswith("quantity") for key in errors) or warnings.get("quantity_without_context") or warnings.get("quantity_1_without_context"):
        items.append("Исправить quantity: хранить источник колонки/метки и не подтверждать одиночные числа без контекста количества.")
    if any(key.startswith("part_number") for key in errors) or warnings.get("part_number_without_product_context"):
        items.append("Усилить part_number: подтверждать артикул по подписи/таблице и отбрасывать даты, телефоны и короткие номера документов.")
    if warnings.get("claim_kind_conflict"):
        items.append("Пересмотреть приоритеты классификации claim_kind на явных словах отказ/брак/недовоз/пересорт/маркировка.")
    if errors.get("weak_followup_link"):
        items.append("Для linked_event сохранять причину связи и требовать message headers либо совпадение документа/претензии/артикула.")
    if not items:
        items.append("Сначала проверить самые рискованные кейсы и добавить контекстные ограничения для наиболее частого warning.")
    return items


def build_report(summary: dict[str, Any], results: list[dict[str, Any]]) -> str:
    total = int(summary.get("total_cases") or 0)
    buckets = summary.get("risk_buckets") or {}
    high = int(buckets.get("51-80", 0)) + int(buckets.get("81-100", 0))
    buyers = Counter()
    for item in results:
        if item.get("errors") or item.get("warnings"):
            buyers[str(item.get("buyer_code") or "(empty)")] += len(item.get("errors") or []) + len(item.get("warnings") or [])

    risky = sorted(results, key=lambda row: (-int(row.get("risk_score") or 0), str(row.get("case_id") or "")))[:20]
    lines = [
        "# Offline evidence audit",
        "",
        f"Сформирован: {summary.get('created_at')}",
        "",
        "## Общая картина",
        "",
        f"- Проверено кейсов: **{total}**.",
        f"- Высокий риск (51-100): **{high}**.",
        f"- JSON-ошибок во входе: original={summary['input']['original_json_errors']}, processed={summary['input']['processed_json_errors']}.",
        "",
        "### Риск",
        "",
        md_table(["Диапазон", "Кейсов"], [[k, v] for k, v in buckets.items()]),
        "",
        "## Топ поставщиков по ошибкам",
        "",
        md_table(["buyer_code", "Ошибок и предупреждений"], [[k, v] for k, v in buyers.most_common(15)]),
        "",
        "## Топ типов ошибок",
        "",
        md_table(["Ошибка", "Количество"], [[k, v] for k, v in list((summary.get("top_errors") or {}).items())[:20]]),
        "",
        "## Топ предупреждений",
        "",
        md_table(["Предупреждение", "Количество"], [[k, v] for k, v in list((summary.get("top_warnings") or {}).items())[:20]]),
        "",
        "## 20 самых рискованных кейсов",
        "",
        md_table(
            ["case_id", "raw_email_id", "buyer", "risk", "state", "errors", "warnings"],
            [
                [
                    item.get("case_id"),
                    item.get("raw_email_id"),
                    item.get("buyer_code"),
                    item.get("risk_score"),
                    item.get("state"),
                    ", ".join(item.get("errors") or [])[:240],
                    ", ".join(item.get("warnings") or [])[:240],
                ]
                for item in risky
            ],
        ),
        "",
        "## Что чинить первым",
        "",
    ]
    lines.extend(f"{idx}. {text}" for idx, text in enumerate(recommendations(summary), 1))
    lines.extend(
        [
            "",
            "## Статусы evidence",
            "",
            "Статусы показывают подтверждение значений исходным письмом, а не только заполненность JSON.",
            "",
        ]
    )
    for field, counts in (summary.get("field_evidence_status") or {}).items():
        lines.append(f"- `{field}`: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
    lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline audit of processed cases against original emails")
    parser.add_argument("--original", required=True, type=Path, help="Path to original_emails.jsonl")
    parser.add_argument("--processed", required=True, type=Path, help="Path to processed_cases.jsonl")
    parser.add_argument("--out-dir", required=True, type=Path, help="Directory for audit reports")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    for path in (args.original, args.processed):
        if not path.is_file():
            print(f"Input file not found: {path}", file=sys.stderr)
            return 2

    original_rows, original_errors = load_jsonl(args.original)
    processed_rows, processed_errors = load_jsonl(args.processed)
    originals: dict[str, dict[str, Any]] = {}
    for row in original_rows:
        key = raw_id(row)
        if key:
            originals[key] = row

    cases_to_audit = [case for case in processed_rows if case.get("has_case") is not False]
    skipped_without_case = len(processed_rows) - len(cases_to_audit)
    results = [audit_case(case, originals.get(raw_id(case))) for case in cases_to_audit]
    summary = build_summary(
        results,
        len(original_rows),
        len(processed_rows),
        skipped_without_case,
        original_errors,
        processed_errors,
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    audit_path = args.out_dir / "audit_cases.jsonl"
    with audit_path.open("w", encoding="utf-8") as handle:
        for item in results:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
    (args.out_dir / "audit_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (args.out_dir / "audit_report.md").write_text(
        build_report(summary, results),
        encoding="utf-8",
    )
    if original_errors or processed_errors:
        (args.out_dir / "input_errors.json").write_text(
            json.dumps(
                {"original": original_errors, "processed": processed_errors},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    print(f"Audited cases: {len(results)}")
    print(f"Report: {args.out_dir / 'audit_report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
