"""A/B топ-3 текстовых моделей на НОВОМ промте. Без записи в БД (con=None).
Запуск: docker exec -e PYTHONPATH=/app readmail_21 python3 /app/app/_abtest_models.py
"""
from __future__ import annotations

import sqlite3
import time

from app.runtime_settings import apply_runtime_settings
apply_runtime_settings()

from app.config import settings
from app.classifier import classify_email, load_buyer_rules
from app.ai_client import run_ai_suggestion

MODELS = [
    "qwen/qwen3-next-80b-a3b-instruct",
    "deepseek/deepseek-v4-flash",
    "qwen/qwen3-235b-a22b-2507",
]

# (raw_email_id, ожидаемый event_type, ключевые поля что должны быть непустыми)
CASES = [
    (3750, "pre_delivery_refusal", ["part_number"]),
    (3777, "new_return", ["part_number", "document_number"]),
    (3693, "new_return", ["claim_number", "part_number"]),
    (3708, "supplier_report", []),
    (3972, "ready_to_ship", []),
    (3696, "followup_dialog", []),
]

DB = "/app/data/readmail.sqlite3"


def _email(rid: int) -> dict:
    db = sqlite3.connect(DB); db.row_factory = sqlite3.Row
    r = db.execute("SELECT * FROM raw_emails WHERE id=?", (rid,)).fetchone()
    e = dict(r) if r else {}
    e["attachments"] = [dict(a) for a in db.execute(
        "SELECT filename,content_type,size_bytes,file_path FROM attachments WHERE raw_email_id=?", (rid,)).fetchall()]
    e["visible_text"] = e.get("body_text") or e.get("snippet") or ""
    db.close()
    return e


def main() -> None:
    rules = load_buyer_rules()
    score = {m: {"json": 0, "type": 0, "fields": 0, "sec": 0.0, "n": 0} for m in MODELS}
    orig = settings.ai_model
    print(f"=== A/B на НОВОМ промте: {len(CASES)} писем × {len(MODELS)} моделей ===\n")
    for rid, exp_type, exp_fields in CASES:
        e = _email(rid)
        skel = classify_email(e, rules)
        print(f"--- письмо {rid}: {str(e.get('subject'))[:48]} | ждём: {exp_type}")
        for m in MODELS:
            settings.ai_model = m  # type: ignore[attr-defined]
            t0 = time.time()
            try:
                res = run_ai_suggestion(e, skel, con=None, purpose="case_extract")
            except Exception as exc:
                print(f"   {m.split('/')[-1]:32} ОШИБКА {exc}")
                score[m]["n"] += 1
                continue
            dt = time.time() - t0
            resp = res.get("response") or {}
            ok_json = bool(res.get("ok")) and isinstance(resp, dict) and bool(resp)
            got_type = str(resp.get("event_type") or "")
            ok_type = got_type == exp_type
            flds = resp.get("fields") or {}
            ok_fields = all(flds.get(k) for k in exp_fields) if exp_fields else True
            s = score[m]
            s["n"] += 1; s["sec"] += dt
            s["json"] += int(ok_json); s["type"] += int(ok_type); s["fields"] += int(ok_fields)
            mark = "✅" if (ok_json and ok_type) else ("🟡" if ok_json else "❌")
            print(f"   {m.split('/')[-1]:32} {mark} {dt:4.1f}s | json={int(ok_json)} type={got_type or '—'} fields={int(ok_fields)}")
        print()
    settings.ai_model = orig  # type: ignore[attr-defined]

    print("=== ИТОГ ===")
    n = len(CASES)
    ranked = sorted(MODELS, key=lambda m: (score[m]["type"], score[m]["json"], score[m]["fields"], -score[m]["sec"]), reverse=True)
    for i, m in enumerate(ranked, 1):
        s = score[m]
        avg = s["sec"] / s["n"] if s["n"] else 0
        print(f"{i}. {m}")
        print(f"   JSON {s['json']}/{n} | тип верный {s['type']}/{n} | поля {s['fields']}/{n} | ⌀{avg:.1f}с")
    print(f"\nПОБЕДИТЕЛЬ: {ranked[0]}")


if __name__ == "__main__":
    main()
