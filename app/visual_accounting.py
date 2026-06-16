"""Visual Accounting + Safety Router (read-only).

Цель: каждое raw_email получает РОВНО ОДИН visible_bucket, сумма bucket'ов == total raw.
Для каждого case/raw — понятная причина маршрута и решение (ready_to_1c/ai_assist/manual/terminal/service).
Не вызывает AI/1С, не меняет БД/outbox. Слой поверх существующей классификации, без рефактора.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any

# ── 12 целевых visible_bucket ──────────────────────────────────────────
ACTION_REVIEW = "action.review"
ACTION_AI_ASSIST = "action.ai_assist"
ACTION_READY_1C = "action.ready_to_1c"
ACTION_ERRORS = "action.errors"
TERMINAL_LINKED_ACTIVE = "terminal.linked_active"
TERMINAL_LINKED_COMPLETED = "terminal.linked_completed"
TERMINAL_SUPPLIER_REPORT = "terminal.supplier_report"
TERMINAL_SERVICE = "terminal.service"
TERMINAL_DUPLICATE = "terminal.duplicate"
TERMINAL_JUNK = "terminal.junk"
TECH_RAW_WITHOUT_CASE = "technical.raw_without_case"
TECH_UNKNOWN = "technical.unknown"

ALL_BUCKETS = [
    ACTION_REVIEW, ACTION_AI_ASSIST, ACTION_READY_1C, ACTION_ERRORS,
    TERMINAL_LINKED_ACTIVE, TERMINAL_LINKED_COMPLETED, TERMINAL_SUPPLIER_REPORT,
    TERMINAL_SERVICE, TERMINAL_DUPLICATE, TERMINAL_JUNK,
    TECH_RAW_WITHOUT_CASE, TECH_UNKNOWN,
]
ACTION_BUCKETS = {ACTION_REVIEW, ACTION_AI_ASSIST, ACTION_READY_1C, ACTION_ERRORS}
HIDDEN_BUCKETS = {
    TERMINAL_LINKED_ACTIVE, TERMINAL_LINKED_COMPLETED, TERMINAL_SUPPLIER_REPORT,
    TERMINAL_SERVICE, TERMINAL_DUPLICATE, TERMINAL_JUNK,
}

_SERVICE_EVENTS = {"marking_request", "correction_request", "document_flow_notice", "info_update",
                   "number_replacement"}
_SERVICE_KINDS = {"marking_request", "correction_request", "number_replacement"}
_FOLLOWUP_EVENTS = {
    "followup_dialog", "followup_reminder", "supplier_decision", "ready_to_ship",
    "shortage_link_event",
}
import re as _re
_NUMBER_REPLACEMENT_RE = _re.compile(
    r"замен\w*\s+(?:номер\w*|бренд\w*|артикул\w*)|"
    r"перезавести\s+номер|номер\s*/\s*бренд",
    _re.I | _re.U,
)


def is_number_replacement(case: dict[str, Any] | None, subject: str = "") -> bool:
    """Замена номера/бренда — служебное (по claim_kind/event_type или теме «ЗАМЕНА НОМЕРА/БРЕНДА»)."""
    if case and (str(case.get("claim_kind") or "") == "number_replacement"
                 or str(case.get("event_type") or "") == "number_replacement"):
        return True
    return bool(_NUMBER_REPLACEMENT_RE.search(subject or ""))


def corrected_event_type(case: dict[str, Any] | None, subject: str = "") -> str | None:
    """Скорректированный event_type для отчёта (не мутирует БД).

    Замена номера/бренда → number_replacement, даже если в БД сохранён new_return.
    """
    if not case:
        return None
    et = str(case.get("event_type") or "")
    if is_number_replacement(case, subject) and et == "new_return":
        return "number_replacement"
    return et or None


def _payload(case: dict[str, Any] | None) -> dict[str, Any]:
    if not case:
        return {}
    p = case.get("payload")
    if isinstance(p, dict):
        return p
    try:
        return json.loads(case.get("payload_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def evidence_strength(case: dict[str, Any] | None, payload: dict[str, Any]) -> str:
    """strong | medium | weak | none — по evidence_gate + missing + field_statuses."""
    if not case:
        return "none"
    quality = payload.get("quality") or payload.get("_quality") or {}
    gate = payload.get("evidence_gate") or quality.get("evidence_gate") or {}
    missing = case.get("missing_json")
    try:
        missing_list = json.loads(missing) if isinstance(missing, str) else (missing or [])
    except (TypeError, ValueError):
        missing_list = []
    fs = (payload.get("evidence_gate") or {}).get("field_statuses") or {}
    if not fs:
        fs = (quality.get("evidence_gate") or {}).get("field_statuses") or {}
    confirmed = sum(1 for v in fs.values() if "confirm" in str(v))
    weak = any(("weak" in str(v) or "missing" in str(v) or "guess" in str(v)) for v in fs.values())
    blocking = bool(gate.get("blocking_errors"))
    if case.get("ready_for_export") and gate.get("passed") and not missing_list and not blocking:
        return "strong"
    if blocking or (missing_list and not confirmed):
        return "weak"
    if confirmed >= 2 and not weak:
        return "medium"
    if weak or missing_list:
        return "weak"
    return "medium"


def route_case_for_operator(case: dict[str, Any] | None, payload: dict[str, Any] | None = None, *,
                            has_ai: bool = False) -> dict[str, Any]:
    """Safety Router: куда кейс должен идти с точки зрения оператора/безопасности.

    Возвращает decision ∈ {ready_to_1c, ai_assist, manual_review, errors, terminal, service}
    + requires_action/next_action/reasons/conflicts. НЕ меняет бизнес-логику (только для UI/stats/safety).
    """
    payload = payload if payload is not None else _payload(case)
    conflicts: list[str] = []
    reasons: list[str] = []
    if not case:
        return {"decision": "manual_review", "requires_action": True,
                "next_action": "создать/привязать кейс", "reasons": ["raw без кейса"],
                "conflicts": ["raw_without_case"], "evidence_strength": "none"}

    event_type = str(case.get("event_type") or "")
    claim_kind = str(case.get("claim_kind") or "")
    state = str(case.get("state") or "")
    status = str(case.get("status") or "")
    strength = evidence_strength(case, payload)
    quality = payload.get("quality") or payload.get("_quality") or {}
    gate = payload.get("evidence_gate") or quality.get("evidence_gate") or {}
    blocking = bool(gate.get("blocking_errors"))
    pre_delivery = bool(payload.get("pre_delivery_refusal"))

    # errors
    if status == "error" or state == "error" or (payload.get("processing_error")):
        return {"decision": "errors", "requires_action": True, "next_action": "разобрать ошибку обработки",
                "reasons": ["ошибка обработки"], "conflicts": ["processing_error"], "evidence_strength": strength}

    # service (marking/TNVED/correction/requisites) — видеть отдельно
    if event_type in _SERVICE_EVENTS or claim_kind in _SERVICE_KINDS:
        ra = state not in {"linked_event"}  # активное служебное требует внимания
        return {"decision": "service", "requires_action": ra,
                "next_action": "служебное: маркировка/ТНВЭД/корректировка — проверить оператору" if ra else "служебное, завершено",
                "reasons": [f"service event_type={event_type or claim_kind}"],
                "conflicts": conflicts, "evidence_strength": strength}

    # supplier_report / info / junk — терминальные, без действия
    if event_type in {"info_only", "supplier_report"} or state == "ignored_info_only":
        return {"decision": "terminal", "requires_action": False, "next_action": "архив/инфо",
                "reasons": [f"{event_type or 'info_only'}/ignored"], "conflicts": conflicts,
                "evidence_strength": strength}

    # followup / linked — не новые возвраты
    if event_type in _FOLLOWUP_EVENTS or state == "linked_event":
        if state == "needs_link":
            return {"decision": "manual_review", "requires_action": True,
                    "next_action": "привязать к родительскому кейсу", "reasons": ["followup без parent"],
                    "conflicts": ["missing_parent"], "evidence_strength": strength}
        return {"decision": "terminal", "requires_action": state != "linked_event",
                "next_action": "связка завершена" if state == "linked_event" else "проверить связку",
                "reasons": [f"linked/{event_type or state}"], "conflicts": conflicts, "evidence_strength": strength}

    if state == "needs_link":
        return {"decision": "manual_review", "requires_action": True,
                "next_action": "привязать/разрешить ссылку", "reasons": ["needs_link"],
                "conflicts": ["needs_link"], "evidence_strength": strength}

    # new_return / возвраты → ready_to_1c | ai_assist | manual_review
    is_return = event_type in {"new_return", "pre_delivery_refusal"} or claim_kind in {
        "quality_refusal", "defect", "nonconforming", "shortage", "wrong_item",
        "incomplete_set", "overdelivery"}
    if is_return:
        missing = case.get("missing_json")
        try:
            missing_list = json.loads(missing) if isinstance(missing, str) else (missing or [])
        except (TypeError, ValueError):
            missing_list = []
        # ready_to_1c — строго безопасно
        if (case.get("ready_for_export") and state == "ready_to_1c" and gate.get("passed")
                and not blocking and strength in {"strong", "medium"}):
            return {"decision": "ready_to_1c", "requires_action": True, "next_action": "выгрузить в 1С (payload готов)",
                    "reasons": ["evidence ok, gate passed, обяз.поля есть"], "conflicts": conflicts,
                    "evidence_strength": strength}
        # pre_delivery — document не нужен
        if pre_delivery:
            return {"decision": "manual_review", "requires_action": True,
                    "next_action": "подтвердить отказ до поставки (document_required=false)",
                    "reasons": ["pre_delivery_refusal"], "conflicts": conflicts, "evidence_strength": strength}
        # ai_assist — слабое/не хватает 1-2 полей и AI может помочь
        if strength == "weak" or (0 < len(missing_list) <= 2) or bool(case.get("needs_ai")):
            if claim_kind and event_type and claim_kind != event_type and claim_kind == "defect" and "refus" in event_type:
                conflicts.append("claim_kind_conflict")
            return {"decision": "ai_assist", "requires_action": True,
                    "next_action": "добрать поля AI/оператором",
                    "reasons": ["weak evidence / не хватает полей"],
                    "conflicts": conflicts + (["weak_evidence"] if strength == "weak" else [])
                    + (["missing_required_field"] if missing_list else []),
                    "evidence_strength": strength}
        # иначе — ручная проверка
        return {"decision": "manual_review", "requires_action": True, "next_action": "ручная проверка возврата",
                "reasons": ["возврат не готов к 1С"], "conflicts": conflicts, "evidence_strength": strength}

    # unknown
    return {"decision": "manual_review", "requires_action": True, "next_action": "классифицировать вручную",
            "reasons": [f"unknown event_type={event_type or 'NULL'}"], "conflicts": ["unknown_type"],
            "evidence_strength": strength}


def _supplier_report_subcat(case: dict[str, Any], payload: dict[str, Any], subject: str) -> bool:
    sub = str(payload.get("classification_subcategory") or "").lower()
    s = (subject or "").lower()
    return ("price" in sub or "report" in sub or "прайс" in s or "остатк" in s
            or "отчет" in s or "отчёт" in s or "stock" in s)


def visible_bucket(raw: dict[str, Any], case: dict[str, Any] | None, *,
                   has_ai: bool = False, outbox: list | None = None) -> dict[str, Any]:
    """Назначить РОВНО ОДИН visible_bucket + причину/действие. Mutually exclusive."""
    payload = _payload(case)
    subject = str(raw.get("subject") or "")

    # 1. raw без кейса
    if not case:
        return {"visible_bucket": TECH_RAW_WITHOUT_CASE, "subcategory": "raw_without_case",
                "requires_action": True, "next_action": "классифицировать письмо",
                "routing_reason": "raw email без кейса", "is_hidden_from_operator": False,
                "why_hidden": None, "decision": "manual_review", "evidence_strength": "none"}

    # 2. дубликат
    if str(raw.get("status") or "") == "duplicate" or raw.get("duplicate_of_raw_email_id") \
            or str(case.get("event_type") or "") == "duplicate":
        return {"visible_bucket": TERMINAL_DUPLICATE, "subcategory": "duplicate",
                "requires_action": False, "next_action": "ничего (дубль)",
                "routing_reason": "duplicate_of_raw_email_id / status=duplicate",
                "is_hidden_from_operator": True, "why_hidden": "дубликат уже учтён оригиналом",
                "decision": "terminal", "evidence_strength": "none"}

    dec = route_case_for_operator(case, payload, has_ai=has_ai)
    event_type = str(case.get("event_type") or "")
    state = str(case.get("state") or "")
    decision = dec["decision"]
    sub = str(payload.get("classification_subcategory") or "") or None

    # number_replacement (замена номера/бренда) → служебное, даже если в БД сохранён new_return
    if is_number_replacement(case, subject) and decision not in {"errors"}:
        return {"visible_bucket": TERMINAL_SERVICE, "subcategory": "number_replacement",
                "requires_action": state not in {"linked_event"},
                "next_action": "служебное: замена номера/бренда — проверить оператору",
                "routing_reason": "замена номера/бренда (number_replacement)",
                "is_hidden_from_operator": True,
                "why_hidden": "служебное (замена номера/бренда) вынесено отдельно",
                "decision": "service", "evidence_strength": dec["evidence_strength"],
                "conflicts": dec.get("conflicts", [])}

    # needs_link → активная связка (отдельная группа «активные связки», требует внимания)
    if state == "needs_link" and decision not in {"service", "errors"}:
        return {"visible_bucket": TERMINAL_LINKED_ACTIVE, "subcategory": sub or event_type or "needs_link",
                "requires_action": True, "next_action": "привязать к родительскому кейсу / разрешить ссылку",
                "routing_reason": "активная связка ожидает привязки (needs_link)",
                "is_hidden_from_operator": True, "why_hidden": "вынесено в группу «активные связки»",
                "decision": "manual_review", "evidence_strength": dec["evidence_strength"],
                "conflicts": dec.get("conflicts", [])}

    # 3. service
    if decision == "service":
        return {"visible_bucket": TERMINAL_SERVICE, "subcategory": sub or "service",
                "requires_action": dec["requires_action"], "next_action": dec["next_action"],
                "routing_reason": "; ".join(dec["reasons"]) or "служебное (маркировка/ТНВЭД/корректировка)",
                "is_hidden_from_operator": True,
                "why_hidden": "служебное вынесено отдельно из рабочих вкладок",
                "decision": decision, "evidence_strength": dec["evidence_strength"]}

    # 4. supplier_report / junk / info terminal
    if decision == "terminal" and (event_type == "info_only" or state == "ignored_info_only"):
        if _supplier_report_subcat(case, payload, subject):
            return {"visible_bucket": TERMINAL_SUPPLIER_REPORT, "subcategory": sub or "price_list",
                    "requires_action": False, "next_action": "архив (отчёт/прайс)",
                    "routing_reason": "прайс/остатки/отчёт поставщика", "is_hidden_from_operator": True,
                    "why_hidden": "отчёт поставщика — не претензия", "decision": decision,
                    "evidence_strength": dec["evidence_strength"]}
        return {"visible_bucket": TERMINAL_JUNK, "subcategory": sub or "info_or_noise",
                "requires_action": False, "next_action": "архив (инфо/мусор)",
                "routing_reason": "информационное/нерелевантное письмо", "is_hidden_from_operator": True,
                "why_hidden": "не требует действия (инфо/мусор)", "decision": decision,
                "evidence_strength": dec["evidence_strength"]}

    # 5. linked active / completed
    if decision == "terminal":
        if state == "linked_event":
            return {"visible_bucket": TERMINAL_LINKED_COMPLETED, "subcategory": sub or event_type,
                    "requires_action": False, "next_action": "архив связок",
                    "routing_reason": "связка завершена (linked_event)", "is_hidden_from_operator": True,
                    "why_hidden": "завершённая связка — вне списка ожидания", "decision": decision,
                    "evidence_strength": dec["evidence_strength"]}
        return {"visible_bucket": TERMINAL_LINKED_ACTIVE, "subcategory": sub or event_type,
                "requires_action": dec["requires_action"], "next_action": dec["next_action"],
                "routing_reason": "; ".join(dec["reasons"]) or "активная связка", "is_hidden_from_operator": True,
                "why_hidden": "связка вынесена в отдельную группу", "decision": decision,
                "evidence_strength": dec["evidence_strength"]}

    # 6. action.* (видимые)
    bucket = {"ready_to_1c": ACTION_READY_1C, "ai_assist": ACTION_AI_ASSIST,
              "errors": ACTION_ERRORS}.get(decision, ACTION_REVIEW)
    if decision == "manual_review" and event_type not in {"new_return", "pre_delivery_refusal"} \
            and "unknown" in "; ".join(dec.get("conflicts", [])):
        # действительно неизвестные → видимый review, но помечаем как technical.unknown если совсем нет типа
        if not event_type or event_type == "unknown":
            return {"visible_bucket": TECH_UNKNOWN, "subcategory": "unknown",
                    "requires_action": True, "next_action": "классифицировать вручную",
                    "routing_reason": "не классифицировано", "is_hidden_from_operator": False,
                    "why_hidden": None, "decision": decision, "evidence_strength": dec["evidence_strength"]}
    return {"visible_bucket": bucket, "subcategory": sub or event_type or "review",
            "requires_action": dec["requires_action"], "next_action": dec["next_action"],
            "routing_reason": "; ".join(dec["reasons"]) or decision, "is_hidden_from_operator": False,
            "why_hidden": None, "decision": decision, "evidence_strength": dec["evidence_strength"],
            "conflicts": dec.get("conflicts", [])}


def build_visual_accounting(con: Any, *, include_items: bool = False) -> dict[str, Any]:
    """Полная визуальная бухгалтерия: каждое raw → 1 visible_bucket; сумма == total raw."""
    from ._accounting_cache import cached
    key = "visual_items" if include_items else "visual"
    return cached(con, key, lambda: _compute_visual_accounting(con, include_items=include_items))


def _compute_visual_accounting(con: Any, *, include_items: bool = False) -> dict[str, Any]:
    raw_rows = [dict(r) for r in con.execute(
        "SELECT id, subject, from_addr, status, duplicate_of_raw_email_id FROM raw_emails ORDER BY id")]
    case_rows = [dict(r) for r in con.execute(
        "SELECT c.*, EXISTS(SELECT 1 FROM ai_suggestions s WHERE s.case_id=c.id) AS has_ai_suggestion "
        "FROM cases c ORDER BY c.id")]
    outbox_by_case: dict[int, list] = defaultdict(list)
    for o in con.execute("SELECT id, case_id FROM outbox"):
        outbox_by_case[int(o["case_id"])].append(int(o["id"]))
    cases_by_raw: dict[int, list] = defaultdict(list)
    for c in case_rows:
        cases_by_raw[int(c["raw_email_id"])].append(c)
        # buyer_code on raw via case
    buckets: Counter[str] = Counter()
    requires_action = 0
    hidden = 0
    items: list[dict[str, Any]] = []

    for raw in raw_rows:
        raw_cases = cases_by_raw.get(int(raw["id"]), [])
        case = raw_cases[0] if raw_cases else None
        has_ai = bool(case and case.get("has_ai_suggestion"))
        outbox = outbox_by_case.get(int(case["id"]), []) if case else []
        vb = visible_bucket(raw, case, has_ai=has_ai, outbox=outbox)
        buckets[vb["visible_bucket"]] += 1
        if vb["requires_action"]:
            requires_action += 1
        if vb["is_hidden_from_operator"]:
            hidden += 1
        if include_items:
            items.append({
                "raw_email_id": raw["id"], "case_id": case.get("id") if case else None,
                "subject": raw.get("subject"), "sender": raw.get("from_addr"),
                "buyer_code": case.get("buyer_code") if case else None,
                "current_bucket": (case.get("state") if case else "no_case"),
                "visible_bucket": vb["visible_bucket"], "subcategory": vb["subcategory"],
                "requires_action": vb["requires_action"], "next_action": vb["next_action"],
                "routing_reason": vb["routing_reason"], "source_layer": "pattern",
                "evidence_strength": vb["evidence_strength"],
                "is_terminal": vb["visible_bucket"] in HIDDEN_BUCKETS,
                "is_duplicate": vb["visible_bucket"] == TERMINAL_DUPLICATE,
                "is_linked_event": vb["visible_bucket"] in {TERMINAL_LINKED_ACTIVE, TERMINAL_LINKED_COMPLETED},
                "is_ready_to_1c": vb["visible_bucket"] == ACTION_READY_1C,
                "is_hidden_from_operator": vb["is_hidden_from_operator"], "why_hidden": vb["why_hidden"],
            })

    total = len(raw_rows)
    accounted = sum(buckets.values())
    # гарантия полноты: ни одно письмо не пропало
    for b in ALL_BUCKETS:
        buckets.setdefault(b, 0)
    summary = {
        "schema": "readmail-visual-accounting-v1", "read_only": True,
        "total_raw": total, "accounted": accounted, "unaccounted": total - accounted,
        "requires_action": requires_action, "no_action_required": total - requires_action,
        "hidden_from_operator": hidden,
        "by_bucket": {b: buckets[b] for b in ALL_BUCKETS},
        "action_total": sum(buckets[b] for b in ACTION_BUCKETS),
        "terminal_total": sum(buckets[b] for b in HIDDEN_BUCKETS),
        "technical_total": buckets[TECH_RAW_WITHOUT_CASE] + buckets[TECH_UNKNOWN],
    }
    if include_items:
        summary["items"] = items
    return summary


def decision_for_case(con: Any, case_id: int) -> dict[str, Any]:
    """Read-only decision trace для кейса."""
    c = con.execute("SELECT * FROM cases WHERE id=?", (int(case_id),)).fetchone()
    if not c:
        return {"ok": False, "error": "case_not_found", "case_id": int(case_id)}
    case = dict(c)
    raw = con.execute("SELECT id, subject, from_addr, status, duplicate_of_raw_email_id "
                      "FROM raw_emails WHERE id=?", (case.get("raw_email_id"),)).fetchone()
    raw = dict(raw) if raw else {"id": case.get("raw_email_id")}
    case["has_ai_suggestion"] = bool(con.execute(
        "SELECT 1 FROM ai_suggestions WHERE case_id=? LIMIT 1", (int(case_id),)).fetchone())
    payload = _payload(case)
    vb = visible_bucket(raw, case, has_ai=case["has_ai_suggestion"])
    router = route_case_for_operator(case, payload, has_ai=case["has_ai_suggestion"])
    quality = payload.get("quality") or payload.get("_quality") or {}
    gate = payload.get("evidence_gate") or quality.get("evidence_gate") or {}
    return {
        "ok": True, "case_id": int(case_id), "raw_email_id": case.get("raw_email_id"),
        "subject": raw.get("subject"), "buyer_code": case.get("buyer_code"),
        "inbox_sorter_result": (payload.get("inbox_reasons") or payload.get("reasons") or [])[:5]
        if isinstance(payload.get("inbox_reasons") or payload.get("reasons"), list) else None,
        "classifier_result": {"event_type": case.get("event_type"), "claim_kind": case.get("claim_kind")},
        "final_sorter_result": {"state": case.get("state"), "status": case.get("status")},
        "evidence_gate_result": {"passed": gate.get("passed"),
                                 "blocking_errors": gate.get("blocking_errors") or [],
                                 "field_statuses": gate.get("field_statuses") or {}},
        "safety_router_result": router,
        "visible_bucket": vb["visible_bucket"], "subcategory": vb["subcategory"],
        "requires_action": vb["requires_action"], "next_action": vb["next_action"],
        "conflicts": router.get("conflicts", []),
        "explanation_short": f"{vb['visible_bucket']}: {vb['routing_reason']}",
        "is_hidden_from_operator": vb["is_hidden_from_operator"], "why_hidden": vb["why_hidden"],
    }


def decision_for_raw(con: Any, raw_email_id: int) -> dict[str, Any]:
    r = con.execute("SELECT id FROM cases WHERE raw_email_id=? ORDER BY id LIMIT 1",
                    (int(raw_email_id),)).fetchone()
    if r:
        out = decision_for_case(con, int(r["id"]))
        out["via_raw_email_id"] = int(raw_email_id)
        return out
    raw = con.execute("SELECT id, subject, from_addr, status, duplicate_of_raw_email_id "
                      "FROM raw_emails WHERE id=?", (int(raw_email_id),)).fetchone()
    if not raw:
        return {"ok": False, "error": "raw_not_found", "raw_email_id": int(raw_email_id)}
    vb = visible_bucket(dict(raw), None)
    return {"ok": True, "raw_email_id": int(raw_email_id), "case_id": None,
            "subject": raw["subject"], "visible_bucket": vb["visible_bucket"],
            "subcategory": vb["subcategory"], "requires_action": vb["requires_action"],
            "next_action": vb["next_action"], "explanation_short": vb["routing_reason"],
            "is_hidden_from_operator": vb["is_hidden_from_operator"], "why_hidden": vb["why_hidden"]}


def render_bucket_report(summary: dict[str, Any]) -> str:
    b = summary["by_bucket"]
    L = [
        f"TOTAL RAW: {summary['total_raw']}",
        f"ACCOUNTED: {summary['accounted']}",
        f"UNACCOUNTED: {summary['unaccounted']}",
        "",
        "ACTION:",
        f"  Review:        {b[ACTION_REVIEW]}",
        f"  AI Assist:     {b[ACTION_AI_ASSIST]}",
        f"  Ready to 1C:   {b[ACTION_READY_1C]}",
        f"  Errors:        {b[ACTION_ERRORS]}",
        "",
        "TERMINAL:",
        f"  Active links:      {b[TERMINAL_LINKED_ACTIVE]}",
        f"  Completed links:   {b[TERMINAL_LINKED_COMPLETED]}",
        f"  Supplier reports:  {b[TERMINAL_SUPPLIER_REPORT]}",
        f"  Service/marking:   {b[TERMINAL_SERVICE]}",
        f"  Duplicates:        {b[TERMINAL_DUPLICATE]}",
        f"  Junk/info:         {b[TERMINAL_JUNK]}",
        "",
        "TECHNICAL:",
        f"  Raw without case:  {b[TECH_RAW_WITHOUT_CASE]}",
        f"  Unknown:           {b[TECH_UNKNOWN]}",
        "",
        f"requires_action:      {summary['requires_action']}",
        f"no_action_required:   {summary['no_action_required']}",
        f"hidden_from_operator: {summary['hidden_from_operator']}",
        f"unaccounted:          {summary['unaccounted']}",
    ]
    return "\n".join(L)
