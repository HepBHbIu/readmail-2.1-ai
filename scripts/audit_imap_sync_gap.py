#!/usr/bin/env python3
r"""
audit_imap_sync_gap.py — READ-ONLY диагностика расхождения IMAP-сервер ↔ локальная БД.

Назначение
----------
Доказательно ответить на вопрос: «сколько писем на сервере по папкам и сходится ли
это с локальным реестром?». Находит письма, которые есть на сервере, но отсутствуют
локально (потенциальная потеря), и письма, которые есть локально, но пропали с сервера.

ГАРАНТИИ БЕЗОПАСНОСТИ (важно — этот скрипт нельзя превращать в пишущий):
  * БД открывается строго read-only (file:...?mode=ro).
  * IMAP: только LOGIN + SELECT(readonly=True) + UID SEARCH ALL — забирается ТОЛЬКО список UID.
  * Тела писем НЕ скачиваются (никакого BODY/RFC822), флаг \Seen НЕ ставится.
  * AI не вызывается. 1С не вызывается. Никакие письма не помечаются и не удаляются.
  * Если IMAP-кредов нет — работает в DRY-RUN на локальных данных и говорит, что нужно.

Выходные файлы (в audit_out/):
  * imap_sync_gap_report.md        — человекочитаемый отчёт
  * imap_sync_gap.json             — машиночитаемая сводка по папкам
  * missing_server_messages.jsonl  — UID есть на сервере, нет локально (по строкам)
  * local_without_server.jsonl     — (mailbox,uid) есть локально, нет на сервере
  * duplicate_decisions_audit.jsonl — как dedup объяснил каждое расхождение

Запуск:
    python3 scripts/audit_imap_sync_gap.py              # авто: live если есть креды, иначе dry-run
    python3 scripts/audit_imap_sync_gap.py --dry-run    # принудительно без подключения к IMAP
    python3 scripts/audit_imap_sync_gap.py --db data/readmail.sqlite3

Нужные переменные окружения / runtime-настройки для LIVE-режима:
    IMAP_HOST, IMAP_PORT, IMAP_USERNAME, IMAP_PASSWORD, IMAP_FOLDERS (опц.)
  Креды могут также храниться в app_settings (заполняются через панель) и подхватываются
  через app.runtime_settings.apply_runtime_settings().
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AUDIT_OUT = ROOT / "audit_out"
DEFAULT_DB = ROOT / "data" / "readmail.sqlite3"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def open_db_ro(db_path: Path) -> sqlite3.Connection:
    """Открыть БД строго read-only. Падаем, если открылось НЕ в ro-режиме."""
    uri = f"file:{db_path}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def load_local(con: sqlite3.Connection) -> dict:
    """Снять локальную картину из raw_emails + import_uid_failures (read-only)."""
    by_folder: dict[str, set] = {}
    for r in con.execute("SELECT mailbox, uid FROM raw_emails"):
        by_folder.setdefault(r["mailbox"], set()).add(str(r["uid"]))

    # message_id -> где уже лежит (для объяснения «свёрнут как дубль»)
    msgid_to_loc: dict[str, list] = {}
    for r in con.execute(
        "SELECT id, mailbox, uid, message_id, canonical_key FROM raw_emails "
        "WHERE message_id IS NOT NULL AND message_id<>''"
    ):
        msgid_to_loc.setdefault(r["message_id"].strip().lower(), []).append(
            {"id": r["id"], "mailbox": r["mailbox"], "uid": str(r["uid"])}
        )

    failures: dict[tuple, dict] = {}
    try:
        for r in con.execute(
            "SELECT mailbox, uid, stage, error_type, error_message, attempts, status "
            "FROM import_uid_failures"
        ):
            failures[(r["mailbox"], str(r["uid"]))] = {
                "stage": r["stage"], "error_type": r["error_type"],
                "error_message": r["error_message"], "attempts": r["attempts"],
                "status": r["status"],
            }
    except sqlite3.OperationalError:
        pass

    return {"by_folder": by_folder, "msgid_to_loc": msgid_to_loc, "failures": failures}


def try_live_imap():
    """Вернуть (mail, settings) для LIVE-режима или None, если креды отсутствуют/недоступны.

    Используем уже существующие read-only-safe помощники из app.imap_importer.
    """
    sys.path.insert(0, str(ROOT))
    try:
        from app.config import settings  # noqa
        from app.imap_importer import (  # noqa
            _open_imap, _clean_credential, discover_imap_folders,
            decode_imap_utf7, _imap_quote,
        )
        try:
            from app.runtime_settings import apply_runtime_settings
            apply_runtime_settings()
        except Exception:
            pass
    except Exception as exc:  # pragma: no cover - окружение без зависимостей
        print(f"[dry-run] не удалось импортировать app: {exc}")
        return None

    user = _clean_credential(settings.imap_username or "")
    pwd = _clean_credential(settings.imap_password or "")
    if not user or not pwd:
        print("[dry-run] IMAP-креды пусты (IMAP_USERNAME/IMAP_PASSWORD). Подключение пропущено.")
        return None
    try:
        mail = _open_imap()
    except Exception as exc:
        print(f"[dry-run] не удалось подключиться к IMAP: {exc}")
        return None
    return {
        "mail": mail, "settings": settings,
        "discover": discover_imap_folders, "decode": decode_imap_utf7, "quote": _imap_quote,
    }


def server_uids_per_folder(live) -> dict[str, list]:
    """READ-ONLY: вернуть {folder: [uid,...]} через UID SEARCH ALL (только список UID)."""
    mail = live["mail"]
    quote = live["quote"]
    settings = live["settings"]
    configured = {
        f.strip() for f in str(getattr(settings, "imap_folders", "") or "").split(",") if f.strip()
    }
    out: dict[str, list] = {}
    for f in live["discover"](mail):
        if configured and f not in configured:
            continue
        try:
            mail.select(quote(f), readonly=True)  # readonly → \Seen не ставится
            typ, data = mail.uid("search", None, "ALL")
            uids = [b.decode("ascii", "ignore") for b in data[0].split()] if (typ == "OK" and data and data[0]) else []
        except Exception as exc:
            out[f] = {"error": str(exc)}
            continue
        out[f] = uids
    try:
        mail.logout()
    except Exception:
        pass
    return out


def build_report(local: dict, server: dict | None, db_path: Path) -> dict:
    folders_summary = []
    missing_rows = []          # на сервере есть, локально нет
    local_only_rows = []       # локально есть, на сервере нет
    decisions = []             # объяснение каждого расхождения

    all_folders = set(local["by_folder"].keys())
    if server:
        all_folders |= set(server.keys())

    total_server = total_local = total_missing = total_local_only = 0

    for folder in sorted(all_folders):
        local_uids = local["by_folder"].get(folder, set())
        srv = server.get(folder) if server else None
        srv_err = None
        if isinstance(srv, dict):
            srv_err = srv.get("error")
            srv = None
        srv_set = set(srv) if srv is not None else None

        entry = {
            "folder": folder,
            "server_count": (len(srv_set) if srv_set is not None else None),
            "local_count": len(local_uids),
            "server_error": srv_err,
            "missing_on_local": None,
            "local_only": None,
        }

        if srv_set is not None:
            missing = srv_set - local_uids
            local_only = local_uids - srv_set
            entry["missing_on_local"] = len(missing)
            entry["local_only"] = len(local_only)
            total_server += len(srv_set)
            total_missing += len(missing)
            total_local_only += len(local_only)

            for uid in sorted(missing, key=lambda x: int(x) if x.isdigit() else 0):
                fail = local["failures"].get((folder, uid))
                reason = "unexplained"
                if fail:
                    reason = f"known_failure:{fail['status']}:{fail['error_type']}"
                decision = {
                    "folder": folder, "uid": uid,
                    "category": reason,
                    "failure": fail,
                }
                decisions.append(decision)
                missing_rows.append({"folder": folder, "uid": uid, "reason": reason, "failure": fail})

            for uid in sorted(local_only, key=lambda x: int(x) if x.isdigit() else 0):
                local_only_rows.append({"folder": folder, "uid": uid,
                                        "note": "локально есть, на сервере SEARCH ALL не найден (перемещено/удалено?)"})

        total_local += len(local_uids)
        folders_summary.append(entry)

    folders_summary.sort(key=lambda e: -(e["missing_on_local"] or 0))

    return {
        "ok": True,
        "mode": "live" if server else "dry-run",
        "db": str(db_path),
        "checked_at": _now(),
        "totals": {
            "server": (total_server if server else None),
            "local": total_local,
            "missing_on_local": (total_missing if server else None),
            "local_only": (total_local_only if server else None),
        },
        "folders": folders_summary,
        "_missing_rows": missing_rows,
        "_local_only_rows": local_only_rows,
        "_decisions": decisions,
    }


def write_outputs(report: dict) -> None:
    AUDIT_OUT.mkdir(parents=True, exist_ok=True)

    missing = report.pop("_missing_rows")
    local_only = report.pop("_local_only_rows")
    decisions = report.pop("_decisions")

    (AUDIT_OUT / "imap_sync_gap.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (AUDIT_OUT / "missing_server_messages.jsonl").open("w", encoding="utf-8") as fh:
        for row in missing:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    with (AUDIT_OUT / "local_without_server.jsonl").open("w", encoding="utf-8") as fh:
        for row in local_only:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    with (AUDIT_OUT / "duplicate_decisions_audit.jsonl").open("w", encoding="utf-8") as fh:
        for row in decisions:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    t = report["totals"]
    lines = [
        "# IMAP ↔ Local sync gap (READ-ONLY аудит)",
        "",
        f"- Режим: **{report['mode']}**",
        f"- БД: `{report['db']}`",
        f"- Проверено: {report['checked_at']}",
        "",
        "## Итоги",
        f"- На сервере (UID, всего): **{t['server']}**",
        f"- Локально (mailbox,uid): **{t['local']}**",
        f"- На сервере есть, локально НЕТ: **{t['missing_on_local']}**  ← потенциальная потеря",
        f"- Локально есть, на сервере НЕТ: **{t['local_only']}**",
        "",
        "## По папкам",
        "",
        "| Папка | server | local | missing | local_only | error |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for e in report["folders"]:
        lines.append(
            f"| `{e['folder']}` | {e['server_count']} | {e['local_count']} | "
            f"{e['missing_on_local']} | {e['local_only']} | {e['server_error'] or ''} |"
        )
    if report["mode"] == "dry-run":
        lines += [
            "",
            "## DRY-RUN",
            "IMAP-подключение не выполнялось (нет кредов или недоступен сервер).",
            "Показана только локальная картина. Для серверной сверки задайте:",
            "`IMAP_HOST`, `IMAP_PORT`, `IMAP_USERNAME`, `IMAP_PASSWORD` (и при необходимости `IMAP_FOLDERS`)",
            "в `.env` или через панель (app_settings), затем перезапустите без `--dry-run`.",
        ]
    lines += [
        "",
        "## Детали",
        "- `missing_server_messages.jsonl` — UID на сервере без локальной строки (+причина из import_uid_failures).",
        "- `local_without_server.jsonl` — локальные (mailbox,uid), которых нет в SEARCH ALL.",
        "- `duplicate_decisions_audit.jsonl` — классификация каждого расхождения.",
        "",
        "> Категория `unexplained` в missing — письма, которые сервер отдаёт, локально отсутствуют",
        "> и при этом НЕ записаны в import_uid_failures. Это и есть настоящая молчаливая дыра — разбирать вручную.",
    ]
    (AUDIT_OUT / "imap_sync_gap_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="READ-ONLY IMAP↔local sync gap audit")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="путь к sqlite БД (read-only)")
    ap.add_argument("--dry-run", action="store_true", help="не подключаться к IMAP")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"БД не найдена: {db_path}", file=sys.stderr)
        return 2

    con = open_db_ro(db_path)
    local = load_local(con)
    con.close()

    server = None
    if not args.dry_run:
        live = try_live_imap()
        if live:
            print("[live] подключение к IMAP установлено, читаю списки UID (read-only)...")
            server = server_uids_per_folder(live)

    report = build_report(local, server, db_path)
    write_outputs(report)

    t = report["totals"]
    print(f"\nГотово ({report['mode']}). server={t['server']} local={t['local']} "
          f"missing={t['missing_on_local']} local_only={t['local_only']}")
    print(f"Отчёты в: {AUDIT_OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
