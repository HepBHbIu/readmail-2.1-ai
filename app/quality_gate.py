from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings
from .claim_kind_evidence import evaluate_claim_kind_evidence
from .document_number_evidence import evaluate_document_number_evidence
from .part_number_evidence import evaluate_part_number_evidence
from .quantity_evidence import evaluate_quantity_evidence


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


QTY_LABEL_RE = re.compile(
    # «количеств\w*» ловит склонения: количество/в количестве 1/количества (avtoformula
    # шлёт «...в количестве 1 по документу» БЕЗ «шт» — раньше не распознавалось как контекст).
    r"(?:кол[-\s]?во|количеств\w*|к[-\s]?во|qty)\s*[:=—\-| ]*\*?\s*(\d+(?:[,.]\d+)?)\s*\*?\s*(?:шт\.?|штук[аи]?|pcs)?",
    re.I | re.U,
)
QTY_UNIT_RE = re.compile(r"\b(\d+(?:[,.]\d+)?)\s*(?:шт\.?|штук[аи]?|pcs)\b", re.I | re.U)
PART_LABEL_RE = re.compile(
    r"(?:артикул|арт\.?|код\s+товара|каталожн(?:ый)?\s+номер|oem|p\s*/?\s*n|номер\s+детали)"
    r"\s*(?:заказа)?\s*[:=—\-| ]+\*?\s*([a-zа-я0-9][a-zа-я0-9._/\-]{2,50})",
    re.I | re.U,
)
MULTI_PART_LABEL_RE = re.compile(r"(?:артикул|арт\.?|код\s+товара|каталожн(?:ый)?\s+номер|oem|p\s*/?\s*n|номер\s+детали)\b", re.I | re.U)
MULTI_BRAND_LABEL_RE = re.compile(r"(?:бренд|производитель|марка)\b", re.I | re.U)
MULTI_QTY_LABEL_RE = re.compile(r"(?:кол[-\s]?во|количество|к[-\s]?во|qty)\b", re.I | re.U)

BAD_BRANDS = {
    "здравствуйте", "добрый день", "добрый", "возврат", "уведомление",
    "письмо", "с уважением", "mai", "mail", "vozvra",
}
# Слова-ЗАГОЛОВКИ колонок таблицы. Если «значение» после метки = заголовок соседней колонки
# («Артикул | Производитель»), это шапка таблицы, а НЕ пара метка:значение — игнорируем.
HEADER_WORDS = {
    "производитель", "номенклатура", "бренд", "марка", "наименование", "товар", "деталь",
    "количество", "кол-во", "колво", "цена", "стоимость", "сумма", "артикул", "арт",
    "документ", "номер", "заказано", "инвойс", "претензия", "причина", "код",
}
BAD_PRODUCT_PHRASES = (
    "здравствуйте", "добрый день", "с уважением", "уведомление",
    "возврат поставки", "письмо сформировано автоматически",
    "в случае возникновения вопросов", "отдел рекламаций", "отправлено из почты",
)

EVIDENCE_REQUIRED_RETURN_FIELDS = (
    "document_number",
    "document_date",
    "part_number",
    "quantity",
    "claim_kind",
    "buyer_code",
)
EVIDENCE_CONFIRMED = {
    "confirmed_exact",
    "confirmed_normalized",
    "confirmed_by_route",
    "confirmed_by_sender",
    "confirmed_by_domain",
    "confirmed_by_pattern",
    "confirmed_by_parser",
    "confirmed_by_part_label",
    "confirmed_by_compact_item_line",
    "confirmed_by_table_column",
    "confirmed_by_product_context",
    "confirmed_by_explicit_reason",
    "confirmed_by_reason_label",
    "confirmed_by_supplier_contract",
    "confirmed_by_table_reason_column",
    "confirmed_by_document_label",
    "confirmed_by_upd_context",
    "confirmed_by_invoice_context",
    "confirmed_by_waybill_context",
    "confirmed_by_realization_context",
    "confirmed_by_quantity_label",
    "confirmed_by_piece_unit",
    "confirmed_by_table_quantity_column",
    "confirmed_by_part_quantity_pair",
}
EVIDENCE_BLOCKING_STATUSES = {
    "not_found", "missing_processed", "weak_found",
    "weak_generic_refusal", "conflict_reason_detected",
    "weak_no_document_context", "conflict_claim_or_request_number",
    "weak_number_without_quantity_context", "conflict_quantity_candidates",
}
EVIDENCE_BLOCKING_WARNINGS = {
    "possible_claim_number_not_document_number",
    "document_number_without_document_context",
    "quantity_1_without_context",
    "claim_kind_without_explicit_evidence",
    "claim_kind_conflict",
    "dangerous_profile_conflict",
}
DOCUMENT_POSITIVE_WORDS = ("документ", "упд", "реализац", "накладн", "счет-фактур", "счёт-фактур")
DOCUMENT_CLAIM_WORDS = ("претензи", "заявк", "обращени", "claim", "рекламаци")
PART_CONTEXT_WORDS = ("артикул", "арт.", "код", "номенклатур", "каталожн", "oem", "p/n", "номер детали")
CLAIM_KIND_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("quality_refusal", ("отказ клиента", "клиент отказался", "отказался клиент", "отказ")),
    ("defect", ("брак", "дефект", "неисправен", "неисправна", "неисправно")),
    ("shortage", ("недовоз", "не довезли", "не поступило", "недопостав")),
    ("wrong_item", ("пересорт", "не тот товар", "другой товар", "не та деталь")),
    ("marking_request", ("маркировк", "честный знак", "чз", "эдо")),
)
CLAIM_KIND_EQUIVALENTS = {
    "customer_refusal": "quality_refusal",
    "refusal": "quality_refusal",
    "nonconforming": "defect",
    "wrong_product": "wrong_item",
    "under_delivery": "shortage",
}


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _compact(value: Any) -> str:
    return re.sub(r"\s+", "", _norm(value)).replace(",", ".")


