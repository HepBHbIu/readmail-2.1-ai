from __future__ import annotations

import imaplib
import re
import uuid
from pathlib import Path
from typing import Any

from .classifier import apply_ai_overlay, classify_email, force_operator_review, load_buyer_rules
from .config import settings
from .db import (
    connect,
    create_import_job,
    dumps,
    finish_import_job,
    get_import_job_status,
    load_buyer_identities,
    queue_case_event,
    record_import_error,
    record_uid_failure,
    save_case,
    update_import_job_heartbeat,
    upsert_email,
    utcnow,
)
from .email_parser import parse_email_bytes, parse_eml_file

# ── Конфигурация по умолчанию ─────────────────────────────────────────
# Настройки переопределяются через settings.imap_batch_size, settings.imap_timeout_seconds и т.д.
def _max_raw_bytes() -> int:
    """Return max raw email size in bytes from settings."""
    mb = int(getattr(settings, "imap_max_raw_email_mb", 25) or 25)
    return max(1, mb) * 1024 * 1024


_IMAP_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _imap_date(value: object, *, plus_day: bool = False) -> str | None:
    """ISO/датавремя → формат IMAP 'DD-Mon-YYYY' (англ. месяц). plus_day — для «до» включительно."""
    from datetime import datetime, timedelta
    s = str(value or "").strip()
    if not s:
        return None
    dt = None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            dt = datetime.strptime(s, fmt)
            break
        except ValueError:
            dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(s)
        except Exception:
            return None
    if plus_day:
        dt = dt + timedelta(days=1)  # IMAP BEFORE строгий → «до» включительно = следующий день
    return f"{dt.day:02d}-{_IMAP_MONTHS[dt.month - 1]}-{dt.year}"


def _apply_date_filter(search: str) -> str:
    """Добавить SINCE/BEFORE по настройкам периода. Галочка «от» → SINCE, «до» → BEFORE.
    Обе → промежуток; одна → открытый интервал; ни одной → как было."""
    parts: list[str] = []
    if getattr(settings, "imap_date_from_enabled", False):
        d = _imap_date(getattr(settings, "imap_date_from", ""))
        if d:
            parts.append(f"SINCE {d}")
    if getattr(settings, "imap_date_to_enabled", False):
        d = _imap_date(getattr(settings, "imap_date_to", ""), plus_day=True)
        if d:
            parts.append(f"BEFORE {d}")
    if not parts:
        return search
    base = (search or "ALL").strip()
    if base.upper() == "ALL":
        base = ""  # SINCE/BEFORE сами выбирают за период, ALL не нужен
    return (" ".join(parts) + ((" " + base) if base else "")).strip()


def _default_search(search: str | None) -> str:
    """Resolve runtime import mode to an IMAP SEARCH expression (+ фильтр периода дат)."""
    if search and str(search).strip():
        base = str(search).strip()
    else:
        mode = str(getattr(settings, "import_mode", "new") or "new").strip().lower()
        if mode == "unseen":
            base = "UNSEEN"
        elif mode == "search":
            base = str(getattr(settings, "import_search_query", "") or "").strip() or "ALL"
        else:
            base = str(getattr(settings, "imap_search", "") or "").strip() or "ALL"
    return _apply_date_filter(base)


def _max_known_uid(known_uids: set[str]) -> int | None:
    numeric = [int(uid) for uid in known_uids if str(uid).isdigit()]
    return max(numeric) if numeric else None


def _search_from_last_uid(search: str, known_uids: set[str]) -> str:
    """For new-mail mode, ask IMAP only for UIDs after the last stored UID."""
    if (search or "").strip().upper() != "ALL":
        return search
    max_uid = _max_known_uid(known_uids)
    if not max_uid:
        return search
    return f"UID {max_uid + 1}:4294967295"


def _parse_fetch_sizes(fetch_data: list[Any] | tuple[Any, ...] | None) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for part in fetch_data or []:
        hdr = b""
        if isinstance(part, tuple) and part:
            hdr = part[0] if isinstance(part[0], bytes) else b""
        elif isinstance(part, bytes):
            hdr = part
        if not hdr:
            continue
        uid_m = re.search(rb"UID\s+(\d+)", hdr)
        size_m = re.search(rb"RFC822\.SIZE\s+(\d+)", hdr)
        if uid_m and size_m:
            sizes[uid_m.group(1).decode("ascii")] = int(size_m.group(1))
    return sizes

# ── IMAP UTF-7 helpers ────────────────────────────────────────────────


def _imap_quote(folder: str) -> str:
    """Quote folder name for IMAP SELECT, handling UTF-7."""
    wire = encode_imap_utf7(folder) if any(ord(ch) > 127 for ch in (folder or "")) else (folder or "")
    escaped = wire.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


def decode_imap_utf7(value: str | None) -> str:
    """Decode IMAP modified UTF-7 folder names for humans."""
    if not value:
        return ""
    text = str(value)
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch != "&":
            out.append(ch)
            i += 1
            continue
        j = text.find("-", i)
        if j < 0:
            out.append(ch)
            i += 1
            continue
        token = text[i + 1:j]
        if token == "":
            out.append("&")
        else:
            import base64
            b64 = token.replace(",", "/")
            b64 += "=" * ((4 - len(b64) % 4) % 4)
            try:
                out.append(base64.b64decode(b64).decode("utf-16-be"))
            except Exception:
                out.append(text[i:j + 1])
        i = j + 1
    return "".join(out)


def encode_imap_utf7(value: str | None) -> str:
    """Encode unicode mailbox name to IMAP modified UTF-7. ASCII-safe input is preserved."""
    if not value:
        return ""
    text = str(value)
    if "&" in text and all(ord(ch) < 128 for ch in text):
        return text
    import base64
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf
        if not buf:
            return
        raw = "".join(buf).encode("utf-16-be")
        token = base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",")
        out.append("&" + token + "-")
        buf = []

    for ch in text:
        code = ord(ch)
        if 0x20 <= code <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


# ── Raw email storage ─────────────────────────────────────────────────


def _save_raw(mailbox: str, uid: str, raw: bytes) -> str | None:
    if not settings.store_raw_emails:
        return None
    folder = settings.raw_email_dir / re.sub(r"[^a-zA-Z0-9_.-]+", "_", mailbox)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uid}.eml"
    if not path.exists():
        path.write_bytes(raw)
    return str(path)


# ── Vision on attachments ─────────────────────────────────────────────


