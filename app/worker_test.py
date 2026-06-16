"""Worker test harness: безопасная проверка «ядер» обработки без AI/1С/outbox.

Гарантии: БД открывается read-only (mode=ro) → запись невозможна. AI не вызывается (classify_email
детерминирован; AI живёт в _save_classify_learn, который тут НЕ используется). 1С не вызывается.
Real outbox не меняется. Данные не удаляются. Уважает pause-флаги (только отображает).
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from .config import settings

STAGES = ("import", "sorter", "stage2", "outbox-preview", "all")


def _ro(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _email_from_raw(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    return {
        "mailbox": d.get("mailbox"), "uid": d.get("uid"), "message_id": d.get("message_id"),
        "in_reply_to": d.get("in_reply_to"),
        "references": json.loads(d.get("references_json") or "[]") if d.get("references_json") else [],
        "subject": d.get("subject"), "from_addr": d.get("from_addr"), "to_addr": d.get("to_addr"),
        "cc_addr": d.get("cc_addr"), "received_at": d.get("received_at"),
        "body_text": d.get("body_text"), "body_html": d.get("body_html"),
        "snippet": d.get("snippet"), "visible_text": d.get("visible_text") or d.get("body_text") or "",
        "raw_hash": d.get("raw_hash"), "duplicate_of_raw_email_id": d.get("duplicate_of_raw_email_id"),
        "status": d.get("status"), "attachments": [],
    }


def run_worker_test(stage: str = "all", limit: int = 20, db: Path | None = None) -> dict[str, Any]:
    db = Path(db or settings.database_path)
    report: dict[str, Any] = {"ok": True, "stage": stage, "limit": limit, "database": str(db),
                              "dry_run": True, "read_only": True, "stages": {}}
    if not db.exists():
        return {**report, "ok": False, "error": "database_not_found"}

    # pause-флаги (только отображаем, не меняем)
    try:
        from . import runtime_control
        report["pause_flags"] = runtime_control.get_runtime_status()
    except Exception:
        report["pause_flags"] = None

    con = _ro(db)
    try:
        if stage in ("import", "all"):
            r = {"raw_total": int(con.execute("SELECT COUNT(*) FROM raw_emails").fetchone()[0])}
            r["by_status"] = {str(x["status"]): int(x["n"]) for x in con.execute(
                "SELECT status, COUNT(*) n FROM raw_emails GROUP BY status")}
            r["raw_without_case"] = int(con.execute(
                "SELECT COUNT(*) FROM raw_emails r LEFT JOIN cases c ON c.raw_email_id=r.id WHERE c.id IS NULL"
            ).fetchone()[0])
            report["stages"]["import"] = r

        if stage in ("sorter", "all"):
            from .inbox_sorter import classify_inbox
            rows = con.execute("SELECT * FROM raw_emails ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
            buckets: Counter[str] = Counter()
            errors = 0
            for row in rows:
                try:
                    res = classify_inbox(_email_from_raw(row))
                    buckets[str(res.get("inbox_bucket"))] += 1
                except Exception:
                    errors += 1
            report["stages"]["sorter"] = {"checked": len(rows), "buckets": dict(buckets), "errors": errors}

        if stage in ("stage2", "all"):
            from .classifier import classify_email, load_buyer_rules
            buyer_rules = load_buyer_rules()
            rows = con.execute(
                "SELECT r.* FROM raw_emails r LEFT JOIN cases c ON c.raw_email_id=r.id "
                "WHERE c.id IS NULL ORDER BY r.id DESC LIMIT ?", (int(limit),)
            ).fetchall()
            events: Counter[str] = Counter()
            states: Counter[str] = Counter()
            ready = errors = 0
            for row in rows:
                try:
                    case = classify_email(_email_from_raw(row), buyer_rules)  # детерминированно, без AI/записи
                    events[str(case.get("event_type"))] += 1
                    states[str(case.get("state"))] += 1
                    if case.get("ready_for_export"):
                        ready += 1
                except Exception:
                    errors += 1
            report["stages"]["stage2"] = {"checked": len(rows), "by_event_type": dict(events),
                                          "by_state": dict(states), "ready_for_export": ready, "errors": errors}

        if stage in ("outbox-preview", "all"):
            from .db import build_case_event_payload
            rows = con.execute(
                "SELECT id FROM cases WHERE state='ready_to_1c' ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
            previews = pre_delivery = no_doc = errors = 0
            for row in rows:
                try:
                    payload = build_case_event_payload(con, int(row["id"]), profile="standard")
                    if payload is None:
                        continue
                    previews += 1
                    if payload.get("pre_delivery_refusal"):
                        pre_delivery += 1
                    if not (payload.get("return") or {}).get("document_number"):
                        no_doc += 1
                except Exception:
                    errors += 1
            report["stages"]["outbox-preview"] = {"checked": len(rows), "previews_built": previews,
                                                   "pre_delivery_refusal": pre_delivery,
                                                   "without_document_number": no_doc, "errors": errors}
    finally:
        con.close()
    return report


def write_report(report: dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "worker_test_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# Worker test (read-only, dry-run)", "",
             f"- Stage: {report.get('stage')}  · limit: {report.get('limit')}",
             f"- DB: `{report.get('database')}`  · read_only: {report.get('read_only')}", ""]
    for name, data in (report.get("stages") or {}).items():
        lines.append(f"## {name}")
        for k, v in data.items():
            lines.append(f"- {k}: {v}")
        lines.append("")
    lines.append("> AI НЕ вызывался, 1С НЕ вызывалась, real outbox НЕ менялся, БД read-only.")
    (out_dir / "worker_test_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
