"""Read-only operator folder taxonomy layered over visual accounting."""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from typing import Any

from . import visual_accounting as va


# Операторские названия (ТЗ разд.5): бизнес-смысл, не технический статус.
FOLDER_REVIEW = "Ручной разбор"
FOLDER_AI = "Проверяется AI"
FOLDER_READY = "Готово к 1С"
FOLDER_DEFECT_UNCHECKED = "Брак: документы не проверены"
FOLDER_DEFECT_INCOMPLETE = "Брак: не хватает документов"
FOLDER_ERRORS = "Ошибки обработки"
FOLDER_PRE_DELIVERY = "Отказы до поставки"
FOLDER_SHORTAGE_LINK = "Недовоз / ссылка АвтоТО"
FOLDER_NUMBER_REPLACEMENT = "Служебное: замена номера/бренда"
FOLDER_MARKING = "Служебное: маркировка / ТНВЭД"
FOLDER_CORRECTION = "Служебное: корректировки / ЭДО"
FOLDER_READY_TO_SHIP = "Возврат готов к выдаче"
FOLDER_LINKS_ACTIVE = "Диалоги: требуют привязки"
FOLDER_LINKS_COMPLETED = "Диалоги: уже привязаны"
FOLDER_REPORTS = "Прайсы / отчёты / остатки"
FOLDER_PROBLEM_NOTICE = "Возможная будущая проблема"
FOLDER_DUPLICATES = "Дубли"
FOLDER_JUNK = "Не по теме / авторассылки"
FOLDER_RAW_NO_CASE = "Не создан кейс"
FOLDER_UNKNOWN = "AI не смог классифицировать"
FOLDER_SERVICE_OTHER = "Служебные прочие"

ALL_FOLDERS = [
    FOLDER_REVIEW,
    FOLDER_AI,
    FOLDER_READY,
    FOLDER_DEFECT_UNCHECKED,
    FOLDER_DEFECT_INCOMPLETE,
    FOLDER_ERRORS,
    FOLDER_PRE_DELIVERY,
    FOLDER_SHORTAGE_LINK,
    FOLDER_NUMBER_REPLACEMENT,
    FOLDER_MARKING,
    FOLDER_CORRECTION,
    FOLDER_READY_TO_SHIP,
    FOLDER_LINKS_ACTIVE,
    FOLDER_LINKS_COMPLETED,
    FOLDER_REPORTS,
    FOLDER_PROBLEM_NOTICE,
    FOLDER_DUPLICATES,
    FOLDER_JUNK,
    FOLDER_RAW_NO_CASE,
    FOLDER_UNKNOWN,
    FOLDER_SERVICE_OTHER,
]

_MARKING_RE = re.compile(r"маркиров|тн\s*вэд|тнвэд|код\s+маркиров", re.I)
_PRE_DELIVERY_RE = re.compile(
    r"запрос\s+на\s+снятие|не\s+поставлять|снять\s+(?:этот\s+)?товар|"
    r"отказ\s+(?:от\s+)?клиента\s+на\s+заказан|до\s+поставки|до\s+получения",
    re.I,
)
_SHORTAGE_LINK_RE = re.compile(
    r"https?://(?:www\.)?avtoto\.ru/nondelivery(?:/|$)|trusted\s+nondelivery",
    re.I,
)