def _run_vision_on_attachments(email_data: dict[str, Any], case_data: dict[str, Any]) -> dict[str, Any]:
    """Try vision model on image/PDF attachments to fill missing fields."""
    from .ai_client import run_vision_on_attachment, should_ask_ai
    if not settings.ai_vision_enabled:
        return case_data.get("fields") or {}

    attachments = email_data.get("attachments") or []
    hint = str(email_data.get("subject") or "") + "\n" + str(email_data.get("snippet") or "")[:300]
    merged_fields = dict(case_data.get("fields") or {})
    missing = set(case_data.get("missing") or [])

    for att in attachments:
        if not missing & {"part_number", "document_number", "document_date", "brand"}:
            break
        fname = att.get("filename") or ""
        ctype = att.get("content_type") or ""
        raw = att.get("_bytes") or b""
        if not raw:
            continue
        result = run_vision_on_attachment(raw, fname, ctype, hint_text=hint)
        if not result.get("ok"):
            continue
        if result.get("source") == "pdf_text":
            pdf_text = result.get("text") or ""
            if pdf_text:
                from .classifier import extract_fields, load_buyer_rules
                buyer_rules = load_buyer_rules()
                buyer_code = case_data.get("buyer_code")
                buyer_rule = next((r for r in buyer_rules if r.code == buyer_code), None)
                pdf_fields = extract_fields(pdf_text, buyer_rule)
                for k, v in pdf_fields.items():
                    if v and k not in merged_fields:
                        merged_fields[k] = v
                        missing.discard(k)
        else:
            for k, v in (result.get("fields") or {}).items():
                if v and k not in merged_fields:
                    merged_fields[k] = str(v).strip()
                    missing.discard(k)
    return merged_fields


# ── Classify + learn (Stage 2) ────────────────────────────────────────


def _save_classify_learn(
    con: Any,
    raw_email_id: int,
    email_data: dict[str, Any],
    buyer_rules: list[Any],
    skip_ai: bool = False,
) -> tuple[int, dict[str, Any]]:
    from .ai_client import run_ai_suggestion, should_ask_ai

    learned = load_buyer_identities(con)
    case_data = classify_email(email_data, buyer_rules, learned_identities=learned)
    case_id = save_case(con, raw_email_id, case_data)
    case_data["export"]["case_id"] = case_id
    save_case(con, raw_email_id, case_data)

    learning: dict[str, Any] = {"actions": []}
    if not skip_ai and should_ask_ai(case_data, email_data):
        purpose = "first_unknown_customer" if not case_data.get("buyer_code") else "repair_missing_fields"
        suggestion = run_ai_suggestion(email_data, case_data, con=con, case_id=case_id, purpose=purpose)
        con.execute(
            "INSERT INTO ai_suggestions(case_id, model, prompt_hash, response_json, accepted, created_at) VALUES (?, ?, ?, ?, 0, ?)",
            (case_id, suggestion.get("model"), suggestion.get("prompt_hash"), dumps(suggestion), utcnow()),
        )
        learning["ai_suggestion"] = {
            "ok": suggestion.get("ok"),
            "cached": suggestion.get("cached"),
            "provider": suggestion.get("provider"),
            "model": suggestion.get("model"),
            "usage": suggestion.get("usage"),
            "error": suggestion.get("error"),
        }
        can_apply = bool(settings.auto_apply_ai_validated or (purpose == "first_unknown_customer" and settings.auto_apply_ai_on_first_unknown_customer))
        if can_apply and suggestion.get("ok") and isinstance(suggestion.get("response"), dict):
            case_data = apply_ai_overlay(email_data, case_data, suggestion["response"])
            case_id = save_case(con, raw_email_id, case_data)
            case_data["export"]["case_id"] = case_id
            save_case(con, raw_email_id, case_data)
            con.execute("UPDATE ai_suggestions SET accepted=1 WHERE case_id=? AND prompt_hash=?", (case_id, suggestion.get("prompt_hash")))
            learning.setdefault("actions", []).append("ai_overlay_applied")

    # v2.1 AI-only: наблюдение/промоция паттернов убраны.

    # Vision on attachments
    has_image_or_pdf = any(
        (att.get("filename") or "").lower().endswith((".jpg", ".jpeg", ".png", ".pdf", ".webp", ".heic"))
        or (att.get("content_type") or "").startswith("image/")
        or "pdf" in (att.get("content_type") or "")
        for att in (email_data.get("attachments") or [])
    )
    if (
        settings.ai_vision_enabled
        and has_image_or_pdf
        and case_data.get("missing")
        and case_data.get("event_type") == "new_return"
    ):
        enriched_fields = _run_vision_on_attachments(email_data, case_data)
        if enriched_fields != (case_data.get("fields") or {}):
            case_data["fields"] = enriched_fields
            from .classifier import quality_check, make_strong_key
            direction = (case_data.get("payload") or {}).get("direction", "inbound_customer")
            strong_key = make_strong_key(case_data.get("buyer_code"), enriched_fields)
            missing, quality_issues = quality_check(
                case_data.get("event_type") or "new_return",
                case_data.get("claim_kind"),
                enriched_fields, strong_key, direction,
                case_data.get("buyer_code"),
            )
            case_data["missing"] = missing
            case_data["quality"] = quality_issues
            case_data["state"] = "ready_to_1c" if not missing and not [q for q in quality_issues if q.get("level") == "error"] else case_data.get("state", "needs_review")
            case_data["ready_for_export"] = not missing and case_data["state"] == "ready_to_1c"
            case_id = save_case(con, raw_email_id, case_data)
            learning["vision_enriched"] = True
            learning["actions"].append("vision_fields_applied")

    if (
        case_data.get("state") == "needs_review"
        and case_data.get("event_type") in ("new_return", "unknown")
        and not case_data.get("buyer_code")
        and not case_data.get("claim_kind")
    ):
        try:
            from .telegram import notify_unresolved_immediate
            notify_unresolved_immediate({
                "case_id": case_id,
                "buyer_name": email_data.get("from_addr", "?"),
                "subject": email_data.get("subject", "—"),
                "missing": case_data.get("missing") or [],
            })
        except Exception:
            pass

    return case_id, learning


# ── IMAP folder discovery ─────────────────────────────────────────────


def _parse_folder_from_list_line(line: bytes) -> tuple[str | None, bool]:
    text = line.decode("utf-8", errors="replace")
    noselect = "\\Noselect" in text
    quoted = re.findall(r'"((?:[^"\\]|\\.)*)"', text)
    if quoted:
        folder = quoted[-1].replace('\\"', '"').replace('\\\\', '\\')
        if folder in {"/", "."} and len(quoted) >= 2:
            folder = quoted[-2]
        return folder, noselect
    parts = text.split()
    if parts:
        return parts[-1].strip(), noselect
    return None, noselect


def discover_imap_folders_detailed(mail: imaplib.IMAP4_SSL) -> list[dict[str, Any]]:
    typ, data = mail.list()
    if typ != "OK" or not data:
        fallback = settings.folders if not settings.discover_all_folders else ["INBOX"]
        return [{"name": f, "raw_name": encode_imap_utf7(f), "display_name": decode_imap_utf7(f), "selectable": True} for f in fallback]
    exclude = re.compile(settings.imap_exclude_folders_regex) if settings.imap_exclude_folders_regex else None
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in data:
        if not isinstance(raw, bytes):
            continue
        folder, noselect = _parse_folder_from_list_line(raw)
        if not folder or noselect:
            continue
        raw_name = folder
        display = decode_imap_utf7(raw_name)
        if exclude and (exclude.search(raw_name) or exclude.search(display)):
            continue
        if raw_name not in seen:
            seen.add(raw_name)
            items.append({
                "name": raw_name,
                "raw_name": raw_name,
                "display_name": display,
                "selectable": True,
            })
    items.sort(key=lambda it: (0 if str(it["raw_name"]).upper() == "INBOX" else 1, str(it["display_name"]).lower()))
    return items or [{"name": "INBOX", "raw_name": "INBOX", "display_name": "INBOX", "selectable": True}]


