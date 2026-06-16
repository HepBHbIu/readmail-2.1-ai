#!/usr/bin/env python3
from __future__ import annotations

import argparse
import imaplib
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from email.parser import BytesHeaderParser
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_DB = ROOT / "data" / "readmail.sqlite3"
DEFAULT_OUT = ROOT / "audit_out"


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def open_ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def load_local(con: sqlite3.Connection, account: str) -> tuple[list[dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    rows = [
        {"account": account, **dict(row)}
        for row in con.execute(
            """
            SELECT id, mailbox, uid, message_id, raw_hash, duplicate_of_raw_email_id,
                   status, received_at, imported_at
            FROM raw_emails
            ORDER BY id
            """
        )
    ]
    failures: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        for row in con.execute(
            """
            SELECT account, mailbox, uid, stage, error_type, error_message,
                   attempts, status, first_seen_at, last_seen_at
            FROM import_uid_failures
            """
        ):
            item = dict(row)
            failures[(str(row["mailbox"]), str(row["uid"]))] = item
    except sqlite3.OperationalError:
        pass
    return rows, failures


def _response_number(mail: imaplib.IMAP4_SSL, name: str) -> str | None:
    try:
        _typ, values = mail.response(name)
        if values and values[0]:
            value = values[0].decode("ascii", "ignore") if isinstance(values[0], bytes) else str(values[0])
            match = re.search(r"\d+", value)
            return match.group(0) if match else None
    except Exception:
        pass
    return None


def _parse_fetch_metadata(fetch_data: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    parser = BytesHeaderParser()
    for part in fetch_data or []:
        if not isinstance(part, tuple) or len(part) < 2:
            continue
        meta = part[0] if isinstance(part[0], bytes) else b""
        header = part[1] if isinstance(part[1], bytes) else b""
        uid_match = re.search(rb"\bUID\s+(\d+)", meta)
        if not uid_match:
            continue
        uid = uid_match.group(1).decode("ascii")
        date_match = re.search(rb'INTERNALDATE\s+"([^"]+)"', meta)
        message_id = ""
        try:
            message_id = str(parser.parsebytes(header).get("Message-ID") or "").strip().lower()
        except Exception:
            pass
        result[uid] = {
            "message_id": message_id,
            "internal_date": date_match.group(1).decode("ascii", "replace") if date_match else None,
        }
    return result


def apply_runtime_settings_ro(database: Path) -> dict[str, Any]:
    from app.config import settings
    from app.runtime_settings import SETTING_DEFS, _cast_value

    applied: dict[str, Any] = {}
    with open_ro(database) as con:
        try:
            stored = {
                str(row["key"]): json.loads(row["value_json"])
                for row in con.execute("SELECT key, value_json FROM app_settings")
            }
        except sqlite3.OperationalError:
            stored = {}
    for definition in SETTING_DEFS:
        key = definition["key"]
        if key not in stored:
            continue
        value = _cast_value(definition, stored[key])
        setattr(settings, definition["attr"], value)
        applied[key] = value
    settings.database_path = database
    return applied


def collect_server_snapshot(database: Path, local_rows: list[dict[str, Any]]) -> dict[str, Any]:
    from app.config import settings
    from app.imap_importer import (
        _clean_credential,
        _imap_quote,
        _open_imap,
        decode_imap_utf7,
        discover_imap_folders,
    )
    apply_runtime_settings_ro(database)
    account = _clean_credential(settings.imap_username or "")
    if not account or not _clean_credential(settings.imap_password or ""):
        raise RuntimeError("IMAP credentials are not configured")
    configured = {
        value.strip()
        for value in str(getattr(settings, "imap_folders", "") or "").split(",")
        if value.strip()
    }
    local_keys = {
        (str(row.get("mailbox") or ""), str(row.get("uid") or ""))
        for row in local_rows
    }
    mail = _open_imap()
    folders: dict[str, Any] = {}
    try:
        for folder in discover_imap_folders(mail):
            if configured and folder not in configured:
                continue
            entry: dict[str, Any] = {
                "account": account,
                "folder": folder,
                "display_folder": decode_imap_utf7(folder),
                "uidvalidity": None,
                "message_count": 0,
                "unseen_count": None,
                "date_from": None,
                "date_to": None,
                "uids": {},
                "error": None,
            }
            try:
                typ, _data = mail.select(_imap_quote(folder), readonly=True)
                if typ != "OK":
                    raise RuntimeError(f"SELECT returned {typ}")
                entry["uidvalidity"] = _response_number(mail, "UIDVALIDITY")
                typ, data = mail.uid("search", None, "ALL")
                if typ != "OK":
                    raise RuntimeError(f"UID SEARCH ALL returned {typ}")
                uid_values = [
                    value.decode("ascii", "ignore")
                    for value in (data[0].split() if data and data[0] else [])
                ]
                entry["message_count"] = len(uid_values)
                unseen_typ, unseen_data = mail.uid("search", None, "UNSEEN")
                if unseen_typ == "OK":
                    entry["unseen_count"] = len(unseen_data[0].split()) if unseen_data and unseen_data[0] else 0
                metadata: dict[str, dict[str, Any]] = {}
                missing_uids = [uid for uid in uid_values if (folder, uid) not in local_keys]
                metadata_targets = list(dict.fromkeys(
                    ([uid_values[0], uid_values[-1]] if uid_values else []) + missing_uids
                ))
                for start in range(0, len(metadata_targets), 100):
                    uid_chunk = ",".join(metadata_targets[start:start + 100])
                    if not uid_chunk:
                        continue
                    fetch_typ, fetch_data = mail.uid(
                        "fetch",
                        uid_chunk,
                        "(UID INTERNALDATE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])",
                    )
                    if fetch_typ == "OK":
                        metadata.update(_parse_fetch_metadata(fetch_data))
                entry["uids"] = {uid: metadata.get(uid, {"message_id": "", "internal_date": None}) for uid in uid_values}
                if uid_values:
                    entry["date_from"] = (metadata.get(uid_values[0]) or {}).get("internal_date")
                    entry["date_to"] = (metadata.get(uid_values[-1]) or {}).get("internal_date")
            except Exception as exc:
                entry["error"] = str(exc)
            folders[folder] = entry
    finally:
        try:
            mail.logout()
        except Exception:
            pass
    return {"account": account, "folders": folders, "configured_folders": sorted(configured)}


def reconcile_snapshots(
    local_rows: list[dict[str, Any]],
    failures: dict[tuple[str, str], dict[str, Any]],
    server_snapshot: dict[str, Any],
) -> dict[str, Any]:
    folders = server_snapshot.get("folders") or {}
    local_by_key = {(str(row.get("mailbox") or ""), str(row.get("uid") or "")): row for row in local_rows}
    local_by_message_id: dict[str, list[dict[str, Any]]] = {}
    for row in local_rows:
        message_id = str(row.get("message_id") or "").strip().lower()
        if message_id:
            local_by_message_id.setdefault(message_id, []).append(row)
    server_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    server_by_message_id: dict[str, list[dict[str, Any]]] = {}
    server_rows: list[dict[str, Any]] = []
    missing_rows: list[dict[str, Any]] = []
    duplicate_rows: list[dict[str, Any]] = []
    by_folder: list[dict[str, Any]] = []

    for folder, folder_data in folders.items():
        folder_counts: Counter[str] = Counter()
        for uid, metadata in (folder_data.get("uids") or {}).items():
            key = (str(folder), str(uid))
            server_record = {
                "account": folder_data.get("account") or server_snapshot.get("account"),
                "folder": folder,
                "uidvalidity": folder_data.get("uidvalidity"),
                "uid": str(uid),
                "message_id": str(metadata.get("message_id") or "").strip().lower(),
                "internal_date": metadata.get("internal_date"),
            }
            server_by_key[key] = server_record
            if server_record["message_id"]:
                server_by_message_id.setdefault(server_record["message_id"], []).append(server_record)
            local = local_by_key.get(key)
            failure = failures.get(key)
            if local:
                if (
                    server_record["message_id"]
                    and local.get("message_id")
                    and server_record["message_id"] != str(local.get("message_id")).strip().lower()
                ):
                    status = "unknown"
                    reason = "uid_reused_suspected_message_id_mismatch"
                elif local.get("duplicate_of_raw_email_id"):
                    status = "imported_duplicate_linked"
                    reason = "local raw exists and links to original raw"
                else:
                    status = "imported_raw"
                    reason = "exact folder+uid exists locally"
            elif failure and str(failure.get("status") or "") in {"skipped", "ignored"}:
                status = "skipped_before_start"
                reason = "recorded import skip"
            elif failure:
                status = "fetch_failed"
                reason = "recorded import failure"
            elif server_record["message_id"] and server_record["message_id"] in local_by_message_id:
                status = "exact_duplicate_known"
                reason = "same Message-ID exists locally under another server identity"
            else:
                status = "missing_local"
                reason = "server UID has no local raw or recorded failure"
            row = {**server_record, "local_status": status, "reason": reason, "failure": failure}
            if local:
                row["local_raw"] = {
                    "id": local.get("id"),
                    "mailbox": local.get("mailbox"),
                    "uid": local.get("uid"),
                    "message_id": local.get("message_id"),
                    "raw_hash": local.get("raw_hash"),
                    "duplicate_of_raw_email_id": local.get("duplicate_of_raw_email_id"),
                    "status": local.get("status"),
                }
            if status in {"missing_local", "fetch_failed", "skipped_before_start", "unknown"}:
                missing_rows.append(row)
            if status in {"imported_duplicate_linked", "exact_duplicate_known"}:
                duplicate_rows.append(row)
            folder_counts[status] += 1
            server_rows.append(row)
        by_folder.append({
            "account": folder_data.get("account") or server_snapshot.get("account"),
            "folder": folder,
            "display_folder": folder_data.get("display_folder") or folder,
            "uidvalidity": folder_data.get("uidvalidity"),
            "server_total": int(folder_data.get("message_count") or len(folder_data.get("uids") or {})),
            "unseen_count": folder_data.get("unseen_count"),
            "date_from": folder_data.get("date_from"),
            "date_to": folder_data.get("date_to"),
            "server_error": folder_data.get("error"),
            "by_local_status": dict(folder_counts),
        })

    local_without_server: list[dict[str, Any]] = []
    local_server_statuses: Counter[str] = Counter()
    for local in local_rows:
        folder = str(local.get("mailbox") or "")
        uid = str(local.get("uid") or "")
        key = (folder, uid)
        if not uid:
            status = "no_uid"
        elif key in server_by_key:
            server_message_id = server_by_key[key].get("message_id")
            local_message_id = str(local.get("message_id") or "").strip().lower()
            status = "uid_reused_suspected" if server_message_id and local_message_id and server_message_id != local_message_id else "exists_on_server"
        else:
            local_message_id = str(local.get("message_id") or "").strip().lower()
            if local_message_id and local_message_id in server_by_message_id:
                status = "folder_mismatch"
            elif folder not in folders:
                status = "folder_mismatch"
            else:
                status = "deleted_or_moved_on_server"
        local_server_statuses[status] += 1
        if status != "exists_on_server":
            local_without_server.append({
                "account": local.get("account"),
                "raw_email_id": local.get("id"),
                "folder": folder,
                "uid": uid,
                "message_id": local.get("message_id"),
                "raw_hash": local.get("raw_hash"),
                "duplicate_of_raw_email_id": local.get("duplicate_of_raw_email_id"),
                "local_status": local.get("status"),
                "server_status": status,
            })

    server_statuses = Counter(row["local_status"] for row in server_rows)
    server_total = len(server_rows)
    explained_total = sum(server_statuses.values())
    summary = {
        "test_status": "ok" if server_total and explained_total == server_total else "failed",
        "checked_at": utcnow(),
        "account": server_snapshot.get("account"),
        "server_total": server_total,
        "explained_server_uid_total": explained_total,
        "all_server_uids_explained": server_total == explained_total,
        "local_raw_total": len(local_rows),
        "local_unique_server_identity_total": len(local_by_key),
        "duplicate_linked_total": sum(bool(row.get("duplicate_of_raw_email_id")) for row in local_rows),
        "missing_local_total": server_statuses["missing_local"],
        "local_without_server_total": len(local_without_server),
        "fetch_failed_total": server_statuses["fetch_failed"],
        "skipped_before_start_total": server_statuses["skipped_before_start"],
        "exact_duplicate_known_total": server_statuses["exact_duplicate_known"],
        "by_server_uid_status": dict(server_statuses),
        "by_local_server_status": dict(local_server_statuses),
        "by_folder": by_folder,
    }
    return {
        "summary": summary,
        "server_rows": server_rows,
        "missing_rows": missing_rows,
        "local_without_server": local_without_server,
        "duplicate_rows": duplicate_rows,
    }


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_reports(result: dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = result["summary"]
    (out_dir / "imap_reconcile_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _write_jsonl(out_dir / "imap_reconcile_missing_server_uids.jsonl", result["missing_rows"])
    _write_jsonl(out_dir / "imap_reconcile_local_without_server.jsonl", result["local_without_server"])
    _write_jsonl(out_dir / "imap_reconcile_duplicates.jsonl", result["duplicate_rows"])
    lines = [
        "# IMAP Count Reconciliation", "",
        f"- Checked: {summary['checked_at']}",
        f"- Account: `{summary.get('account') or ''}`",
        f"- Server total: **{summary['server_total']}**",
        f"- Explained server UIDs: **{summary['explained_server_uid_total']}**",
        f"- Local raw total: **{summary['local_raw_total']}**",
        f"- Local unique server identities: **{summary['local_unique_server_identity_total']}**",
        f"- Linked duplicates: **{summary['duplicate_linked_total']}**",
        f"- Missing local: **{summary['missing_local_total']}**",
        f"- Local without server: **{summary['local_without_server_total']}**",
        f"- Fetch failed: **{summary['fetch_failed_total']}**",
        f"- Skipped before start: **{summary['skipped_before_start_total']}**",
        "",
        "## Server UID statuses", "",
    ]
    lines.extend(f"- {key}: {value}" for key, value in summary["by_server_uid_status"].items())
    lines.extend([
        "", "## By folder", "",
        "| Folder | UIDVALIDITY | Server | Unseen | Imported | Linked | Missing | Failed |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for folder in summary["by_folder"]:
        statuses = folder.get("by_local_status") or {}
        lines.append(
            f"| `{folder['display_folder']}` | {folder.get('uidvalidity') or ''} | "
            f"{folder['server_total']} | {folder.get('unseen_count')} | "
            f"{statuses.get('imported_raw', 0)} | {statuses.get('imported_duplicate_linked', 0)} | "
            f"{statuses.get('missing_local', 0)} | {statuses.get('fetch_failed', 0)} |"
        )
    if summary["missing_local_total"] or summary["fetch_failed_total"]:
        lines.extend([
            "", "## Attention", "",
            "Есть UID на сервере без успешной локальной raw-записи. Смотрите `imap_reconcile_missing_server_uids.jsonl`.",
        ])
    elif not summary["all_server_uids_explained"]:
        lines.extend(["", "## Attention", "", "Не все серверные UID получили объяснимый статус."])
    else:
        lines.extend(["", "## Result", "", "Каждый серверный UID получил конкретный статус."])
    (out_dir / "imap_reconcile_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Strict read-only IMAP UID reconciliation")
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    if not args.database.exists():
        parser.error(f"database not found: {args.database}")
    with open_ro(args.database) as con:
        local_rows, failures = load_local(con, "")
    server = collect_server_snapshot(args.database, local_rows)
    for row in local_rows:
        row["account"] = server.get("account") or ""
    result = reconcile_snapshots(local_rows, failures, server)
    write_reports(result, args.out_dir)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    return 0 if result["summary"]["test_status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