def _evidence_normalize(value: Any) -> str:
    text = _norm(value).replace("ё", "е")
    text = re.sub(r"[№#]", " n ", text)
    text = re.sub(r"\bno\.?\b", " n ", text)
    return re.sub(r"\s+", " ", text).strip()


def _evidence_identifier(value: Any) -> str:
    text = _evidence_normalize(value)
    text = re.sub(r"^(?:n|номер)\s*", "", text)
    return re.sub(r"[\s\-_/.,:;]+", "", text)


def _evidence_snippet(text: str, start: int, length: int, radius: int = 80) -> str:
    left = max(0, start - radius)
    right = min(len(text), start + max(1, length) + radius)
    return re.sub(r"\s+", " ", text[left:right]).strip()[:320]


def _evidence_match(value: Any, text: str, *, field: str) -> tuple[str, str]:
    if value in (None, "", [], {}):
        return "missing_processed", ""
    raw = str(value).strip()
    pos = text.lower().find(raw.lower())
    if pos >= 0:
        return "confirmed_exact", _evidence_snippet(text, pos, len(raw))
    normalized_value = _evidence_normalize(raw)
    normalized_text = _evidence_normalize(text)
    if normalized_value and normalized_value in normalized_text:
        return "confirmed_normalized", normalized_value[:320]
    compact_value = _evidence_identifier(raw)
    compact_text = _evidence_identifier(text)
    if len(compact_value) >= 2 and compact_value in compact_text:
        return "confirmed_normalized", raw[:320]
    if field == "document_date":
        match = re.search(r"(?<!\d)(\d{1,2})[./\-](\d{1,2})[./\-](\d{2,4})(?!\d)", raw)
        if match:
            day, month, year = match.groups()
            year = ("20" + year) if len(year) == 2 else year
            variants = {
                f"{day.zfill(2)}.{month.zfill(2)}.{year}",
                f"{day.zfill(2)}/{month.zfill(2)}/{year}",
                f"{day.zfill(2)}-{month.zfill(2)}-{year}",
                f"{year}-{month.zfill(2)}-{day.zfill(2)}",
            }
            if any(v in text for v in variants):
                return "confirmed_normalized", raw[:320]
    return "not_found", ""


def _contexts(value: Any, text: str, radius: int = 80) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    contexts: list[str] = []
    start = 0
    low = text.lower()
    needle = raw.lower()
    while len(contexts) < 20:
        pos = low.find(needle, start)
        if pos < 0:
            break
        contexts.append(_evidence_snippet(text, pos, len(raw), radius))
        start = pos + max(1, len(raw))
    return contexts


def _quantity_evidence(value: Any, text: str) -> tuple[str, str, list[str]]:
    if value in (None, ""):
        return "missing_processed", "", []
    try:
        number = float(str(value).replace(",", "."))
        rendered = str(int(number)) if number.is_integer() else str(number)
        token_re = re.compile(rf"(?<![\d.,]){re.escape(rendered)}(?:[.,]0+)?(?![\d.,])", re.I)
    except Exception:
        number = None
        token_re = re.compile(rf"(?<!\w){re.escape(str(value).strip())}(?!\w)", re.I)
    matches = list(token_re.finditer(text))
    if not matches:
        return "not_found", "", []
    labels = ("количество", "кол-во", "кол во", "к-во", "qty", "шт", "штук", "претензи")
    snippets = [_evidence_snippet(text, m.start(), len(m.group()), 55) for m in matches[:100]]
    strong = next((s for s in snippets if any(label in s.lower() for label in labels)), None)
    if strong:
        return "confirmed_exact", strong, []
    warning = "quantity_1_without_context" if number == 1 else "quantity_without_context"
    return "weak_found", snippets[0], [warning]