def discover_imap_folders(mail: imaplib.IMAP4_SSL) -> list[str]:
    return [str(it["raw_name"]) for it in discover_imap_folders_detailed(mail)]


def server_counts() -> dict[str, Any]:
    """Щиток серверной почты: сколько писем на сервере (SEARCH ALL) vs в базе, по папкам.

    Дёшево — IMAP отдаёт только список UID, не письма. Для панели настроек.
    """
    from .runtime_settings import apply_runtime_settings
    apply_runtime_settings()
    user = _clean_credential(settings.imap_username or "")
    pwd = _clean_credential(settings.imap_password or "")
    if not user or not pwd:
        return {"ok": False, "error": "IMAP не настроен (логин/пароль пусты)", "folders": []}
    try:
        mail = _open_imap()
    except Exception as exc:
        return {"ok": False, "error": f"Не удалось подключиться: {exc}", "folders": []}
    out: list[dict[str, Any]] = []
    tot_s = tot_d = 0
    try:
        configured = {f.strip() for f in str(getattr(settings, "imap_folders", "") or "").split(",") if f.strip()}
        with connect() as con:
            db_by_folder = {r["mailbox"]: r["n"] for r in con.execute(
                "SELECT mailbox, COUNT(*) n FROM raw_emails GROUP BY mailbox")}
        for f in discover_imap_folders(mail):
            if configured and f not in configured:
                continue
            try:
                mail.select(_imap_quote(f), readonly=True)
                typ, data = mail.uid("search", None, "ALL")
                srv = len(data[0].split()) if (typ == "OK" and data and data[0]) else 0
            except Exception:
                srv = -1
            dbn = int(db_by_folder.get(f, 0))
            disp = decode_imap_utf7(f)
            short = disp.split("|")[-1] if "|" in disp else disp
            out.append({"folder": f, "name": short, "server": srv, "db": dbn,
                        "gap": (srv - dbn) if srv >= 0 else None})
            if srv >= 0:
                tot_s += srv
                tot_d += dbn
    finally:
        try:
            mail.logout()
        except Exception:
            pass
    out.sort(key=lambda x: -(x["gap"] or 0))
    return {"ok": True, "total_server": tot_s, "total_db": tot_d, "total_gap": tot_s - tot_d,
            "folders": out, "checked_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()}


def list_imap_folders() -> dict[str, Any]:
    from .runtime_settings import apply_runtime_settings
    apply_runtime_settings()
    user = _clean_credential(settings.imap_username or "")
    pwd = _clean_credential(settings.imap_password or "")
    if not user or not pwd:
        return {"ok": False, "error": "IMAP_USERNAME / IMAP_PASSWORD are empty. Fill mail settings in the panel first.", "folders": [], "items": []}
    mail = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    try:
        _imap_login(mail, user, pwd)
        items = discover_imap_folders_detailed(mail)
        folders = [str(it["raw_name"]) for it in items]
        return {"ok": True, "folders": folders, "items": items, "count": len(folders)}
    finally:
        try:
            mail.logout()
        except Exception:
            pass


# ── IMAP helpers ──────────────────────────────────────────────────────


def _imap_login(mail: imaplib.IMAP4_SSL, user: str, pwd: str) -> None:
    """Login with proper quoting for credentials with special chars."""
    specials = set(' "\\')
    if not (specials & set(user)) and not (specials & set(pwd)):
        mail.login(user, pwd)
        return

    def _q(s: str) -> str:
        return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'

    typ, data = mail._simple_command('LOGIN', _q(user), _q(pwd))
    if typ != 'OK':
        raise mail.error(data[-1] if data else b'LOGIN failed')
    mail.state = 'AUTH'


def _clean_credential(value: str) -> str:
    """Strip whitespace and invisible Unicode chars."""
    for ch in ('\ufeff', '\u200b', '\u200c', '\u200d', '\u00ad', '\u2060', '\u180e', '\xa0'):
        value = value.replace(ch, '')
    return value.strip()


def _open_imap() -> imaplib.IMAP4_SSL:
    import socket
    timeout = getattr(settings, 'imap_timeout_seconds', 30)
    mail = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port, timeout=timeout)
    if mail.sock:
        mail.sock.settimeout(timeout)
    user = _clean_credential(settings.imap_username or "")
    pwd = _clean_credential(settings.imap_password or "")
    if not user or not pwd:
        raise ValueError("IMAP username/password are empty")
    _imap_login(mail, user, pwd)
    return mail


# ── Stop flag ─────────────────────────────────────────────────────────


_IMPORT_STOP_REQUESTED = False


def request_import_stop() -> None:
    global _IMPORT_STOP_REQUESTED
    _IMPORT_STOP_REQUESTED = True


def clear_import_stop() -> None:
    global _IMPORT_STOP_REQUESTED
    _IMPORT_STOP_REQUESTED = False


# ── Single UID fetch (fallback) ───────────────────────────────────────


def fetch_one_uid(mail: imaplib.IMAP4_SSL, uid_b: bytes) -> bytes:
    """Fetch a single email body by UID.

    Raises RuntimeError on failure.
    """
    typ, fetch_data = mail.uid("fetch", uid_b, "(BODY.PEEK[] FLAGS)")
    if typ != "OK" or not fetch_data:
        raise RuntimeError(f"IMAP fetch failed: typ={typ}")

    for part in fetch_data:
        if isinstance(part, tuple) and len(part) == 2 and part[1]:
            return part[1]

    raise RuntimeError("IMAP fetch returned empty body")


# ── Filter folders: exclude parents if children present ───────────────


def _filter_folders(requested_folders: list[str]) -> list[str]:
    """Exclude parent folders when their subfolders are also in the list.

    Example: ["Клиенты", "Клиенты|auto-sputnik.ru"] → ["Клиенты|auto-sputnik.ru"]
    """
    filtered = []
    for f in requested_folders:
        is_parent = False
        for p in requested_folders:
            if p != f and p.startswith(f + "|"):
                is_parent = True
                break
        if not is_parent:
            filtered.append(f)
    return filtered


# ── Import a single folder (raw only) ─────────────────────────────────


def _read_uidvalidity(mail: imaplib.IMAP4_SSL) -> str:
    """Прочитать UIDVALIDITY текущей выбранной папки (после SELECT). '' если недоступно."""
    try:
        _typ, values = mail.response("UIDVALIDITY")
        if values and values[0]:
            v = values[0].decode("ascii", "ignore") if isinstance(values[0], bytes) else str(values[0])
            m = re.search(r"\d+", v)
            return m.group(0) if m else ""
    except Exception:
        pass
    return ""