def _payload(case: dict[str, Any] | None) -> dict[str, Any]:
    if not case:
        return {}
    if isinstance(case.get("payload"), dict):
        return case["payload"]
    try:
        return json.loads(case.get("payload_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _text(raw: dict[str, Any]) -> str:
    return "\n".join(
        str(raw.get(key) or "")
        for key in ("subject", "snippet", "visible_text", "body_text", "body_html")
    )


def _folder(
    group: str,
    name: str,
    subcategory: str,
    requires_action: bool,
    next_action: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "folder_group": group,
        "folder_name": name,
        "subcategory": subcategory,
        "folder_requires_action": bool(requires_action),
        "next_action": next_action,
        "folder_reason": reason,
    }


def folder_for(
    raw: dict[str, Any],
    case: dict[str, Any] | None,
    *,
    visible: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assign exactly one operator folder to one raw email."""
    visible = visible or va.visible_bucket(raw, case)
    bucket = str(visible.get("visible_bucket") or "")
    text = _text(raw)
    payload = _payload(case)
    event_type = str((case or {}).get("event_type") or "")
    claim_kind = str((case or {}).get("claim_kind") or "")
    state = str((case or {}).get("state") or "")
    subcategory = str(payload.get("classification_subcategory") or visible.get("subcategory") or "")

    if bucket == va.TECH_RAW_WITHOUT_CASE:
        return _folder("TECHNICAL", FOLDER_RAW_NO_CASE, "raw_without_case", True,
                       "создать/классифицировать кейс", "raw email не имеет case")
    if bucket == va.TERMINAL_DUPLICATE:
        return _folder("TERMINAL", FOLDER_DUPLICATES, "duplicate", False,
                       "ничего", "дубль уже связан с оригиналом")
    if bucket == va.ACTION_ERRORS:
        return _folder("ACTION", FOLDER_ERRORS, "processing_error", True,
                       "разобрать ошибку", visible.get("routing_reason") or "ошибка обработки")

    if va.is_number_replacement(case, text):
        return _folder("SPECIAL_ACTION", FOLDER_NUMBER_REPLACEMENT, "number_replacement",
                       state != "linked_event", "проверить замену номера/бренда",
                       "claim/event/text указывает на замену номера или бренда")
    if event_type == "marking_request" or claim_kind == "marking_request" or _MARKING_RE.search(text):
        return _folder("SPECIAL_ACTION", FOLDER_MARKING, subcategory or "marking_request",
                       state != "linked_event", "проверить маркировку/ТНВЭД",
                       "marking_request или явный текст маркировки/ТНВЭД")
    if (
        event_type == "pre_delivery_refusal"
        or bool(payload.get("pre_delivery_refusal"))
        or _PRE_DELIVERY_RE.search(text)
    ):
        return _folder("SPECIAL_ACTION", FOLDER_PRE_DELIVERY, "pre_delivery_refusal", True,
                       "подтвердить снятие до поставки",
                       "отказ до поставки; документ реализации не требуется")
    shortage_link = payload.get("shortage_link")
    if (
        claim_kind == "shortage"
        and (
            subcategory == "shortage.link_only"
            or isinstance(shortage_link, dict) and shortage_link.get("trusted_link_domain")
            or _SHORTAGE_LINK_RE.search(text)
        )
    ):
        return _folder("SPECIAL_ACTION", FOLDER_SHORTAGE_LINK, "shortage.link_only", True,
                       "открыть ссылку или передать в AI Assist",
                       "недопоставка с доверенной nondelivery-ссылкой")
    if event_type == "correction_request" or claim_kind == "correction_request":
        return _folder("SPECIAL_ACTION", FOLDER_CORRECTION, subcategory or "correction_request",
                       bool(visible.get("requires_action")), visible.get("next_action") or "проверить корректировку",
                       "корректировка/ЭДО/КСФ")
    if event_type == "ready_to_ship":
        return _folder("SPECIAL_ACTION", FOLDER_READY_TO_SHIP, "ready_to_ship", True,
                       "забрать возврат или подтвердить выдачу", "товар/возврат готов к выдаче")
    if event_type == "problem_notice" or state == "problem_notice":
        # ТЗ разд.6/8: НЕ архив — отдельная наблюдательная папка «Возможная будущая проблема».
        return _folder("OBSERVE", FOLDER_PROBLEM_NOTICE, "problem_notice", False,
                       "наблюдать; связать с будущим возвратом, если появится",
                       "поставщик/площадка предупредили о дефекте/отказе, но прямого запроса на возврат нет")

    mapping = {
        va.ACTION_REVIEW: ("ACTION", FOLDER_REVIEW),
        va.ACTION_AI_ASSIST: ("ACTION", FOLDER_AI),
        va.ACTION_READY_1C: ("ACTION", FOLDER_READY),
        va.TERMINAL_LINKED_ACTIVE: ("LINKED", FOLDER_LINKS_ACTIVE),
        va.TERMINAL_LINKED_COMPLETED: ("LINKED", FOLDER_LINKS_COMPLETED),
        va.TERMINAL_SUPPLIER_REPORT: ("TERMINAL", FOLDER_REPORTS),
        va.TERMINAL_JUNK: ("TERMINAL", FOLDER_JUNK),
        va.TECH_UNKNOWN: ("TECHNICAL", FOLDER_UNKNOWN),
        va.TERMINAL_SERVICE: ("SPECIAL_ACTION", FOLDER_SERVICE_OTHER),
    }
    group, name = mapping.get(bucket, ("TECHNICAL", FOLDER_UNKNOWN))
    # ТЗ разд.4: БРАК не идёт в «Готово к 1С», пока документы брака не проверены/неполные.
    if name == FOLDER_READY and claim_kind == "defect":
        dstatus = str((payload.get("defect_doc_flag") or {}).get("defect_documents_status") or "")
        if dstatus in ("", "unknown_not_read", "metadata_only", "missing"):
            return _folder("ACTION", FOLDER_DEFECT_UNCHECKED, "defect.docs_unchecked", True,
                           "открыть АвтоТО / проверить вложения / запустить vision",
                           "брак: документы (наряд/акт/фото) ещё не проверены")
        if dstatus == "incomplete":
            return _folder("ACTION", FOLDER_DEFECT_INCOMPLETE, "defect.docs_incomplete", True,
                           "запросить недостающие документы / ручное решение",
                           "брак: документы проверены, но комплект неполный")
    return _folder(
        group,
        name,
        subcategory or bucket or "unknown",
        bool(visible.get("requires_action")),
        str(visible.get("next_action") or "классифицировать вручную"),
        str(visible.get("routing_reason") or f"visible_bucket={bucket or 'unknown'}"),
    )


def build_folder_accounting(con: Any, *, include_items: bool = False) -> dict[str, Any]:
    from ._accounting_cache import cached
    full = cached(con, "folder", lambda: _compute_folder_accounting(con))
    if include_items:
        return full
    return {k: v for k, v in full.items() if k != "items"}


def _compute_folder_accounting(con: Any) -> dict[str, Any]:
    include_items = True
    raw_rows = [dict(row) for row in con.execute(
        """
        SELECT id, subject, from_addr, status, duplicate_of_raw_email_id,
               snippet, substr(visible_text, 1, 8000) AS visible_text,
               substr(body_text, 1, 4000) AS body_text,
               substr(body_html, 1, 4000) AS body_html
        FROM raw_emails ORDER BY id
        """
    )]
    case_rows = [dict(row) for row in con.execute(
        """
        SELECT c.*, EXISTS(
            SELECT 1 FROM ai_suggestions s WHERE s.case_id=c.id
        ) AS has_ai_suggestion
        FROM cases c ORDER BY c.id
        """
    )]
    cases_by_raw: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for case in case_rows:
        cases_by_raw[int(case["raw_email_id"])].append(case)

    by_folder: Counter[str] = Counter()
    by_group: Counter[str] = Counter()
    by_subcategory: Counter[str] = Counter()
    action = 0
    items: list[dict[str, Any]] = []

    for raw in raw_rows:
        raw_cases = cases_by_raw.get(int(raw["id"]), [])
        case = raw_cases[0] if raw_cases else None
        visible = va.visible_bucket(raw, case, has_ai=bool(case and case.get("has_ai_suggestion")))
        folder = folder_for(raw, case, visible=visible)
        by_folder[folder["folder_name"]] += 1
        by_group[folder["folder_group"]] += 1
        by_subcategory[folder["subcategory"]] += 1
        action += int(folder["folder_requires_action"])
        if include_items:
            # ТЗ разд.7: объяснение к каждому письму (почему сюда / что делать / контекст).
            _pl = _payload(case)
            _q = _pl.get("quality") or {}
            _ev = _q.get("evidence") or {}
            _missing = case.get("missing_json") if case else None
            try:
                _missing = json.loads(_missing) if isinstance(_missing, str) else (_missing or [])
            except (TypeError, ValueError):
                _missing = []
            _strength = str(_q.get("evidence_strength") or _ev.get("strength") or "none")
            _ddf = _pl.get("defect_doc_flag") or {}
            items.append({
                "raw_email_id": raw["id"],
                "case_id": case.get("id") if case else None,
                "subject": raw.get("subject"),
                "buyer_code": case.get("buyer_code") if case else None,
                "visible_bucket": visible["visible_bucket"],
                # объяснение оператору
                "ai_checked": bool(case and case.get("has_ai_suggestion")),
                "evidence": _strength,  # strong / medium / weak / none
                "missing_fields": _missing,
                "has_attachments": int(_ev.get("attachments_count") or 0) > 0,
                "has_defect_docs": bool(_ddf.get("has_defect_documents")),
                "defect_documents_status": _ddf.get("defect_documents_status"),
                "linked_to_parent": str(case.get("state") or "") == "linked_event" if case else False,
                **folder,
            })

    for name in ALL_FOLDERS:
        by_folder.setdefault(name, 0)
    total = len(raw_rows)
    accounted = sum(by_folder.values())

    # ТЗ разд.2: операторская ВОРОНКА (бизнес-смысл, фиксированный порядок).
    g = lambda grp: sum(by_folder[n] for n in ALL_FOLDERS if _folder_group_for_name(n) == grp)
    funnel = [
        {"stage": "Всего писем", "count": total, "kind": "total"},
        {"stage": "Новые / ожидают обработки", "count": by_folder[FOLDER_RAW_NO_CASE], "kind": "wait"},
        {"stage": "Проверяется AI", "count": by_folder[FOLDER_AI], "kind": "ai"},
        {"stage": "Требуют ручного разбора", "count": by_folder[FOLDER_REVIEW] + by_folder[FOLDER_UNKNOWN], "kind": "manual"},
        {"stage": "Брак: проверить документы", "count": by_folder[FOLDER_DEFECT_UNCHECKED] + by_folder[FOLDER_DEFECT_INCOMPLETE], "kind": "defect"},
        {"stage": "Возможная будущая проблема", "count": by_folder[FOLDER_PROBLEM_NOTICE], "kind": "observe"},
        {"stage": "Связки / диалоги", "count": by_folder[FOLDER_LINKS_ACTIVE] + by_folder[FOLDER_LINKS_COMPLETED], "kind": "linked"},
        {"stage": "Готово к 1С", "count": by_folder[FOLDER_READY], "kind": "ready"},
        {"stage": "Служебные (маркировка/ЭДО/недовоз/замена)", "count": by_folder[FOLDER_MARKING] + by_folder[FOLDER_CORRECTION] + by_folder[FOLDER_SHORTAGE_LINK] + by_folder[FOLDER_NUMBER_REPLACEMENT] + by_folder[FOLDER_PRE_DELIVERY] + by_folder[FOLDER_READY_TO_SHIP] + by_folder[FOLDER_SERVICE_OTHER], "kind": "service"},
        {"stage": "Не по теме / прайсы / архив", "count": by_folder[FOLDER_REPORTS] + by_folder[FOLDER_JUNK] + by_folder[FOLDER_DUPLICATES], "kind": "archive"},
        {"stage": "Ошибки обработки", "count": by_folder[FOLDER_ERRORS], "kind": "error"},
    ]
    result: dict[str, Any] = {
        "ok": True,
        "schema": "readmail-folder-accounting-v2",
        "read_only": True,
        "total_raw": total,
        "accounted": accounted,
        "unaccounted": total - accounted,
        "requires_action": action,
        "no_action": total - action,
        "by_group": dict(by_group.most_common()),
        "by_folder": {name: by_folder[name] for name in ALL_FOLDERS},
        "by_subcategory": dict(by_subcategory.most_common()),
        "funnel": funnel,
    }
    if include_items:
        result["items"] = items
    return result


def render_folder_report(summary: dict[str, Any]) -> str:
    folders = summary["by_folder"]
    groups = (
        ("ТРЕБУЮТ ДЕЙСТВИЯ", "ACTION"),
        ("СПЕЦИАЛЬНЫЕ / СЛУЖЕБНЫЕ", "SPECIAL_ACTION"),
        ("ДИАЛОГИ / СВЯЗКИ", "LINKED"),
        ("ВОЗМОЖНАЯ БУДУЩАЯ ПРОБЛЕМА", "OBSERVE"),
        ("АРХИВ / НЕ ТРЕБУЕТ РЕАКЦИИ", "TERMINAL"),
        ("ТЕХНИЧЕСКИЕ", "TECHNICAL"),
    )
    lines = [
        f"TOTAL RAW: {summary['total_raw']}",
        f"ACCOUNTED: {summary['accounted']}",
        f"UNACCOUNTED: {summary['unaccounted']}",
        "",
    ]
    for title, group in groups:
        lines.append(f"{title}:")
        for name in ALL_FOLDERS:
            if _folder_group_for_name(name) == group:
                lines.append(f"  {name}: {folders.get(name, 0)}")
        lines.append("")
    lines.extend([
        f"requires_action: {summary['requires_action']}",
        f"no_action: {summary['no_action']}",
    ])
    return "\n".join(lines)


def _folder_group_for_name(name: str) -> str:
    if name in {FOLDER_REVIEW, FOLDER_AI, FOLDER_READY, FOLDER_ERRORS,
                FOLDER_DEFECT_UNCHECKED, FOLDER_DEFECT_INCOMPLETE}:
        return "ACTION"
    if name in {
        FOLDER_PRE_DELIVERY, FOLDER_SHORTAGE_LINK, FOLDER_NUMBER_REPLACEMENT,
        FOLDER_MARKING, FOLDER_CORRECTION, FOLDER_READY_TO_SHIP, FOLDER_SERVICE_OTHER,
    }:
        return "SPECIAL_ACTION"
    if name in {FOLDER_LINKS_ACTIVE, FOLDER_LINKS_COMPLETED}:
        return "LINKED"
    if name == FOLDER_PROBLEM_NOTICE:
        return "OBSERVE"
    if name in {FOLDER_REPORTS, FOLDER_DUPLICATES, FOLDER_JUNK}:
        return "TERMINAL"
    return "TECHNICAL"
