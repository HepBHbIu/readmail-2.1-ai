"""Безопасная классификация новых raw_emails → cases (без вложенных соединений).

ФАЗА 1: скелет (classify_email + save_case), своё соединение, коммит.
ФАЗА 2: AI по каждому новому кейсу отдельным соединением (_apply_ai_to_case_id).
Запуск:  docker exec readmail_21 python3 /app/app/_classify_new.py
"""
from __future__ import annotations

import sys

from app.runtime_settings import apply_runtime_settings

apply_runtime_settings()

from app.config import settings
from app.classifier import classify_email, load_buyer_rules, norm, normalize_subject
from app.db import connect, save_case, dumps, utcnow, load_buyer_identities, row_to_dict
from app import main as M


def phase1_skeleton(limit: int = 1000) -> list[int]:
    buyer_rules = load_buyer_rules()
    new_case_ids: list[int] = []
    with connect() as con:
        existing_rows = con.execute(
            """SELECT c.id, c.event_type, e.from_addr, e.subject
               FROM cases c JOIN raw_emails e ON e.id=c.raw_email_id
               WHERE c.event_type IN ('new_return','followup_dialog','followup_reminder','supplier_decision','unknown')
               ORDER BY c.id"""
        ).fetchall()
        existing_cases = [{
            "from_addr": norm(dict(r).get("from_addr", "")),
            "subject_template": normalize_subject(dict(r).get("subject", "")),
            "event_type": dict(r).get("event_type"),
        } for r in existing_rows]

        raw_no_case = con.execute(
            """SELECT r.* FROM raw_emails r
               LEFT JOIN cases c ON c.raw_email_id=r.id
               WHERE c.id IS NULL ORDER BY r.id LIMIT ?""",
            (limit,),
        ).fetchall()
        print(f"ФАЗА 1: писем без кейса = {len(raw_no_case)}", flush=True)

        for i, row in enumerate(raw_no_case, 1):
            email_data = row_to_dict(row) or {}
            email_data["attachments"] = [dict(a) for a in con.execute(
                "SELECT filename, content_type, size_bytes FROM attachments WHERE raw_email_id=?",
                (row["id"],)).fetchall()]
            email_data["visible_text"] = email_data.get("body_text") or email_data.get("snippet") or ""
            learned = load_buyer_identities(con)
            case_data = classify_email(email_data, buyer_rules,
                                       learned_identities=learned, existing_cases=existing_cases)
            case_id = save_case(con, int(row["id"]), case_data)
            case_data["export"]["case_id"] = case_id
            con.execute("UPDATE cases SET export_json=?, updated_at=? WHERE id=?",
                        (dumps(case_data.get("export") or {}), utcnow(), case_id))
            et = case_data.get("event_type", "")
            # ВАЖНО: в скелете (до ИИ) НЕ линкуем и НЕ распределяем — всё висит «Поступило
            # в обработку» (needs_review+needs_ai). Линковка/папки — только ПОСЛЕ ИИ.
            existing_cases.append({
                "from_addr": norm(email_data.get("from_addr", "")),
                "subject_template": normalize_subject(email_data.get("subject", "")),
                "event_type": et,
            })
            new_case_ids.append(case_id)
            if i % 50 == 0:
                con.commit()
                print(f"  скелет {i}/{len(raw_no_case)}", flush=True)
        con.commit()
    print(f"ФАЗА 1 готово: создано кейсов = {len(new_case_ids)}", flush=True)
    return new_case_ids


def phase2_ai(case_ids: list[int]) -> None:
    if not settings.enable_ai:
        print("AI выключен — пропуск ФАЗЫ 2", flush=True)
        return
    print(f"ФАЗА 2: AI по {len(case_ids)} кейсам", flush=True)
    ok = 0
    for i, cid in enumerate(case_ids, 1):
        # только те, что ждут ИИ и ещё не обработаны
        with connect() as con:
            r = con.execute(
                """SELECT 1 FROM cases c WHERE c.id=? AND c.needs_ai=1
                   AND NOT EXISTS(SELECT 1 FROM ai_suggestions s WHERE s.case_id=c.id AND s.accepted=1)""",
                (cid,)).fetchone()
        if not r:
            continue
        try:
            res = M._apply_ai_to_case_id(int(cid), purpose="batch_classify_new")
            if res.get("applied"):
                ok += 1
        except Exception as exc:
            print(f"  AI кейс {cid}: {exc}", flush=True)
        if i % 25 == 0:
            print(f"  AI {i}/{len(case_ids)} (применено {ok})", flush=True)
    print(f"ФАЗА 2 готово: AI применён к {ok} кейсам", flush=True)


def _pending_ai_ids() -> list[int]:
    """Все кейсы, ждущие ИИ (needs_ai=1, без принятой подсказки) — для резюме прогона."""
    with connect() as con:
        rows = con.execute(
            """SELECT c.id FROM cases c WHERE c.needs_ai=1
               AND NOT EXISTS(SELECT 1 FROM ai_suggestions s WHERE s.case_id=c.id AND s.accepted=1)
               ORDER BY c.id"""
        ).fetchall()
    return [int(r["id"]) for r in rows]


def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
    ids = phase1_skeleton(limit)
    # Резюме: если скелет ничего не создал (кейсы уже есть) — берём всех, кто ждёт ИИ.
    if not ids:
        ids = _pending_ai_ids()
        print(f"РЕЗЮМЕ: кейсы уже есть, ждут ИИ = {len(ids)}", flush=True)
    phase2_ai(ids)
    # очередь outbox для готовых
    with connect() as con:
        q = M.queue_control_events(con, limit=1000)
    print("Очередь outbox:", q, flush=True)


if __name__ == "__main__":
    main()