def _import_folder_raw(
    mail: imaplib.IMAP4_SSL,
    folder: str,
    job_id: str,
    per_folder_limit: int,
    batch_size: int,
    search: str,
    total_seen: int,
    imap_total_limit: int,
) -> dict[str, Any]:
    """Import one folder, return folder-level result.

    This function:
    - selects the folder
    - searches for UIDs
    - filters known UIDs
    - fetches in batches with single-UID fallback
    - detects oversized emails
    - logs every error to import_errors and import_uid_failures
    """
    display_folder = decode_imap_utf7(folder)
    folder_result: dict[str, Any] = {
        "folder": folder,
        "display_folder": display_folder,
        "stage": "connect",
        "imported": 0,
        "skipped": 0,
        "failed": 0,
        "oversized": 0,
        "errors": [],
        "total_on_server": 0,
        "known_uids_count": 0,
        "new_uids_count": 0,
        "selected_uids_count": 0,
        "batches_total": 0,
    }

    # ── Select folder ────────────────────────────────────────────────
    folder_result["stage"] = "select_folder"
    try:
        typ, _ = mail.select(_imap_quote(folder), readonly=settings.imap_readonly)
        if typ != "OK":
            typ, _ = mail.select(folder, readonly=settings.imap_readonly)
    except Exception as exc:
        folder_result["errors"].append(f"select failed: {exc}")
        folder_result["stage"] = "select_error"
        with connect() as con:
            record_import_error(con, job_id, mailbox=folder, display_folder=display_folder,
                                uid="?", stage="select_folder",
                                error_type=type(exc).__name__, error_message=str(exc))
        return folder_result

    if typ != "OK":
        folder_result["errors"].append(f"select failed: {typ}")
        folder_result["stage"] = "select_error"
        return folder_result

    # UIDVALIDITY этой папки — входит в raw identity (см. db.upsert_email).
    folder_uidvalidity = _read_uidvalidity(mail)
    folder_result["uidvalidity"] = folder_uidvalidity

    # ── Load known UIDs ──────────────────────────────────────────────
    folder_result["stage"] = "load_known_uids"
    try:
        with connect() as con:
            known_uids = set(
                r["uid"] for r in con.execute(
                    "SELECT uid FROM raw_emails WHERE mailbox=?", (folder,)
                ).fetchall()
            )
            quarantined_uids = set(
                r["uid"] for r in con.execute(
                    """
                    SELECT uid
                    FROM import_uid_failures
                    WHERE mailbox=? AND status='quarantined'
                    """,
                    (folder,),
                ).fetchall()
            )
            retry_pending_uids = set(
                r["uid"] for r in con.execute(
                    """
                    SELECT uid
                    FROM import_uid_failures
                    WHERE mailbox=? AND status='retry_pending'
                    """,
                    (folder,),
                ).fetchall()
            )
            known_uids.update(quarantined_uids)
            # «Мёртвые» (5+ неудачных попыток) — пропускаем, чтобы не качать вечно битое,
            # но они остаются в import_uid_failures для алерта/ручного разбора.
            dead_uids = set(
                r["uid"] for r in con.execute(
                    "SELECT uid FROM import_uid_failures WHERE mailbox=? AND attempts>=5", (folder,)
                ).fetchall()
            )
            known_uids.update(dead_uids)
    except Exception as exc:
        folder_result["errors"].append(f"load known uids failed: {exc}")
        folder_result["stage"] = "db_error"
        return folder_result

    folder_result["known_uids_count"] = len(known_uids)
    folder_result["retry_pending_count"] = len(retry_pending_uids)
    # ВСЕГДА полный поиск по папке: иначе инкрементальный «от последнего UID» НЕ ВИДИТ старые
    # пропущенные/упавшие письма → молчаливые дыры (была потеря pr-lg/trinity/avtoto). Перечисление
    # UID дёшево (~сек), скачиваются только отсутствующие (uid not in known_uids). Без пропусков.
    effective_search = search
    folder_result["search"] = effective_search

    # ── Search UIDs ──────────────────────────────────────────────────
    folder_result["stage"] = "search_uids"
    try:
        typ, data = mail.uid("search", None, effective_search)
    except Exception as exc:
        folder_result["errors"].append(f"search failed: {exc}")
        folder_result["stage"] = "search_error"
        with connect() as con:
            record_import_error(con, job_id, mailbox=folder, display_folder=display_folder,
                                uid="?", stage="search_uids",
                                error_type=type(exc).__name__, error_message=str(exc))
        return folder_result

    if typ != "OK" or not data:
        folder_result["errors"].append(f"search failed: {typ}")
        folder_result["stage"] = "search_error"
        return folder_result

    all_uids = data[0].split()
    folder_result["total_on_server"] = len(all_uids)

    # ── Filter new UIDs ──────────────────────────────────────────────
    folder_result["stage"] = "filter_uids"
    uids = []
    for uid_b in all_uids:
        uid = uid_b.decode("ascii", errors="ignore")
        if uid in retry_pending_uids or uid not in known_uids:
            uids.append(uid_b)
    folder_result["new_uids_count"] = len(uids)

    if not uids:
        folder_result["stage"] = "done"
        folder_result["note"] = "no_new_uids"
        return folder_result

    # ── Import window: письма раньше границы НЕ качаем (skipped_before_start) ──
    from . import import_window
    _from_dt = import_window.window_from_dt()
    if _from_dt is not None:
        folder_result["stage"] = "import_window_filter"
        try:
            uid_csv = b",".join(uids)
            d_typ, d_data = mail.uid("fetch", uid_csv, "(UID INTERNALDATE)")
            uid_dates: dict[str, str] = {}
            if d_typ == "OK":
                for part in d_data or []:
                    blob = part if isinstance(part, bytes) else (part[0] if isinstance(part, tuple) else b"")
                    if not isinstance(blob, bytes):
                        continue
                    um = re.search(rb"UID\s+(\d+)", blob)
                    dm = re.search(rb'INTERNALDATE\s+"([^"]+)"', blob)
                    if um and dm:
                        uid_dates[um.group(1).decode("ascii")] = dm.group(1).decode("ascii", "replace")
            keep, skip = import_window.partition_uids(uid_dates, _from_dt)
            if skip and getattr(settings, "skip_before_start", True):
                with connect() as con:
                    for su in skip:
                        record_uid_skipped(con, mailbox=folder, uid=su, uidvalidity=folder_uidvalidity)
                folder_result["skipped_before_start"] = len(skip)
                keep_set = set(keep)
                uids = [u for u in uids if u.decode("ascii", "ignore") in keep_set]
            folder_result["imported_after_start_candidates"] = len(uids)
        except Exception as exc:
            folder_result["errors"].append(f"import_window_filter: {exc}")
        if not uids:
            folder_result["stage"] = "done"
            folder_result["note"] = "all_before_import_window"
            return folder_result

    # Take only the freshest (UIDs grow), then reverse so newest first
    if len(uids) > per_folder_limit:
        uids = uids[-per_folder_limit:]
    uids.reverse()
    folder_result["selected_uids_count"] = len(uids)

    # ── Process in batches ────────────────────────────────────────────
    # Retry/backfill folders may contain large historical attachments. Keep those
    # fetches single-UID so one heavy message cannot stall a whole IMAP batch.
    effective_batch_size = 1 if retry_pending_uids else batch_size
    folder_result["effective_batch_size"] = effective_batch_size
    uid_chunks = [uids[i:i + effective_batch_size] for i in range(0, len(uids), effective_batch_size)]
    folder_result["batches_total"] = len(uid_chunks)

    for chunk_idx, chunk in enumerate(uid_chunks):
        if _IMPORT_STOP_REQUESTED:
            folder_result["stage"] = "stopped"
            break
        if total_seen >= imap_total_limit:
            folder_result["stage"] = "total_limit_reached"
            break

        batch_first_uid = chunk[0].decode("ascii", errors="ignore") if chunk else "?"
        batch_last_uid = chunk[-1].decode("ascii", errors="ignore") if chunk else "?"

        folder_result["stage"] = f"fetch_batch:{chunk_idx + 1}/{len(uid_chunks)} uid:{batch_first_uid}-{batch_last_uid}"
        try:
            with connect() as con:
                update_import_job_heartbeat(
                    con, job_id,
                    stage=folder_result["stage"],
                    folder=folder,
                    display_folder=display_folder,
                    uid=batch_first_uid,
                    processed=total_seen,
                    imported=folder_result["imported"],
                    skipped=folder_result["skipped"],
                    failed=folder_result["failed"],
                    errors=len(folder_result.get("errors", [])),
                )
        except Exception:
            pass

        raw_by_uid: dict[str, bytes] = {}
        batch_successful = False

        # Ask IMAP for sizes first so oversized messages are skipped before body download.
        uid_list = b",".join(chunk)
        oversized_uids: set[str] = set()
        try:
            size_typ, size_data = mail.uid("fetch", uid_list, "(RFC822.SIZE)")
            if size_typ == "OK":
                sizes = _parse_fetch_sizes(size_data)
                max_bytes = _max_raw_bytes()
                for uid, raw_size in sizes.items():
                    if raw_size > max_bytes:
                        oversized_uids.add(uid)
                        folder_result["oversized"] += 1
                        folder_result["errors"].append(f"uid={uid}: oversized {raw_size} bytes")
                        _save_uid_error_simple(
                            job_id, folder, display_folder, uid,
                            "oversized", "OversizedEmail",
                            f"raw_size={raw_size} > {max_bytes}",
                        )
        except Exception:
            pass

        fetch_chunk = [uid_b for uid_b in chunk if uid_b.decode("ascii", errors="ignore") not in oversized_uids]
        uid_list = b",".join(fetch_chunk)
        try:
            typ, fetch_data = mail.uid("fetch", uid_list, "(BODY.PEEK[] FLAGS)") if fetch_chunk else ("OK", [])
            batch_successful = (typ == "OK" and bool(fetch_data))
        except Exception:
            batch_successful = False

        if batch_successful:
            # Parse batch response
            pending_uid: str | None = None
            for part in fetch_data:
                if isinstance(part, tuple) and len(part) == 2:
                    hdr = part[0] if isinstance(part[0], bytes) else b""
                    body = part[1] if isinstance(part[1], bytes) else b""
                    m = re.search(rb'UID\s+(\d+)', hdr)
                    if m:
                        pending_uid = m.group(1).decode("ascii")
                    if pending_uid and body:
                        raw_by_uid[pending_uid] = body
                        pending_uid = None
        else:
            # Batch failed — fall back to single UID fetch
            folder_result["stage"] = f"batch_fallback_single:{chunk_idx}"

        # Process each UID in chunk
        for uid_b in chunk:
            if _IMPORT_STOP_REQUESTED:
                break
            if total_seen >= imap_total_limit:
                break
            total_seen += 1

            uid = uid_b.decode("ascii", errors="ignore")
            if uid in oversized_uids:
                continue
            folder_result["stage"] = f"uid:{uid}"

            # Get raw email bytes
            raw = raw_by_uid.get(uid)

            if not raw:
                # Single UID fetch as fallback
                try:
                    raw = fetch_one_uid(mail, uid_b)
                except Exception as exc:
                    folder_result["failed"] += 1
                    folder_result["errors"].append(f"fetch uid={uid}: {exc}")
                    _save_uid_error(job_id, folder, display_folder, uid,
                                    "fetch_single", exc)
                    continue

            if not raw:
                folder_result["failed"] += 1
                folder_result["errors"].append(f"fetch uid={uid}: empty response")
                _save_uid_error_simple(job_id, folder, display_folder, uid,
                                       "fetch_single", "EmptyResponse", "IMAP returned empty body")
                continue

            # Check oversized
            raw_size = len(raw)
            max_bytes = _max_raw_bytes()
            if raw_size > max_bytes:
                folder_result["oversized"] += 1
                folder_result["errors"].append(f"uid={uid}: oversized {raw_size} bytes")
                _save_uid_error_simple(job_id, folder, display_folder, uid,
                                       "oversized", "OversizedEmail",
                                       f"raw_size={raw_size} > {max_bytes}")
                # Still save the oversized as raw with status
                raw_path = _save_raw(folder, uid, raw)
                try:
                    with connect() as con:
                        email_data = parse_email_bytes(raw, mailbox=folder, uid=uid, raw_path=raw_path)
                        email_data["raw_size"] = raw_size
                        email_data["status"] = "oversized"
                        email_data["uidvalidity"] = folder_uidvalidity
                        _raw_email_id, created = upsert_email(con, email_data)
                        if created:
                            con.execute("UPDATE raw_emails SET raw_size=?, status='oversized' WHERE id=?", (raw_size, _raw_email_id))
                except Exception:
                    pass
                continue

            # Save raw email
            raw_path = _save_raw(folder, uid, raw)

            # Parse and save to DB
            try:
                email_data = parse_email_bytes(raw, mailbox=folder, uid=uid, raw_path=raw_path)
                if not email_data:
                    folder_result["errors"].append(f"parse uid={uid}: empty result")
                    _save_uid_error_simple(job_id, folder, display_folder, uid,
                                           "parse", "EmptyParseResult", "parse_email_bytes returned empty")
                    folder_result["failed"] += 1
                    continue

                email_data["raw_size"] = raw_size
                email_data["uidvalidity"] = folder_uidvalidity
                with connect() as con:
                    _raw_email_id, created = upsert_email(con, email_data)
                    con.execute(
                        """
                        UPDATE import_uid_failures
                        SET status='resolved', last_seen_at=?
                        WHERE mailbox=? AND uid=?
                        """,
                        (utcnow(), folder, uid),
                    )
                    if created:
                        con.execute("UPDATE raw_emails SET raw_size=?, status='imported' WHERE id=?", (raw_size, _raw_email_id))
                        folder_result["imported"] += 1
                    else:
                        folder_result["skipped"] += 1

            except Exception as exc:
                folder_result["failed"] += 1
                folder_result["errors"].append(f"parse/save uid={uid}: {exc}")
                _save_uid_error(job_id, folder, display_folder, uid, "parse", exc)
                continue

        try:
            with connect() as con:
                update_import_job_heartbeat(
                    con, job_id,
                    stage=f"fetch_batch:{chunk_idx + 1}/{len(uid_chunks)}",
                    folder=folder,
                    display_folder=display_folder,
                    processed=total_seen,
                    imported=folder_result["imported"],
                    skipped=folder_result["skipped"],
                    failed=folder_result["failed"],
                    errors=len(folder_result.get("errors", [])),
                )
        except Exception:
            pass

    return folder_result


