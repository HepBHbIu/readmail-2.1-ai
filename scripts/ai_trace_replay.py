#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.ai_trace import append_ai_trace, build_trace_entry, defect_evidence
from app.classifier import apply_ai_overlay, classify_email, load_buyer_rules
from app.db import load_buyer_identities, row_to_dict


DEFECT_WORDS = re.compile(
    r"(?i)(?:\bбрак\b|\bдефект\w*\b|\bнеисправ\w*\b|не\s+работает|поврежд[её]н\w*)"
)


def loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if value not in (None, "") else default
    except Exception:
        return default


def email_from_row(row: sqlite3.Row, attachments: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        key: row[key]
        for key in (
            "subject", "from_addr", "to_addr", "cc_addr", "received_at",
            "body_text", "body_html", "visible_text", "snippet", "in_reply_to",
        )
    } | {"references": loads(row["references_json"], []), "attachments": attachments}


def suggestion_response(row: sqlite3.Row) -> dict[str, Any]:
    payload = loads(row["response_json"], {})
    return payload.get("response") if isinstance(payload.get("response"), dict) else {}


def usage_for(con: sqlite3.Connection, case_id: int, prompt_hash: str) -> dict[str, Any]:
    row = con.execute(
        """
        SELECT provider, model, prompt_tokens, completion_tokens
        FROM ai_usage WHERE case_id=? AND prompt_hash=?
        ORDER BY id DESC LIMIT 1
        """,
        (case_id, prompt_hash),
    ).fetchone()
    return dict(row) if row else {}