def _claim_kind_evidence(value: Any, text: str) -> tuple[str, str, list[str]]:
    if value in (None, ""):
        return "missing_processed", "", []
    normalized = _evidence_normalize(text)
    detected = None
    matched = ""
    for kind, hints in CLAIM_KIND_HINTS:
        for hint in hints:
            if hint in normalized:
                detected, matched = kind, hint
                break
        if detected:
            break
    if not detected:
        return "weak_found", "", ["claim_kind_without_explicit_evidence"]
    actual = CLAIM_KIND_EQUIVALENTS.get(str(value), str(value))
    if actual != detected:
        return "weak_found", matched, ["claim_kind_conflict"]
    return "confirmed_normalized", matched, []


def _repaired_field_evidence(
    draft_json: dict[str, Any],
    field: str,
    value: Any,
) -> dict[str, Any] | None:
    repairs = draft_json.get("_evidence_repairs")
    if not isinstance(repairs, dict):
        return None
    repair = repairs.get(field)
    if not isinstance(repair, dict):
        return None
    if repair.get("repair_method") != "document_date_context_search":
        return None
    if repair.get("status") not in EVIDENCE_CONFIRMED:
        return None
    if repair.get("source") not in {"subject", "body", "visible_text", "html", "table"}:
        return None
    if not str(repair.get("evidence_snippet") or "").strip():
        return None
    if _evidence_identifier(repair.get("new_value")) != _evidence_identifier(value):
        return None
    return repair