def _save_uid_error(job_id: str, folder: str, display_folder: str, uid: str, stage: str, exc: Exception) -> None:
    """Log UID error to import_errors and import_uid_failures."""
    try:
        with connect() as con:
            record_import_error(con, job_id, mailbox=folder, display_folder=display_folder,
                                uid=uid, stage=stage,
                                error_type=type(exc).__name__, error_message=str(exc))
            record_uid_failure(con, mailbox=folder, uid=uid, stage=stage,
                               error_type=type(exc).__name__, error_message=str(exc))
    except Exception:
        pass


def _save_uid_error_simple(job_id: str, folder: str, display_folder: str, uid: str, stage: str, error_type: str, error_message: str) -> None:
    """Log UID error with explicit type+message."""
    try:
        with connect() as con:
            record_import_error(con, job_id, mailbox=folder, display_folder=display_folder,
                                uid=uid, stage=stage,
                                error_type=error_type, error_message=error_message)
            record_uid_failure(con, mailbox=folder, uid=uid, stage=stage,
                               error_type=error_type, error_message=error_message)
    except Exception:
        pass


# ── Main raw import (Stage 1) ─────────────────────────────────────────


def import_from_imap_raw(job_id: str | None = None, limit: int | None = None,
                          folders: list[str] | None = None, search: str | None = None) -> dict[str, Any]:
    """Import emails from IMAP without classifying — store raw_emails only.

    This is the primary safe import mode. It creates an import_job for diagnostics.

    Args:
        job_id: Optional explicit job_id. Auto-generated if not provided.
        limit: Per-folder limit (default: settings.imap_limit).
        folders: List of folders. If None, uses settings.folders or discovers all.
        search: IMAP search string. If None, uses settings.imap_search or "ALL".

    Returns:
        Dict with import results including job_id, folder results, errors.
    """
    from .runtime_settings import apply_runtime_settings
    clear_import_stop()
    apply_runtime_settings()

    if not settings.imap_username or not settings.imap_password:
        return {
            "ok": False,
            "error": "IMAP_USERNAME / IMAP_PASSWORD are empty. Fill mail settings in the panel first.",
            "imported": 0,
        }

    actual_job_id = job_id or f"import_raw_{uuid.uuid4().hex[:12]}"
    per_folder_limit = int(limit or settings.imap_limit or 200)
    search_str = _default_search(search)

    result: dict[str, Any] = {
        "ok": True,
        "job_id": actual_job_id,
        "imported": 0,
        "skipped": 0,
        "failed": 0,
        "oversized": 0,
        "errors": [],
        "folders": [],
        "classify": False,
    }

    # ── Determine folders ─────────────────────────────────────────────
    if folders:
        requested_folders = list(folders)
    elif settings.discover_all_folders:
        probe = list_imap_folders()
        requested_folders = probe.get("folders") or ["INBOX"]
    else:
        requested_folders = settings.folders or ["INBOX"]

    filtered_folders = _filter_folders(requested_folders)

    # ── Batch size ─────────────────────────────────────────────────────
    batch_size = max(1, min(int(getattr(settings, "imap_batch_size", 20) or 20), 50))

    # ── Create import job ──────────────────────────────────────────────
    try:
        with connect() as con:
            create_import_job(con, actual_job_id, mode="raw")
    except Exception:
        pass  # Non-fatal: job tracking is diagnostic

    total_seen = 0
    imap_total_limit = int(getattr(settings, "imap_total_limit", 5000) or 5000)

    for folder in filtered_folders:
        if _IMPORT_STOP_REQUESTED:
            result["error"] = "stopped_by_operator"
            break
        if total_seen >= imap_total_limit:
            break

        display_folder = decode_imap_utf7(folder)

        mail: imaplib.IMAP4_SSL | None = None
        try:
            mail = _open_imap()
            folder_result = _import_folder_raw(
                mail, folder, actual_job_id,
                per_folder_limit, batch_size, search_str,
                total_seen, imap_total_limit,
            )
        except Exception as exc:
            folder_result = {
                "folder": folder,
                "display_folder": display_folder,
                "stage": "imap_error",
                "errors": [str(exc)],
                "imported": 0, "skipped": 0, "failed": 0, "oversized": 0,
            }
        finally:
            if mail is not None:
                try:
                    mail.logout()
                except Exception:
                    pass

            # Update totals
            total_seen += folder_result.get("imported", 0) + folder_result.get("failed", 0)
            result["imported"] += folder_result.get("imported", 0)
            result["skipped"] += folder_result.get("skipped", 0)
            result["failed"] += folder_result.get("failed", 0)
            result["oversized"] += folder_result.get("oversized", 0)
            result["errors"].extend(folder_result.get("errors", []))

            result["folders"].append(folder_result)

            # Update job heartbeat
            try:
                with connect() as con:
                    update_import_job_heartbeat(
                        con, actual_job_id,
                        stage=folder_result.get("stage", "done"),
                        folder=folder, display_folder=display_folder,
                        processed=total_seen,
                        imported=result["imported"],
                        skipped=result["skipped"],
                        failed=result["failed"],
                        errors=len(result.get("errors", [])),
                    )
            except Exception:
                pass

    # ── Finish import job ──────────────────────────────────────────
    final_status = "stopped" if _IMPORT_STOP_REQUESTED else "completed"
    try:
        with connect() as con:
            finish_import_job(con, actual_job_id, status=final_status, result=result)
    except Exception:
        pass

    result["total_seen"] = total_seen
    return result