def select_suggestions(
    con: sqlite3.Connection,
    *,
    case_id: int | None,
    buyer_code: str | None,
    claim_kind: str | None,
    limit: int,
) -> list[sqlite3.Row]:
    conditions = ["1=1"]
    params: list[Any] = []
    if case_id is not None:
        conditions.append("c.id=?")
        params.append(case_id)
    if buyer_code:
        conditions.append("c.buyer_code=?")
        params.append(buyer_code)
    if claim_kind:
        conditions.append("(c.claim_kind=? OR s.response_json LIKE ?)")
        params.extend([claim_kind, f'%\"claim_kind\": \"{claim_kind}\"%'])
    params.append(limit)
    return con.execute(
        f"""
        SELECT s.*, c.raw_email_id, c.buyer_code, c.claim_kind, c.fields_json,
               c.payload_json, c.event_type, c.state, c.ready_for_export,
               e.subject, e.from_addr, e.to_addr, e.cc_addr, e.received_at,
               e.body_text, e.body_html, e.visible_text, e.snippet,
               e.in_reply_to, e.references_json
        FROM ai_suggestions s
        JOIN cases c ON c.id=s.case_id
        JOIN raw_emails e ON e.id=c.raw_email_id
        WHERE {' AND '.join(conditions)}
        ORDER BY s.created_at DESC, s.id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()


def build_historical_traces(
    con: sqlite3.Connection,
    rows: list[sqlite3.Row],
    trace_path: Path,
) -> list[dict[str, Any]]:
    rules = load_buyer_rules()
    identities = load_buyer_identities(con)
    traces = []
    for row in rows:
        attachments = [
            dict(item)
            for item in con.execute(
                "SELECT filename, content_type, size_bytes FROM attachments WHERE raw_email_id=?",
                (row["raw_email_id"],),
            ).fetchall()
        ]
        email = email_from_row(row, attachments)
        pattern = classify_email(email, rules, learned_identities=identities)
        ai_result = suggestion_response(row)
        final_result = apply_ai_overlay(email, pattern, ai_result) if ai_result else pattern
        usage = usage_for(con, int(row["case_id"]), str(row["prompt_hash"] or ""))
        entry = build_trace_entry(
            email_data=email,
            pattern_result=pattern,
            ai_result=ai_result,
            final_result=final_result,
            provider=str(usage.get("provider") or "historical"),
            model=str(row["model"] or usage.get("model") or ""),
            mode="sandbox_replay",
            prompt_hash=str(row["prompt_hash"] or ""),
            case_id=row["case_id"],
            raw_email_id=row["raw_email_id"],
            usage=usage,
            error=None if ai_result else "historical_ai_response_missing",
        )
        append_ai_trace(entry, trace_path)
        traces.append(entry)
    return traces


def build_defect_audit(con: sqlite3.Connection) -> dict[str, Any]:
    suggestion_map: dict[int, list[dict[str, Any]]] = {}
    for row in con.execute("SELECT case_id, response_json FROM ai_suggestions ORDER BY id"):
        response = loads(row["response_json"], {}).get("response") or {}
        suggestion_map.setdefault(int(row["case_id"]), []).append(response)
    rows = con.execute(
        """
        SELECT c.id case_id, c.raw_email_id, c.buyer_code, c.claim_kind, c.event_type,
               e.subject, e.from_addr, e.to_addr, e.cc_addr, e.received_at,
               e.body_text, e.body_html, e.visible_text, e.snippet,
               e.in_reply_to, e.references_json
        FROM cases c JOIN raw_emails e ON e.id=c.raw_email_id
        ORDER BY c.id
        """
    ).fetchall()
    cases = []
    for row in rows:
        attachments = [
            dict(item)
            for item in con.execute(
                "SELECT filename, content_type, size_bytes FROM attachments WHERE raw_email_id=?",
                (row["raw_email_id"],),
            ).fetchall()
        ]
        email = email_from_row(row, attachments)
        text = "\n".join(str(email.get(key) or "") for key in ("subject", "visible_text", "body_text", "body_html"))
        ai_results = suggestion_map.get(int(row["case_id"]), [])
        ai_proposed = any(result.get("claim_kind") in {"defect", "nonconforming"} for result in ai_results)
        current_defect = row["claim_kind"] in {"defect", "nonconforming"}
        if not current_defect and not ai_proposed and not DEFECT_WORDS.search(text):
            continue
        evidence = defect_evidence(
            claim_kind=row["claim_kind"],
            email_data=email,
            ai_proposed_defect=ai_proposed,
        )
        cases.append(
            {
                "case_id": row["case_id"],
                "raw_email_id": row["raw_email_id"],
                "buyer_code": row["buyer_code"],
                "claim_kind": row["claim_kind"],
                "ai_proposed_defect": ai_proposed,
                "ai_changed_to_defect": ai_proposed and not current_defect,
                "pattern_disagreed": ai_proposed and not current_defect,
                **evidence,
            }
        )
    counts = Counter(item["defect_class"] for item in cases)
    summary = {
        "total_defect_candidates": len(cases),
        "confirmed_defect": counts["confirmed_defect"],
        "weak_defect": counts["weak_defect"],
        "conflict_defect": counts["conflict_defect"],
        "defect_rejected_by_evidence": counts["defect_rejected_by_evidence"],
        "defect_with_photos": sum(bool(item["has_photos"]) for item in cases),
        "defect_without_explicit_reason": sum(not item["explicit_reason"] for item in cases),
        "defect_where_ai_changed_claim_kind": sum(bool(item["ai_changed_to_defect"]) for item in cases),
        "defect_where_ai_disagreed_with_pattern": sum(bool(item["pattern_disagreed"]) for item in cases),
    }
    return {"summary": summary, "cases": cases}


def render_report(traces: list[dict[str, Any]], defect: dict[str, Any]) -> str:
    changed = Counter(
        field for trace in traces for field, diff in trace.get("field_diff", {}).items() if diff.get("ai_changed")
    )
    lines = [
        "# AI Trace Replay",
        "",
        "Read-only historical replay. No AI, database, outbox or 1C calls were made.",
        "",
        f"- Trace records added: {len(traces)}",
        f"- Accepted AI fields: {sum(len(x.get('accepted_fields') or []) for x in traces)}",
        f"- Rejected AI fields: {sum(len(x.get('rejected_fields') or []) for x in traces)}",
        "",
        "## Changed fields",
        "",
    ]
    lines.extend(f"- {field}: {count}" for field, count in changed.most_common())
    lines.extend(["", "## Defect audit", ""])
    lines.extend(f"- {key}: {value}" for key, value in defect["summary"].items())
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay historical AI suggestions without calling AI.")
    parser.add_argument("--database", type=Path, default=ROOT / "backups/full_evidence_safety_dry_run_20260609_210721/readmail_snapshot.sqlite3")
    parser.add_argument("--case-id", type=int)
    parser.add_argument("--claim-kind")
    parser.add_argument("--buyer-code")
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true", required=True)
    parser.add_argument("--trace", type=Path, default=ROOT / "data/ai_trace.jsonl")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "audit_out")
    args = parser.parse_args()
    if args.limit < 1 or args.limit > 200:
        parser.error("--limit must be between 1 and 200")
    if not args.database.exists():
        parser.error(f"database not found: {args.database}")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{args.database.resolve()}?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        selected = select_suggestions(
            con,
            case_id=args.case_id,
            buyer_code=args.buyer_code,
            claim_kind=args.claim_kind,
            limit=args.limit,
        )
        traces = build_historical_traces(con, selected, args.trace)
        defect = build_defect_audit(con)
    (args.out_dir / "ai_trace_report.md").write_text(render_report(traces, defect), encoding="utf-8")
    (args.out_dir / "defect_ai_audit.json").write_text(
        json.dumps(defect, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.out_dir / "defect_ai_audit.md").write_text(
        "# Defect AI Audit\n\n" + "\n".join(
            f"- {key}: {value}" for key, value in defect["summary"].items()
        ) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"traces_added": len(traces), **defect["summary"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
