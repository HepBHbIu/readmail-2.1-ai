"""Переприменить overlay из УЖЕ СОХРАНённых ответов ИИ — БЕЗ новых вызовов модели (бесплатно).

Нужно после фикса apply_ai_overlay (ИИ перетирает скелет): пересобирает поля/распределение
всех кейсов из оплаченных ответов. Запуск:
  docker exec -e PYTHONPATH=/app readmail_21 python3 /app/app/_reapply_overlay.py
"""
from __future__ import annotations

import json
import sqlite3

from app.runtime_settings import apply_runtime_settings
apply_runtime_settings()

from app.config import settings
from app.classifier import apply_ai_overlay
from app.db import connect, save_case, dumps, utcnow
from app import main as M

DB = "/app/data/readmail.sqlite3"


def main() -> None:
    db = sqlite3.connect(DB); db.row_factory = sqlite3.Row
    ids = [int(r["id"]) for r in db.execute(
        """SELECT DISTINCT c.id FROM cases c
           WHERE EXISTS(SELECT 1 FROM ai_suggestions s WHERE s.case_id=c.id AND s.accepted=1)
           ORDER BY c.id""").fetchall()]
    db.close()
    print(f"Переприменяю overlay (без вызовов модели) к {len(ids)} кейсам…", flush=True)
    done = 0
    for i, cid in enumerate(ids, 1):
        try:
            loaded = M._load_case_email_for_ai(cid)
            if not loaded:
                continue
            email_data, case_data = loaded
            with connect() as con:
                srow = con.execute(
                    "SELECT response_json FROM ai_suggestions WHERE case_id=? AND accepted=1 ORDER BY id DESC LIMIT 1",
                    (cid,)).fetchone()
                if not srow:
                    continue
                sugg = json.loads(srow["response_json"] or "{}")
                resp = sugg.get("response") or {}
                if not isinstance(resp, dict) or not resp:
                    continue
                updated = apply_ai_overlay(email_data, case_data, resp)
                rid_row = con.execute("SELECT raw_email_id FROM cases WHERE id=?", (cid,)).fetchone()
                if not rid_row:
                    continue
                uid = save_case(con, int(rid_row["raw_email_id"]), updated)
                updated["export"]["case_id"] = uid
                con.execute("UPDATE cases SET export_json=?, updated_at=? WHERE id=?",
                            (dumps(updated.get("export") or {}), utcnow(), uid))
                # перезапрос outbox для готовых + линковка по подтверждённому типу
                con.execute("DELETE FROM outbox WHERE case_id=? AND status='new'", (uid,))
                if updated.get("state") == "ready_to_1c" and updated.get("ready_for_export"):
                    M.queue_case_event(con, uid)
                _et = str(updated.get("event_type") or "")
                if _et in ("followup_reminder", "followup_dialog", "supplier_decision",
                           "correction_request", "marking_request", "ready_to_ship"):
                    M._auto_link_followup(con, uid, updated)
                M._link_problem_notice(con, uid, updated)
            done += 1
            if i % 50 == 0:
                print(f"  {i}/{len(ids)}", flush=True)
        except Exception as exc:
            print(f"  кейс {cid}: {type(exc).__name__} {exc}", flush=True)
    print(f"ГОТОВО: переприменено {done}/{len(ids)} (0 вызовов модели, 0₽)", flush=True)


if __name__ == "__main__":
    main()
