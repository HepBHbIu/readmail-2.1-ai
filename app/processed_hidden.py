"""Hidden Processed Mail — раздел «Обработанные / не требуют действия».

Слой ПОВЕРХ folder_accounting (без новой SQL-правды): партиционирует все письма на
рабочие папки vs скрытый раздел и даёт сводку/постраничный список по 12 группам.
Read-only: НЕ вызывает AI/1С, НЕ меняет БД/outbox.
"""
from __future__ import annotations

from typing import Any

from . import folder_accounting as fa

# 12 групп скрытого раздела: (key, folder_name, title)
HIDDEN_GROUPS: list[tuple[str, str, str]] = [
    ("correction_edo_ksf", fa.FOLDER_CORRECTION, "Корректировки / ЭДО / КСФ"),
    ("marking_tnved", fa.FOLDER_MARKING, "Маркировка / ТНВЭД"),
    ("number_replacement", fa.FOLDER_NUMBER_REPLACEMENT, "Замена номера / бренда"),
    ("ready_to_ship", fa.FOLDER_READY_TO_SHIP, "Готово к выдаче / забрать возврат"),
    ("linked_active", fa.FOLDER_LINKS_ACTIVE, "Связки активные"),
    ("linked_completed", fa.FOLDER_LINKS_COMPLETED, "Связки завершённые"),
    ("supplier_reports", fa.FOLDER_REPORTS, "Прайсы / отчёты / остатки"),
    ("problem_notice", fa.FOLDER_PROBLEM_NOTICE, "Уведомления о проблеме"),
    ("duplicates", fa.FOLDER_DUPLICATES, "Дубли"),
    ("junk", fa.FOLDER_JUNK, "Не по теме / мусор"),
    ("raw_without_case", fa.FOLDER_RAW_NO_CASE, "Raw без кейса"),
    ("unknown", fa.FOLDER_UNKNOWN, "Неизвестные / ручная классификация"),
]
_KEY_TO_FOLDER = {key: folder for key, folder, _title in HIDDEN_GROUPS}
_KEY_TO_TITLE = {key: title for key, _folder, title in HIDDEN_GROUPS}
_FOLDER_TO_KEY = {folder: key for key, folder, _title in HIDDEN_GROUPS}
_HIDDEN_FOLDERS = set(_KEY_TO_FOLDER.values())

# Рабочие папки (остаются в обычных вкладках оператора) — для accounting-проверки.
WORKING_FOLDERS = [f for f in fa.ALL_FOLDERS if f not in _HIDDEN_FOLDERS]


def build_processed_hidden_summary(con: Any) -> dict[str, Any]:
    """Сводка скрытого раздела поверх folder_accounting. accounted_ok гарантирует sum == total_raw."""
    acc = fa.build_folder_accounting(con, include_items=True)
    items = acc.get("items", [])
    total = acc["total_raw"]
    by_folder = acc["by_folder"]

    groups = []
    sum_groups = 0
    for key, folder, title in HIDDEN_GROUPS:
        folder_items = [it for it in items if it["folder_name"] == folder]
        count = len(folder_items)
        ra = sum(1 for it in folder_items if it["folder_requires_action"])
        sub_counter: dict[str, int] = {}
        for it in folder_items:
            sc = str(it.get("subcategory") or "—")
            sub_counter[sc] = sub_counter.get(sc, 0) + 1
        subgroups = [{"subcategory": sc, "count": n}
                     for sc, n in sorted(sub_counter.items(), key=lambda x: -x[1])]
        sum_groups += count
        groups.append({"key": key, "title": title, "folder_name": folder, "count": count,
                       "requires_action": ra > 0, "requires_action_count": ra,
                       "subgroups": subgroups})

    working_total = sum(int(by_folder.get(f, 0)) for f in WORKING_FOLDERS)
    technical = int(by_folder.get(fa.FOLDER_RAW_NO_CASE, 0)) + int(by_folder.get(fa.FOLDER_UNKNOWN, 0))
    hidden_no_action = sum(g["count"] - g["requires_action_count"] for g in groups)
    accounted_ok = (acc["unaccounted"] == 0) and (sum_groups + working_total == total)
    return {
        "ok": True, "read_only": True,
        "total_raw": total,
        "requires_action": acc["requires_action"],
        "no_action": acc["no_action"],
        "hidden_from_operator": sum_groups,
        "hidden_no_action": hidden_no_action,
        "working_total": working_total,
        "technical": technical,
        "unaccounted": acc["unaccounted"],
        "groups": groups,
        "sum_groups": sum_groups,
        "accounted_ok": accounted_ok,
    }