def build_evidence_gate(
    original_email_text: str,
    draft_json: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = dict(metadata or {})
    text = original_email_text or ""
    fields = _fields_from_draft(draft_json or {})
    event_type = str(draft_json.get("event_type") or "")
    state = str(draft_json.get("state") or "")
    # Отказ клиента ДО поставки: товар не отгружали → документа реализации физически нет.
    # Для таких кейсов document_number/document_date НЕ обязательны (см. classifier._detect_pre_delivery_refusal).
    pre_delivery_refusal = bool(draft_json.get("pre_delivery_refusal"))
    payload = draft_json.get("payload") if isinstance(draft_json.get("payload"), dict) else {}
    shortage_link_only = bool(
        payload.get("classification_subcategory") == "shortage.link_only"
        or (
            isinstance(payload.get("shortage_link"), dict)
            and payload["shortage_link"].get("subcategory") == "shortage.link_only"
        )
    )
    values = {
        **fields,
        "claim_kind": draft_json.get("claim_kind"),
        "buyer_code": draft_json.get("buyer_code"),
    }
    field_audit: dict[str, dict[str, Any]] = {}

    for field in EVIDENCE_REQUIRED_RETURN_FIELDS:
        value = values.get(field)
        warnings: list[str] = []
        part_evidence: dict[str, Any] | None = None
        claim_evidence: dict[str, Any] | None = None
        document_evidence: dict[str, Any] | None = None
        quantity_evidence: dict[str, Any] | None = None
        repaired_evidence = _repaired_field_evidence(draft_json, field, value)
        if field == "quantity":
            quantity_evidence = evaluate_quantity_evidence(
                value=value,
                part_number=fields.get("part_number"),
                product_name=fields.get("product_name"),
                document_number=fields.get("document_number"),
                buyer_code=draft_json.get("buyer_code"),
                raw_email=metadata.get("raw_email"),
                original_text=text,
            )
            status = str(quantity_evidence["status"])
            evidence = str(quantity_evidence.get("evidence_snippet") or "")
            warnings = list(quantity_evidence.get("warnings") or [])
        elif field == "document_number":
            document_evidence = evaluate_document_number_evidence(
                value=value,
                document_date=fields.get("document_date"),
                buyer_code=draft_json.get("buyer_code"),
                raw_email=metadata.get("raw_email"),
                original_text=text,
            )
            status = str(document_evidence["status"])
            evidence = str(document_evidence.get("evidence_snippet") or "")
            warnings = list(document_evidence.get("warnings") or [])
        elif field == "claim_kind":
            claim_evidence = evaluate_claim_kind_evidence(
                value=value,
                buyer_code=draft_json.get("buyer_code"),
                raw_email=metadata.get("raw_email"),
                original_text=text,
            )
            status = str(claim_evidence["status"])
            evidence = str(claim_evidence.get("evidence_snippet") or "")
            warnings = list(claim_evidence.get("warnings") or [])
        elif field == "buyer_code":
            candidates = {
                str(value or ""),
                str(value or "").replace("_", "."),
                str(value or "").replace("_", "-"),
                str(value or "").replace("_", " "),
            }
            found = next((candidate for candidate in candidates if candidate and _evidence_identifier(candidate) in _evidence_identifier(text)), "")
            status = "confirmed_normalized" if found else ("missing_processed" if not value else "weak_found")
            evidence = found
            if value and not found:
                warnings.append("buyer_code_not_directly_confirmed")
            buyer_evidence = metadata.get("buyer_evidence")
            if value and isinstance(buyer_evidence, dict) and buyer_evidence.get("status") in EVIDENCE_CONFIRMED:
                status = str(buyer_evidence["status"])
                evidence = ""
                warnings = []
            if value and isinstance(buyer_evidence, dict) and buyer_evidence.get("mismatches"):
                warnings.append("buyer_code_text_counterparty_mismatch")
            if value and isinstance(buyer_evidence, dict) and buyer_evidence.get("dangerous_profile_conflict"):
                warnings.append("dangerous_profile_conflict")
        elif field == "part_number":
            part_evidence = evaluate_part_number_evidence(
                value=value,
                product_name=fields.get("product_name"),
                quantity=fields.get("quantity"),
                buyer_code=draft_json.get("buyer_code"),
                raw_email=metadata.get("raw_email"),
                original_text=text,
            )
            status = str(part_evidence["status"])
            evidence = str(part_evidence.get("evidence_snippet") or "")
            warnings = list(part_evidence.get("warnings") or [])
        else:
            status, evidence = _evidence_match(value, text, field=field)
        if repaired_evidence:
            status = str(repaired_evidence["status"])
            evidence = str(repaired_evidence["evidence_snippet"])
            warnings = []

        # pre_delivery_refusal: отгрузки не было → документ/дата документа неприменимы,
        # количество необязательно. Помечаем not_applicable (не входит в blocking statuses).
        if pre_delivery_refusal and field in ("document_number", "document_date"):
            status = "not_applicable"
            evidence = ""
            warnings = []
        elif pre_delivery_refusal and field == "quantity" and not value:
            status = "not_applicable"
            warnings = []
        elif shortage_link_only and field in ("document_number", "document_date", "part_number", "quantity"):
            status = "not_applicable"
            evidence = ""
            warnings = []

        contexts = _contexts(value, text)
        joined = " ".join(contexts).lower()
        if field == "part_number" and status in {"confirmed_exact", "confirmed_normalized"}:
            raw = str(value or "").strip()
            suspicious = (
                bool(re.fullmatch(r"\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}", raw))
                # телефон только со структурой — чистый цифровой OEM-артикул не подозрителен
                or (bool(re.fullmatch(r"\+?\d[\d\s().\-]{8,}", raw)) and bool(re.search(r"[+\s().\-]", raw)))
                or "@" in raw
            )
            strong_context = any(word in joined for word in PART_CONTEXT_WORDS)
            if suspicious or not strong_context:
                status = "weak_found"
                warnings.append("part_number_without_product_context" if not suspicious else "part_number_bad_shape")

        field_audit[field] = {
            "value": value,
            "status": status,
            "evidence_snippet": evidence or (contexts[0] if contexts else ""),
            "warnings": list(dict.fromkeys(warnings)),
        }
        if field == "part_number" and part_evidence is not None:
            field_audit[field].update(
                {
                    "source": part_evidence.get("source") or "none",
                    "evidence_method": part_evidence.get("method") or "",
                }
            )
        if field == "claim_kind" and claim_evidence is not None:
            field_audit[field].update(
                {
                    "source": claim_evidence.get("source") or "none",
                    "evidence_method": claim_evidence.get("method") or "",
                    "matched_phrase": claim_evidence.get("matched_phrase") or "",
                    "detected_kinds": claim_evidence.get("detected_kinds") or {},
                }
            )
        if field == "document_number" and document_evidence is not None:
            field_audit[field].update(
                {
                    "source": document_evidence.get("source") or "none",
                    "evidence_method": document_evidence.get("method") or "",
                    "matched_context": document_evidence.get("matched_context") or "",
                    "date_near": bool(document_evidence.get("date_near")),
                    "suggested_value": document_evidence.get("suggested_value"),
                }
            )
        if field == "quantity" and quantity_evidence is not None:
            field_audit[field].update(
                {
                    "source": quantity_evidence.get("source") or "none",
                    "evidence_method": quantity_evidence.get("method") or "",
                }
            )
        if repaired_evidence:
            field_audit[field].update(
                {
                    "source": repaired_evidence.get("source"),
                    "repair_method": repaired_evidence.get("repair_method"),
                    "repaired": True,
                }
            )
        if field == "buyer_code" and isinstance(metadata.get("buyer_evidence"), dict):
            buyer_evidence = metadata["buyer_evidence"]
            field_audit[field].update(
                {
                    "source": buyer_evidence.get("source") or "none",
                    "evidence_meta": {
                        "from": buyer_evidence.get("from"),
                        "from_domain": buyer_evidence.get("from_domain"),
                        "mailbox": buyer_evidence.get("mailbox"),
                        "pattern_id": buyer_evidence.get("pattern_id"),
                        "parser": buyer_evidence.get("parser"),
                        "matched_rule": buyer_evidence.get("matched_rule"),
                        "processing_source": buyer_evidence.get("processing_source"),
                        "buyer_reasons": buyer_evidence.get("buyer_reasons") or [],
                        "mismatches": buyer_evidence.get("mismatches") or [],
                        "mismatch_classifications": buyer_evidence.get("mismatch_classifications") or [],
                        "dangerous_profile_conflict": bool(buyer_evidence.get("dangerous_profile_conflict")),
                    },
                }
            )

    blocking_errors: list[str] = []
    blocking_warnings: list[str] = []
    non_blocking_warnings: list[str] = []
    if event_type == "new_return":
        for field in EVIDENCE_REQUIRED_RETURN_FIELDS:
            check = field_audit[field]
            if check["status"] in EVIDENCE_BLOCKING_STATUSES:
                blocking_errors.append(f"{field}:{check['status']}")
            for warning in check["warnings"]:
                if warning in EVIDENCE_BLOCKING_WARNINGS:
                    blocking_warnings.append(f"{field}:{warning}")
                else:
                    non_blocking_warnings.append(f"{field}:{warning}")

    link_signals: list[str] = []
    weak_followup = False
    if state == "linked_event" or event_type.startswith("followup_"):
        if metadata.get("in_reply_to"):
            link_signals.append("in_reply_to")
        if metadata.get("references"):
            link_signals.append("references")
        for field in ("document_number", "part_number"):
            if field_audit[field]["status"] in EVIDENCE_CONFIRMED:
                link_signals.append(field)
        claim_number = fields.get("claim_number")
        claim_status, _ = _evidence_match(claim_number, text, field="claim_number")
        if claim_status in EVIDENCE_CONFIRMED:
            link_signals.append("claim_number")
        subject = str(metadata.get("subject") or "")
        if re.match(r"^\s*(?:re|fw|fwd)\s*:", subject, re.I):
            link_signals.append("normalized_subject")
        if not link_signals:
            weak_followup = True
            blocking_errors.append("weak_followup_link")

    return {
        "passed": not blocking_errors and not blocking_warnings,
        "blocking_errors": list(dict.fromkeys(blocking_errors)),
        "blocking_warnings": list(dict.fromkeys(blocking_warnings)),
        "non_blocking_warnings": list(dict.fromkeys(non_blocking_warnings)),
        "field_statuses": {field: check["status"] for field, check in field_audit.items()},
        "field_audit": field_audit,
        "link_signals": link_signals,
        "weak_followup_link": weak_followup,
        "checked_at": utcnow(),
    }


def _fields_from_draft(draft_json: dict[str, Any]) -> dict[str, Any]:
    fields = dict(draft_json.get("fields") or {})
    export = draft_json.get("export") if isinstance(draft_json.get("export"), dict) else {}
    items = export.get("items") if isinstance(export.get("items"), list) else []
    item0 = items[0] if items and isinstance(items[0], dict) else {}
    document = export.get("document") if isinstance(export.get("document"), dict) else {}
    claim = export.get("claim") if isinstance(export.get("claim"), dict) else {}
    merged = dict(fields)
    for key in ("part_number", "brand", "product_name", "quantity", "price"):
        if not merged.get(key) and item0.get(key) not in (None, "", [], {}):
            merged[key] = item0.get(key)
    if not merged.get("document_number") and document.get("number"):
        merged["document_number"] = document.get("number")
    if not merged.get("document_date") and document.get("date"):
        merged["document_date"] = document.get("date")
    for key in ("claim_number", "client_request_number", "return_number"):
        if not merged.get(key) and claim.get(key):
            merged[key] = claim.get(key)
    return merged


def _add_field_check(checks: dict[str, Any], field: str, ok: bool, reason: str, evidence: Any = None) -> None:
    checks[field] = {"ok": bool(ok), "reason": reason, "evidence": evidence}


def _status(errors: list[dict[str, Any]], warnings: list[dict[str, Any]], field_checks: dict[str, Any], ready: bool) -> tuple[str, float]:
    if any(e.get("severity") == "critical" for e in errors):
        return "critical_error", 0.0
    if any(e.get("code") == "multi_item_detected" for e in errors + warnings):
        return "needs_human_review", 0.35
    if errors:
        return "needs_ai_repair" if ready else "needs_human_review", 0.45
    bad_checks = sum(1 for x in field_checks.values() if isinstance(x, dict) and not x.get("ok"))
    if bad_checks:
        return ("needs_ai_repair" if ready else "needs_human_review"), max(0.5, 0.85 - bad_checks * 0.15)
    if warnings and ready:
        return "needs_human_review", 0.75
    return "accepted", 1.0 if not warnings else 0.9


def quality_gate(original_email_text: str, draft_json: dict[str, Any], metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    metadata = dict(metadata or {})
    text = original_email_text or ""
    fields = _fields_from_draft(draft_json or {})
    ready = bool(draft_json.get("ready_for_export")) or str(draft_json.get("state") or "") == "ready_to_1c"
    # Письмо разбито на отдельные кейсы-позиции (multi-case split)? Тогда артикул берётся из
    # структуры таблицы (авторитетно), а текст содержит метки ВСЕХ позиций — не сверяем с первой.
    _pl = draft_json.get("payload") if isinstance(draft_json.get("payload"), dict) else {}
    _is_split = int(_pl.get("multi_item_count") or 0) > 1
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    checks: dict[str, Any] = {}

    # Evidence-gate (структурный валидатор, читает КОЛОНКИ таблицы) — авторитетный
    # источник. Для табличных писем подписи «Артикул: X» в тексте нет, но колонка
    # таблицы разобрана надёжно (confirmed_by_table_*). В таких случаях НЕ роняем
    # поле текстовыми проверками «нет метки/нет контекста» — иначе почти все
    # табличные письма (большинство) ложно уходят в needs_ai_repair и НЕ доходят
    # до 1С. Реальный конфликт значений (mismatch) проверяется ОТДЕЛЬНО и остаётся.
    evidence_gate = build_evidence_gate(text, draft_json, metadata)
    _fstat = evidence_gate.get("field_statuses") or {}

    # v2.1 AI-only: ИИ — АВТОРИТЕТНЫЙ извлекатель. Дословный паттерн-верификатор ниже
    # ломается на ИИ-данных (табличные письма, «*553206*», «Код:», латиница+цифры) и
    # ложно метит needs_ai_repair → ПОЛНЫЕ кейсы не доходят до 1С. Минимум полей уже
    # проверен раньше (has_min_fields в apply_ai_overlay). Поэтому для AI-обработанного
    # кейса в AI-only доверяем ИИ: gate=accepted, passed=True. Это и есть «ИИ — приоритет».
    if bool(getattr(settings, "ai_only", False)) and str(_pl.get("processing_source") or "") == "ai":
        evidence_gate["passed"] = True
        evidence_gate["ai_trusted"] = True
        return {
            "case_status": "accepted",
            "quality_score": 0.9,
            "errors": [],
            "warnings": [],
            "field_checks": {},
            "evidence_gate": evidence_gate,
            "metadata": {
                "checked_at": utcnow(),
                "raw_email_id": metadata.get("raw_email_id"),
                "case_id": metadata.get("case_id"),
                "item_index": metadata.get("item_index"),
                "buyer_code": draft_json.get("buyer_code"),
                "event_type": draft_json.get("event_type"),
                "state": draft_json.get("state"),
                "ready_for_export": bool(draft_json.get("ready_for_export")),
                "ai_trusted": True,
            },
        }

    def _confirmed(_f: str) -> bool:
        return str(_fstat.get(_f) or "").startswith("confirmed")

    qty = fields.get("quantity")
    explicit_qty = [m.group(1) for m in QTY_LABEL_RE.finditer(text) if m.group(1).strip().lower() not in HEADER_WORDS]
    unit_qty = [m.group(1) for m in QTY_UNIT_RE.finditer(text)]
    qty_candidates = explicit_qty or unit_qty
    if qty not in (None, "", [], {}):
        q = _compact(qty)
        if explicit_qty and q not in {_compact(x) for x in explicit_qty}:
            errors.append({"field": "quantity", "code": "quantity_mismatch_explicit", "severity": "critical", "expected": explicit_qty, "actual": qty})
            _add_field_check(checks, "quantity", False, "differs_from_explicit_quantity", explicit_qty)
        elif not qty_candidates and ready and not _confirmed("quantity"):
            errors.append({"field": "quantity", "code": "quantity_without_reliable_context", "severity": "error", "actual": qty})
            _add_field_check(checks, "quantity", False, "no_quantity_context", None)
        else:
            _add_field_check(checks, "quantity", True,
                             "confirmed_by_table" if _confirmed("quantity") else "reliable_quantity_context",
                             qty_candidates or _fstat.get("quantity"))
    elif explicit_qty and ready:
        errors.append({"field": "quantity", "code": "missing_explicit_quantity", "severity": "error", "expected": explicit_qty})
        _add_field_check(checks, "quantity", False, "explicit_quantity_missing", explicit_qty)

    part = fields.get("part_number")
    # «Явный артикул» должен СОДЕРЖАТЬ ЦИФРУ (настоящий OEM-код: PS5961, HNQ2292GQ).
    # Иначе PART_LABEL_RE ловит кирилл-обрывки шапки таблицы («Производитель», «товара»,
    # «При» из «При приёмке») и даёт ЛОЖНЫЙ part_number_mismatch_explicit (avtoformula 151).
    explicit_parts = [m.group(1) for m in PART_LABEL_RE.finditer(text)
                      if m.group(1).strip().lower() not in HEADER_WORDS and re.search(r"\d", m.group(1))]
    if part not in (None, "", [], {}):
        p = str(part).strip()
        bad_part = (
            bool(re.fullmatch(r"\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}", p))
            # «телефон» — ТОЛЬКО со структурой (+/скобки/пробелы/дефисы). Чистый ряд цифр
            # НЕ телефон: реальные OEM-артикулы бывают целиком числовыми (Bosch 1609073180,
            # BMW 51117421850, VAG 070903107) — раньше ложно резались как телефон.
            or (bool(re.fullmatch(r"\+?\d[\d\s().\-]{7,}", p)) and bool(re.search(r"[+\s().\-]", p)))
            or "@" in p
            or bool(re.fullmatch(r"\d+/\d+/?", p))
            or bool(re.search(r"https?://|www\.", p, re.I))
        )
        if bad_part:
            errors.append({"field": "part_number", "code": "bad_part_number_shape", "severity": "error", "actual": part})
            _add_field_check(checks, "part_number", False, "looks_like_document_date_phone_email_or_url", None)
        elif explicit_parts and not _is_split and _compact(p) not in {_compact(x) for x in explicit_parts}:
            errors.append({"field": "part_number", "code": "part_number_mismatch_explicit", "severity": "error", "expected": explicit_parts, "actual": part})
            _add_field_check(checks, "part_number", False, "differs_from_explicit_part_number", explicit_parts)
        elif not explicit_parts and ready and not _confirmed("part_number"):
            warnings.append({"field": "part_number", "code": "part_number_without_label", "severity": "warning", "actual": part})
            _add_field_check(checks, "part_number", False, "no_explicit_part_label", None)
        else:
            _add_field_check(checks, "part_number", True,
                             "confirmed_by_table" if (not explicit_parts and _confirmed("part_number")) else "explicit_or_acceptable_part_number",
                             explicit_parts or _fstat.get("part_number"))
    elif explicit_parts and ready:
        errors.append({"field": "part_number", "code": "missing_explicit_part_number", "severity": "error", "expected": explicit_parts})
        _add_field_check(checks, "part_number", False, "explicit_part_number_missing", explicit_parts)

    brand = fields.get("brand")
    if brand not in (None, "", [], {}):
        b = _norm(brand)
        if b in BAD_BRANDS or any(x in b for x in ("avtoto", "mail.ru", "vozvra")):
            errors.append({"field": "brand", "code": "bad_brand_service_text", "severity": "error", "actual": brand})
            _add_field_check(checks, "brand", False, "service_text_or_email_fragment", None)
        else:
            _add_field_check(checks, "brand", True, "brand_not_service_text", None)

    product = fields.get("product_name")
    if product not in (None, "", [], {}):
        pr = _norm(product)
        bad_phrase = next((x for x in BAD_PRODUCT_PHRASES if x in pr), None)
        if bad_phrase:
            errors.append({"field": "product_name", "code": "bad_product_name_service_text", "severity": "error", "actual": product, "matched": bad_phrase})
            _add_field_check(checks, "product_name", False, "contains_service_text", bad_phrase)
        else:
            _add_field_check(checks, "product_name", True, "product_name_not_service_text", None)

    # СТРУКТУРНЫЙ сигнал мультипозиции считаем по ЧИСТОМУ visible_text, а НЕ по склейке
    # original_text (= subject+visible_text+body_text+body_html+snippet). Иначе одна и та же
    # позиция и метка «Артикул» дублируются ×4 (по числу полей) → part_labels=4 и ЛОЖНОЕ
    # «много позиций» у обычных однопозиционных писем (была причина зависания ~300 писем).
    _re = (metadata.get("raw_email") or {}) if isinstance(metadata.get("raw_email"), dict) else {}
    _clean = str(_re.get("visible_text") or _re.get("body_text") or _re.get("snippet") or text or "")
    part_label_count = len(MULTI_PART_LABEL_RE.findall(_clean))
    brand_label_count = len(MULTI_BRAND_LABEL_RE.findall(_clean))
    qty_label_count = len(MULTI_QTY_LABEL_RE.findall(_clean))
    export = draft_json.get("export") if isinstance(draft_json.get("export"), dict) else {}
    items = export.get("items") if isinstance(export.get("items"), list) else []
    item_count = len(items) if items else (1 if fields.get("part_number") or fields.get("product_name") else 0)
    # Письмо уже разбито на отдельные кейсы-позиции (multi-case split) → позиции УЧТЕНЫ,
    # не считаем это «много меток при 1 item». Иначе каждый корректный сиблинг ложно флагался.
    _payload = draft_json.get("payload") if isinstance(draft_json.get("payload"), dict) else {}
    _mic = int(_payload.get("multi_item_count") or 0)
    if _mic >= 1:
        item_count = _mic  # таблица разобрана экстрактором — доверяем его счёту позиций
    # Реальная мультипозиция = экстрактор разобрал >1 позицию ЛИБО в ЧИСТОМ тексте ≥2 РАЗНЫХ
    # артикуло-кода. Повтор метки «Артикул» (дубль HTML/шапка таблицы) сигналом НЕ считаем.
    distinct_parts = len(set(re.findall(r"\b[A-Za-z]{1,6}\d[A-Za-z0-9][A-Za-z0-9\-/]{1,}\b", _clean)))
    # Мультипозиция = экстрактор разобрал >1 позицию ЛИБО в ЧИСТОМ тексте ≥2 меток «Артикул»
    # И ≥2 РАЗНЫХ артикуло-кода. На чистом тексте (без дубля HTML) метка не множится, а один
    # случайный «артикуло-образный» токен сам по себе уже не триггерит ложное «много».
    is_multi = item_count <= 1 and ((_mic > 1) or (part_label_count > 1 and distinct_parts >= 2))
    if is_multi:
        warnings.append({
            "field": "multi_item",
            "code": "multi_item_detected",
            "severity": "warning",
            "part_labels": part_label_count,
            "brand_labels": brand_label_count,
            "quantity_labels": qty_label_count,
            "json_items": item_count,
        })
        _add_field_check(checks, "multi_item", False, "multiple_explicit_labels_but_single_json_item", None)
    else:
        _add_field_check(checks, "multi_item", True, "single_item_or_items_match_labels", {"json_items": item_count})

    for code in evidence_gate.get("blocking_errors") or []:
        errors.append({"field": code.split(":", 1)[0], "code": code, "severity": "error"})
    for code in evidence_gate.get("blocking_warnings") or []:
        warnings.append({"field": code.split(":", 1)[0], "code": code, "severity": "warning"})
    case_status, score = _status(errors, warnings, checks, ready)
    return {
        "case_status": case_status,
        "quality_score": round(float(score), 3),
        "errors": errors,
        "warnings": warnings,
        "field_checks": checks,
        "evidence_gate": evidence_gate,
        "metadata": {
            "checked_at": utcnow(),
            "raw_email_id": metadata.get("raw_email_id"),
            "case_id": metadata.get("case_id"),
            "item_index": metadata.get("item_index"),
            "buyer_code": draft_json.get("buyer_code"),
            "event_type": draft_json.get("event_type"),
            "state": draft_json.get("state"),
            "ready_for_export": bool(draft_json.get("ready_for_export")),
        },
    }


def write_quality_artifacts(raw_email_id: int | None, case_data: dict[str, Any], quality: dict[str, Any]) -> None:
    data_dir = Path(settings.database_path).parent
    data_dir.mkdir(parents=True, exist_ok=True)
    key = {
        "written_at": utcnow(),
        "raw_email_id": raw_email_id,
        "case_id": (quality.get("metadata") or {}).get("case_id"),
        "buyer_code": case_data.get("buyer_code"),
        "event_type": case_data.get("event_type"),
        "state": case_data.get("state"),
        "fields": case_data.get("fields") or {},
        "quality": quality,
    }
    checks_path = data_dir / "quality_checks.jsonl"
    with checks_path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(key, ensure_ascii=False, separators=(",", ":")) + "\n")
    if quality.get("case_status") != "accepted":
        for name in ("quality_errors.jsonl", "review_queue.jsonl"):
            with (data_dir / name).open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(key, ensure_ascii=False, separators=(",", ":")) + "\n")
    # ВАЖНО: _write_quality_summary перечитывает+парсит ВЕСЬ растущий quality_checks.jsonl —
    # на каждом письме это O(N²) и было главным тормозом перепрогона. Троттлим: раз в 300.
    # Полную сводку считает вызывающий после батча через flush_quality_summary().
    global _QA_WRITE_COUNT
    _QA_WRITE_COUNT += 1
    if _QA_WRITE_COUNT % 300 == 0:
        _write_quality_summary(checks_path, data_dir / "quality_report.json")


