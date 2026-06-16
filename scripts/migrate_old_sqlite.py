#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))

from app.classifier import classify_email, load_buyer_rules  # noqa: E402
from app.db import connect, init_db, save_case, upsert_email  # noqa: E402


def loads(value: Any, default: Any = None) -> Any:
    if not value:
        return default
    if isinstance(value, dict) or isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def migrate(old_db: Path, limit: int | None = None) -> dict[str, Any]:
    if not old_db.exists():
        raise FileNotFoundError(old_db)
    init_db()
    rules = load_buyer_rules()
    old = sqlite3.connect(old_db)
    old.row_factory = sqlite3.Row
    result = {"old_db": str(old_db), "emails": 0, "skipped": 0, "cases": 0, "errors": []}
    query = "SELECT * FROM raw_emails ORDER BY id"
    if limit:
        query += f" LIMIT {int(limit)}"
    rows = old.execute(query).fetchall()
    with connect() as con:
        for row in rows:
            try:
                old_id = int(row["id"])
                attachments = []
                try:
                    attachments = [dict(a) for a in old.execute("SELECT filename, content_type, size_bytes FROM attachments WHERE raw_email_id=?", (old_id,))]
                except Exception:
                    pass
                email_data = {
                    "mailbox": row["mailbox"] if "mailbox" in row.keys() else "old_import",
                    "uid": str(row["uid"] if "uid" in row.keys() else old_id),
                    "message_id": row["message_id"] if "message_id" in row.keys() else None,
                    "in_reply_to": None,
                    "references": [],
                    "subject": row["subject"] if "subject" in row.keys() else None,
                    "from_addr": row["from_addr"] if "from_addr" in row.keys() else None,
                    "to_addr": row["to_addr"] if "to_addr" in row.keys() else None,
                    "received_at": row["received_at"] if "received_at" in row.keys() else None,
                    "body_text": row["body_text"] if "body_text" in row.keys() else None,
                    "body_html": row["body_html"] if "body_html" in row.keys() else None,
                    "visible_text": row["body_text"] if "body_text" in row.keys() else None,
                    "snippet": ((row["body_text"] or row["subject"] or "")[:400] if "body_text" in row.keys() else row["subject"]),
                    "raw_hash": f"old:{old_id}",
                    "raw_path": row["raw_path"] if "raw_path" in row.keys() else None,
                    "attachments": attachments,
                }
                raw_email_id, created = upsert_email(con, email_data)
                result["emails" if created else "skipped"] += 1
                case_data = classify_email(email_data, rules)

                # Preserve useful old payload as history, but do not trust old ready/linking state blindly.
                old_case = old.execute("SELECT * FROM return_cases WHERE raw_email_id=? ORDER BY id LIMIT 1", (old_id,)).fetchone()
                if old_case:
                    old_payload = loads(old_case["payload"] if "payload" in old_case.keys() else None, {}) or {}
                    case_data.setdefault("payload", {})["old_project"] = {
                        "case_id": old_case["id"],
                        "buyer_code": old_case["buyer_code"] if "buyer_code" in old_case.keys() else None,
                        "status": old_case["status"] if "status" in old_case.keys() else None,
                        "confidence": old_case["confidence"] if "confidence" in old_case.keys() else None,
                        "payload": old_payload,
                    }
                case_id = save_case(con, raw_email_id, case_data)
                case_data["export"]["case_id"] = case_id
                save_case(con, raw_email_id, case_data)
                result["cases"] += 1
            except Exception as exc:
                result["errors"].append(f"old raw_email {row['id']}: {exc}")
    old.close()
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="Migrate old Project Read Mail SQLite to Project Readmail New")
    ap.add_argument("--old", required=True, help="Path to old return_mail_hub.sqlite3")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    print(json.dumps(migrate(Path(args.old), args.limit), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
