"""Единый поиск и трассировка по цепочке: письмо → кейс → стадия → outbox → 1С.

Read-only по данным. Не вызывает AI/1С, не меняет outbox. Не возвращает секреты и полные body
(snippet ограничен). Типизированные результаты с подсказкой, какую вкладку открыть.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any

from .db import loads

SNIPPET_MAX = 300
DEFAULT_LIMIT = 20

# Эвристики типа запроса
_MESSAGE_ID_RE = re.compile(r"^<?[^@\s]+@[^@\s>]+>?$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PART_RE = re.compile(r"^(?=.*\d)[A-Za-z0-9][A-Za-z0-9._\-/]{2,}$")

CASE_JSON_SEARCH_FIELDS = (
    ("claim_number", 94),
    ("client_request_number", 93),
    ("return_number", 92),
    ("order_number", 91),
    ("request_number", 91),
    ("document_number", 89),
    ("part_number", 87),
)


def detect_query_type(q: str) -> str:
    """Грубая детекция: message_id | email | numeric | part_number | text | unknown."""
    s = (q or "").strip()
    if not s:
        return "unknown"
    if s.isdigit():
        return "numeric"
    if "@" in s:
        if "<" in s or ">" in s:
            return "message_id"
        if _EMAIL_RE.match(s):
            return "email"
        if _MESSAGE_ID_RE.match(s):
            return "message_id"
    if _PART_RE.match(s) and any(c.isdigit() for c in s) and any(c.isalpha() for c in s):
        return "part_number"
    return "text"


def _snippet(text: str | None) -> str:
    s = (text or "").strip().replace("\n", " ")
    return s[:SNIPPET_MAX]


def _case_id_for_raw(con: sqlite3.Connection, raw_email_id: int | None) -> int | None:
    if not raw_email_id:
        return None
    r = con.execute("SELECT id FROM cases WHERE raw_email_id=? ORDER BY id LIMIT 1",
                    (raw_email_id,)).fetchone()
    return int(r["id"]) if r else None


def _normalized(value: Any) -> str:
    return str(value if value is not None else "").strip().lower()


def _json_values_for_key(value: Any, key: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        for current_key, current_value in value.items():
            if current_key == key:
                found.append(current_value)
            if isinstance(current_value, (dict, list)):
                found.extend(_json_values_for_key(current_value, key))
    elif isinstance(value, list):
        for item in value:
            found.extend(_json_values_for_key(item, key))
    return found


def _case_json_matches(row: sqlite3.Row, q: str) -> list[tuple[str, int]]:
    query = _normalized(q)
    if not query:
        return []
    documents = (
        loads(row["fields_json"], {}) or {},
        loads(row["payload_json"], {}) or {},
    )
    matches: list[tuple[str, int]] = []
    for field, exact_score in CASE_JSON_SEARCH_FIELDS:
        values = [
            candidate
            for document in documents
            for candidate in _json_values_for_key(document, field)
            if candidate not in (None, "")
        ]
        normalized_values = [_normalized(candidate) for candidate in values]
        if query in normalized_values:
            matches.append((field, exact_score))
        elif any(query in candidate for candidate in normalized_values):
            matches.append((field, exact_score - 12))
    return matches


def _result(rtype: str, rid: Any, title: str, subtitle: str, *, matched_fields: list[str],
            score: int, open_tab: str, open_params: dict, links: dict,
            status: Any = None, buyer_code: Any = None, created_at: Any = None) -> dict[str, Any]:
    return {
        "type": rtype, "id": rid, "title": title or "", "subtitle": subtitle or "",
        "matched_fields": matched_fields, "score": score,
        "open_tab": open_tab, "open_params": open_params,
        "links": {k: v for k, v in links.items() if v is not None},
        "status": status, "buyer_code": buyer_code, "created_at": created_at,
    }


# ── Поиск по сущностям ────────────────────────────────────────────────

def _search_raw_emails(con: sqlite3.Connection, q: str, qtype: str, limit: int) -> list[dict]:
    out: list[dict] = []
    seen: set[int] = set()
    like = f"%{q}%"

    def emit(row: sqlite3.Row, matched: list[str], score: int) -> None:
        rid = int(row["id"])
        if rid in seen:
            return
        seen.add(rid)
        cid = _case_id_for_raw(con, rid)
        out.append(_result(
            "raw_email", rid,
            row["subject"] or "(без темы)",
            f"{row['from_addr'] or ''} · {_snippet(row['snippet'])}",
            matched_fields=matched, score=score,
            open_tab="emails", open_params={"raw_email_id": rid},
            links={"raw_email_id": rid, "case_id": cid},
            status=row["status"], created_at=row["received_at"],
        ))

    cols = "id, subject, from_addr, snippet, status, received_at, message_id"
    if qtype == "numeric":
        for r in con.execute(f"SELECT {cols} FROM raw_emails WHERE id=?", (int(q),)):
            emit(r, ["raw_email_id"], 100)
    if qtype == "message_id" or "@" in q:
        mid = q.strip("<>")
        for r in con.execute(
                f"SELECT {cols} FROM raw_emails "
                f"WHERE lower(trim(message_id, '<>'))=lower(?) LIMIT ?", (mid, limit)):
            emit(r, ["message_id"], 98)
        for r in con.execute(f"SELECT {cols} FROM raw_emails WHERE message_id LIKE ? LIMIT ?",
                             (f"%{mid}%", limit)):
            emit(r, ["message_id"], 80)
        for r in con.execute(f"SELECT {cols} FROM raw_emails WHERE from_addr LIKE ? LIMIT ?",
                             (like, limit)):
            emit(r, ["from_addr"], 75)
    if qtype in ("text", "part_number", "email"):
        for r in con.execute(
                f"SELECT {cols} FROM raw_emails WHERE subject LIKE ? OR from_addr LIKE ? "
                f"OR snippet LIKE ? ORDER BY id DESC LIMIT ?", (like, like, like, limit)):
            matched = []
            if r["subject"] and q.lower() in (r["subject"] or "").lower():
                matched.append("subject")
            if r["from_addr"] and q.lower() in (r["from_addr"] or "").lower():
                matched.append("from_addr")
            if not matched:
                matched = ["snippet"]
            emit(r, matched, 60)
    return out


def _search_cases(con: sqlite3.Connection, q: str, qtype: str, limit: int) -> list[dict]:
    out: list[dict] = []
    seen: set[int] = set()
    like = f"%{q}%"

    def emit(cid: int, matched: list[str], score: int) -> None:
        if cid in seen:
            return
        row = con.execute(
            "SELECT c.id, c.raw_email_id, c.buyer_code, c.buyer_name, c.event_type, c.claim_kind, "
            "c.state, c.status, c.fields_json, c.payload_json, c.created_at, e.subject, e.message_id "
            "FROM cases c LEFT JOIN raw_emails e ON e.id=c.raw_email_id WHERE c.id=?", (cid,)).fetchone()
        if not row:
            return
        seen.add(cid)
        flds = loads(row["fields_json"], {}) or {}
        obx = [int(o["id"]) for o in con.execute(
            "SELECT id FROM outbox WHERE case_id=? ORDER BY id", (cid,)).fetchall()]
        doc = flds.get("document_number")
        part = flds.get("part_number")
        sub = f"{row['buyer_code'] or ''} · {row['event_type'] or ''}"
        if doc:
            sub += f" · док {doc}"
        if part:
            sub += f" · арт {part}"
        out.append(_result(
            "case", cid, row["subject"] or f"кейс #{cid}", sub,
            matched_fields=matched, score=score,
            open_tab="review", open_params={"case_id": cid},
            links={"raw_email_id": row["raw_email_id"], "case_id": cid,
                   "outbox_id": obx[0] if obx else None},
            status=row["state"], buyer_code=row["buyer_code"], created_at=row["created_at"],
        ))

    if qtype == "numeric":
        n = int(q)
        for r in con.execute("SELECT id FROM cases WHERE id=?", (n,)):
            emit(int(r["id"]), ["case_id"], 100)
        for r in con.execute("SELECT id FROM cases WHERE raw_email_id=? LIMIT ?", (n, limit)):
            emit(int(r["id"]), ["raw_email_id"], 95)
        for r in con.execute("SELECT case_id FROM outbox WHERE id=?", (n,)):
            if r["case_id"]:
                emit(int(r["case_id"]), ["outbox_id"], 92)
    else:
        if qtype in ("message_id", "email") or "@" in q:
            mid = q.strip("<>")
            for r in con.execute(
                    "SELECT c.id FROM cases c JOIN raw_emails e ON e.id=c.raw_email_id "
                    "WHERE lower(trim(e.message_id, '<>'))=lower(?) LIMIT ?", (mid, limit)):
                emit(int(r["id"]), ["message_id"], 98)
            for r in con.execute(
                    "SELECT c.id FROM cases c JOIN raw_emails e ON e.id=c.raw_email_id "
                    "WHERE e.message_id LIKE ? LIMIT ?", (f"%{mid}%", limit)):
                emit(int(r["id"]), ["message_id"], 80)
        for r in con.execute(
                "SELECT id FROM cases WHERE buyer_code LIKE ? OR buyer_name LIKE ? LIMIT ?",
                (like, like, limit)):
            emit(int(r["id"]), ["buyer_code"], 70)
        for r in con.execute("SELECT export_json FROM cases WHERE export_json LIKE ? LIMIT 0", (like,)):
            pass  # export_id покрывается ниже при необходимости

    # Business numbers live inside JSON and must be searched for numeric and text queries alike.
    candidates = con.execute(
        """
        SELECT id, fields_json, payload_json
        FROM cases
        WHERE fields_json LIKE ? OR payload_json LIKE ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (like, like, max(limit * 10, 100)),
    ).fetchall()
    for row in candidates:
        matches = _case_json_matches(row, q)
        if not matches:
            continue
        best_score = max(score for _field, score in matches)
        matched_fields = [field for field, score in matches if score == best_score]
        emit(int(row["id"]), matched_fields, best_score)
    return out


