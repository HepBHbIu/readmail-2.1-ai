from __future__ import annotations

import re
import json
from email.utils import parseaddr
from typing import Any


REPORT_RE = re.compile(
    r"(?i)\b(ежедневн\w*\s+отч[её]т|отч[её]т|реестр|остатк\w*|прайс(?:-лист)?|"
    r"price[\s-]*list|наличи\w*|stock|выгрузк\w*|сводк\w*|sales report|stock report)\b"
)
RETURN_RE = re.compile(
    r"(?i)\b(возврат\w*|претензи\w*|рекламаци\w*|отказ\w*|брак\w*|дефект\w*|"
    r"недовоз\w*|недопостав\w*|пересорт\w*|некондици\w*|неисправ\w*)\b"
)
MARKING_RE = re.compile(r"(?i)\b(честн\w*\s+знак|маркировк\w*|эдо|чз)\b")
CORRECTION_RE = re.compile(
    r"(?i)\b(укд|ксф|корректировочн\w*\s+(?:сч[её]т-фактур\w*|документ\w*)|корректировк\w*)\b"
)
INFO_RE = re.compile(
    r"(?i)(готов\w*\s+к\s+выдаче|товар\s+находится\s+на\s+складе|информационн\w*\s+письм\w*|"
    r"уведомляем|для\s+сведени\w*)"
)
NOISE_RE = re.compile(
    r"(?i)(письмо\s+сформировано\s+автоматически|не\s+отвечайте\s+на\s+это\s+письмо|"
    r"mailer-daemon|delivery status notification|автоматическ\w*\s+рассылк\w*|unsubscribe)"
)
PRODUCT_RE = re.compile(
    r"(?i)\b(арт\.?|артикул|код\s+товара|номенклатур\w*|part\s*number|sku|oem|кол-во|количество|шт\.?)\b"
)
DOCUMENT_RE = re.compile(
    r"(?i)\b(упд|накладн\w*|торг-?12|сч[её]т-фактур\w*|документ\s*(?:реализации)?\s*[№#N]?)\b"
)
REPLY_RE = re.compile(r"(?i)^\s*(re|fw|fwd|ответ|пересл)\s*:")
REPORT_EXTENSIONS = (".xlsx", ".xls", ".csv", ".ods")


def sender_domain(from_addr: Any) -> str:
    address = parseaddr(str(from_addr or ""))[1].lower()
    return address.rsplit("@", 1)[-1] if "@" in address else ""


def _text(email: dict[str, Any]) -> str:
    return "\n".join(
        str(email.get(key) or "")
        for key in ("subject", "visible_text", "body_text", "body_html", "snippet")
    )


def _attachments(email: dict[str, Any]) -> list[dict[str, Any]]:
    value = email.get("attachments")
    return value if isinstance(value, list) else []