def _matches(it: dict[str, Any], q: str) -> bool:
    if not q:
        return True
    ql = q.lower()
    return any(ql in str(it.get(k) or "").lower()
               for k in ("subject", "sender", "from_addr", "buyer_code", "subcategory"))


def list_processed_hidden_items(con: Any, *, group: str | None = None, subcategory: str | None = None,
                                page: int = 1, page_size: int = 50, q: str = "") -> dict[str, Any]:
    """Постраничный список писем скрытого раздела (опц. по группе и подкатегории). Read-only."""
    acc = fa.build_folder_accounting(con, include_items=True)
    raw_by_id = {int(r["id"]): dict(r) for r in con.execute(
        "SELECT id, from_addr, received_at FROM raw_emails")}
    has_att = {int(r["raw_email_id"]) for r in con.execute(
        "SELECT DISTINCT raw_email_id FROM attachments")}

    folder_filter = _KEY_TO_FOLDER.get(group) if group else None
    out_items: list[dict[str, Any]] = []
    for it in acc.get("items", []):
        if it["folder_name"] not in _HIDDEN_FOLDERS:
            continue
        if folder_filter and it["folder_name"] != folder_filter:
            continue
        if subcategory and str(it.get("subcategory") or "—") != subcategory:
            continue
        rid = int(it["raw_email_id"]) if it["raw_email_id"] is not None else None
        meta = raw_by_id.get(rid, {})
        row = {
            "raw_email_id": rid, "case_id": it.get("case_id"), "subject": it.get("subject"),
            "from_addr": meta.get("from_addr"), "buyer_code": it.get("buyer_code"),
            "event_type": None, "claim_kind": None,  # заполняется ниже из visible/subcat при необходимости
            "visible_bucket": it.get("visible_bucket"), "folder_name": it.get("folder_name"),
            "folder_group": it.get("folder_group"), "group_key": _FOLDER_TO_KEY.get(it["folder_name"]),
            "subcategory": it.get("subcategory"), "requires_action": it.get("folder_requires_action"),
            "next_action": it.get("next_action"), "routing_reason": it.get("folder_reason"),
            "why_hidden": it.get("folder_reason"), "received_at": meta.get("received_at"),
            "has_attachments": rid in has_att,
            "open_trace_url": (f"/api/cases/{it['case_id']}/decision" if it.get("case_id")
                               else f"/api/raw-emails/{rid}/decision" if rid else None),
            "trace_target": ("case" if it.get("case_id") else "raw"),
            "trace_id": it.get("case_id") or rid,
        }
        if not _matches({**row, "sender": meta.get("from_addr")}, q):
            continue
        out_items.append(row)

    total = len(out_items)
    page = max(1, int(page)); page_size = max(1, int(page_size))
    start = (page - 1) * page_size
    page_items = out_items[start:start + page_size]
    return {"ok": True, "read_only": True, "group": group, "subcategory": subcategory, "q": q, "total": total,
            "page": page, "page_size": page_size,
            "shown_from": (start + 1) if page_items else 0, "shown_to": start + len(page_items),
            "items": page_items}


def render_hidden_summary(summary: dict[str, Any]) -> str:
    L = ["ОБРАБОТАННЫЕ / НЕ ТРЕБУЮТ ДЕЙСТВИЯ",
         f"Всего в разделе: {summary['hidden_from_operator']}", ""]
    for g in summary["groups"]:
        mark = " ⚠" if g["requires_action"] else ""
        L.append(f"  {g['title']}: {g['count']}{mark}")
    L += ["",
          f"total_raw: {summary['total_raw']}",
          f"requires_action (всего): {summary['requires_action']}",
          f"no_action (всего): {summary['no_action']}",
          f"скрытый раздел: {summary['hidden_from_operator']}  (рабочие: {summary['working_total']})",
          f"технические: {summary['technical']}",
          f"unaccounted: {summary['unaccounted']}",
          f"accounted_ok: {'OK ✅ (рабочие + раздел == total_raw)' if summary['accounted_ok'] else 'FAIL ❌'}"]
    return "\n".join(L)
