#!/usr/bin/env python3
"""Generate read-only raw -> case -> bucket accounting reports."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.bucket_accounting import build_bucket_accounting, markdown_summary, select_not_visible
from app.config import settings
from app.db import connect


def _write_jsonl(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=ROOT / "data" / "readmail.sqlite3")
    parser.add_argument("--audit-out", type=Path, default=ROOT / "audit_out")
    parser.add_argument("--reports", type=Path, default=ROOT / "reports")
    args = parser.parse_args()

    settings.database_path = args.database.resolve()
    with connect() as con:
        result = build_bucket_accounting(con, include_items=True)

    items = result["items"]
    summary = result["summary"]
    not_visible_raw, not_visible_cases = select_not_visible(items)
    _write_jsonl(args.audit_out / "bucket_accounting_matrix.jsonl", items)
    _write_jsonl(args.audit_out / "not_visible_raw_emails.jsonl", not_visible_raw)
    _write_jsonl(args.audit_out / "not_visible_cases.jsonl", not_visible_cases)
    args.reports.mkdir(parents=True, exist_ok=True)
    (args.reports / "bucket_accounting_matrix_summary.md").write_text(
        markdown_summary(summary), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["accounting_gap"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
