#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.inbox_sorter import classify_inbox


def open_ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def load_all(con: sqlite3.Connection) -> list[dict]:
    case_ids = {int(row[0]) for row in con.execute("SELECT DISTINCT raw_email_id FROM cases")}
    rows = []
    for row in con.execute("SELECT * FROM raw_emails ORDER BY id"):
        item = dict(row)
        item["has_case"] = int(row["id"]) in case_ids
        item["attachments"] = [
            dict(att) for att in con.execute(
                "SELECT filename, content_type, size_bytes FROM attachments WHERE raw_email_id=?",
                (row["id"],),
            )
        ]
        rows.append(item)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=ROOT / "data/readmail.sqlite3")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "audit_out")
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    with open_ro(args.database) as con:
        rows = load_all(con)
    decisions = []
    by_sender: dict[str, Counter] = defaultdict(Counter)
    for source in rows:
        item = classify_inbox(source)
        item["has_case"] = bool(source["has_case"])
        item["status"] = source.get("status")
        item["received_at"] = source.get("received_at")
        decisions.append(item)
        by_sender[item["sender_domain"] or "(unknown)"][item["inbox_bucket"]] += 1
    buckets = Counter(row["inbox_bucket"] for row in decisions)
    without = Counter(row["inbox_bucket"] for row in decisions if not row["has_case"])
    actions = Counter(row["next_action"] for row in decisions)
    summary = {
        "database": str(args.database),
        "read_only": True,
        "total_raw": len(decisions),
        "raw_without_case": sum(not row["has_case"] for row in decisions),
        "by_inbox_bucket": dict(buckets.most_common()),
        "raw_without_case_by_bucket": dict(without.most_common()),
        "by_next_action": dict(actions.most_common()),
        "should_enter_return_pipeline": sum(
            buckets[key] for key in ("return_claim", "return_followup", "edo_marking", "correction_doc")
        ),
        "non_return_automatic": sum(
            buckets[key] for key in ("supplier_report", "info_only", "junk_or_noise")
        ),
        "duplicate_or_linked": buckets["duplicate_or_linked"],
        "unknown_needs_review": buckets["unknown_needs_review"],
    }
    with (args.out_dir / "inbox_sorting.jsonl").open("w", encoding="utf-8") as handle:
        for row in decisions:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    lines = ["# Inbox Sorting Snapshot", "", f"- Total raw: **{len(decisions)}**"]
    lines += [
        f"- Raw without case: **{summary['raw_without_case']}**",
        f"- Should enter return pipeline: **{summary['should_enter_return_pipeline']}**",
        f"- Reports/info/noise: **{summary['non_return_automatic']}**",
        f"- Unknown review: **{summary['unknown_needs_review']}**",
        f"- Duplicate/linked: **{summary['duplicate_or_linked']}**",
        "", "## Buckets", "",
    ]
    lines.extend(f"- {key}: {value}" for key, value in buckets.most_common())
    lines.extend(["", "## Raw without case by bucket", ""])
    lines.extend(f"- {key}: {value}" for key, value in without.most_common())
    (args.out_dir / "inbox_sorting_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    sender_lines = ["# Inbox Sorting by Sender", ""]
    for domain, counts in sorted(by_sender.items(), key=lambda x: -sum(x[1].values())):
        sender_lines.append(f"## {domain} ({sum(counts.values())})")
        sender_lines.extend(f"- {key}: {value}" for key, value in counts.most_common())
        sender_lines.append("")
    (args.out_dir / "inbox_sorting_by_sender.md").write_text("\n".join(sender_lines), encoding="utf-8")
    (args.out_dir / "inbox_sorting_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