def _has_references(email: dict[str, Any]) -> bool:
    value = email.get("references")
    if isinstance(value, (list, tuple, set)):
        return bool(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return bool(parsed) if isinstance(parsed, list) else bool(str(parsed).strip())
        except json.JSONDecodeError:
            return value.strip() not in {"[]", "null", "None"}
    value = email.get("references_json")
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return bool(parsed) if isinstance(parsed, list) else bool(str(parsed).strip())
        except json.JSONDecodeError:
            return value.strip() not in {"[]", "null", "None"}
    return bool(value)


def classify_inbox(email: dict[str, Any]) -> dict[str, Any]:
    text = _text(email)
    subject = str(email.get("subject") or "")
    visible_top = str(email.get("visible_text") or email.get("body_text") or "")[:1200]
    current_message_text = f"{subject}\n{visible_top}"
    mailbox = str(email.get("mailbox") or "")
    attachments = _attachments(email)
    names = [str(item.get("filename") or "") for item in attachments if isinstance(item, dict)]
    has_report_attachment = any(name.lower().endswith(REPORT_EXTENSIONS) for name in names)
    has_named_report_attachment = any(
        name.lower().endswith(REPORT_EXTENSIONS)
        and re.search(r"(?i)(прайс|price|остатк|stock|наличи|report|отч[её]т)", name)
        for name in names
    )
    has_product_evidence = bool(PRODUCT_RE.search(text))
    has_document_evidence = bool(DOCUMENT_RE.search(text))
    reasons: list[str] = []
    rules: list[str] = []

    def result(bucket: str, confidence: int, next_action: str) -> dict[str, Any]:
        return {
            "raw_email_id": email.get("id") or email.get("raw_email_id"),
            "mailbox": mailbox,
            "sender": email.get("from_addr") or "",
            "sender_domain": sender_domain(email.get("from_addr")),
            "subject": subject,
            "inbox_bucket": bucket,
            "confidence": confidence,
            "reasons": reasons,
            "matched_rules": rules,
            "next_action": next_action,
        }

    if email.get("duplicate_of_raw_email_id") or str(email.get("status") or "").lower() == "duplicate":
        reasons.append("Письмо сохранено как связанный semantic duplicate")
        rules.append("duplicate_link")
        return result("duplicate_or_linked", 100, "link_followup")

    status = str(email.get("status") or "").lower()
    if status in {"error", "failed", "quarantined", "import_error"} or email.get("import_error"):
        reasons.append(f"Статус импорта: {status or 'error'}")
        rules.append("import_status_error")
        return result("import_error", 100, "human_review")

    report_folder = bool(re.search(r"(?i)(reports?|supplierreports?|отч[её]т)", mailbox))
    if (
        (REPORT_RE.search(current_message_text) and has_report_attachment)
        or has_named_report_attachment
        or report_folder
    ):
        reasons.append("Отчётная тема/папка и табличное вложение" if has_report_attachment else "Папка отчётов")
        rules.extend([
            "supplier_report_text",
            "named_report_attachment" if has_named_report_attachment else
            "report_attachment" if has_report_attachment else "report_folder",
        ])
        return result("supplier_report", 95 if has_report_attachment else 88, "ignore_report")

    if MARKING_RE.search(current_message_text):
        reasons.append("Явная тема маркировки/ЭДО")
        rules.append("edo_marking_terms")
        return result("edo_marking", 94, "process_return")

    if CORRECTION_RE.search(current_message_text):
        reasons.append("Явный корректировочный документ")
        rules.append("correction_document_terms")
        return result("correction_doc", 94, "process_return")

    has_thread_link = bool(
        email.get("in_reply_to")
        or _has_references(email)
        or REPLY_RE.search(subject)
    )
    if has_thread_link and RETURN_RE.search(text):
        reasons.append("Возвратная лексика и признаки продолжения цепочки")
        rules.extend(["return_terms", "thread_headers_or_subject"])
        return result("return_followup", 92, "link_followup")

    if RETURN_RE.search(text) and (has_product_evidence or has_document_evidence):
        reasons.append("Возвратная лексика подтверждена товарным или документным контекстом")
        rules.extend([
            "return_terms",
            "product_evidence" if has_product_evidence else "document_evidence",
        ])
        return result("return_claim", 93, "process_return")

    if REPORT_RE.search(current_message_text) or (
        has_report_attachment and not RETURN_RE.search(current_message_text)
    ):
        reasons.append("Признаки отчёта или табличного файла без возвратного evidence")
        rules.append("supplier_report_weak")
        return result("supplier_report", 78, "ignore_report")

    if NOISE_RE.search(text):
        reasons.append("Автоматическая или техническая рассылка")
        rules.append("automated_noise")
        return result("junk_or_noise", 90, "ignore_report")

    if INFO_RE.search(text):
        reasons.append("Информационное уведомление без возвратной задачи")
        rules.append("information_notice")
        return result("info_only", 82, "ignore_report")

    if RETURN_RE.search(text):
        reasons.append("Есть возвратная лексика, но нет товарного/документного evidence")
        rules.append("return_terms_without_evidence")
        return result("unknown_needs_review", 58, "quick_review")

    reasons.append("Недостаточно детерминированных признаков")
    rules.append("no_confident_rule")
    return result("unknown_needs_review", 35, "human_review")
