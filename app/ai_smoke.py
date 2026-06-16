"""Контролируемый AI smoke-test для презентации.

Manual-only: вызывает AI ТОЛЬКО по явному confirm и ТОЛЬКО на одном выбранном кейсе/письме.
НЕ auto-apply, НЕ создаёт outbox, НЕ меняет финальный кейс. Пишет результат в
audit_out/ai_smoke_test.jsonl. Поддерживает mock-провайдер (без реального вызова) для тестов.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings
from .db import connect, loads
from .email_parser import select_visible_text

_REPO_ROOT = Path(__file__).resolve().parent.parent
SMOKE_PATH = _REPO_ROOT / "audit_out" / "ai_smoke_test.jsonl"

# Запрещённые источники бренда (поставщики/домены/марки/тип детали) — для warning brand_guard.
_FORBIDDEN_BRAND = [
    "trinity", "тринити", "tea parts", "автото", "autoto", "автоформула", "avtoformula",
    "autoeuro", "авто-евро", "profit", "профит", "росско", "rossko", "ixora", "икора",
    "шате", "shate", "favorit", "фаворит", "autorus", "авторусь", "спутник", "sputnik",
    "parterra", "партерра", "motexc", "питстоп", "pitstop", "@", ".ru", ".com",
]

_SMOKE_FIELDS = ("document_number", "document_date", "part_number", "brand", "product_name", "quantity")

SMOKE_SYSTEM = (
    "Ты извлекаешь поля претензии по автозапчасти из письма поставщику. "
    "Верни ТОЛЬКО валидный JSON по схеме, без markdown, без пояснений, без reasoning, без текста вне JSON. "
    'Схема: {"event_type":"...","claim_kind":"...","fields":{"document_number":"...","document_date":"...",'
    '"part_number":"...","brand":"...","product_name":"...","quantity":0},"flags":[],"confidence":0.0}. '
    "Если поля нет — null. В brand укажи ТОЛЬКО производителя детали, НЕ поставщика, НЕ домен, НЕ марку авто."
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def brand_guard_check(brand: Any) -> dict[str, Any]:
    """Проверить, не похож ли предложенный AI бренд на поставщика/домен/мусор."""
    b = str(brand or "").strip()
    if not b:
        return {"ok": True, "warning": None, "brand": None}
    bl = b.lower()
    if any(f in bl for f in _FORBIDDEN_BRAND):
        return {"ok": False, "brand": b,
                "warning": f"brand '{b}' похож на поставщика/домен/марку — это НЕ производитель детали"}
    try:
        from .classifier import _looks_like_bad_value
        if _looks_like_bad_value(b, "brand"):
            return {"ok": False, "brand": b, "warning": f"brand '{b}' отбракован guard (_looks_like_bad_value)"}
    except Exception:
        pass
    return {"ok": True, "warning": None, "brand": b}


def current_ai_settings() -> dict[str, Any]:
    return {
        "provider": settings.ai_provider,
        "model": settings.ai_model,
        "base_url": settings.ai_base_url,
        "api_key_present": bool(settings.ai_api_key and settings.ai_api_key not in {"local", "none", ""}),
        "max_chars": settings.ai_max_chars,
        "max_output_tokens": settings.ai_max_output_tokens,
        "response_format": settings.ai_response_format,
        "context_mode": settings.ai_context_mode,
        "conserve_tokens": settings.ai_conserve_tokens,
        "cache_enabled": settings.ai_cache_enabled,
        "enable_ai": settings.enable_ai,
    }


def _load_case_and_text(con: Any, *, case_id: int | None, raw_email_id: int | None) -> dict[str, Any] | None:
    con.row_factory = __import__("sqlite3").Row
    if case_id is not None:
        c = con.execute("SELECT id, raw_email_id, buyer_code, event_type, claim_kind, fields_json "
                        "FROM cases WHERE id=?", (int(case_id),)).fetchone()
        if not c:
            return None
        rid = c["raw_email_id"]
        current = loads(c["fields_json"], {}) or {}
        case_meta = {"case_id": c["id"], "event_type": c["event_type"], "claim_kind": c["claim_kind"],
                     "buyer_code": c["buyer_code"]}
    else:
        rid = int(raw_email_id) if raw_email_id is not None else None
        cc = con.execute("SELECT id, fields_json, event_type, claim_kind, buyer_code FROM cases "
                         "WHERE raw_email_id=? ORDER BY id LIMIT 1", (rid,)).fetchone() if rid else None
        current = loads(cc["fields_json"], {}) or {} if cc else {}
        case_meta = {"case_id": cc["id"] if cc else None,
                     "event_type": cc["event_type"] if cc else None,
                     "claim_kind": cc["claim_kind"] if cc else None,
                     "buyer_code": cc["buyer_code"] if cc else None}
    if not rid:
        return None
    e = con.execute("SELECT id, subject, body_text, body_html, visible_text FROM raw_emails WHERE id=?",
                    (rid,)).fetchone()
    if not e:
        return None
    text = select_visible_text(e["body_text"], e["body_html"], e["visible_text"])
    hint = str(e["subject"] or "")
    return {"raw_email_id": rid, "subject": hint, "visible_text": text, "current_fields": current,
            **case_meta}


def run_ai_smoke(*, case_id: int | None = None, raw_email_id: int | None = None,
                 confirm: bool = False, mock: bool = False,
                 max_output_tokens: int = 1024) -> dict[str, Any]:
    """Контролируемый smoke. Без confirm (и не mock) — НЕ вызывает AI."""
    ai_settings = current_ai_settings()
    if case_id is None and raw_email_id is None:
        return {"ok": False, "error": "no_target", "ai_settings": ai_settings, "called": False}
    if not confirm and not mock:
        return {"ok": False, "error": "confirm_required", "called": False, "auto_applied": False,
                "ai_settings": ai_settings,
                "hint": "Добавьте --confirm для реального вызова AI (или --mock для проверки без вызова)."}

    with connect() as con:
        data = _load_case_and_text(con, case_id=case_id, raw_email_id=raw_email_id)
    if not data:
        return {"ok": False, "error": "case_or_raw_not_found", "called": False, "ai_settings": ai_settings}

    user_text = (f"ТЕМА: {data['subject']}\n\nТЕКСТ:\n{data['visible_text']}")[: int(settings.ai_max_chars)]
    messages = [{"role": "system", "content": SMOKE_SYSTEM}, {"role": "user", "content": user_text}]

    provider = "mock"
    model = "mock"
    ptok = ctok = 0
    error = None
    if mock:
        cur = data["current_fields"]
        ai_result = {"event_type": data.get("event_type") or "new_return",
                     "claim_kind": data.get("claim_kind") or "quality_refusal",
                     "fields": {k: cur.get(k) for k in _SMOKE_FIELDS},
                     "flags": [], "confidence": 0.9}
    else:
        from . import ai_client
        old_tokens = settings.ai_max_output_tokens
        settings.ai_max_output_tokens = int(max_output_tokens)  # временно, восстановим в finally
        try:
            raw, provider, model, _url = ai_client._request_chat(messages)
            content = ai_client._response_content(raw)
            ai_result = ai_client._extract_json(content)
            ptok, ctok = ai_client._usage_tokens(
                raw, sum(len(m["content"]) for m in messages), len(content or ""))
        except Exception as exc:
            error = str(exc)[:300]
            ai_result = {}
        finally:
            settings.ai_max_output_tokens = old_tokens
        # запись usage в ai_usage (best-effort), mode=smoke
        try:
            from .db import record_ai_usage
            with connect() as con2:
                record_ai_usage(con2, case_id=data.get("case_id"), provider=provider, model=model,
                                prompt_chars=len(user_text), response_chars=ctok and 0 or 0,
                                prompt_tokens=ptok, completion_tokens=ctok, cached=False,
                                ok=bool(ai_result), error=error, mode="smoke", kind="text")
        except Exception:
            pass

    ai_fields = (ai_result or {}).get("fields") or {}
    cur = data["current_fields"]
    diff = {}
    for k in _SMOKE_FIELDS:
        cv, av = cur.get(k), ai_fields.get(k)
        diff[k] = {"current": cv, "ai": av, "changed": (str(cv or "") != str(av or ""))}
    bguard = brand_guard_check(ai_fields.get("brand"))

    record = {
        "received_at": _utcnow(), "case_id": data.get("case_id"), "raw_email_id": data["raw_email_id"],
        "buyer_code": data.get("buyer_code"), "provider": provider, "model": model,
        "prompt_tokens": ptok, "completion_tokens": ctok, "mock": mock, "error": error,
        "ai_result": ai_result, "diff": diff, "brand_guard": bguard,
        "auto_applied": False,
    }
    SMOKE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SMOKE_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    return {"ok": bool(ai_result) and error is None, "called": not mock, "auto_applied": False,
            "case_id": data.get("case_id"), "raw_email_id": data["raw_email_id"],
            "provider": provider, "model": model, "prompt_tokens": ptok, "completion_tokens": ctok,
            "ai_result": ai_result, "diff": diff, "brand_guard": bguard, "error": error,
            "ai_settings": ai_settings, "sink": str(SMOKE_PATH)}
