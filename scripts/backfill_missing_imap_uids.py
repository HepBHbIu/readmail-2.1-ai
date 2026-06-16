#!/usr/bin/env python3
r"""
backfill_missing_imap_uids.py — точечная дозагрузка конкретных IMAP UID.

Назначение
----------
Дозагрузить только указанные (folder, uid), которые reconcile пометил как `missing_local`
или `fetch_failed`/`quarantine`, НЕ запуская массовый импорт. Уважает существующую
save+link dedup-логику (`app.db.upsert_email`). Stage 2 (создание кейсов) НЕ запускается,
outbox/AI/1С НЕ трогаются.

Безопасность (жёсткие гарантии)
-------------------------------
  * IMAP: SELECT(readonly=True) + UID SEARCH + FETCH через BODY.PEEK[...] — флаг \Seen НЕ ставится.
  * dry-run: только проверка существования UID + UIDVALIDITY + заголовки (BODY.PEEK[HEADER]).
            Письмо НЕ скачивается целиком и НЕ сохраняется.
  * apply: FETCH BODY.PEEK[] → parse → upsert_email (raw + attachments). НИКАКИХ case/outbox/AI/1С.
  * Для нечитаемого UID (TLS EOF и т.п.): reconnect + ограниченный retry; при неудаче — остаётся
    в quarantine с ясной причиной (next_retry_at/attempts/last_error/message_id). Без raw = НЕ imported.

Примеры
-------
  python3 scripts/backfill_missing_imap_uids.py --from-missing audit_out/imap_reconcile_missing_server_uids.jsonl --dry-run
  python3 scripts/backfill_missing_imap_uids.py --uid "&BCIENQRBBEI- Berru|avtoto.ru:1043" --apply
  python3 scripts/backfill_missing_imap_uids.py --from-missing audit_out/imap_reconcile_missing_server_uids.jsonl --include-quarantine --apply
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.parser import BytesHeaderParser
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_OUT = ROOT / "audit_out"
MISSING_DEFAULT = DEFAULT_OUT / "imap_reconcile_missing_server_uids.jsonl"

# Сколько раз пытаться вытащить «битый» UID (TLS EOF) с переподключением.
FULL_FETCH_RETRIES = 4
RETRY_BACKOFF_HOURS = 6


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_target(token: str) -> tuple[str, str]:
    """'folder:uid' → (folder, uid). UID — числовой хвост после последнего ':'."""
    token = token.strip()
    if ":" not in token:
        raise ValueError(f"bad --uid '{token}', expected 'folder:uid'")
    folder, uid = token.rsplit(":", 1)
    folder, uid = folder.strip(), uid.strip()
    if not folder or not uid:
        raise ValueError(f"bad --uid '{token}', empty folder or uid")
    return folder, uid


def _load_targets(args: argparse.Namespace) -> list[dict[str, Any]]:
    targets: dict[tuple[str, str], dict[str, Any]] = {}
    for token in args.uid or []:
        folder, uid = _parse_target(token)
        targets[(folder, uid)] = {"folder": folder, "uid": uid, "source": "cli", "local_status": None}
    if args.from_missing:
        path = Path(args.from_missing)
        if not path.exists():
            raise FileNotFoundError(f"--from-missing file not found: {path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            folder = str(row.get("folder") or "")
            uid = str(row.get("uid") or "")
            status = str(row.get("local_status") or "")
            if not folder or not uid:
                continue
            if status in {"fetch_failed", "quarantine", "quarantined"} and not args.include_quarantine:
                # пропускаем «битые», если не попросили явно
                continue
            targets.setdefault((folder, uid), {
                "folder": folder, "uid": uid, "source": "from_missing",
                "local_status": status,
                "expected_message_id": row.get("message_id"),
                "uidvalidity": row.get("uidvalidity"),
            })
    return list(targets.values())


def _response_number(mail, name: str) -> str | None:
    try:
        _typ, values = mail.response(name)
        if values and values[0]:
            value = values[0].decode("ascii", "ignore") if isinstance(values[0], bytes) else str(values[0])
            m = re.search(r"\d+", value)
            return m.group(0) if m else None
    except Exception:
        pass
    return None


def _header_message_id(mail, uid: str) -> str:
    """BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)] — заголовок без снятия \\Seen."""
    try:
        typ, data = mail.uid("fetch", uid, "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
        if typ != "OK":
            return ""
        parser = BytesHeaderParser()
        for part in data or []:
            if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], bytes):
                mid = str(parser.parsebytes(part[1]).get("Message-ID") or "").strip()
                if mid:
                    return mid
    except Exception:
        pass
    return ""


def _bodystructure(mail, uid: str) -> str:
    try:
        typ, data = mail.uid("fetch", uid, "(BODYSTRUCTURE)")
        if typ == "OK" and data:
            return " ".join(
                p.decode("ascii", "replace") if isinstance(p, bytes) else str(p)
                for p in data if p
            )[:500]
    except Exception:
        pass
    return ""


def _uid_exists(mail, uid: str) -> bool:
    try:
        typ, data = mail.uid("search", None, f"UID {uid}")
        if typ == "OK" and data and data[0]:
            return uid.encode() in data[0].split()
    except Exception:
        pass
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Targeted IMAP UID backfill (BODY.PEEK, no Seen)")
    ap.add_argument("--uid", action="append", help="folder:uid (можно несколько раз)")
    ap.add_argument("--from-missing", default=None,
                    help=f"jsonl от reconcile (default при отсутствии --uid: {MISSING_DEFAULT})")
    ap.add_argument("--include-quarantine", action="store_true",
                    help="включить UID со статусом fetch_failed/quarantine")
    ap.add_argument("--dry-run", action="store_true", help="только проверка, без сохранения (default)")
    ap.add_argument("--apply", action="store_true", help="реально дозагрузить и сохранить raw")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--database", default=None, help="путь к sqlite (default: data/readmail.sqlite3)")
    args = ap.parse_args()

    apply = bool(args.apply)
    dry_run = not apply  # apply имеет приоритет; иначе всегда dry-run
    if not args.uid and not args.from_missing:
        args.from_missing = str(MISSING_DEFAULT)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    targets = _load_targets(args)

    # Ленивая загрузка app.* — чтобы --help работал без зависимостей.
    from app.config import settings
    from app.runtime_settings import apply_runtime_settings
    from app.imap_importer import _open_imap, _imap_quote, _clean_credential, fetch_one_uid, _save_raw
    from app.email_parser import parse_email_bytes
    from app.db import connect, init_db, upsert_email, record_uid_failure, utcnow

    # Указать локальную БД ДО connect() — иначе settings.database_path = '/app/data' (Docker).
    db_path = Path(args.database) if args.database else (ROOT / "data" / "readmail.sqlite3")
    if not db_path.exists():
        raise FileNotFoundError(f"database not found: {db_path}")
    settings.database_path = db_path

    apply_runtime_settings()
    settings.database_path = db_path  # apply_runtime_settings мог перезаписать из app_settings
    if apply:
        init_db()  # гарантируем поля карантина (idempotent)

    account = _clean_credential(settings.imap_username or "")
    if not account or not _clean_credential(settings.imap_password or ""):
        report = {"ok": False, "error": "IMAP credentials are not configured",
                  "mode": "apply" if apply else "dry-run", "targets": len(targets)}
        (out_dir / "imap_backfill_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2

    attempts_log: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []

    def reconnect():
        return _open_imap()

    mail = reconnect()
    try:
        # группируем по папке, чтобы не пере-SELECT-ить лишний раз
        targets.sort(key=lambda t: (t["folder"], int(t["uid"]) if str(t["uid"]).isdigit() else 0))
        current_folder = None
        uidvalidity = None
        for tgt in targets:
            folder, uid = tgt["folder"], str(tgt["uid"])
            entry: dict[str, Any] = {
                "folder": folder, "uid": uid, "source": tgt.get("source"),
                "prior_status": tgt.get("local_status"),
            }
            try:
                if folder != current_folder:
                    typ, _ = mail.select(_imap_quote(folder), readonly=True)
                    if typ != "OK":
                        typ, _ = mail.select(folder, readonly=True)
                    if typ != "OK":
                        entry.update({"action": "skip", "result": "select_failed", "detail": typ})
                        results.append(entry)
                        continue
                    current_folder = folder
                    uidvalidity = _response_number(mail, "UIDVALIDITY")
                entry["uidvalidity"] = uidvalidity

                exists = _uid_exists(mail, uid)
                entry["exists_on_server"] = exists
                if not exists:
                    entry.update({"action": "skip", "result": "uid_not_on_server"})
                    results.append(entry)
                    continue

                header_mid = _header_message_id(mail, uid)
                entry["message_id"] = header_mid

                if dry_run:
                    entry.update({"action": "dry_run", "result": "would_fetch_and_upsert",
                                  "would_full_fetch": "BODY.PEEK[]"})
                    results.append(entry)
                    attempts_log.append({"ts": _now(), "folder": folder, "uid": uid,
                                         "mode": "dry-run", "exists": exists, "message_id": header_mid})
                    continue

                # ── APPLY: полный fetch с reconnect-retry ──
                raw = None
                last_error = ""
                for attempt in range(1, FULL_FETCH_RETRIES + 1):
                    try:
                        raw = fetch_one_uid(mail, uid.encode("ascii"))
                        if raw:
                            attempts_log.append({"ts": _now(), "folder": folder, "uid": uid,
                                                 "attempt": attempt, "result": "fetched", "bytes": len(raw)})
                            break
                    except Exception as exc:
                        last_error = f"{type(exc).__name__}: {exc}"
                        attempts_log.append({"ts": _now(), "folder": folder, "uid": uid,
                                             "attempt": attempt, "result": "error", "error": last_error})
                        # reconnect + reselect перед следующей попыткой
                        try:
                            mail.logout()
                        except Exception:
                            pass
                        time.sleep(min(2 * attempt, 6))
                        try:
                            mail = reconnect()
                            mail.select(_imap_quote(folder), readonly=True)
                            current_folder = folder
                            uidvalidity = _response_number(mail, "UIDVALIDITY")
                        except Exception as rexc:
                            last_error = f"reconnect_failed: {type(rexc).__name__}: {rexc}"

                if not raw:
                    # Не удалось — оставить/обновить quarantine с ясной причиной. Без raw = НЕ imported.
                    bodystructure = _bodystructure(mail, uid)
                    next_retry = (datetime.now(timezone.utc) + timedelta(hours=RETRY_BACKOFF_HOURS)).replace(microsecond=0).isoformat()
                    with connect() as con:
                        record_uid_failure(
                            con, account=account, mailbox=folder, uid=uid, stage="fetch_single",
                            error_type="abort", error_message=last_error or "full fetch failed",
                            uidvalidity=uidvalidity, message_id=header_mid or None,
                            recoverable=True, next_retry_at=next_retry,
                        )
                    entry.update({
                        "action": "quarantine", "result": "fetch_failed_kept_quarantine",
                        "last_error": last_error, "next_retry_at": next_retry,
                        "bodystructure": bodystructure, "recoverable": True,
                    })
                    results.append(entry)
                    continue

                # ── успех: сохранить raw через upsert_email (save+link), без case/outbox/AI ──
                raw_path = _save_raw(folder, uid, raw)
                email_data = parse_email_bytes(raw, mailbox=folder, uid=uid, raw_path=raw_path)
                email_data["raw_size"] = len(raw)
                with connect() as con:
                    rid, created = upsert_email(con, email_data)
                    row = con.execute(
                        "SELECT mailbox, uid, duplicate_of_raw_email_id FROM raw_emails WHERE id=?", (rid,)
                    ).fetchone()
                    dup_of = row["duplicate_of_raw_email_id"] if row else None
                    same_identity = bool(row and str(row["mailbox"]) == folder and str(row["uid"]) == uid)
                    # снять прежний failure для этого UID
                    con.execute(
                        "UPDATE import_uid_failures SET status='resolved', last_seen_at=? WHERE mailbox=? AND uid=?",
                        (utcnow(), folder, uid),
                    )
                if not same_identity:
                    # created=False и строка принадлежит ДРУГОМУ (folder,uid) → байт-идентичная копия.
                    status = "exact_duplicate_known"
                elif dup_of:
                    status = "imported_duplicate_linked"
                elif created:
                    status = "imported_raw"
                else:
                    status = "already_present_raw"
                entry.update({"action": "imported", "result": status, "raw_email_id": rid,
                              "duplicate_of_raw_email_id": dup_of, "bytes": len(raw)})
                results.append(entry)
            except Exception as exc:
                entry.update({"action": "error", "result": f"{type(exc).__name__}: {exc}"})
                results.append(entry)
    finally:
        try:
            mail.logout()
        except Exception:
            pass

    # ── отчёты ──
    by_result: dict[str, int] = {}
    for r in results:
        by_result[r.get("result", "?")] = by_result.get(r.get("result", "?"), 0) + 1
    report = {
        "ok": True,
        "mode": "apply" if apply else "dry-run",
        "account": account,
        "checked_at": _now(),
        "targets_total": len(targets),
        "by_result": by_result,
        "results": results,
    }
    (out_dir / "imap_backfill_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with (out_dir / "imap_backfill_attempts.jsonl").open("w", encoding="utf-8") as fh:
        for row in attempts_log:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    lines = [
        "# IMAP targeted backfill", "",
        f"- Режим: **{report['mode']}**",
        f"- Аккаунт: `{account}`",
        f"- Время: {report['checked_at']}",
        f"- Целей: **{len(targets)}**",
        "", "## Результаты по статусам", "",
    ]
    lines += [f"- {k}: {v}" for k, v in by_result.items()]
    lines += ["", "## По каждому UID", "",
              "| Folder | UID | exists | action | result | raw_email_id | message_id |",
              "|---|---|:--:|---|---|---:|---|"]
    for r in results:
        lines.append(
            f"| `{r['folder']}` | {r['uid']} | {r.get('exists_on_server','')} | "
            f"{r.get('action','')} | {r.get('result','')} | {r.get('raw_email_id','')} | "
            f"{(r.get('message_id') or '')[:60]} |"
        )
    if dry_run:
        lines += ["", "> DRY-RUN: ничего не сохранено. Для реальной дозагрузки запустите с `--apply`."]
    (out_dir / "imap_backfill_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(json.dumps({"mode": report["mode"], "targets": len(targets), "by_result": by_result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
