#!/usr/bin/env python3
r"""
audit_outbox_payloads.py — READ-ONLY аудит 1С-payload в outbox.

НИЧЕГО не отправляет в 1С, не меняет БД, не вызывает AI. Открывает БД строго read-only.

Что показывает:
  * сколько payload по event_type;
  * сколько payload без document_number;
  * сколько из них легитимный pre_delivery_refusal (документ не нужен);
  * сколько РЕАЛЬНО ошибочных (обычный возврат без document_number);
  * топ лишних/debug-полей (что срежет профиль minimal/standard);
  * 20 примеров payload (full vs профилированный).

Выход:
  audit_out/outbox_payload_audit.md
  audit_out/outbox_payload_audit.json

Запуск:
  python3 scripts/audit_outbox_payloads.py [--database data/readmail.sqlite3] [--profile standard] [--limit 20]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_DB = ROOT / "data" / "readmail.sqlite3"
DEFAULT_OUT = ROOT / "audit_out"


def open_ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _doc_number(payload: dict[str, Any]) -> Any:
    ret = payload.get("return") or {}
    return ret.get("document_number")


def _event_type(payload: dict[str, Any]) -> str:
    if payload.get("event_type"):
        return str(payload["event_type"])
    return str((payload.get("event") or {}).get("type") or "unknown")


# Поля верхнего уровня, которые НЕ входят в боевой профиль (debug/служебное).
DEBUG_TOP_FIELDS = ("export_data", "export_ready_payload", "control", "quality")
DEBUG_EVENT_FIELDS = ("fingerprint", "confidence", "control_status", "control_action", "control_owner")


def main() -> int:
    ap = argparse.ArgumentParser(description="READ-ONLY outbox 1C payload audit")
    ap.add_argument("--database", type=Path, default=DEFAULT_DB)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--profile", default=None, help="minimal|standard|debug (default из настроек)")
    ap.add_argument("--limit", type=int, default=20, help="сколько примеров payload показать")
    args = ap.parse_args()
    if not args.database.exists():
        ap.error(f"database not found: {args.database}")

    # профайлер payload берём из приложения (та же логика, что и при доставке)
    try:
        from app.config import settings
        from app.db import apply_one_c_payload_profile
        settings.database_path = args.database
    except Exception as exc:  # pragma: no cover
        print(f"cannot import app: {exc}", file=sys.stderr)
        return 2

    profile = args.profile or getattr(settings, "one_c_payload_profile", "standard")

    by_event: Counter[str] = Counter()
    by_status: Counter[str] = Counter()
    without_doc = 0
    legit_pre_delivery = 0
    erroneous_missing_doc = 0
    debug_field_hits: Counter[str] = Counter()
    examples: list[dict[str, Any]] = []
    total = 0

    with open_ro(args.database) as con:
        try:
            rows = con.execute(
                "SELECT id, case_id, status, event_type, payload_json FROM outbox ORDER BY id DESC"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            print(f"no outbox table or error: {exc}", file=sys.stderr)
            rows = []

        for row in rows:
            total += 1
            try:
                payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
            except json.JSONDecodeError:
                payload = {}
            etype = _event_type(payload)
            by_event[etype] += 1
            by_status[str(row["status"])] += 1
            is_pre_delivery = bool(payload.get("pre_delivery_refusal"))
            doc = _doc_number(payload)
            if not doc:
                without_doc += 1
                if is_pre_delivery:
                    legit_pre_delivery += 1
                elif etype in {"return_ready", "new_return", "duplicate_or_repeat_ready"}:
                    erroneous_missing_doc += 1

            # какие debug-поля реально присутствуют (их срежет профиль)
            for key in DEBUG_TOP_FIELDS:
                if payload.get(key) not in (None, {}, []):
                    debug_field_hits[key] += 1
            ev = payload.get("event") or {}
            for key in DEBUG_EVENT_FIELDS:
                if ev.get(key) not in (None, "", {}):
                    debug_field_hits[f"event.{key}"] += 1

            if len(examples) < args.limit:
                profiled = apply_one_c_payload_profile(payload, profile)
                full_keys = sorted(payload.keys())
                stripped = sorted(set(full_keys) - set(profiled.keys()))
                examples.append({
                    "outbox_id": row["id"],
                    "case_id": row["case_id"],
                    "status": row["status"],
                    "event_type": etype,
                    "pre_delivery_refusal": is_pre_delivery,
                    "document_number": doc,
                    "full_top_keys": full_keys,
                    "profiled_top_keys": sorted(profiled.keys()),
                    "stripped_by_profile": stripped,
                    "profiled_payload": profiled,
                })

    report = {
        "ok": True,
        "database": str(args.database),
        "profile": profile,
        "outbox_total": total,
        "by_event_type": dict(by_event),
        "by_status": dict(by_status),
        "without_document_number": without_doc,
        "legit_pre_delivery_refusal": legit_pre_delivery,
        "erroneous_missing_document_number": erroneous_missing_doc,
        "top_debug_fields_present": debug_field_hits.most_common(15),
        "examples": examples,
    }

    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "outbox_payload_audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# Outbox 1C payload audit (READ-ONLY)", "",
        f"- БД: `{args.database}`",
        f"- Профиль (что уйдёт в 1С): **{profile}**",
        f"- Всего payload в outbox: **{total}**",
        f"- Без document_number: **{without_doc}**",
        f"  - из них легитимный pre_delivery_refusal (документ не нужен): **{legit_pre_delivery}**",
        f"  - РЕАЛЬНО ошибочных (обычный возврат без документа): **{erroneous_missing_doc}**",
        "", "## По event_type", "",
    ]
    lines += [f"- {k}: {v}" for k, v in by_event.most_common()]
    lines += ["", "## По status", ""]
    lines += [f"- {k}: {v}" for k, v in by_status.most_common()]
    lines += ["", "## Топ лишних/debug-полей (срезаются профилем)", ""]
    lines += [f"- {k}: {v}" for k, v in debug_field_hits.most_common(15)] or ["- (нет)"]
    lines += ["", f"## Примеры payload (до {args.limit})", ""]
    for ex in examples:
        lines.append(
            f"- outbox#{ex['outbox_id']} case#{ex['case_id']} `{ex['event_type']}` "
            f"pre_delivery={ex['pre_delivery_refusal']} doc={ex['document_number']} "
            f"→ срезано профилем: {', '.join(ex['stripped_by_profile']) or '—'}"
        )
    (args.out_dir / "outbox_payload_audit.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(json.dumps({k: report[k] for k in (
        "outbox_total", "without_document_number", "legit_pre_delivery_refusal",
        "erroneous_missing_document_number", "by_event_type", "profile")},
        ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
