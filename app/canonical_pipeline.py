"""Canonical Pipeline — единая схема маршрутизации писем поверх visual/folder accounting.

6 canonical_route × reason_group (причина — фильтр ВНУТРИ route, не верхнее меню).
Read-only: НЕ вызывает AI/1С, НЕ меняет БД/outbox. Без рефактора — слой над существующим.

Ключевой реврейминг: marking / number_replacement / pre_delivery_refusal — это ПРИЧИНЫ возврата/отказа
(route action-уровня), а НЕ «service»-архив.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from typing import Any

from . import visual_accounting as va
from . import folder_accounting as fa

# ── 6 canonical routes ─────────────────────────────────────────────────
READY_FOR_OPERATOR = "ready_for_operator"
AI_ASSIST = "ai_assist"
MANUAL_REVIEW = "manual_review"
READY_TO_1C = "ready_to_1c"
NO_ACTION_ARCHIVE = "no_action_archive"
ERROR_TECHNICAL = "error_technical"
ALL_ROUTES = [READY_FOR_OPERATOR, AI_ASSIST, MANUAL_REVIEW, READY_TO_1C, NO_ACTION_ARCHIVE, ERROR_TECHNICAL]

# ── reason groups ──────────────────────────────────────────────────────
CAUSE_REASONS = {
    "quality_refusal", "defect", "nonconforming", "shortage", "wrong_item",
    "overdelivery", "incomplete_set", "marking", "number_replacement", "pre_delivery_refusal",
}
EVENT_REASONS = {
    "correction", "ready_to_ship", "linked_reminder", "linked_decision", "linked_documents",
    "linked_completed", "supplier_report", "duplicate", "junk", "unknown",
}
REASON_LABEL = {
    "quality_refusal": "Отказ клиента", "defect": "Брак", "nonconforming": "Некондиция",
    "shortage": "Недовоз / недопоставка", "wrong_item": "Пересорт", "overdelivery": "Излишек",
    "incomplete_set": "Некомплект", "marking": "Маркировка / ТНВЭД / код маркировки",
    "number_replacement": "Замена номера / бренда / артикула", "pre_delivery_refusal": "Отказ до поставки",
    "correction": "Корректировки / ЭДО / КСФ", "ready_to_ship": "Готово к выдаче / забрать возврат",
    "linked_reminder": "Напоминание / запрос ответа", "linked_decision": "Решение поставщика",
    "linked_documents": "Дополнительные документы", "linked_completed": "Завершённая связка",
    "supplier_report": "Прайсы / отчёты / остатки", "duplicate": "Дубли",
    "junk": "Мусор / не по теме", "unknown": "Неизвестные",
}

_PRIORITY_RE = re.compile(
    r"срочно|напомина|когда\s+ответ|жд[её]м\s+решени|повторно|просим\s+ответить|ответьте|требуется\s+ответ",
    re.I | re.U)
_REQUEST_NUM_KEYS = ("client_request_number", "claim_number", "return_number", "order_number", "request_number")


def _loads(s: Any, d: Any) -> Any:
    if isinstance(s, (dict, list)):
        return s
    try:
        return json.loads(s) if s else d
    except (TypeError, ValueError, json.JSONDecodeError):
        return d


def _text(raw: dict[str, Any]) -> str:
    return " ".join(str(raw.get(k) or "") for k in ("subject", "snippet", "visible_text", "body_text"))


def reason_group_for(raw: dict[str, Any], case: dict[str, Any] | None, folder: dict[str, Any]) -> tuple[str, str]:
    """Причина/событие письма (нормализованный reason_group)."""
    fname = folder.get("folder_name")
    et = str((case or {}).get("event_type") or "")
    ck = str((case or {}).get("claim_kind") or "")
    folder_map = {
        fa.FOLDER_MARKING: "marking", fa.FOLDER_NUMBER_REPLACEMENT: "number_replacement",
        fa.FOLDER_PRE_DELIVERY: "pre_delivery_refusal", fa.FOLDER_SHORTAGE_LINK: "shortage",
        fa.FOLDER_CORRECTION: "correction", fa.FOLDER_READY_TO_SHIP: "ready_to_ship",
        fa.FOLDER_LINKS_COMPLETED: "linked_completed", fa.FOLDER_REPORTS: "supplier_report",
        fa.FOLDER_DUPLICATES: "duplicate", fa.FOLDER_JUNK: "junk",
        fa.FOLDER_RAW_NO_CASE: "unknown", fa.FOLDER_UNKNOWN: "unknown",
    }
    if fname in folder_map:
        rg = folder_map[fname]
        if rg == "supplier_report" or rg in EVENT_REASONS or rg in CAUSE_REASONS:
            return rg, REASON_LABEL.get(rg, rg)
    if fname == fa.FOLDER_LINKS_ACTIVE:
        if et == "supplier_decision":
            return "linked_decision", REASON_LABEL["linked_decision"]
        if et == "followup_reminder":
            return "linked_reminder", REASON_LABEL["linked_reminder"]
        if "document" in str(folder.get("subcategory") or ""):
            return "linked_documents", REASON_LABEL["linked_documents"]
        return "linked_reminder", REASON_LABEL["linked_reminder"]
    # причина-возврат по claim_kind / event_type
    if et == "pre_delivery_refusal":
        return "pre_delivery_refusal", REASON_LABEL["pre_delivery_refusal"]
    for cause in ("defect", "nonconforming", "shortage", "wrong_item", "overdelivery",
                  "incomplete_set", "quality_refusal"):
        if ck == cause:
            return cause, REASON_LABEL[cause]
    if et == "new_return":
        return "quality_refusal", REASON_LABEL["quality_refusal"]
    if et == "correction_request":
        return "correction", REASON_LABEL["correction"]
    return "unknown", REASON_LABEL["unknown"]


def required_fields_ok(reason_group: str, fields: dict[str, Any], *, delivered: bool = True) -> tuple[bool, list[str]]:
    """Проверка обязательных полей по причине (Фаза 4). Возвращает (ok, missing)."""
    f = fields or {}
    def has(k): return bool(str(f.get(k) or "").strip())
    missing: list[str] = []
    if reason_group == "pre_delivery_refusal":
        if not (has("part_number") or any(has(k) for k in _REQUEST_NUM_KEYS)):
            missing.append("part_number_or_request_number")
        return (not missing), missing  # document НЕ требуется
    if reason_group in ("marking", "number_replacement"):
        if not has("part_number"):
            missing.append("part_number")
        return (not missing), missing  # document может быть необязателен
    if reason_group == "shortage":
        if not has("part_number"):
            missing.append("part_number")
        return (not missing), missing
    if reason_group in CAUSE_REASONS:  # quality_refusal/defect/nonconforming/wrong_item/...
        if not has("part_number"):
            missing.append("part_number")
        if delivered and not has("document_number"):
            missing.append("document_number")
        return (not missing), missing
    return True, []  # события не требуют return-полей


def static_uplift(raw: dict[str, Any], case: dict[str, Any] | None, fields: dict[str, Any],
                  reason_group: str) -> str | None:
    """Точечное усиление evidence у топ-поставщиков по явным фразам claim_kind.

    Возвращает 'strong' если письмо уверенно классифицировано по supplier-specific фразе
    И есть part_number (чтобы не пропускать мусор). Иначе None. НЕ трогает regex.
    """
    if not case or not str(fields.get("part_number") or "").strip():
        return None
    buyer = str(case.get("buyer_code") or "")
    subj = (raw.get("subject") or "").lower()
    text = _text(raw).lower()
    STRONG_PHRASES = {
        "autoeuro_ru": ["отказ покупателя", "не понадобился", "запрос на возврат товара"],
        "profit_liga_ru": ["отказ покупателя", "рекламация. брак", "недопоставка"],
        "ixora_auto_ru": ["отказ от детали", "возврат товара", "вернуть ранее приобрет"],
        "avtoto_ru": ["отказ от товара", "отказ клиента"],
        "avtoformula": ["запрос на возврат товара надлежащего качества"],
    }
    phrases = STRONG_PHRASES.get(buyer, [])
    if any(p in subj or p in text for p in phrases):
        # не усиливаем, если в тексте явный конфликт причин
        if reason_group == "quality_refusal" and any(w in text for w in ("брак", "пересорт", "недовоз", "недопостав")):
            return None
        return "strong"
    return None


def _parent_lookup(case_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """thread_key → материнский кейс (первый new_return в треде)."""
    parents: dict[str, dict[str, Any]] = {}
    for c in case_rows:
        tk = c.get("thread_key")
        if not tk:
            continue
        if str(c.get("event_type") or "") == "new_return" and tk not in parents:
            parents[tk] = c
    return parents


def link_info(case: dict[str, Any] | None, reason_group: str, raw: dict[str, Any],
              parents: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """parent_case_id / link_type / priority_flag / link_reason для связок."""
    out = {"is_parent_case": False, "parent_case_id": None, "parent_raw_email_id": None,
           "link_type": None, "priority_flag": False, "link_confidence": None, "link_reason": None}
    if not case:
        return out
    et = str(case.get("event_type") or "")
    if et == "new_return":
        out["is_parent_case"] = True
    link_type_map = {"linked_reminder": "followup_reminder", "linked_decision": "supplier_decision",
                     "linked_documents": "additional_documents", "linked_completed": "completed_dialog",
                     "correction": "correction_edo_ksf", "ready_to_ship": "ready_to_ship"}
    if reason_group in link_type_map:
        out["link_type"] = link_type_map[reason_group]
        tk = case.get("thread_key")
        parent = parents.get(tk) if tk else None
        if parent and parent.get("id") != case.get("id"):
            out["parent_case_id"] = parent.get("id")
            out["parent_raw_email_id"] = parent.get("raw_email_id")
            out["link_confidence"] = "thread_key"
            out["link_reason"] = "matched parent by thread_key"
        else:
            out["link_reason"] = "parent not found"
    out["priority_flag"] = bool(_PRIORITY_RE.search(_text(raw)))
    return out


def multi_item_info(payload: dict[str, Any]) -> dict[str, Any]:
    items = payload.get("table_items") or payload.get("items") or []
    n = payload.get("multi_item_count")
    try:
        n = int(n) if n is not None else (len(items) if isinstance(items, list) else 0)
    except (TypeError, ValueError):
        n = 0
    is_multi = n > 1
    return {"is_multi_item": is_multi, "item_count_estimate": n,
            "multi_item_reason": "multiple table_items/articles" if is_multi else None,
            "needs_split": is_multi, "split_strategy": "pending" if is_multi else None}


def defect_documents_status(payload: dict[str, Any], reason_group: str) -> dict[str, Any]:
    if reason_group not in ("defect", "nonconforming"):
        return {"defect_documents_status": None, "operator_attention": bool(payload.get("operator_attention"))}
    flag = payload.get("defect_doc_flag") or {}
    ev = (payload.get("quality") or payload.get("_quality") or {}).get("evidence") or {}
    state = flag.get("state")
    present = flag.get("present") or {}
    # Реальный статус из defect_doc_flag (Excel/PDF-акт уже прочитан при классификации).
    status = {"absent": "missing", "present_unverified": "metadata_only", "partial": "partial",
              "complete": "complete"}.get(state)
    if status is None:
        status = "unknown_not_read"
    return {"defect_documents_status": status, "has_service_document": bool(present.get("service_act")),
            "has_photos": bool(ev.get("has_photo")), "operator_attention": True,
            "defect_doc_state": state}


def canonical_for(raw: dict[str, Any], case: dict[str, Any] | None, *,
                  visible: dict[str, Any] | None = None, folder: dict[str, Any] | None = None,
                  parents: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Назначить РОВНО ОДИН canonical_route + причину + флаги. Mutually exclusive."""
    has_ai = bool(case and case.get("has_ai_suggestion"))
    visible = visible or va.visible_bucket(raw, case, has_ai=has_ai)
    folder = folder or fa.folder_for(raw, case, visible=visible)
    parents = parents or {}
    bucket = str(visible.get("visible_bucket") or "")
    payload = _loads((case or {}).get("payload_json"), {}) if case else {}
    fields = _loads((case or {}).get("fields_json"), {}) if case else {}
    reason_group, reason_label = reason_group_for(raw, case, folder)
    strength = va.evidence_strength(case, payload) if case else "none"
    uplift = static_uplift(raw, case, fields, reason_group)
    if uplift == "strong":
        strength = "strong"
    quality = payload.get("quality") or payload.get("_quality") or {}
    gate = payload.get("evidence_gate") or quality.get("evidence_gate") or {}
    blocking = bool(gate.get("blocking_errors"))
    conflicts = visible.get("conflicts", []) or []
    link = link_info(case, reason_group, raw, parents)
    multi = multi_item_info(payload)
    defect = defect_documents_status(payload, reason_group)

    route = None
    next_action = visible.get("next_action")
    routing_reason = visible.get("routing_reason")

    # 1. technical
    if bucket in (va.TECH_RAW_WITHOUT_CASE, va.TECH_UNKNOWN, va.ACTION_ERRORS):
        route, routing_reason = ERROR_TECHNICAL, (visible.get("routing_reason") or "raw/parse/unknown")
        next_action = "классифицировать вручную" if bucket != va.ACTION_ERRORS else "разобрать ошибку"
    # 2. archive (terminal events without action)
    elif reason_group in ("supplier_report", "duplicate", "junk", "linked_completed"):
        route, next_action = NO_ACTION_ARCHIVE, "архив"
        routing_reason = f"{reason_label} — без действия"
    # 3. events: correction / ready_to_ship / linked active
    elif reason_group in ("correction", "ready_to_ship"):
        if visible.get("requires_action"):
            route, next_action = MANUAL_REVIEW, (visible.get("next_action") or "проверить событие")
        else:
            route, next_action = NO_ACTION_ARCHIVE, "архив"
        routing_reason = reason_label
    elif reason_group in ("linked_reminder", "linked_decision", "linked_documents"):
        if link["parent_case_id"]:
            route = MANUAL_REVIEW
            next_action = "обновить родительский кейс (update/reminder)"
            routing_reason = f"{reason_label} → parent #{link['parent_case_id']}"
        else:
            route, next_action = MANUAL_REVIEW, "найти родительский кейс"
            routing_reason = f"{reason_label} — parent не найден"
    # 4. cause reasons (возвраты/отказы/маркировка/замена/недовоз/брак/pre_delivery)
    elif reason_group in CAUSE_REASONS:
        ok, missing = required_fields_ok(reason_group, fields)
        if bucket == va.ACTION_READY_1C:
            route, next_action = READY_TO_1C, "выгрузить в 1С (payload готов)"
            routing_reason = f"{reason_label}: payload готов"
        elif ok and strength in ("strong", "medium") and not blocking and not conflicts:
            route = READY_FOR_OPERATOR
            next_action = "оператор подтверждает → 1С"
            routing_reason = f"{reason_label}: данные есть, evidence {strength}"
        elif (missing and len(missing) <= 2) or strength == "weak" or has_ai \
                or visible.get("visible_bucket") == va.ACTION_AI_ASSIST \
                or reason_group == "shortage":
            route = AI_ASSIST
            next_action = "добрать поля (AI/оператор)"
            routing_reason = f"{reason_label}: слабый evidence / не хватает {missing or 'полей'}"
        else:
            route, next_action = MANUAL_REVIEW, "ручная проверка"
            routing_reason = f"{reason_label}: требуется ручная проверка"
    # 5. fallback
    else:
        route, next_action = MANUAL_REVIEW, "классифицировать вручную"
        routing_reason = routing_reason or "no canonical route matched"

    if route is None:
        route, reason_group, routing_reason = ERROR_TECHNICAL, "unknown", "no canonical route matched"

    requires_action = route != NO_ACTION_ARCHIVE
    can_send_to_1c = route in (READY_FOR_OPERATOR, READY_TO_1C) and reason_group in CAUSE_REASONS
    from .config import settings as _s
    autopilot = bool(getattr(_s, "auto_deliver_outbox", False))
    # manual_gate — письмо обработано вручную и ждёт ручного «Старт» оператора;
    # САМО в 1С не уходит даже при включённом auto_deliver. Автопилот этот флаг
    # не ставит → его письма авто-отправляются (минуя сверку).
    can_auto_send = bool(can_send_to_1c and autopilot and not blocking and not conflicts
                         and not payload.get("manual_gate")
                         and reason_group not in ("supplier_report", "duplicate", "junk")
                         and route == READY_TO_1C)
    payload_policy = "standard"
    ok2, missing2 = required_fields_ok(reason_group, fields)
    field_statuses = (gate.get("field_statuses") or {})
    weak_fields = [k for k, v in field_statuses.items()
                   if ("weak" in str(v) or "missing" in str(v) or "guess" in str(v))]
    return {
        "canonical_route": route, "reason_group": reason_group, "reason_label": reason_label,
        "requires_action": requires_action, "next_action": next_action,
        "can_send_to_1c": can_send_to_1c, "can_auto_send_to_1c": can_auto_send,
        "needs_ai": route == AI_ASSIST, "needs_manual": route == MANUAL_REVIEW,
        "routing_reason": routing_reason, "evidence_strength": strength,
        "validation_summary": {"ok": ok2, "missing": missing2, "blocking": blocking,
                               "conflicts": conflicts, "uplift": uplift},
        "payload_policy": payload_policy, "visible_bucket": bucket,
        "folder_name": folder.get("folder_name"), "subcategory": folder.get("subcategory"),
        # бизнес-поля для UI/items
        "event_type": (case or {}).get("event_type"), "claim_kind": (case or {}).get("claim_kind"),
        "state": (case or {}).get("state"),
        "document_number": fields.get("document_number"), "document_date": fields.get("document_date"),
        "part_number": fields.get("part_number"), "quantity": fields.get("quantity"),
        "missing_fields": missing2, "weak_fields": weak_fields,
        **link, **multi, **defect,
    }


