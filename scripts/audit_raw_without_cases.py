#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.inbox_sorter import classify_inbox, sender_domain


def open_ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def load_without_cases(con: sqlite3.Connection) -> list[dict]:
    rows = con.execute(
        """
        SELECT r.*
        FROM raw_emails r
        WHERE NOT EXISTS (SELECT 1 FROM cases c WHERE c.raw_email_id=r.id)
        ORDER BY r.id
        """
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["attachments"] = [
            dict(att) for att in con.execute(
                "SELECT filename, content_type, size_bytes FROM attachments WHERE raw_email_id=?",
                (row["id"],),
            )
        ]
        result.append(item)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=ROOT / "data/readmail.sqlite3")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "audit_out")
    parser.add_argument("--sample", type=int, default=100)
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    with open_ro(args.database) as con:
        rows = load_without_cases(con)
    decisions = [classify_inbox(row) for row in rows]
    by_mailbox = Counter(str(row.get("mailbox") or "") for row in rows)
    by_domain = Counter(sender_domain(row.get("from_addr")) or "(unknown)" for row in rows)
    by_status = Counter(str(row.get("status") or "(empty)") for row in rows)
    by_bucket = Counter(row["inbox_bucket"] for row in decisions)
    summary = {
        "database": str(args.database),
        "total_raw_without_case": len(rows),
        "by_mailbox": dict(by_mailbox.most_common()),
        "by_sender_domain": dict(by_domain.most_common()),
        "by_status": dict(by_status.most_common()),
        "by_inbox_bucket": dict(by_bucket.most_common()),
    }
    (args.out_dir / "raw_without_cases.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (args.out_dir / "raw_without_cases_sample.jsonl").open("w", encoding="utf-8") as handle:
        for source, decision in list(zip(rows, decisions))[: max(1, args.sample)]:
            handle.write(json.dumps({
                **decision,
                "status": source.get("status"),
                "received_at": source.get("received_at"),
                "body_excerpt": str(source.get("visible_text") or source.get("body_text") or "")[:500],
            }, ensure_ascii=False) + "\n")
    lines = [
        "# Raw emails without cases", "",
        f"- Database: `{args.database}`",
        f"- Total raw without case: **{len(rows)}**", "",
        "## Inbox buckets", "",
    ]
    lines.extend(f"- {key}: {value}" for key, value in by_bucket.most_common())
    lines.extend(["", "## Top sender domains", ""])
    lines.extend(f"- {key}: {value}" for key, value in by_domain.most_common(20))
    lines.extend(["", "## Mailboxes", ""])
    lines.extend(f"- {key}: {value}" for key, value in by_mailbox.most_common(20))
    (args.out_dir / "raw_without_cases_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