# ── Old backward-compatible alias ────────────────────────────────────


def _import_raw_legacy(limit: int | None = None) -> dict[str, Any]:
    """Legacy wrapper for import_from_imap_raw (used by old API endpoint)."""
    return import_from_imap_raw(limit=limit)


# ── Full import (old behavior, Stage 1 + Stage 2 mixed) ──────────────


def import_from_imap(limit: int | None = None, folders: list[str] | None = None,
                     search: str | None = None) -> dict[str, Any]:
    """Full import: fetch raw emails, then parse, classify, and learn.

    DEPRECATED: Prefer import_from_imap_raw() + process_imported() separately.
    Kept for backward compatibility.

    This function does Stage 1 (IMAP → raw_emails) and Stage 2 (raw_emails → cases)
    in a single pass. For fresh imports this is faster, but for diagnostics
    use the two-stage pipeline.
    """
    from .runtime_settings import apply_runtime_settings
    clear_import_stop()
    apply_runtime_settings()

    if not settings.imap_username or not settings.imap_password:
        return {
            "ok": False,
            "error": "IMAP_USERNAME / IMAP_PASSWORD are empty. Fill mail settings in the panel first.",
            "imported": 0,
        }

    per_folder_limit = int(limit or settings.imap_limit or 200)
    import socket as _socket
    _socket.setdefaulttimeout(30)
    search_str = _default_search(search)
    buyer_rules = load_buyer_rules()
    result: dict[str, Any] = {
        "ok": True, "folders": [], "imported": 0, "skipped": 0,
        "classified": 0, "autolearned": 0, "errors": [],
    }

    if folders:
        requested_folders = list(folders)
    elif settings.discover_all_folders:
        probe = list_imap_folders()
        requested_folders = probe.get("folders") or ["INBOX"]
    else:
        requested_folders = settings.folders or ["INBOX"]

    filtered_folders = _filter_folders(requested_folders)
    total_seen = 0
    batch_size = max(1, min(int(getattr(settings, "imap_batch_size", 20) or 20), 50))

    for folder in filtered_folders:
        if total_seen >= settings.imap_total_limit:
            break
        display_folder = decode_imap_utf7(folder)
        folder_result: dict[str, Any] = {
            "folder": folder,
            "display_folder": display_folder,
            "stage": "connect",
            "imported": 0, "skipped": 0, "classified": 0, "autolearned": 0,
            "errors": [],
            "total_on_server": 0, "known_uids_count": 0,
            "found_uids_count": 0, "new_uids_count": 0, "selected_uids_count": 0,
        }
        mail: imaplib.IMAP4_SSL | None = None
        try:
            mail = _open_imap()
            folder_result["stage"] = "select_folder"
            typ, _ = mail.select(_imap_quote(folder), readonly=settings.imap_readonly)
            if typ != "OK":
                typ, _ = mail.select(folder, readonly=settings.imap_readonly)
            if typ != "OK":
                folder_result["errors"].append(f"select failed: {typ}")
                folder_result["stage"] = "error"
                result["folders"].append(folder_result)
                continue

            folder_result["stage"] = "load_known_uids"
            with connect() as con:
                known_uids = set(
                    r["uid"] for r in con.execute(
                        "SELECT uid FROM raw_emails WHERE mailbox=?", (folder,)
                    ).fetchall()
                )
            folder_result["known_uids_count"] = len(known_uids)
            effective_search = _search_from_last_uid(search_str, known_uids)
            folder_result["search"] = effective_search

            folder_result["stage"] = "search_uids"
            typ, data = mail.uid("search", None, effective_search)
            if typ != "OK" or not data:
                folder_result["errors"].append(f"search failed: {typ}")
                folder_result["stage"] = "error"
                result["folders"].append(folder_result)
                continue

            all_uids = data[0].split()
            folder_result["total_on_server"] = len(all_uids)
            folder_result["found_uids_count"] = len(all_uids)

            folder_result["stage"] = "filter_new_uids"
            uids = [u for u in all_uids if u.decode("ascii", errors="ignore") not in known_uids]
            folder_result["new_uids_count"] = len(uids)

            if not uids:
                folder_result["imported"] = 0
                folder_result["selected_uids_count"] = 0
                folder_result["stage"] = "done"
                folder_result["note"] = "no_new_uids"
                result["folders"].append(folder_result)
                continue

            # Freshest first
            if len(uids) > per_folder_limit:
                uids = uids[-per_folder_limit:]
            uids.reverse()
            folder_result["selected_uids_count"] = len(uids)

            # ── Batch fetch ──
            folder_result["stage"] = "fetch_batch"
            uid_chunks = [uids[i:i + batch_size] for i in range(0, len(uids), batch_size)]
            raw_by_uid: dict[str, bytes] = {}

            for chunk_idx, chunk in enumerate(uid_chunks):
                if _IMPORT_STOP_REQUESTED:
                    break
                if total_seen >= settings.imap_total_limit:
                    break
                uid_list = b",".join(chunk)
                try:
                    typ, fetch_data = mail.uid("fetch", uid_list, "(BODY.PEEK[] FLAGS)")
                except Exception as exc:
                    try:
                        try:
                            mail.logout()
                        except Exception:
                            pass
                        mail = _open_imap()
                        typ2, _ = mail.select(_imap_quote(folder), readonly=settings.imap_readonly)
                        if typ2 != "OK":
                            typ2, _ = mail.select(folder, readonly=settings.imap_readonly)
                        typ, fetch_data = mail.uid("fetch", uid_list, "(BODY.PEEK[] FLAGS)") if typ2 == "OK" else (typ2, [])
                    except Exception as exc2:
                        folder_result["errors"].append(f"batch {chunk_idx} fetch failed: {exc2}")
                        continue
                if typ != "OK" or not fetch_data:
                    continue
                pending_uid: str | None = None
                for part in fetch_data:
                    if isinstance(part, tuple) and len(part) == 2:
                        hdr = part[0] if isinstance(part[0], bytes) else b""
                        body = part[1] if isinstance(part[1], bytes) else b""
                        m = re.search(rb'UID\s+(\d+)', hdr)
                        if m:
                            pending_uid = m.group(1).decode("ascii")
                        if pending_uid and body:
                            raw_by_uid[pending_uid] = body
                            pending_uid = None

            # ── Process each UID ──
            for uid_b in uids:
                if total_seen >= settings.imap_total_limit:
                    break
                if _IMPORT_STOP_REQUESTED:
                    result["folders"].append(folder_result)
                    return result
                total_seen += 1
                uid = uid_b.decode("ascii", errors="ignore")
                folder_result["stage"] = f"fetch_uid:{uid}"
                raw = raw_by_uid.get(uid)
                if not raw:
                    try:
                        typ2, fetch_data2 = mail.uid("fetch", uid, "(BODY.PEEK[] FLAGS)")
                        if typ2 == "OK" and fetch_data2:
                            for part in fetch_data2:
                                if isinstance(part, tuple) and part[1]:
                                    raw = part[1]
                                    break
                    except Exception as exc:
                        folder_result["errors"].append(f"fetch {uid} failed: {exc}")
                        continue
                if not raw:
                    folder_result["errors"].append(f"fetch {uid}: empty raw")
                    continue

                # Check oversized
                if len(raw) > _max_raw_bytes():
                    folder_result["errors"].append(f"uid={uid}: oversized {len(raw)} bytes")
                    continue

                folder_result["stage"] = f"parse:{uid}"
                raw_path = _save_raw(folder, uid, raw)
                try:
                    email_data = parse_email_bytes(raw, mailbox=folder, uid=uid, raw_path=raw_path)
                    if not email_data:
                        folder_result["errors"].append(f"parse {uid}: empty after parse")
                        continue

                    folder_result["stage"] = f"classify:{uid}"
                    buyer_rules_local = load_buyer_rules()
                    from .classifier import classify_email
                    test_case = classify_email(dict(email_data), buyer_rules_local)
                    skip_types = {"info_only", "spam_promo"}
                    if test_case.get("event_type") in skip_types:
                        with connect() as con:
                            raw_email_id, created = upsert_email(con, email_data)
                            if created:
                                folder_result["imported"] += 1
                                result["imported"] += 1
                            else:
                                folder_result["skipped"] += 1
                                result["skipped"] += 1
                        folder_result["classified"] += 1
                        result["classified"] += 1
                        continue

                    folder_result["stage"] = f"save:{uid}"
                    with connect() as con:
                        raw_email_id, created = upsert_email(con, email_data)
                        if created:
                            folder_result["imported"] += 1
                            result["imported"] += 1
                        else:
                            folder_result["skipped"] += 1
                            result["skipped"] += 1
                        _case_id, learning = _save_classify_learn(con, raw_email_id, email_data, buyer_rules, skip_ai=True)
                        if learning.get("actions"):
                            folder_result["autolearned"] += 1
                            result["autolearned"] += 1
                        folder_result["classified"] += 1
                        result["classified"] += 1
                except Exception as exc:
                    folder_result["errors"].append(f"{uid}: {exc}")
        except Exception as exc:
            folder_result["errors"].append(str(exc))
            result["errors"].append(f"{display_folder or folder}: {exc}")
            result["ok"] = False
            folder_result["stage"] = "error"
        finally:
            if mail is not None:
                try:
                    mail.logout()
                except Exception:
                    pass
            folder_result["stage"] = "done"
            result["folders"].append(folder_result)

    result["total_on_server"] = sum(f.get("total_on_server", 0) for f in result.get("folders", []))
    result["folders_processed"] = sum(1 for f in result.get("folders", []) if f.get("imported", 0) > 0 or f.get("skipped", 0) > 0)
    return result