def _compute_pipeline_full(con: Any) -> dict[str, Any]:
    """Тяжёлая сборка (всегда с items). Кэшируется в build_pipeline_accounting."""
    raw_rows = [dict(r) for r in con.execute(
        "SELECT id, subject, from_addr, received_at, status, duplicate_of_raw_email_id, snippet, "
        "substr(visible_text,1,8000) AS visible_text, substr(body_text,1,4000) AS body_text, "
        "substr(body_html,1,4000) AS body_html FROM raw_emails ORDER BY id")]
    case_rows = [dict(r) for r in con.execute(
        "SELECT c.*, EXISTS(SELECT 1 FROM ai_suggestions s WHERE s.case_id=c.id) AS has_ai_suggestion "
        "FROM cases c ORDER BY c.id")]
    cases_by_raw: dict[int, list] = defaultdict(list)
    for c in case_rows:
        cases_by_raw[int(c["raw_email_id"])].append(c)
    has_att = {int(r["raw_email_id"]) for r in con.execute("SELECT DISTINCT raw_email_id FROM attachments")}
    parents = _parent_lookup(case_rows)

    by_route: Counter[str] = Counter()
    reason_in_route: dict[str, Counter] = defaultdict(Counter)
    items: list[dict[str, Any]] = []
    for raw in raw_rows:
        rc = cases_by_raw.get(int(raw["id"]), [])
        case = rc[0] if rc else None
        c = canonical_for(raw, case, parents=parents)
        by_route[c["canonical_route"]] += 1
        reason_in_route[c["canonical_route"]][c["reason_group"]] += 1
        items.append({"raw_email_id": raw["id"], "case_id": case.get("id") if case else None,
                      "subject": raw.get("subject"), "from_addr": raw.get("from_addr"),
                      "buyer_code": case.get("buyer_code") if case else None,
                      "received_at": raw.get("received_at"),
                      "has_attachments": int(raw["id"]) in has_att, **c})
    for r in ALL_ROUTES:
        by_route.setdefault(r, 0)
    total = len(raw_rows)
    accounted = sum(by_route.values())
    return {
        "ok": True, "schema": "readmail-canonical-pipeline-v1", "read_only": True,
        "total_raw": total, "accounted": accounted, "unaccounted": total - accounted,
        "by_route": {r: by_route[r] for r in ALL_ROUTES},
        "reason_in_route": {r: dict(reason_in_route[r].most_common()) for r in ALL_ROUTES},
        "items": items,
    }


