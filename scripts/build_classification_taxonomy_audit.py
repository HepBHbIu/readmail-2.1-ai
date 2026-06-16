#!/usr/bin/env python3
"""Generate the read-only classification taxonomy audit and risk summary."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.classification_taxonomy import audit_classifications
from app.config import settings
from app.db import connect


def _table(values: dict[str, int]) -> str:
    return "\n".join(f"| `{key}` | {value} |" for key, value in values.items())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=ROOT / "data" / "readmail.sqlite3")
    parser.add_argument("--audit-out", type=Path, default=ROOT / "audit_out")
    parser.add_argument("--reports", type=Path, default=ROOT / "reports")
    args = parser.parse_args()
    settings.database_path = args.database.resolve()

    with connect() as con:
        result = audit_classifications(con)
    summary = result["summary"]
    args.audit_out.mkdir(parents=True, exist_ok=True)
    with (args.audit_out / "classification_taxonomy_audit.jsonl").open("w", encoding="utf-8") as handle:
        for item in result["items"]:
            handle.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")

    args.reports.mkdir(parents=True, exist_ok=True)
    summary_md = f"""# Classification Taxonomy Audit Summary

- Total raw: **{summary['total_raw']}**
- Accounted: **{summary['accounted']}**
- Unaccounted: **{summary['unaccounted']}**
- Mismatches/warnings: **{summary['mismatches']}**
- Missing subcategory: **{summary['missing_subcategory']}**
- Dangerous misclassified: **{summary['dangerous_misclassified']}**

## Categories

| Category | Count |
|---|---:|
{_table(summary['by_category'])}

## Subcategories

| Subcategory | Count |
|---|---:|
{_table(summary['by_subcategory'])}

## Risks

| Risk | Count |
|---|---:|
{_table(summary['top_risks'])}
"""
    (args.reports / "classification_taxonomy_audit_summary.md").write_text(
        summary_md, encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["unaccounted"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