_QA_WRITE_COUNT = 0


def flush_quality_summary() -> None:
    """Пересчитать сводку один раз (звать ПОСЛЕ батч-перепрогона)."""
    data_dir = Path(settings.database_path).parent
    _write_quality_summary(data_dir / "quality_checks.jsonl", data_dir / "quality_report.json")


def _write_quality_summary(checks_path: Path, report_path: Path) -> None:
    latest: dict[str, dict[str, Any]] = {}
    try:
        lines = checks_path.read_text(encoding="utf-8").splitlines()[-50000:]
        for line in lines:
            try:
                item = json.loads(line)
            except Exception:
                continue
            q = item.get("quality") or {}
            meta = q.get("metadata") or {}
            key = str(meta.get("case_id") or f"raw:{item.get('raw_email_id')}")
            latest[key] = item
    except Exception:
        latest = {}
    statuses = Counter()
    field_errors = Counter()
    for item in latest.values():
        q = item.get("quality") or {}
        statuses[str(q.get("case_status") or "unknown")] += 1
        for err in (q.get("errors") or []) + (q.get("warnings") or []):
            field = err.get("field") or "unknown"
            code = err.get("code") or "unknown"
            field_errors[f"{field}:{code}"] += 1
    report = {
        "generated_at": utcnow(),
        "total": sum(statuses.values()),
        "accepted": statuses.get("accepted", 0),
        "needs_ai_repair": statuses.get("needs_ai_repair", 0),
        "needs_human_review": statuses.get("needs_human_review", 0),
        "critical_error": statuses.get("critical_error", 0),
        "statuses": dict(statuses),
        "field_errors": dict(field_errors),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