def build_pipeline_accounting(con: Any, *, include_items: bool = False) -> dict[str, Any]:
    from ._accounting_cache import cached
    full = cached(con, "pipeline", lambda: _compute_pipeline_full(con))
    if include_items:
        return full
    return {k: v for k, v in full.items() if k != "items"}


def list_pipeline_items(con: Any, *, route: str | None = None, reason: str | None = None,
                        page: int = 1, page_size: int = 50, q: str = "") -> dict[str, Any]:
    acc = build_pipeline_accounting(con, include_items=True)
    ql = (q or "").lower()
    out = []
    for it in acc["items"]:
        if route and it["canonical_route"] != route:
            continue
        if reason and it["reason_group"] != reason:
            continue
        if ql and not any(ql in str(it.get(k) or "").lower()
                          for k in ("subject", "from_addr", "buyer_code", "reason_label")):
            continue
        out.append(it)
    total = len(out)
    page = max(1, int(page)); page_size = max(1, int(page_size))
    start = (page - 1) * page_size
    chunk = out[start:start + page_size]
    return {"ok": True, "route": route, "reason": reason, "q": q, "total": total,
            "total_count": total, "shown_count": len(chunk), "page": page,
            "page_size": page_size, "shown_from": (start + 1) if chunk else 0,
            "shown_to": start + len(chunk), "has_more": start + len(chunk) < total, "items": chunk}


def render_pipeline_report(acc: dict[str, Any]) -> str:
    L = [f"TOTAL RAW: {acc['total_raw']}", f"ACCOUNTED: {acc['accounted']}",
         f"UNACCOUNTED: {acc['unaccounted']}", "", "ROUTES:"]
    titles = {READY_FOR_OPERATOR: "Готово к проверке (оператор)", AI_ASSIST: "AI-разбор",
              MANUAL_REVIEW: "Ручная обработка", READY_TO_1C: "Готово к 1С",
              NO_ACTION_ARCHIVE: "Не требуют действия / архив", ERROR_TECHNICAL: "Ошибки / технические"}
    for r in ALL_ROUTES:
        L.append(f"  {titles[r]} [{r}]: {acc['by_route'][r]}")
        for rg, n in acc["reason_in_route"][r].items():
            L.append(f"      └ {REASON_LABEL.get(rg, rg)} ({rg}): {n}")
    return "\n".join(L)