def _search_outbox(con: sqlite3.Connection, q: str, qtype: str, limit: int) -> list[dict]:
    out: list[dict] = []
    seen: set[int] = set()
    like = f"%{q}%"

    def emit(row: sqlite3.Row, matched: list[str], score: int) -> None:
        oid = int(row["id"])
        if oid in seen:
            return
        seen.add(oid)
        out.append(_result(
            "outbox", oid,
            f"outbox #{oid} · {row['event_type'] or ''}",
            f"статус {row['status'] or ''}" + (f" · ошибка: {_snippet(row['last_error'])}"
                                              if row["last_error"] else ""),
            matched_fields=matched, score=score,
            open_tab="onec", open_params={"outbox_id": oid, "case_id": row["case_id"]},
            links={"case_id": row["case_id"], "outbox_id": oid},
            status=row["status"], created_at=row["created_at"],
        ))

    cols = "id, case_id, event_type, status, last_error, event_key, created_at"
    if qtype == "numeric":
        for r in con.execute(f"SELECT {cols} FROM outbox WHERE id=?", (int(q),)):
            emit(r, ["outbox_id"], 100)
        for r in con.execute(f"SELECT {cols} FROM outbox WHERE case_id=? ORDER BY id LIMIT ?",
                             (int(q), limit)):
            emit(r, ["case_id"], 85)
    else:
        for r in con.execute(f"SELECT {cols} FROM outbox WHERE event_key LIKE ? LIMIT ?", (like, limit)):
            emit(r, ["event_key"], 80)
        for r in con.execute(f"SELECT {cols} FROM outbox WHERE event_type LIKE ? LIMIT ?", (like, limit)):
            emit(r, ["event_type"], 65)
        for r in con.execute(f"SELECT {cols} FROM outbox WHERE last_error LIKE ? LIMIT ?", (like, limit)):
            emit(r, ["last_error"], 60)
    return out


