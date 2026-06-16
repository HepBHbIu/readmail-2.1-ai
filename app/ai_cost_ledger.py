"""Единый журнал стоимости AI: data/ai_cost_ledger.jsonl.

Принцип владельца: ПАТТЕРНЫ = 0 ТОКЕНОВ. Токены/стоимость считаются ТОЛЬКО для AI-операций
(static_plus_ai_assist / full_ai_pipeline / defect_vision_check / manual_ai_test).
Режим static_only НИКОГДА не вызывает record_ai_cost → его стоимость всегда 0.

Цены берутся из настроек (ai_text_input_per_1k и т.д.). Если цена не задана — пишем
unknown_cost=true и НЕ падаем (total_cost=0).
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fcntl

from .config import settings

PIPELINE_MODES = (
    "static_only",
    "static_plus_ai_assist",
    "full_ai_pipeline",
    "defect_vision_check",
    "manual_ai_test",
)
AI_TASKS = ("missing_fields", "defect_vision", "full_ai", "link_reader", "document_reader")


def _ledger_path() -> Path:
    try:
        base = Path(settings.database_path).parent
    except Exception:
        base = Path("data")
    return base / "ai_cost_ledger.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def compute_cost(input_tokens: int, output_tokens: int, image_count: int = 0) -> dict[str, Any]:
    """Стоимость по ценам из настроек. Если все цены = 0 → unknown_cost=true (total=0)."""
    in_price = float(getattr(settings, "ai_text_input_per_1k", 0.0) or 0.0)
    out_price = float(getattr(settings, "ai_text_output_per_1k", 0.0) or 0.0)
    img_price = float(getattr(settings, "ai_vision_per_image", 0.0) or 0.0)
    base = float(getattr(settings, "ai_call_base_price", 0.0) or 0.0)
    input_cost = (int(input_tokens or 0) / 1000.0) * in_price
    output_cost = (int(output_tokens or 0) / 1000.0) * out_price
    vision_cost = int(image_count or 0) * img_price
    total = round(input_cost + output_cost + vision_cost + base, 6)
    pricing_known = any([in_price, out_price, img_price, base])
    return {
        "input_cost": round(input_cost, 6),
        "output_cost": round(output_cost, 6),
        "vision_cost": round(vision_cost, 6),
        "total_cost": total,
        "unknown_cost": not pricing_known,
    }


def record_ai_cost(
    *,
    pipeline_mode: str,
    ai_task: str,
    case_id: int | None = None,
    raw_email_id: int | None = None,
    buyer_code: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    image_count: int = 0,
    fields_requested: list[str] | None = None,
    fields_found: list[str] | None = None,
    fields_accepted_by_evidence: list[str] | None = None,
    fields_rejected_by_evidence: list[str] | None = None,
    saved_from_human_review: bool = False,
    error: str | None = None,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Записать одну AI-операцию в ledger. Никогда не вызывать для static_only.

    Best-effort: ошибки записи не ломают pipeline.
    """
    if pipeline_mode == "static_only":
        # Паттерны не стоят токенов — для static_only журнал не пишем.
        return {"skipped": True, "reason": "static_only_zero_cost"}
    cost = compute_cost(input_tokens, output_tokens, image_count)
    row: dict[str, Any] = {
        "timestamp": _now(),
        "case_id": case_id,
        "raw_email_id": raw_email_id,
        "buyer_code": buyer_code,
        "pipeline_mode": pipeline_mode,
        "ai_task": ai_task,
        "model": model,
        "provider": provider or getattr(settings, "ai_pricing_provider", None),
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "image_count": int(image_count or 0),
        "vision_units": int(image_count or 0),
        **cost,
        "fields_requested": fields_requested or [],
        "fields_found": fields_found or [],
        "fields_accepted_by_evidence": fields_accepted_by_evidence or [],
        "fields_rejected_by_evidence": fields_rejected_by_evidence or [],
        "saved_from_human_review": bool(saved_from_human_review),
        "error": error,
    }
    target = Path(path or _ledger_path())
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        with target.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    return row


def read_ledger(path: str | Path | None = None) -> list[dict[str, Any]]:
    target = Path(path or _ledger_path())
    if not target.exists():
        return []
    rows: list[dict[str, Any]] = []
    with target.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


_GROUP_KEYS = {
    "day": lambda r: str(r.get("timestamp") or "")[:10],
    "provider": lambda r: r.get("provider") or "unknown",
    "mode": lambda r: r.get("pipeline_mode") or "unknown",
    "supplier": lambda r: r.get("buyer_code") or "unknown",
    "claim_kind": lambda r: r.get("ai_task") or "unknown",
}


def aggregate(by: str = "day", path: str | Path | None = None) -> dict[str, Any]:
    """Свод стоимости/токенов + acceptance rate по разрезу."""
    rows = read_ledger(path)
    keyfn = _GROUP_KEYS.get(by, _GROUP_KEYS["day"])
    groups: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, "input_tokens": 0, "output_tokens": 0, "image_count": 0,
                 "total_cost": 0.0, "accepted_fields": 0, "rejected_fields": 0,
                 "saved_from_human_review": 0, "unknown_cost_calls": 0}
    )
    totals = {"calls": 0, "total_cost": 0.0, "input_tokens": 0, "output_tokens": 0,
              "image_count": 0, "accepted_fields": 0, "rejected_fields": 0}
    for r in rows:
        g = groups[str(keyfn(r))]
        g["calls"] += 1
        g["input_tokens"] += int(r.get("input_tokens") or 0)
        g["output_tokens"] += int(r.get("output_tokens") or 0)
        g["image_count"] += int(r.get("image_count") or 0)
        g["total_cost"] = round(g["total_cost"] + float(r.get("total_cost") or 0.0), 6)
        g["accepted_fields"] += len(r.get("fields_accepted_by_evidence") or [])
        g["rejected_fields"] += len(r.get("fields_rejected_by_evidence") or [])
        g["saved_from_human_review"] += 1 if r.get("saved_from_human_review") else 0
        g["unknown_cost_calls"] += 1 if r.get("unknown_cost") else 0
        totals["calls"] += 1
        totals["total_cost"] = round(totals["total_cost"] + float(r.get("total_cost") or 0.0), 6)
        totals["input_tokens"] += int(r.get("input_tokens") or 0)
        totals["output_tokens"] += int(r.get("output_tokens") or 0)
        totals["image_count"] += int(r.get("image_count") or 0)
        totals["accepted_fields"] += len(r.get("fields_accepted_by_evidence") or [])
        totals["rejected_fields"] += len(r.get("fields_rejected_by_evidence") or [])
    acc = totals["accepted_fields"]
    rej = totals["rejected_fields"]
    denom = acc + rej
    return {
        "by": by,
        "groups": {k: dict(v) for k, v in groups.items()},
        "totals": totals,
        "acceptance_rate": round(acc / denom, 4) if denom else None,
        "rejection_rate": round(rej / denom, 4) if denom else None,
    }