# ─── Stage 2: Process raw emails into cases ──────────────────────────


def process_imported_emails(limit: int = 500, supplier: str | None = None,
                             dry_run: bool = False, manual_review_gate: bool = False) -> dict[str, Any]:
    """Process raw_emails with status='imported' through parser/patterns/cases.

    This is Stage 2 of the pipeline: raw_emails → cases.
    Stage 1 (import_from_imap_raw) must have been run first.

    Args:
        limit: Maximum number of emails to process.
        supplier: If set, only process emails from this supplier/buyer_code.
        dry_run: If True, only count and report, don't actually create cases.

    Returns:
        Dict with processing results.
    """
    buyer_rules = load_buyer_rules()
    result: dict[str, Any] = {
        "ok": True,
        "processed": 0,
        "classified": 0,
        "autolearned": 0,
        "skipped": 0,
        "errors": [],
        "dry_run": dry_run,
        "manual_review_gate": manual_review_gate,
    }

    try:
        with connect() as con:
            # Get emails without cases
            where = "WHERE c.id IS NULL"
            params: list[Any] = []
            if supplier:
                where += " AND r.from_addr LIKE ?"
                params.append(f"%{supplier}%")

            rows = con.execute(
                f"""
                SELECT r.* FROM raw_emails r
                LEFT JOIN cases c ON c.raw_email_id=r.id
                {where}
                ORDER BY r.id
                LIMIT ?
                """,
                params + [int(limit)],
            ).fetchall()

            if not rows:
                result["note"] = "no_unprocessed_emails"
                return result

            # Load existing cases for context
            existing_cases_rows = con.execute(
                """
                SELECT c.id, c.event_type,
                       e.from_addr, e.subject
                FROM cases c JOIN raw_emails e ON e.id=c.raw_email_id
                WHERE c.event_type IN ('new_return','followup_dialog','followup_reminder','supplier_decision','unknown')
                ORDER BY c.id
                """
            ).fetchall()
            existing_cases = []
            for ecr in existing_cases_rows:
                existing_cases.append({
                    "from_addr": ecr["from_addr"],
                    "subject_template": ecr["subject"],
                    "event_type": ecr["event_type"],
                })

            for row in rows:
                try:
                    from .db import loads as _loads
                    row_data = dict(row)
                    email_data = {
                        "mailbox": row_data.get("mailbox"),
                        "uid": row_data.get("uid"),
                        "message_id": row_data.get("message_id"),
                        "in_reply_to": row_data.get("in_reply_to"),
                        "references": _loads(row_data.get("references_json"), []),
                        "subject": row_data.get("subject"),
                        "from_addr": row_data.get("from_addr"),
                        "to_addr": row_data.get("to_addr"),
                        "cc_addr": row_data.get("cc_addr"),
                        "received_at": row_data.get("received_at"),
                        "body_text": row_data.get("body_text"),
                        "body_html": row_data.get("body_html"),
                        "snippet": row_data.get("snippet"),
                        "raw_hash": row_data.get("raw_hash"),
                        "raw_path": row_data.get("raw_path"),
                        "quote_markers": int(row_data.get("quote_markers") or 0),
                        "visible_text": row_data.get("body_text") or row_data.get("snippet") or "",
                    }
                    attachments = [
                        dict(a) for a in con.execute(
                            "SELECT filename, content_type, size_bytes FROM attachments WHERE raw_email_id=?",
                            (int(row["id"]),),
                        ).fetchall()
                    ]
                    email_data["attachments"] = attachments

                    if dry_run:
                        result["processed"] += 1
                        continue

                    learned = load_buyer_identities(con)
                    case_data = classify_email(
                        email_data, buyer_rules,
                        learned_identities=learned,
                        existing_cases=existing_cases,
                    )
                    if manual_review_gate:
                        case_data = force_operator_review(case_data)
                    case_id = save_case(con, int(row["id"]), case_data)
                    case_data["export"]["case_id"] = case_id
                    save_case(con, int(row["id"]), case_data)

                    learning = observe_case_for_learning(con, case_id)
                    if (learning.get("promotion") or {}).get("promoted"):
                        learned = load_buyer_identities(con)
                        case_data = classify_email(
                            email_data, buyer_rules,
                            learned_identities=learned,
                            existing_cases=existing_cases,
                        )
                        if manual_review_gate:
                            case_data = force_operator_review(case_data)
                        case_id = save_case(con, int(row["id"]), case_data)
                        case_data["export"]["case_id"] = case_id
                        save_case(con, int(row["id"]), case_data)

                    existing_cases.append({
                        "from_addr": email_data.get("from_addr"),
                        "subject_template": email_data.get("subject"),
                        "event_type": case_data.get("event_type"),
                    })

                    result["processed"] += 1
                    result["classified"] += 1
                    if learning.get("actions"):
                        result["autolearned"] += 1

                except Exception as exc:
                    result["errors"].append(f"raw_email_id={row['id']}: {exc}")
                    result["skipped"] += 1

    except Exception as exc:
        result["ok"] = False
        result["error"] = str(exc)

    return result


# ── EML import (unchanged) ────────────────────────────────────────────


def import_from_eml_dir(path: str | Path = "/app/data/eml_inbox") -> dict[str, Any]:
    folder = Path(path)
    buyer_rules = load_buyer_rules()
    result: dict[str, Any] = {
        "ok": True, "path": str(folder), "imported": 0, "skipped": 0,
        "classified": 0, "autolearned": 0, "errors": [],
    }
    if not folder.exists():
        return {**result, "ok": False, "error": f"Folder does not exist: {folder}"}
    for file in sorted(folder.glob("*.eml")):
        try:
            email_data = parse_eml_file(file)
            with connect() as con:
                raw_email_id, created = upsert_email(con, email_data)
                result["imported" if created else "skipped"] += 1
                _case_id, learning = _save_classify_learn(con, raw_email_id, email_data, buyer_rules)
                if learning.get("actions"):
                    result["autolearned"] += 1
                result["classified"] += 1
        except Exception as exc:
            result["errors"].append(f"{file.name}: {exc}")
    return result
