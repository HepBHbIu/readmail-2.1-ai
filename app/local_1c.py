"""Локальный приёмник 1С (контрольная точка обмена) для проверки интеграции.

Это НЕ реальная внешняя 1С: не вызывает внешний HTTP, не меняет real outbox/статусы delivered,
не трогает БД на запись. Принимает тот же payload, что ушёл бы в боевую 1С, и пишет его в
локальный журнал обмена audit_out/local_1c_receiver.jsonl — чтобы проверить состав данных.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import connect, build_case_event_payload

_REPO_ROOT = Path(__file__).resolve().parent.parent
LOCAL_RECEIVER_PATH = _REPO_ROOT / "audit_out" / "local_1c_receiver.jsonl"

# Ключи, которые НИКОГДА не должны попасть в журнал (на случай debug-профиля).
_SECRET_HINTS = ("token", "api_key", "apikey", "password", "secret", "authorization", "http_token")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strip_secrets(obj: Any) -> Any:
    """Рекурсивно вырезать секрето-подобные ключи (безопасность журнала)."""
    if isinstance(obj, dict):
        return {k: ("<hidden>" if any(h in str(k).lower() for h in _SECRET_HINTS) else _strip_secrets(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_secrets(v) for v in obj]
    return obj


def _payload_summary(payload: dict[str, Any]) -> dict[str, Any]:
    ret = payload.get("return") or {}
    buyer = payload.get("buyer") or {}
    ev = payload.get("event") or {}
    df = payload.get("defect_flags") or {}
    defect_state = None
    if df.get("defect_documents_missing"):
        defect_state = "docs_missing"
    elif df.get("defect_documents_incomplete"):
        defect_state = "docs_incomplete"
    elif df.get("defect_documents_complete"):
        defect_state = "docs_complete"
    return {
        "buyer_code": buyer.get("code"),
        "buyer_name": buyer.get("name"),
        "claim_kind": ret.get("claim_kind"),
        "document_number": ret.get("document_number"),
        "part_number": ret.get("part_number"),
        "quantity": ret.get("quantity"),
        "pre_delivery_refusal": payload.get("pre_delivery_refusal"),
        "operator_attention": payload.get("operator_attention") or df.get("operator_attention"),
        "defect_state": defect_state,
        "ready_for_export": ev.get("ready_for_export"),
    }


def _event_type(payload: dict[str, Any]) -> Any:
    return payload.get("event_type") or (payload.get("event") or {}).get("type")


def build_payload_for_case(case_id: int, profile: str = "standard") -> dict[str, Any] | None:
    """Построить payload для кейса напрямую (read-only). НЕ создаёт outbox, НЕ меняет БД."""
    with connect() as con:
        return build_case_event_payload(con, int(case_id), profile=profile)


def receive_payload(payload: dict[str, Any], *, source: str = "local_receiver") -> dict[str, Any]:
    """Приём пакета локальным приёмником 1С: запись в журнал обмена.

    НЕ трогает real outbox/боевую 1С/внешний HTTP. Не помечает событие как delivered.
    """
    if not isinstance(payload, dict) or not payload:
        return {"ok": False, "error": "empty_payload"}
    safe = _strip_secrets(payload)
    record = {
        "received_at": _utcnow(),
        "source": source,
        "integration_mode": "local_receiver",
        "event_type": _event_type(safe),
        "case_id": (safe.get("case") or {}).get("id"),
        "raw_email_id": (safe.get("source_email") or {}).get("raw_email_id"),
        "payload_profile": safe.get("payload_profile"),
        "payload_keys": sorted(safe.keys()),
        "payload_summary": _payload_summary(safe),
        "payload": safe,
    }
    LOCAL_RECEIVER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCAL_RECEIVER_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {"ok": True, "received_at": record["received_at"], "case_id": record["case_id"],
            "event_type": record["event_type"], "payload_profile": record["payload_profile"],
            "integration_mode": "local_receiver", "log": str(LOCAL_RECEIVER_PATH),
            "real_1c_called": False, "outbox_changed": False}


def get_events(limit: int = 20, *, include_payload: bool = False) -> dict[str, Any]:
    """Вернуть последние N пакетов локального приёмника (без полного payload по умолчанию)."""
    if not LOCAL_RECEIVER_PATH.exists():
        return {"ok": True, "total": 0, "returned": 0, "events": [], "log": str(LOCAL_RECEIVER_PATH)}
    rows: list[dict[str, Any]] = []
    with LOCAL_RECEIVER_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    last = rows[-int(limit):] if limit else rows
    events = []
    for r in last:
        ev = {k: r.get(k) for k in ("received_at", "source", "event_type", "case_id",
                                    "raw_email_id", "payload_profile", "payload_keys", "payload_summary")}
        if include_payload:
            ev["payload"] = r.get("payload")
        events.append(ev)
    return {"ok": True, "total": len(rows), "returned": len(events), "events": events,
            "log": str(LOCAL_RECEIVER_PATH)}


def receiver_status() -> dict[str, Any]:
    info = get_events(limit=1)
    last = info["events"][-1] if info.get("events") else None
    return {"ok": True, "integration_mode": "local_receiver", "log": str(LOCAL_RECEIVER_PATH),
            "total_events": info.get("total", 0),
            "last_received_at": (last or {}).get("received_at"),
            "last_case_id": (last or {}).get("case_id"),
            "real_1c_configured": False, "auto_deliver_outbox": False}


def send_case(case_id: int, profile: str = "standard") -> dict[str, Any]:
    """Построить payload кейса и отправить в локальный приёмник 1С. НЕ трогает real outbox/боевую 1С."""
    payload = build_payload_for_case(case_id, profile=profile)
    if not payload:
        return {"ok": False, "error": "case_not_found", "case_id": int(case_id)}
    return receive_payload(payload, source="local_send_case")


def clear_events(confirm: bool = False) -> dict[str, Any]:
    """Очистить журнал локального приёмника. Только по явному confirm."""
    if not confirm:
        return {"ok": False, "error": "confirm_required"}
    if LOCAL_RECEIVER_PATH.exists():
        LOCAL_RECEIVER_PATH.unlink()
    return {"ok": True, "cleared": True, "log": str(LOCAL_RECEIVER_PATH)}
