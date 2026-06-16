from __future__ import annotations

import html
import re
from copy import deepcopy
from datetime import date
from typing import Any


STRONG_DOCUMENT_CONTEXT = (
    "документ реализации",
    "номер документа",
    "счет-фактура",
    "счёт-фактура",
    "накладная",
    "реализация",
    "торг-12",
    "документ",
    "док.",
    "упд",
    "возврат",
)
WEAK_DATE_CONTEXT = (
    "претензия",
    "заявка",
    "обращение",
    "письмо",
    "отправлено",
    "получено",
    "сегодня",
    "вчера",
)
RU_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}
NUMERIC_DATE_RE = re.compile(
    r"(?<!\d)(?:(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})|(\d{4})-(\d{1,2})-(\d{1,2}))(?!\d)"
)
RU_DATE_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s+("
    + "|".join(RU_MONTHS)
    + r")\s+(\d{4})(?!\d)",
    re.IGNORECASE,
)


def _compact(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _strip_html(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return _compact(text)


def _source_segments(raw_email: dict[str, Any]) -> list[tuple[str, str]]:
    segments: list[tuple[str, str]] = []
    for source, key in (
        ("subject", "subject"),
        ("visible_text", "visible_text"),
        ("body", "body_text"),
    ):
        text = str(raw_email.get(key) or "")
        if text:
            segments.append((source, text))

    body_html = str(raw_email.get("body_html") or "")
    if body_html:
        for match in re.finditer(r"(?is)<tr\b[^>]*>(.*?)</tr>", body_html):
            row_text = _strip_html(match.group(1))
            if row_text:
                segments.append(("table", row_text))
        html_text = _strip_html(body_html)
        if html_text:
            segments.append(("html", html_text))
    return segments


def _canonical_date(day: int, month: int, year: int) -> str | None:
    if year < 100:
        year += 2000
    try:
        parsed = date(year, month, day)
    except ValueError:
        return None
    if parsed.year < 2000 or parsed.year > 2100:
        return None
    return parsed.strftime("%d.%m.%Y")


def _date_matches(text: str) -> list[tuple[str, int, int, str]]:
    matches: list[tuple[str, int, int, str]] = []
    for match in NUMERIC_DATE_RE.finditer(text):
        if match.group(1):
            day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        else:
            year, month, day = int(match.group(4)), int(match.group(5)), int(match.group(6))
        normalized = _canonical_date(day, month, year)
        if normalized:
            matches.append((normalized, match.start(), match.end(), match.group(0)))
    for match in RU_DATE_RE.finditer(text):
        normalized = _canonical_date(
            int(match.group(1)),
            RU_MONTHS[match.group(2).lower()],
            int(match.group(3)),
        )
        if normalized:
            matches.append((normalized, match.start(), match.end(), match.group(0)))
    return matches


def _snippet(text: str, start: int, end: int, radius: int = 110) -> str:
    return _compact(text[max(0, start - radius) : min(len(text), end + radius)])[:420]


def _score_occurrence(
    normalized_date: str,
    matched_text: str,
    snippet: str,
    source: str,
    document_number: Any,
) -> tuple[int, list[str]]:
    low = snippet.lower().replace("ё", "е")
    reasons: list[str] = []
    score = 0
    strong_hits = [word for word in STRONG_DOCUMENT_CONTEXT if word in low]
    weak_hits = [word for word in WEAK_DATE_CONTEXT if word in low]
    if strong_hits:
        score += 20 + min(8, len(strong_hits) * 2)
        reasons.append("strong_context:" + ",".join(strong_hits[:4]))
    if weak_hits:
        score -= min(18, len(weak_hits) * 5)
        reasons.append("weak_context:" + ",".join(weak_hits[:4]))
    if source == "subject":
        score += 3
        reasons.append("source:subject")
    elif source == "table":
        score += 5
        reasons.append("source:table")
    if document_number not in (None, ""):
        number = str(document_number).strip().lower()
        if number and number in low:
            score += 14
            reasons.append("document_number_nearby")
    if matched_text == normalized_date:
        score += 2
        reasons.append("exact_normalized_format")
    return score, reasons


def _document_date_candidates(case_data: dict[str, Any], raw_email: dict[str, Any]) -> list[dict[str, Any]]:
    document_number = (case_data.get("fields") or {}).get("document_number")
    by_date: dict[str, dict[str, Any]] = {}
    for source, text in _source_segments(raw_email):
        for normalized, start, end, matched_text in _date_matches(text):
            evidence = _snippet(text, start, end)
            score, reasons = _score_occurrence(
                normalized,
                matched_text,
                evidence,
                source,
                document_number,
            )
            candidate = {
                "value": normalized,
                "source": source,
                "status": "confirmed_exact" if matched_text == normalized else "confirmed_normalized",
                "evidence_snippet": evidence,
                "matched_text": matched_text,
                "score": score,
                "reasons": reasons,
            }
            previous = by_date.get(normalized)
            if previous is None or candidate["score"] > previous["score"]:
                by_date[normalized] = candidate
    return sorted(by_date.values(), key=lambda item: (-int(item["score"]), str(item["value"])))


def repair_evidence(
    case_data: dict[str, Any],
    raw_email: dict[str, Any],
    gate_result: dict[str, Any],
) -> dict[str, Any]:
    """Try deterministic evidence repairs and return a copied case.

    No database access and no AI calls are performed here.
    """
    repaired_case = deepcopy(case_data or {})
    field_statuses = gate_result.get("field_statuses") or {}
    blocking = list(gate_result.get("blocking_errors") or []) + list(gate_result.get("blocking_warnings") or [])
    document_date_blocked = (
        field_statuses.get("document_date") in {"not_found", "missing_processed", "weak_found"}
        or any(str(item).startswith("document_date:") for item in blocking)
    )
    result: dict[str, Any] = {
        "changed": False,
        "case_data": repaired_case,
        "repairs": [],
        "warnings": [],
        "candidates": {},
    }
    if not document_date_blocked:
        return result

    candidates = _document_date_candidates(repaired_case, raw_email or {})
    result["candidates"]["document_date"] = candidates
    if not candidates:
        result["warnings"].append("document_date_no_candidate")
        return result

    top_score = int(candidates[0]["score"])
    winners = [item for item in candidates if int(item["score"]) == top_score]
    if len(winners) != 1:
        result["warnings"].append("document_date_multiple_candidates")
        return result
    winner = winners[0]
    if top_score < 15 or not any(reason.startswith("strong_context:") for reason in winner["reasons"]):
        result["warnings"].append("document_date_no_strong_context")
        return result

    fields = dict(repaired_case.get("fields") or {})
    old_value = fields.get("document_date")
    fields["document_date"] = winner["value"]
    repaired_case["fields"] = fields

    repair = {
        "field": "document_date",
        "old_value": old_value,
        "new_value": winner["value"],
        "source": winner["source"],
        "status": winner["status"],
        "evidence_snippet": winner["evidence_snippet"],
        "repair_method": "document_date_context_search",
        "score": winner["score"],
        "reasons": winner["reasons"],
    }
    evidence_repairs = dict(repaired_case.get("_evidence_repairs") or {})
    evidence_repairs["document_date"] = dict(repair)
    repaired_case["_evidence_repairs"] = evidence_repairs
    repair["value_changed"] = old_value != winner["value"]
    result["changed"] = True
    result["repairs"].append(repair)
    return result