def _search_clients(con: sqlite3.Connection, q: str, limit: int) -> list[dict]:
    out: list[dict] = []
    like = f"%{q}%"
    try:
        for r in con.execute(
                "SELECT buyer_code, MAX(buyer_name) name, COUNT(*) n FROM cases "
                "WHERE buyer_code LIKE ? OR buyer_name LIKE ? GROUP BY buyer_code LIMIT ?",
                (like, like, limit)):
            if not r["buyer_code"]:
                continue
            out.append(_result(
                "client", r["buyer_code"], r["buyer_code"], f"{r['name'] or ''} · кейсов: {r['n']}",
                matched_fields=["buyer_code"], score=72,
                open_tab="clients", open_params={"buyer_code": r["buyer_code"]},
                links={}, buyer_code=r["buyer_code"]))
    except sqlite3.OperationalError:
        pass
    return out


def unified_search(con: sqlite3.Connection, q: str, *, scope: str = "all",
                   limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Единый поиск. scope: all|emails|cases|outbox|clients."""
    s = (q or "").strip()
    qtype = detect_query_type(s)
    res: dict[str, Any] = {"ok": True, "query": s, "normalized_query": s.strip("<>").lower(),
                           "detected_type": qtype, "scope": scope, "results": []}
    if not s:
        return {**res, "ok": False, "error": "empty_query", "total": 0}

    results: list[dict] = []
    want = (lambda name: scope in ("all", name))
    try:
        if want("emails"):
            results += _search_raw_emails(con, s, qtype, limit)
        if want("cases"):
            results += _search_cases(con, s, qtype, limit)
        if want("outbox"):
            results += _search_outbox(con, s, qtype, limit)
        if want("clients"):
            results += _search_clients(con, s, limit)
    except sqlite3.OperationalError as exc:
        return {**res, "ok": False, "error": str(exc), "total": 0}

    results.sort(key=lambda r: r["score"], reverse=True)
    res["results"] = results[: limit * 2]
    res["total"] = len(res["results"])
    # группировка для UI
    groups: dict[str, int] = {}
    for r in res["results"]:
        groups[r["type"]] = groups.get(r["type"], 0) + 1
    res["groups"] = groups
    return res


# ── Trace: цепочка raw → case → outbox ────────────────────────────────

def _trace_attempts(con: sqlite3.Connection, outbox_id: int) -> list[dict]:
    rows = con.execute(
        "SELECT attempt_no, ok, status_code, error, started_at, finished_at "
        "FROM outbox_attempts WHERE outbox_id=? ORDER BY attempt_no", (outbox_id,)).fetchall()
    return [{"attempt_no": r["attempt_no"], "ok": bool(r["ok"]), "status_code": r["status_code"],
             "error": _snippet(r["error"]), "started_at": r["started_at"],
             "finished_at": r["finished_at"]} for r in rows]


def _resolve_ids(con: sqlite3.Connection, entity_type: str, entity_id: int) -> dict[str, Any]:
    """Свести любой id к raw_email_id + список case_id + outbox_id."""
    raw_id: int | None = None
    case_ids: list[int] = []
    if entity_type == "raw_email":
        raw_id = entity_id
        case_ids = [int(r["id"]) for r in con.execute(
            "SELECT id FROM cases WHERE raw_email_id=? ORDER BY id", (entity_id,))]
    elif entity_type == "case":
        case_ids = [entity_id]
        r = con.execute("SELECT raw_email_id FROM cases WHERE id=?", (entity_id,)).fetchone()
        raw_id = int(r["raw_email_id"]) if r and r["raw_email_id"] else None
    elif entity_type == "outbox":
        r = con.execute("SELECT case_id FROM outbox WHERE id=?", (entity_id,)).fetchone()
        if r and r["case_id"]:
            case_ids = [int(r["case_id"])]
            c = con.execute("SELECT raw_email_id FROM cases WHERE id=?", (int(r["case_id"]),)).fetchone()
            raw_id = int(c["raw_email_id"]) if c and c["raw_email_id"] else None
    return {"raw_email_id": raw_id, "case_ids": case_ids}


def trace(con: sqlite3.Connection, entity_type: str, entity_id: int) -> dict[str, Any]:
    """Полная цепочка для расследования: письмо → кейс(ы) → outbox(ы) → попытки доставки."""
    if entity_type not in ("raw_email", "case", "outbox"):
        return {"ok": False, "error": "bad_entity_type"}
    try:
        entity_id = int(entity_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad_id"}

    ids = _resolve_ids(con, entity_type, entity_id)
    out: dict[str, Any] = {"ok": True, "entity_type": entity_type, "entity_id": entity_id,
                           "raw_email": None, "cases": [], "outbox": []}

    raw_id = ids["raw_email_id"]
    if raw_id:
        e = con.execute(
            "SELECT id, message_id, mailbox, uid, subject, from_addr, received_at, status, "
            "duplicate_of_raw_email_id, snippet FROM raw_emails WHERE id=?", (raw_id,)).fetchone()
        if e:
            d = dict(e)
            d["snippet"] = _snippet(e["snippet"])
            try:
                uv = con.execute("SELECT uidvalidity FROM raw_emails WHERE id=?", (raw_id,)).fetchone()
                d["uidvalidity"] = uv["uidvalidity"] if uv else None
            except sqlite3.OperationalError:
                d["uidvalidity"] = None
            out["raw_email"] = d

    for cid in ids["case_ids"]:
        c = con.execute(
            "SELECT id, raw_email_id, buyer_code, buyer_name, event_type, claim_kind, state, status, "
            "needs_review, ready_for_export, fields_json, quality_json FROM cases WHERE id=?",
            (cid,)).fetchone()
        if not c:
            continue
        flds = loads(c["fields_json"], {}) or {}
        qual = loads(c["quality_json"], {}) or {}
        out["cases"].append({
            "case_id": c["id"], "raw_email_id": c["raw_email_id"], "buyer_code": c["buyer_code"],
            "event_type": c["event_type"], "claim_kind": c["claim_kind"], "state": c["state"],
            "status": c["status"], "needs_review": bool(c["needs_review"]),
            "ready_for_export": bool(c["ready_for_export"]),
            "document_number": flds.get("document_number"), "part_number": flds.get("part_number"),
            "product_name": flds.get("product_name"), "quantity": flds.get("quantity"),
            "pre_delivery_refusal": flds.get("pre_delivery_refusal"),
            "evidence_status": qual.get("status") or qual.get("gate_status"),
        })
        for o in con.execute(
                "SELECT id, event_type, status, event_key, last_error, file_path, created_at, sent_at "
                "FROM outbox WHERE case_id=? ORDER BY id", (cid,)):
            out["outbox"].append({
                "outbox_id": o["id"], "case_id": cid, "event_type": o["event_type"],
                "status": o["status"], "event_key": o["event_key"],
                "last_error": _snippet(o["last_error"]), "file_path": o["file_path"],
                "created_at": o["created_at"], "sent_at": o["sent_at"],
                "attempts": _trace_attempts(con, int(o["id"])),
            })

    out["links"] = {"raw_email_id": raw_id, "case_ids": ids["case_ids"],
                    "outbox_ids": [o["outbox_id"] for o in out["outbox"]]}
    return out
