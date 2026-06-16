#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.final_case_sorter import BUCKETS, build_final_sorting, summarize_final_sorting


def load_jsonl(path: Path, *, optional: bool = False) -> list[dict[str, Any]]:
    if not path.exists():
        if optional:
            return []
        raise FileNotFoundError(path)
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc
            if isinstance(value, dict):
                rows.append(value)
    return rows


def load_outbox(path: Path | None) -> dict[str, list[dict[str, Any]]]:
    if not path or not path.exists():
        return {}
    result: dict[str, list[dict[str, Any]]] = {}
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT id, case_id, status, event_type, channel, created_at, sent_at FROM outbox"
            ).fetchall()
        except sqlite3.Error:
            return {}
    for row in rows:
        result.setdefault(str(row["case_id"]), []).append(dict(row))
    return result


def infer_audit_database(summary_path: Path) -> Path | None:
    if not summary_path.exists():
        return None
    try:
        source = str(json.loads(summary_path.read_text(encoding="utf-8")).get("source") or "")
    except (OSError, json.JSONDecodeError):
        return None
    if not source.startswith("sqlite:"):
        return None
    path = Path(source.removeprefix("sqlite:"))
    return path if path.exists() else None


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def render_summary(summary: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    by_bucket = summary["by_bucket"]
    blockers = Counter(
        reason
        for row in rows
        if row.get("final_bucket") == "blocked_needs_rule"
        for reason in row.get("blocking_reasons") or []
    )
    lines = [
        "# Final Case Sorting",
        "",
        f"Generated: {datetime.now(timezone.utc).replace(microsecond=0).isoformat()}",
        f"Total cases: {summary['total_cases']}",
        f"Return cases: {summary['return_cases']}",
        "",
        "## Buckets",
        "",
    ]
    for bucket in BUCKETS:
        lines.append(f"- {bucket}: {by_bucket.get(bucket, 0)}")
    lines.extend(["", "## Next actions", ""])
    for action, count in summary["top_next_actions"].items():
        lines.append(f"- {action}: {count}")
    lines.extend(["", "## Top reusable blockers", ""])
    for reason, count in blockers.most_common(20):
        lines.append(f"- {reason}: {count}")
    return "\n".join(lines) + "\n"


def render_suppliers(summary: dict[str, Any]) -> str:
    lines = [
        "# Final Case Sorting by Supplier",
        "",
        "| Supplier | Total | Staged | Safe not staged | Warning | Quick 1-click | Quick choice | Human | Needs rule | Link | Terminal/followup |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for buyer, row in summary["by_supplier"].items():
        lines.append(
            "| {buyer} | {total} | {staged} | {safe} | {warning} | {one} | {choice} | {human} | {blocked} | {link} | {terminal} |".format(
                buyer=buyer,
                total=row["total"],
                staged=row["auto_safe_staged"],
                safe=row["auto_safe_preview_not_staged"],
                warning=row["auto_warning_candidate"],
                one=row["quick_review_one_click"],
                choice=row["quick_review_choice"],
                human=row["human_review"],
                blocked=row["blocked_needs_rule"],
                link=row["needs_link"],
                terminal=row["terminal_non_export"] + row["duplicate_or_followup"],
            )
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a read-only final routing map for processed cases.")
    parser.add_argument("--cases", type=Path, default=ROOT / "audit_out/full_dry_run_cases.jsonl")
    parser.add_argument("--quick-review", type=Path, default=ROOT / "audit_out/quick_review_queue.jsonl")
    parser.add_argument("--safe-preview", type=Path, default=ROOT / "audit_out/outbox_preview_safe.jsonl")
    parser.add_argument("--warning-preview", type=Path, default=ROOT / "audit_out/outbox_preview_warning.jsonl")
    parser.add_argument("--staging", type=Path, default=ROOT / "data/outbox_staging.jsonl")
    parser.add_argument("--ledger", type=Path, default=ROOT / "data/learning_ledger.jsonl")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--dry-run-summary", type=Path, default=ROOT / "audit_out/full_dry_run_summary.json")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "audit_out")
    args = parser.parse_args()

    cases = load_jsonl(args.cases)
    audit_database = args.database or infer_audit_database(args.dry_run_summary)
    rows = build_final_sorting(
        cases,
        load_jsonl(args.quick_review, optional=True),
        load_jsonl(args.safe_preview, optional=True),
        load_jsonl(args.warning_preview, optional=True),
        load_jsonl(args.staging, optional=True),
        load_jsonl(args.ledger, optional=True),
        load_outbox(audit_database),
    )
    summary = summarize_final_sorting(rows)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(args.out_dir / "final_case_sorting.jsonl", rows)
    (args.out_dir / "final_case_sorting_summary.md").write_text(
        render_summary(summary, rows), encoding="utf-8"
    )
    (args.out_dir / "final_case_sorting_by_supplier.md").write_text(
        render_suppliers(summary), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
