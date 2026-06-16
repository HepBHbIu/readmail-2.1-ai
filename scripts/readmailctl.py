#!/usr/bin/env python3
r"""
readmailctl.py — терминальное управление Readmail v2 (промежуточный CLI до `python -m readmail`).

Команды:
  status                 — живой статус системы (read-only, без секретов)
  pause [worker]         — поставить на паузу всё или конкретный воркер
  resume [worker]        — снять паузу
  server [--with-workers]— показать баннер и запустить uvicorn (host/port из настроек)
  worker                 — запустить фоновый цикл воркеров (autopilot loop)
  diagnostic             — read-only режим (status + reconcile-сводка)
  reconcile              — запустить read-only сверку IMAP↔БД
  backfill [--from-missing] — targeted backfill недостающих UID
  open-url               — напечатать локальный/LAN URL панели

worker ∈ import|stage2|ai|outbox|delivery|telegram

Безопасность: status/diagnostic ничего не меняют; pause/resume пишут только флаги; реальная 1С/AI
не вызываются этим CLI; секреты не печатаются.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _bootstrap_db() -> None:
    """Указать локальную БД до любых connect() (иначе settings.database_path='/app/data')."""
    from app.config import settings
    from app.runtime_settings import apply_runtime_settings
    db = ROOT / "data" / "readmail.sqlite3"
    settings.database_path = db
    try:
        apply_runtime_settings()
    except Exception:
        pass
    settings.database_path = db  # apply мог перезаписать


def _fmt(title: str, pairs: list[tuple[str, object]]) -> str:
    lines = [title]
    for k, v in pairs:
        lines.append(f"  {k}: {v}")
    return "\n".join(lines)


# ── Фаза 2: единый aggregator статуса для терминала ────────────────────

def _derive_warnings(ov: dict) -> tuple[list[str], list[str]]:
    """Понятные предупреждения и следующие шаги. Без секретов."""
    warns: list[str] = []
    acts: list[str] = []
    outbox = ov.get("outbox") or {}
    new_count = (outbox.get("by_status") or {}).get("new", 0)
    if not outbox.get("delivery_enabled") and new_count:
        warns.append(f"Автодоставка в 1С ВЫКЛЮЧЕНА, в очереди {new_count} событий (status=new) — это НЕ ошибка 1С")
        acts.append("Проверить очередь: readmailctl outbox summary / preview")
    if (outbox.get("by_status") or {}).get("error"):
        warns.append("Есть ошибки доставки outbox (status=error)")
        acts.append("Посмотреть ошибки: readmailctl outbox preview")
    workers = (ov.get("workers") or {})
    if workers.get("global_paused"):
        warns.append("Глобальная пауза включена — воркеры стоят")
        acts.append("Снять паузу: readmailctl resume all")
    mail = ov.get("mail") or {}
    if mail.get("stale"):
        warns.append("Снимок сверки почты устарел")
        acts.append("Обновить сверку: readmailctl reconcile")
    proc = ov.get("processing") or {}
    if proc.get("raw_without_case"):
        warns.append(f"Писем без кейса: {proc.get('raw_without_case')}")
    if (ov.get("auth") or {}).get("bootstrap_required"):
        warns.append("Используется временный admin/admin — смените пароль")
    return warns, acts


def collect_terminal_status() -> dict:
    """Read-only снимок для терминала поверх dashboard.build_overview().

    Не печатает секреты, не меняет БД, не падает без snapshot. Добавляет warnings/next_actions.
    """
    from app import dashboard as dash
    try:
        ov = dash.build_overview()
    except Exception as exc:  # noqa: BLE001
        ov = {"ok": False, "error": str(exc)}
    warns, acts = _derive_warnings(ov)
    ov["warnings"] = warns
    ov["next_actions"] = acts
    return ov


def render_tui_status(status: dict) -> str:
    """Текстовый монитор. Содержит 'READMAIL MONITOR', delivery off/paused, паттерны=0 токенов."""
    srv = status.get("server") or {}
    auth = status.get("auth") or {}
    mail = status.get("mail") or {}
    proc = status.get("processing") or {}
    wk = (status.get("workers") or {}).get("workers") or {}
    gp = (status.get("workers") or {}).get("global_paused")
    ob = status.get("outbox") or {}
    ai = status.get("ai") or {}
    L: list[str] = []
    ts = status.get("generated_at") or ""
    L.append(f"═══ READMAIL MONITOR ═══  {ts}")
    L.append(_fmt("SERVER", [
        ("Status", srv.get("status", "?")),
        ("Host", f"{srv.get('host','?')}:{srv.get('port','?')}"),
        ("Auth", "enabled" if auth.get("enforced") else "disabled"),
        ("LAN", "on" if srv.get("allow_lan") else "off"),
        ("Developer mode", "on" if srv.get("developer_mode") else "off"),
        ("Global pause", "ON" if gp else "off"),
    ]))
    src = "snapshot" + (" · УСТАРЕЛ" if mail.get("stale") else "") if mail.get("checked_at") else "нет снимка"
    L.append(_fmt(f"MAIL [{src}]", [
        ("Server total", mail.get("server_total", "n/a")),
        ("Local raw", mail.get("local_raw_total", "n/a")),
        ("Missing", mail.get("missing_local", "n/a")),
        ("Quarantine", mail.get("quarantine", 0)),
        ("Skipped (до старта)", mail.get("skipped_before_start", 0)),
    ]))
    L.append(_fmt("PROCESSING [live]", [
        ("Cases всего", proc.get("cases_total", "n/a")),
        ("Raw без кейса", proc.get("raw_without_case", "n/a")),
        ("Возвраты (new_return)", proc.get("return_claim", "n/a")),
        ("Готово к 1С", proc.get("ready_to_1c", 0)),
        ("На проверку", proc.get("needs_review", 0)),
    ]))
    delivery_state = (wk.get("delivery") or {}).get("state", "?")
    delivery_lbl = "OFF (автодоставка выключена)" if not ob.get("delivery_enabled") else delivery_state
    L.append(_fmt("OUTBOX / 1C", [
        ("New", (ob.get("by_status") or {}).get("new", 0)),
        ("Error", (ob.get("by_status") or {}).get("error", 0)),
        ("Sent", (ob.get("by_status") or {}).get("sent", 0)),
        ("Контрольные события", ob.get("control_events", 0)),
        ("Возвратные заявки", ob.get("business_events", 0)),
        ("Delivery", delivery_lbl),
    ]))
    L.append("WORKERS")
    for w, v in wk.items():
        st = v.get("state") if isinstance(v, dict) else v
        L.append(f"  {w}: {st}")
    L.append(_fmt("AI", [
        ("Включён", "да" if ai.get("enabled") else "нет"),
        ("Паттерны", "= 0 токенов (без AI)"),
        ("Вызовов сегодня", ai.get("calls_today", 0)),
        ("Стоимость сегодня", ai.get("cost_today", 0)),
        ("Стоимость за месяц", ai.get("cost_month", 0)),
    ]))
    warns = status.get("warnings") or []
    if warns:
        L.append("⚠️  WARNINGS")
        for w in warns:
            L.append(f"  - {w}")
    acts = status.get("next_actions") or []
    if acts:
        L.append("NEXT ACTIONS")
        for a in acts:
            L.append(f"  → {a}")
    return "\n".join(L)


def cmd_status(args: argparse.Namespace) -> int:
    _bootstrap_db()
    status = collect_terminal_status()
    if getattr(args, "json", False):
        print(json.dumps(status, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_tui_status(status))
    return 0


# ── Фаза 7: runtime controls (pause/resume) ────────────────────────────

def _render_runtime(res: dict) -> str:
    if not res.get("ok"):
        valid = ", ".join(res.get("workers", [])) if res.get("workers") else ""
        return f"❌ {res.get('error', 'ошибка')}" + (f"\nДоступные воркеры: all, {valid}" if valid else "")
    st = res.get("status") or {}
    lines = [f"✓ {res.get('action','')} {res.get('worker','')}".strip(),
             f"Global pause: {'ON' if st.get('global_paused') else 'off'}"]
    for w, v in (st.get("workers") or {}).items():
        lines.append(f"  {w}: {v.get('state') if isinstance(v, dict) else v}")
    return "\n".join(lines)


def cmd_pause(args: argparse.Namespace) -> int:
    _bootstrap_db()
    from app import runtime_control
    res = runtime_control.pause(args.worker or "all")
    if getattr(args, "json", False):
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(_render_runtime(res))
    return 0 if res.get("ok") else 2


def cmd_resume(args: argparse.Namespace) -> int:
    _bootstrap_db()
    from app import runtime_control
    res = runtime_control.resume(args.worker or "all")
    if getattr(args, "json", False):
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(_render_runtime(res))
    return 0 if res.get("ok") else 2


# ── Фаза 4-5: CLI search / trace ───────────────────────────────────────

def do_search(q: str, scope: str = "all", limit: int = 20) -> dict:
    from app import search as search_mod
    from app.db import connect
    with connect() as con:
        return search_mod.unified_search(con, q, scope=scope, limit=limit)


def cmd_search(args: argparse.Namespace) -> int:
    _bootstrap_db()
    res = do_search(args.query, scope=args.scope, limit=args.limit)
    if getattr(args, "json", False):
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
        return 0 if res.get("ok") else 2
    if not res.get("ok"):
        print(f"❌ {res.get('error', 'пустой запрос')}")
        return 2
    print(f"Запрос: {res['query']!r}  тип: {res['detected_type']}  найдено: {res['total']}")
    groups: dict[str, list] = {}
    for r in res["results"]:
        groups.setdefault(r["type"], []).append(r)
    labels = {"raw_email": "ПИСЬМА", "case": "КЕЙСЫ", "outbox": "OUTBOX", "client": "КЛИЕНТЫ", "pattern": "ПАТТЕРНЫ"}
    for t, items in groups.items():
        print(f"\n{labels.get(t, t.upper())} ({len(items)})")
        for r in items:
            extra = f" [{r.get('status')}]" if r.get("status") else ""
            buyer = f" {r.get('buyer_code')}" if r.get("buyer_code") else ""
            print(f"  #{r['id']}{extra}{buyer}  {r['title']}  → вкладка: {r['open_tab']}")
            if r.get("subtitle"):
                print(f"      {r['subtitle']}")
    return 0


def do_trace(entity_type: str, entity_id: int) -> dict:
    from app import search as search_mod
    from app.db import connect
    with connect() as con:
        return search_mod.trace(con, entity_type, int(entity_id))


def cmd_trace(args: argparse.Namespace) -> int:
    _bootstrap_db()
    res = do_trace(args.entity_type, args.entity_id)
    if getattr(args, "json", False):
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
        return 0 if res.get("ok") else 2
    if not res.get("ok"):
        print(f"❌ {res.get('error')}")
        return 2
    raw = res.get("raw_email")
    if raw:
        print(_fmt("ПИСЬМО", [
            ("raw_email_id", raw.get("id")),
            ("message_id", raw.get("message_id")),
            ("folder/uid/uidv", f"{raw.get('mailbox')}/{raw.get('uid')}/{raw.get('uidvalidity')}"),
            ("subject", raw.get("subject")),
            ("from", raw.get("from_addr")),
            ("status", raw.get("status")),
            ("duplicate_of", raw.get("duplicate_of_raw_email_id")),
        ]))
        if not getattr(args, "compact", False) and raw.get("snippet"):
            print(f"  snippet: {raw['snippet']}")
    for c in res.get("cases", []):
        print(_fmt(f"КЕЙС #{c.get('case_id')}", [
            ("state", c.get("state")), ("event_type", c.get("event_type")),
            ("claim_kind", c.get("claim_kind")), ("buyer", c.get("buyer_code")),
            ("document_number", c.get("document_number")), ("part_number", c.get("part_number")),
            ("pre_delivery_refusal", c.get("pre_delivery_refusal")),
            ("evidence", c.get("evidence_status")),
            ("ready_to_1c", c.get("ready_for_export")), ("needs_review", c.get("needs_review")),
        ]))
    for o in res.get("outbox", []):
        print(_fmt(f"OUTBOX #{o.get('outbox_id')}", [
            ("event_type", o.get("event_type")), ("status", o.get("status")),
            ("event_key", o.get("event_key")), ("last_error", o.get("last_error")),
            ("attempts", len(o.get("attempts", []))),
        ]))
    print(f"\nLinks: {res.get('links')}")
    return 0


# ── Фаза 6: CLI outbox summary / preview (read-only) ───────────────────

def outbox_summary() -> dict:
    from app import dashboard as dash
    return dash._outbox_overview()


def outbox_preview(ids: list[int] | None = None, limit: int = 5, profile: str | None = None) -> dict:
    from app import dashboard as dash
    from app.db import connect, loads, apply_one_c_payload_profile
    out: dict = {"ok": True, "read_only": True,
                 "delivery": "preview-only — 1С НЕ вызывается, status не меняется",
                 "profile": profile, "items": []}
    with connect() as con:
        if ids:
            ph = ",".join("?" * len(ids))
            rows = con.execute(
                f"SELECT id, case_id, event_type, status, payload_json FROM outbox "
                f"WHERE id IN ({ph}) ORDER BY id", [int(i) for i in ids]).fetchall()
        else:
            rows = con.execute(
                "SELECT id, case_id, event_type, status, payload_json FROM outbox "
                "ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        for r in rows:
            payload = loads(r["payload_json"], {}) or {}
            if profile:
                try:
                    payload = apply_one_c_payload_profile(payload, profile)
                except Exception:  # noqa: BLE001
                    pass
            out["items"].append({
                "outbox_id": r["id"], "case_id": r["case_id"], "event_type": r["event_type"],
                "status": r["status"],
                "kind": "business" if r["event_type"] in dash.BUSINESS_EVENT_TYPES else "control",
                "payload": payload,
            })
    if profile == "debug":
        out["warning"] = "profile=debug — расширенный payload, НЕ для боевой доставки"
    return out


def cmd_outbox(args: argparse.Namespace) -> int:
    _bootstrap_db()
    if args.outbox_cmd == "summary":
        res = outbox_summary()
        if getattr(args, "json", False):
            print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
            return 0
        print(_fmt("OUTBOX SUMMARY (read-only)", [
            ("By status", res.get("by_status")),
            ("Контрольные события", res.get("control_events", 0)),
            ("Возвратные заявки", res.get("business_events", 0)),
            ("Автодоставка", "включена" if res.get("delivery_enabled") else "ВЫКЛЮЧЕНА"),
        ]))
        if res.get("explanation"):
            print(f"\n{res['explanation']}")
        return 0
    # preview
    ids = [int(x) for x in args.ids.split(",")] if getattr(args, "ids", None) else None
    res = outbox_preview(ids=ids, limit=args.limit, profile=args.profile)
    if getattr(args, "json", False):
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
        return 0
    print(f"OUTBOX PREVIEW — {res['delivery']}")
    if res.get("warning"):
        print(f"⚠️  {res['warning']}")
    for it in res["items"]:
        print(_fmt(f"#{it['outbox_id']} [{it['kind']}] {it['event_type']} ({it['status']})", [
            ("case_id", it["case_id"]),
            ("payload keys", ", ".join(sorted((it["payload"] or {}).keys()))),
        ]))
    return 0


def cmd_bucket_report(args: argparse.Namespace) -> int:
    _bootstrap_db()
    from app.db import connect
    # По умолчанию — новая визуальная бухгалтерия (12 buckets, sum == total). --legacy → старый отчёт.
    if not getattr(args, "legacy", False):
        from app import visual_accounting as va
        with connect() as con:
            s = va.build_visual_accounting(con)
        if getattr(args, "json", False):
            print(json.dumps(s, ensure_ascii=False, indent=2, default=str))
            return 0
        print(va.render_bucket_report(s))
        return 0
    from app.bucket_accounting import build_bucket_accounting

    with connect() as con:
        result = build_bucket_accounting(con, include_items=False)
    summary = result["summary"]
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if summary.get("accounting_gap") == 0 else 2
    print(_fmt("BUCKET ACCOUNTING (read-only)", [
        ("Total raw", summary.get("total_raw")),
        ("Accounted", summary.get("accounted_raw")),
        ("Gap", summary.get("accounting_gap")),
        ("With case", summary.get("raw_with_case")),
        ("Without case", summary.get("raw_without_case")),
        ("Hidden from operational tabs", summary.get("hidden_from_operational_tabs")),
        ("Outbox", summary.get("outbox_total")),
    ]))
    print("\nMUTUALLY EXCLUSIVE BUCKETS")
    for name, count in (summary.get("by_bucket") or {}).items():
        print(f"  {name}: {count}")
    print("\nUI VIEWS (OVERLAP; DO NOT SUM)")
    for name, count in (summary.get("by_ui_tab") or {}).items():
        print(f"  {name}: {count}")
    print("\nLINK GROUPS")
    for name, count in (summary.get("by_link_group") or {}).items():
        print(f"  {name}: {count}")
    print("\nSERVICE SUBCATEGORIES")
    for name, count in (summary.get("by_service_subcategory") or {}).items():
        print(f"  {name}: {count}")
    return 0 if summary.get("accounting_gap") == 0 else 2


# ── Фаза 3: tui (живой монитор + меню) ─────────────────────────────────

def _clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def cmd_tui(args: argparse.Namespace) -> int:
    if getattr(args, "shell", False):
        return cmd_shell(args)
    _bootstrap_db()
    if getattr(args, "refresh", 0) and args.refresh > 0:
        # авто-монитор: clear + reprint каждые N секунд
        print(f"[tui] авто-монитор, обновление каждые {args.refresh}s. Ctrl+C для выхода. (read-only)")
        try:
            while True:
                _clear_screen()
                print(render_tui_status(collect_terminal_status()))
                print(f"\n(обновление через {args.refresh}s · Ctrl+C для выхода)")
                time.sleep(args.refresh)
        except KeyboardInterrupt:
            print("\n[tui] остановлен")
            return 0
    return _tui_menu()


def _tui_menu() -> int:
    """Кроссплатформенное меню. По умолчанию read-only; write — только pause/resume."""
    from app import runtime_control
    menu = (
        "\n=== READMAIL MONITOR — меню ===\n"
        "1. Обновить статус\n2. Пауза всех\n3. Возобновить всех\n"
        "4. Worker-test (dry-run)\n5. Reconcile (read-only)\n6. Backfill (dry-run)\n"
        "7. Поиск\n8. Trace\n9. Outbox summary\n10. Outbox preview\n0. Выход\n"
        "Выбор: "
    )
    while True:
        try:
            choice = input(menu).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if choice == "0":
            return 0
        elif choice == "1":
            print(render_tui_status(collect_terminal_status()))
        elif choice == "2":
            print(_render_runtime(runtime_control.pause("all")))
        elif choice == "3":
            print(_render_runtime(runtime_control.resume("all")))
        elif choice == "4":
            from app.worker_test import run_worker_test
            print(json.dumps(run_worker_test(stage="all", limit=10).get("stages"), ensure_ascii=False, indent=2))
        elif choice == "5":
            subprocess.call([sys.executable, str(ROOT / "scripts" / "reconcile_imap_counts.py")], cwd=str(ROOT))
        elif choice == "6":
            subprocess.call([sys.executable, str(ROOT / "scripts" / "backfill_missing_imap_uids.py"), "--dry-run"], cwd=str(ROOT))
        elif choice == "7":
            q = input("Поиск: ").strip()
            r = do_search(q)
            print(json.dumps(r.get("results", []), ensure_ascii=False, indent=2, default=str)[:4000])
        elif choice == "8":
            et = input("entity (raw_email/case/outbox): ").strip()
            eid = input("id: ").strip()
            print(json.dumps(do_trace(et, eid), ensure_ascii=False, indent=2, default=str)[:4000])
        elif choice == "9":
            print(json.dumps(outbox_summary(), ensure_ascii=False, indent=2, default=str))
        elif choice == "10":
            print(json.dumps(outbox_preview(limit=5), ensure_ascii=False, indent=2, default=str)[:4000])
        else:
            print("Неизвестный пункт.")


# ══════════════════════════════════════════════════════════════════════
#  Interactive Shell v1 (readmailctl shell)
# ══════════════════════════════════════════════════════════════════════

class ShellResult:
    __slots__ = ("text", "should_exit")

    def __init__(self, text: str = "", should_exit: bool = False) -> None:
        self.text = text
        self.should_exit = should_exit


# Опасные команды — отключены в shell v1
_DANGEROUS = {"deliver", "reset", "cleanup", "mass-import", "import", "wipe"}
_DISABLED_MSG = ("Команда отключена в shell v1. Используйте delivery preview "
                 "(/outbox preview) и отдельный confirm-flow вне shell.")

SHELL_HELP = """\
SYSTEM
  /status            общий статус
  /refresh           обновить статус
  /open              URL веб-панели
  /doctor            быстрый healthcheck
  /buckets           визуальная бухгалтерия писем (где каждое письмо)
  /folders           рабочие папки писем (сумма папок = total raw)
  /hidden [группа]   «Обработанные / не требуют действия» (скрытый раздел + список группы)
  /pipeline [route] [reason]   canonical pipeline: 6 routes + причина внутри route
  /decision case|raw <id>   почему письмо/кейс попало в свой bucket
  /quit /exit        выход

MAIL
  /mail              состояние почты
  /reconcile         снимок сверки IMAP↔БД (read-only)
  /backfill          подсказка по dry-run backfill
  /quarantine        карантин писем

PROCESSING
  /processing        кейсы по стадиям
  /workers           состояние воркеров
  /pause [worker]    пауза (all|import|stage2|ai|outbox|delivery|telegram)
  /resume [worker]   возобновить
  /worker-test       read-only прогон стадий

SEARCH
  /search <q>        единый поиск
  /trace case <id>   трассировка кейса
  /trace raw <id>    трассировка письма
  /trace outbox <id> трассировка outbox

OUTBOX/1C
  /outbox            сводка outbox
  /outbox preview [N] предпросмотр payload (read-only)
  /payload case|outbox <id> [--profile standard|minimal|debug]  payload 1С (read-only)
  /delivery status   режим интеграции / статус доставки
  /delivery local            статус локального приёмника 1С
  /delivery local last       последние пакеты локального приёмника
  /delivery local-send case <id> --confirm   отправить в локальный приёмник 1С (НЕ реальная 1С)

AI
  /ai                AI brain
  /ai cost           стоимость
  /ai modes          режимы обработки
  /ai smoke case|raw <id> --confirm   контролируемый AI smoke (без --confirm не вызывается)
  /vision            vision-статус

SETTINGS (read-only, без секретов)
  /settings          все разделы
  /settings runtime|import|workers|ai|onec|auth|paths|env-safe

LOGS / DIAGNOSTICS
  /logs              последние события
  /logs errors       последние ошибки
  /reports           список отчётов
  /tests info        инфо о тестах

DANGEROUS (ОТКЛЮЧЕНО в shell v1)
  /deliver  /reset  /cleanup  /mass-import  /ai batch
"""


def render_shell_header(status: dict | None = None) -> str:
    """Компактная шапка shell. Без секретов."""
    if status is None:
        status = collect_terminal_status()
    srv = status.get("server") or {}
    mail = status.get("mail") or {}
    proc = status.get("processing") or {}
    ob = status.get("outbox") or {}
    ai = status.get("ai") or {}
    web = f"http://{srv.get('host', '127.0.0.1')}:{srv.get('port', 8765)}"
    delivery = "OFF" if not ob.get("delivery_enabled") else "ON"
    L = [
        "═══ READMAIL CONTROL SHELL ═══",
        f"SERVER: {str(srv.get('status', '?')).upper()}    WEB: {web}",
        f"MAIL: raw={mail.get('local_raw_total', 'n/a')}, missing={mail.get('missing_local', 'n/a')}, "
        f"quarantine={mail.get('quarantine', 0)}" + ("  [snapshot устарел]" if mail.get("stale") else ""),
        f"PROCESSING: cases={proc.get('cases_total', 'n/a')}, ready={proc.get('ready_to_1c', 0)}, "
        f"review={proc.get('needs_review', 0)}",
        f"OUTBOX/1C: new={(ob.get('by_status') or {}).get('new', 0)}, delivery={delivery}",
        f"AI: {'ON' if ai.get('enabled') else 'OFF'}, patterns=0 tokens, "
        f"today={ai.get('cost_today', 0)}",
        "",
        "Введите / для списка команд.",
    ]
    return "\n".join(L)


# ── settings views (read-only, без секретов) ──────────────────────────

_SECRET_HINT = ("password", "passwd", "secret", "token", "api_key", "apikey", "session_secret",
                "hash", "key")


def _is_secret_key(name: str) -> bool:
    n = name.lower()
    return any(h in n for h in _SECRET_HINT)


def _settings_view(section: str | None) -> str:
    from app.config import settings as st
    from app import runtime_control
    secs = ["runtime", "import", "workers", "ai", "onec", "auth", "paths", "env-safe"]
    section = (section or "").strip().lower()
    if not section:
        return "Разделы: " + ", ".join(secs) + "\nПример: /settings workers"
    out: list[tuple[str, object]] = []
    if section == "runtime":
        rt = runtime_control.get_runtime_status()
        out = [("global_paused", rt.get("global_paused")),
               ("developer_mode", getattr(st, "developer_mode", False))]
        for w, v in (rt.get("workers") or {}).items():
            out.append((f"worker.{w}", v.get("state") if isinstance(v, dict) else v))
    elif section == "import":
        out = [("import_window_enabled", getattr(st, "import_window_enabled", None)),
               ("import_from_datetime", getattr(st, "import_from_datetime", "") or "(не задано)"),
               ("skip_before_start", getattr(st, "skip_before_start", None)),
               ("imap_folders", getattr(st, "imap_folders", ""))]  # без пароля
    elif section == "workers":
        out = [("static_workers", getattr(st, "static_workers", None)),
               ("stage2_workers", getattr(st, "stage2_workers", None)),
               ("ai_text_workers", getattr(st, "ai_text_workers", None)),
               ("ai_vision_workers", getattr(st, "ai_vision_workers", None)),
               ("outbox_workers", getattr(st, "outbox_workers", None)),
               ("max_parallel_cases", getattr(st, "max_parallel_cases", None))]
    elif section == "ai":
        out = [("enabled", getattr(st, "enable_ai", False)),
               ("provider", getattr(st, "ai_provider", "")),
               ("model", getattr(st, "ai_model", "")),
               ("api_key", "**** (скрыто)")]
    elif section == "onec":
        url = getattr(st, "one_c_http_url", "") or ""
        out = [("export_mode", getattr(st, "one_c_export_mode", "")),
               ("file_dir", str(getattr(st, "one_c_file_dir", ""))),
               ("http_url_present", bool(url)),  # сам URL НЕ печатаем
               ("delivery", "off (auto_deliver_outbox=false)" if not getattr(st, "auto_deliver_outbox", False) else "on")]
    elif section == "auth":
        from app import auth as auth_mod
        out = [("auth_enforced", getattr(st, "server_require_auth", False)),
               ("allow_lan", getattr(st, "server_allow_lan", False)),
               ("bootstrap_required", _safe(lambda: auth_mod.bootstrap_required())),
               ("admin_configured", _safe(lambda: auth_mod.admin_configured()))]  # без password_hash
    elif section == "paths":
        out = [("database_path", str(getattr(st, "database_path", ""))),
               ("audit_out", str(ROOT / "audit_out")),
               ("reports", str(ROOT / "reports")),
               ("backups", str(ROOT / "backups"))]
    elif section == "env-safe":
        return _settings_env_safe()
    else:
        return f"Неизвестный раздел: {section}\nДоступно: " + ", ".join(secs)
    return _fmt(f"SETTINGS [{section}]", out)


def _safe(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return "n/a"


def _settings_env_safe() -> str:
    """Ключи из .env.example с masked-значениями. Секреты НИКОГДА не печатаются."""
    example = ROOT / ".env.example"
    keys: list[str] = []
    if example.exists():
        for ln in example.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                keys.append(ln.split("=", 1)[0].strip())
    out: list[tuple[str, object]] = []
    for k in keys:
        present = k in os.environ and os.environ[k] != ""
        if _is_secret_key(k):
            out.append((k, "**** (set)" if present else "(unset)"))
        else:
            out.append((k, os.environ.get(k) if present else "(unset)"))
    return _fmt("SETTINGS [env-safe] (секреты замаскированы)", out)


# ── AI brain / logs / doctor ──────────────────────────────────────────

def _ai_brain() -> str:
    status = collect_terminal_status()
    ai = status.get("ai") or {}
    from app.config import settings as st
    mode = "static_only"
    if getattr(st, "enable_ai", False):
        mode = "full_ai_replay" if getattr(st, "ai_vision_enabled", False) else "static_plus_ai_assist"
    L = ["AI BRAIN",
         f"  Mode: {mode}",
         f"  AI enabled: {bool(ai.get('enabled'))}",
         "  Patterns: 0 tokens (детерминированные паттерны не тратят токены)",
         f"  Calls today: {ai.get('calls_today', 0)}",
         f"  Cost today: {ai.get('cost_today', 0)}",
         f"  Cost month: {ai.get('cost_month', 0)}"]
    if not ai.get("enabled"):
        L.append("  Примечание: AI выключен (enable_ai=false) — вызовы не выполняются.")
    return "\n".join(L)


def _logs(errors: bool = False) -> str:
    from app.db import connect
    L = ["LOGS — ошибки" if errors else "LOGS — последние события"]
    try:
        with connect() as con:
            if errors:
                rows = con.execute(
                    "SELECT id, case_id, last_error FROM outbox WHERE status='error' "
                    "ORDER BY id DESC LIMIT 10").fetchall()
                if not rows:
                    L.append("  outbox errors: нет")
                for r in rows:
                    L.append(f"  outbox#{r['id']} case={r['case_id']}: {(r['last_error'] or '')[:120]}")
                q = con.execute("SELECT mailbox, uid, error_type FROM import_uid_failures "
                                "WHERE status='quarantined' ORDER BY last_seen_at DESC LIMIT 5").fetchall()
                for r in q:
                    L.append(f"  quarantine {r['mailbox']}/{r['uid']}: {r['error_type']}")
            else:
                rows = con.execute(
                    "SELECT job_id, status, finished_at, imported_count FROM import_jobs "
                    "ORDER BY id DESC LIMIT 8").fetchall()
                if not rows:
                    L.append("  лог-источник: import_jobs пуст")
                for r in rows:
                    L.append(f"  job {r['job_id']}: {r['status']} imported={r['imported_count']} @ {r['finished_at']}")
    except Exception as exc:  # noqa: BLE001
        L.append(f"  лог-источник не настроен/недоступен: {exc}")
    return "\n".join(L)


def _reports_list() -> str:
    L = ["REPORTS (reports/*.md)"]
    rep = ROOT / "reports"
    if rep.exists():
        files = sorted(rep.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:12]
        for p in files:
            L.append(f"  {p.name}")
    if not (rep.exists() and any(rep.glob("*.md"))):
        L.append("  (нет отчётов)")
    ao = ROOT / "audit_out"
    if ao.exists():
        L.append("AUDIT_OUT (ключевое)")
        for name in ("imap_reconcile_summary.json", "full_dry_run_summary.json", "worker_test_report.md"):
            if (ao / name).exists():
                L.append(f"  {name}")
    return "\n".join(L)


def _doctor() -> str:
    status = collect_terminal_status()
    srv = status.get("server") or {}
    mail = status.get("mail") or {}
    ob = status.get("outbox") or {}
    ai = status.get("ai") or {}
    auth = status.get("auth") or {}
    from app.config import settings as st
    db_ok = "n/a"
    try:
        from app.db import connect
        with connect() as con:
            con.execute("SELECT 1")
        db_ok = "OK"
    except Exception as exc:  # noqa: BLE001
        db_ok = f"FAIL: {exc}"
    checks = [
        ("DB", db_ok),
        ("Reconcile snapshot", ("есть" + (", УСТАРЕЛ" if mail.get("stale") else "")) if mail.get("checked_at") else "нет"),
        ("Missing local", mail.get("missing_local", "n/a")),
        ("Quarantine", mail.get("quarantine", 0)),
        ("Outbox delivery", "OFF" if not ob.get("delivery_enabled") else "ON"),
        ("Outbox errors", (ob.get("by_status") or {}).get("error", 0)),
        ("Auth", "enabled" if auth.get("enforced") else "disabled"),
        ("AI", "ON" if ai.get("enabled") else "OFF (1С/AI не вызываются)"),
        ("1C export_mode", getattr(st, "one_c_export_mode", "?")),
    ]
    return _fmt("DOCTOR — healthcheck (read-only)", checks)


# ── форматирование search/trace для shell ─────────────────────────────

def _format_search(res: dict) -> str:
    if not res.get("ok"):
        return f"❌ {res.get('error', 'пустой запрос')}"
    L = [f"Запрос: {res['query']!r}  тип: {res['detected_type']}  найдено: {res['total']}"]
    labels = {"raw_email": "ПИСЬМА", "case": "КЕЙСЫ", "outbox": "OUTBOX", "client": "КЛИЕНТЫ", "pattern": "ПАТТЕРНЫ"}
    groups: dict[str, list] = {}
    for r in res["results"]:
        groups.setdefault(r["type"], []).append(r)
    for t, items in groups.items():
        L.append(f"\n{labels.get(t, t.upper())} ({len(items)})")
        for r in items[:8]:
            extra = f" [{r.get('status')}]" if r.get("status") else ""
            L.append(f"  #{r['id']}{extra}  {r['title']}  → /trace {t.replace('raw_email','raw')} {r['id']}")
    return "\n".join(L)


def _format_trace(res: dict) -> str:
    if not res.get("ok"):
        return f"❌ {res.get('error')}"
    L: list[str] = []
    raw = res.get("raw_email")
    if raw:
        L.append(_fmt(f"ПИСЬМО #{raw.get('id')}", [
            ("message_id", raw.get("message_id")), ("subject", raw.get("subject")),
            ("from", raw.get("from_addr")), ("status", raw.get("status")),
            ("duplicate_of", raw.get("duplicate_of_raw_email_id"))]))
    for c in res.get("cases", []):
        L.append(_fmt(f"КЕЙС #{c.get('case_id')}", [
            ("state", c.get("state")), ("event_type", c.get("event_type")),
            ("buyer", c.get("buyer_code")), ("document_number", c.get("document_number")),
            ("part_number", c.get("part_number"))]))
    for o in res.get("outbox", []):
        L.append(_fmt(f"OUTBOX #{o.get('outbox_id')}", [
            ("event_type", o.get("event_type")), ("status", o.get("status")),
            ("last_error", o.get("last_error")), ("attempts", len(o.get("attempts", [])))]))
    L.append(f"Links: {res.get('links')}")
    return "\n".join(L)


# ── роутер shell-команд (тестируемый, без I/O) ────────────────────────

def dispatch_shell_command(line: str) -> ShellResult:
    """Разбор и выполнение одной shell-команды. Read-only кроме /pause /resume.
    1С/AI не вызываются. Опасные команды отключены."""
    s = (line or "").strip()
    if not s:
        return ShellResult("")  # пустой ввод — ничего
    if not s.startswith("/"):
        return ShellResult("Команды начинаются с /. Введите / для списка.")
    parts = s[1:].split()
    cmd = parts[0].lower() if parts else ""
    args = parts[1:]

    if cmd in ("", "help", "?"):
        return ShellResult(SHELL_HELP)
    if cmd in ("quit", "exit", "q"):
        return ShellResult("Выход.", should_exit=True)
    if cmd in ("status", "refresh"):
        return ShellResult(render_tui_status(collect_terminal_status()))
    if cmd == "open":
        from app import server_core
        host, port = server_core.resolve_bind()
        local = "127.0.0.1" if host in ("0.0.0.0", "") else host
        urls = [f"http://{local}:{port}"]
        ip = _safe(lambda: server_core.detect_lan_ip())
        if ip and ip != "n/a":
            urls.append(f"http://{ip}:{port}")
        return ShellResult("Веб-панель:\n  " + "\n  ".join(urls))
    if cmd == "doctor":
        return ShellResult(_doctor())
    if cmd in ("buckets", "bucket-report"):
        from app.db import connect
        from app import visual_accounting as va
        with connect() as con:
            s = va.build_visual_accounting(con)
        return ShellResult(va.render_bucket_report(s))
    if cmd in ("folders", "folder-report"):
        from app.db import connect
        from app import folder_accounting as fa
        with connect() as con:
            summary = fa.build_folder_accounting(con)
        return ShellResult(fa.render_folder_report(summary))
    if cmd == "hidden":
        from app.db import connect
        from app import processed_hidden as ph
        with connect() as con:
            if args:  # /hidden <group> — список группы
                key = args[0].lower()
                res = ph.list_processed_hidden_items(con, group=key, page_size=20)
                L = [f"СКРЫТЫЙ РАЗДЕЛ — {key} — показано {res['shown_from']}-{res['shown_to']} из {res['total']}"]
                for it in res["items"]:
                    L.append(f"  #{it['raw_email_id']} case={it['case_id']} · {(it['subject'] or '')[:55]} · "
                             f"{it['why_hidden']} · trace: /decision {it['trace_target']} {it['trace_id']}")
                return ShellResult("\n".join(L) if res["items"] else f"Группа {key}: пусто (или неизвестна)")
            return ShellResult(ph.render_hidden_summary(ph.build_processed_hidden_summary(con)))
    if cmd == "pipeline":
        from app.db import connect
        from app import canonical_pipeline as cp
        with connect() as con:
            if args:  # /pipeline <route> [reason]
                route = args[0].lower()
                reason = args[1].lower() if len(args) > 1 else None
                res = cp.list_pipeline_items(con, route=route, reason=reason, page_size=20)
                L = [f"PIPELINE — {route}{('/' + reason) if reason else ''} — "
                     f"показано {res['shown_from']}-{res['shown_to']} из {res['total']}"]
                for it in res["items"]:
                    L.append(f"  #{it['raw_email_id']} case={it['case_id']} · {(it['subject'] or '')[:50]} · "
                             f"{it['reason_label']} · → {it['next_action']}")
                return ShellResult("\n".join(L) if res["items"] else f"{route}: пусто")
            return ShellResult(cp.render_pipeline_report(cp.build_pipeline_accounting(con)))
    if cmd == "decision":
        if len(args) < 2 or args[0].lower() not in ("case", "raw"):
            return ShellResult("Использование: /decision case|raw <id>")
        if not args[1].isdigit():
            return ShellResult("id должен быть числом")
        from app.db import connect
        from app import visual_accounting as va
        with connect() as con:
            res = (va.decision_for_case(con, int(args[1])) if args[0].lower() == "case"
                   else va.decision_for_raw(con, int(args[1])))
        return ShellResult(json.dumps(res, ensure_ascii=False, indent=2, default=str))

    # MAIL
    if cmd == "mail":
        m = (collect_terminal_status().get("mail") or {})
        return ShellResult(_fmt("MAIL", [
            ("server_total", m.get("server_total")), ("local_raw", m.get("local_raw_total")),
            ("missing", m.get("missing_local")), ("quarantine", m.get("quarantine", 0)),
            ("snapshot", ("устарел" if m.get("stale") else "свежий") if m.get("checked_at") else "нет")]))
    if cmd == "reconcile":
        m = (collect_terminal_status().get("mail") or {})
        return ShellResult("RECONCILE (снимок, read-only)\n" + _fmt("", [
            ("server_total", m.get("server_total")), ("local_raw", m.get("local_raw_total")),
            ("missing", m.get("missing_local"))]) +
            "\nДля свежей сверки выйдите и запустите: readmailctl reconcile")
    if cmd == "backfill":
        return ShellResult("Backfill в shell только как подсказка (write-действие вне shell):\n"
                           "  readmailctl backfill --from-missing --dry-run")
    if cmd == "quarantine":
        m = (collect_terminal_status().get("mail") or {})
        return ShellResult(_fmt("QUARANTINE", [("в карантине", m.get("quarantine", 0)),
                                               ("skipped (до старта)", m.get("skipped_before_start", 0))]))

    # PROCESSING
    if cmd == "processing":
        p = (collect_terminal_status().get("processing") or {})
        return ShellResult(_fmt("PROCESSING", [
            ("cases всего", p.get("cases_total")), ("raw без кейса", p.get("raw_without_case")),
            ("возвраты", p.get("return_claim")), ("готово к 1С", p.get("ready_to_1c", 0)),
            ("на проверку", p.get("needs_review", 0))]))
    if cmd == "workers":
        wk = (collect_terminal_status().get("workers") or {}).get("workers") or {}
        L = ["WORKERS"] + [f"  {w}: {(v.get('state') if isinstance(v, dict) else v)}" for w, v in wk.items()]
        return ShellResult("\n".join(L))
    if cmd == "pause":
        from app import runtime_control
        return ShellResult(_render_runtime(runtime_control.pause(args[0] if args else "all")))
    if cmd == "resume":
        from app import runtime_control
        return ShellResult(_render_runtime(runtime_control.resume(args[0] if args else "all")))
    if cmd == "worker-test":
        from app.worker_test import run_worker_test
        rep = run_worker_test(stage="all", limit=10)
        return ShellResult("WORKER-TEST (read-only)\n" + json.dumps(rep.get("stages"), ensure_ascii=False, indent=2))

    # SEARCH / TRACE
    if cmd == "search":
        if not args:
            return ShellResult("Использование: /search <запрос>")
        return ShellResult(_format_search(do_search(" ".join(args))))
    if cmd == "trace":
        if len(args) < 2:
            return ShellResult("Использование: /trace case|raw|outbox <id>")
        et = {"raw": "raw_email", "email": "raw_email"}.get(args[0].lower(), args[0].lower())
        if et not in ("raw_email", "case", "outbox"):
            return ShellResult("Тип: case | raw | outbox")
        try:
            eid = int(args[1])
        except ValueError:
            return ShellResult("id должен быть числом")
        return ShellResult(_format_trace(do_trace(et, eid)))

    # OUTBOX / DELIVERY
    if cmd == "outbox":
        if args and args[0].lower() == "preview":
            ids = None
            limit = 5
            rest = args[1:]
            if rest and rest[0].lower() == "ids" and len(rest) > 1:
                ids = [int(x) for x in rest[1].split(",") if x.strip().isdigit()]
            elif rest and rest[0].isdigit():
                limit = int(rest[0])
            res = outbox_preview(ids=ids, limit=limit)
            L = [f"OUTBOX PREVIEW — {res['delivery']}"]
            if res.get("warning"):
                L.append(f"⚠️  {res['warning']}")
            for it in res["items"]:
                L.append(f"  #{it['outbox_id']} [{it['kind']}] {it['event_type']} ({it['status']}) "
                         f"keys: {', '.join(sorted((it['payload'] or {}).keys()))}")
            return ShellResult("\n".join(L))
        res = outbox_summary()
        return ShellResult(_fmt("OUTBOX", [
            ("by_status", res.get("by_status")), ("контрольные", res.get("control_events", 0)),
            ("возвратные", res.get("business_events", 0)),
            ("автодоставка", "включена" if res.get("delivery_enabled") else "ВЫКЛЮЧЕНА")]) +
            (f"\n{res.get('explanation')}" if res.get("explanation") else ""))
    if cmd == "delivery":
        if args and args[0].lower() == "local-send":  # /delivery local-send case <id> --confirm
            from app import local_1c
            if "--confirm" not in args:
                return ShellResult("Нужен --confirm: /delivery local-send case <id> --confirm "
                                   "(реальная внешняя 1С НЕ вызывается)")
            cid = next((a for a in args[1:] if a.isdigit()), None)
            if not cid:
                return ShellResult("Использование: /delivery local-send case <case_id> --confirm")
            return ShellResult(json.dumps(local_1c.send_case(int(cid)), ensure_ascii=False, indent=2))
        if args and args[0].lower() in ("local", "demo"):  # "demo" — скрытый back-compat alias
            from app import local_1c
            sub = args[1].lower() if len(args) > 1 else "status"
            if sub == "status":
                return ShellResult(_fmt("ЛОКАЛЬНЫЙ ПРИЁМНИК 1С (НЕ реальная внешняя 1С)",
                                        list(local_1c.receiver_status().items())))
            if sub == "last":
                res = local_1c.get_events(limit=10)
                L = [f"Локальный приёмник 1С — последние {res['returned']}/{res['total']}"]
                for e in res["events"]:
                    L.append(f"  {e['received_at']} · case {e['case_id']} · {e['event_type']} · "
                             f"{json.dumps(e['payload_summary'], ensure_ascii=False)}")
                return ShellResult("\n".join(L))
            if sub in ("send-case", "send"):
                if "--confirm" not in args:
                    return ShellResult("Нужен --confirm: /delivery local-send case <id> --confirm "
                                       "(реальная внешняя 1С НЕ вызывается)")
                cid = next((a for a in args[2:] if a.isdigit()), None)
                if not cid:
                    return ShellResult("Использование: /delivery local send-case <case_id> --confirm")
                return ShellResult(json.dumps(local_1c.send_case(int(cid)), ensure_ascii=False, indent=2))
            return ShellResult("Использование: /delivery local status|last|send-case <id> --confirm")
        ob = (collect_terminal_status().get("outbox") or {})
        return ShellResult(f"Delivery: {'ON' if ob.get('delivery_enabled') else 'OFF (автодоставка выключена, 1С не вызывается)'}")
    if cmd == "payload":
        if len(args) < 2 or args[0].lower() not in ("case", "outbox"):
            return ShellResult("Использование: /payload case|outbox <id> [--profile standard|minimal|debug]")
        prof = "standard"
        if "--profile" in args:
            i = args.index("--profile")
            if i + 1 < len(args):
                prof = args[i + 1]
        cid = next((a for a in args[1:] if a.isdigit()), None)
        if not cid:
            return ShellResult("id должен быть числом")
        from app.db import connect, build_case_event_payload, apply_one_c_payload_profile, loads
        with connect() as con:
            if args[0].lower() == "case":
                p = build_case_event_payload(con, int(cid), profile=prof)
            else:
                con.row_factory = __import__("sqlite3").Row
                row = con.execute("SELECT payload_json FROM outbox WHERE id=?", (int(cid),)).fetchone()
                p = apply_one_c_payload_profile(loads(row["payload_json"], {}) or {}, prof) if row else None
        if not p:
            return ShellResult(f"payload не построен: {args[0]} {cid} не найден")
        return ShellResult(f"PAYLOAD {args[0]} {cid} (profile={prof}, read-only)\n" +
                           json.dumps(p, ensure_ascii=False, indent=1, default=str))

    # AI
    if cmd == "ai":
        if args and args[0].lower() == "batch":
            return ShellResult(_DISABLED_MSG + " (AI batch не запускается)")
        if args and args[0].lower() == "smoke":
            if len(args) < 3 or args[1].lower() not in ("case", "raw"):
                return ShellResult("Использование: /ai smoke case|raw <id> [--confirm] (без --confirm AI не вызывается)")
            cid = next((a for a in args[2:] if a.isdigit()), None)
            if not cid:
                return ShellResult("id должен быть числом")
            from app import ai_smoke
            kw = {("case_id" if args[1].lower() == "case" else "raw_email_id"): int(cid),
                  "confirm": "--confirm" in args, "mock": "--mock" in args}
            res = ai_smoke.run_ai_smoke(**kw)
            if not res.get("ok") and res.get("error") == "confirm_required":
                return ShellResult("⚠️ Без --confirm AI не вызывается. /ai smoke case <id> --confirm")
            L = [f"AI SMOKE case={res.get('case_id')} called={res.get('called')} auto_applied={res.get('auto_applied')} "
                 f"tokens={res.get('prompt_tokens')}/{res.get('completion_tokens')}"]
            for k, d in (res.get("diff") or {}).items():
                L.append(f"  {k:16} {'≠' if d.get('changed') else '='} current={d.get('current')!r} ai={d.get('ai')!r}")
            bg = res.get("brand_guard") or {}
            if bg and not bg.get("ok"):
                L.append(f"  ⚠️ BRAND GUARD: {bg.get('warning')}")
            return ShellResult("\n".join(L))
        if args and args[0].lower() == "cost":
            ai = collect_terminal_status().get("ai") or {}
            return ShellResult(_fmt("AI COST", [("today", ai.get("cost_today", 0)),
                                               ("month", ai.get("cost_month", 0)),
                                               ("calls today", ai.get("calls_today", 0)),
                                               ("patterns", "0 tokens")]))
        if args and args[0].lower() == "modes":
            return ShellResult("AI MODES\n  static_only — паттерны, 0 токенов\n"
                               "  static_plus_ai_assist — AI добор отдельных полей\n"
                               "  full_ai_replay — инженерный/дорогой режим")
        return ShellResult(_ai_brain())
    if cmd == "vision":
        from app.config import settings as st
        return ShellResult(f"Vision enabled: {bool(getattr(st, 'ai_vision_enabled', False))} "
                           f"(workers={getattr(st, 'ai_vision_workers', 0)})")

    # SETTINGS
    if cmd == "settings":
        return ShellResult(_settings_view(args[0] if args else None))

    # LOGS / REPORTS / TESTS
    if cmd == "logs":
        return ShellResult(_logs(errors=bool(args and args[0].lower() == "errors")))
    if cmd == "reports":
        return ShellResult(_reports_list())
    if cmd == "tests":
        n = len(list((ROOT / "tests").glob("test_*.py")))
        return ShellResult(f"TESTS\n  тест-файлов: {n}\n  запуск: python3 -m pytest tests/ -q")

    # DANGEROUS — отключено
    if cmd in _DANGEROUS:
        return ShellResult(_DISABLED_MSG)

    return ShellResult(f"Неизвестная команда: /{cmd}. Введите / для списка.")


def cmd_shell(args: argparse.Namespace) -> int:
    _bootstrap_db()
    print(render_shell_header())
    while True:
        try:
            line = input("readmail> ")
        except (EOFError, KeyboardInterrupt):
            print("\nВыход.")
            return 0
        res = dispatch_shell_command(line)
        if res.text:
            print(res.text)
        if res.should_exit:
            return 0


def cmd_server(args: argparse.Namespace) -> int:
    _bootstrap_db()
    from app import runtime_control, server_core
    host, port = server_core.resolve_bind()
    print(server_core.startup_banner(runtime_control.get_runtime_status()))
    print()
    if server_core.auth_required() and not server_core.admin_configured():
        print("⚠️  Auth включён, но admin не создан. Создайте admin перед открытием в LAN.")
    cmd = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", host, "--port", str(port), "--workers", "1"]
    print("Launch:", " ".join(cmd))
    return subprocess.call(cmd, cwd=str(ROOT))


def cmd_worker(args: argparse.Namespace) -> int:
    """Запустить фоновый цикл воркеров (autopilot loop) в этом процессе.

    Воркеры уважают runtime_control: pause/resume управляют через `readmailctl pause/resume`.
    """
    _bootstrap_db()
    from app.main import _autopilot_cycle
    from app.config import settings
    interval = int(getattr(settings, "scan_interval_seconds", 30) or 30) or 30
    print(f"[worker] autopilot loop, interval={interval}s. Ctrl+C для остановки. "
          f"Управление паузой: readmailctl pause/resume.")
    try:
        while True:
            cycle = _autopilot_cycle(import_limit=int(getattr(settings, "imap_limit", 50) or 50),
                                     ai_limit=10, deliver=False, mode="pattern")
            print(f"[worker] cycle: ok={cycle.get('ok')} import={cycle.get('import',{}).get('imported','?')}")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[worker] stopped")
        return 0


def cmd_diagnostic(args: argparse.Namespace) -> int:
    rc = cmd_status(args)
    print("\n[diagnostic] read-only. Для свежей сверки: readmailctl reconcile")
    return rc


def cmd_reconcile(args: argparse.Namespace) -> int:
    return subprocess.call([sys.executable, str(ROOT / "scripts" / "reconcile_imap_counts.py")], cwd=str(ROOT))


def cmd_backfill(args: argparse.Namespace) -> int:
    cmd = [sys.executable, str(ROOT / "scripts" / "backfill_missing_imap_uids.py")]
    if args.from_missing:
        cmd += ["--from-missing", "audit_out/imap_reconcile_missing_server_uids.jsonl"]
    if args.apply:
        cmd += ["--apply"]
    else:
        cmd += ["--dry-run"]
    return subprocess.call(cmd, cwd=str(ROOT))


def cmd_worker_test(args: argparse.Namespace) -> int:
    _bootstrap_db()
    from app.worker_test import run_worker_test, write_report
    report = run_worker_test(stage=args.stage, limit=args.limit)
    write_report(report, ROOT / "audit_out")
    print(json.dumps({"stage": report.get("stage"), "stages": report.get("stages")},
                     ensure_ascii=False, indent=2))
    print(f"\nОтчёт: audit_out/worker_test_report.md (read-only, без AI/1С/outbox)")
    return 0 if report.get("ok") else 1


def cmd_open_url(args: argparse.Namespace) -> int:
    _bootstrap_db()
    from app import server_core
    host, port = server_core.resolve_bind()
    local = "127.0.0.1" if host in ("0.0.0.0", "") else host
    print(f"http://{local}:{port}")
    ip = server_core.detect_lan_ip()
    if ip:
        print(f"http://{ip}:{port}")
    return 0


# ── Demo AI + 1C receiver команды ──────────────────────────────────────

def cmd_payload(args: argparse.Namespace) -> int:
    """Построить payload от кейса/outbox напрямую (read-only, без создания outbox)."""
    _bootstrap_db()
    from app.db import connect, build_case_event_payload, apply_one_c_payload_profile, loads
    prof = args.profile
    with connect() as con:
        if args.target == "case":
            payload = build_case_event_payload(con, int(args.id), profile=prof)
        else:  # outbox
            con.row_factory = __import__("sqlite3").Row
            row = con.execute("SELECT payload_json FROM outbox WHERE id=?", (int(args.id),)).fetchone()
            payload = apply_one_c_payload_profile(loads(row["payload_json"], {}) or {}, prof) if row else None
    if not payload:
        print(f"payload не построен: {args.target} {args.id} не найден")
        return 1
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0
    print(f"PAYLOAD {args.target} {args.id} — profile={prof} (read-only, outbox НЕ создаётся)")
    print(json.dumps(payload, ensure_ascii=False, indent=1, default=str))
    return 0


def cmd_local_1c(args: argparse.Namespace) -> int:
    """Локальный приёмник 1С: status | last | send-case | clear. НЕ реальная внешняя 1С."""
    _bootstrap_db()
    from app import local_1c
    sub = getattr(args, "local_cmd", None) or getattr(args, "demo_cmd", None)
    if sub == "status":
        print(json.dumps(local_1c.receiver_status(), ensure_ascii=False, indent=2, default=str))
        return 0
    if sub == "last":
        res = local_1c.get_events(limit=args.limit, include_payload=getattr(args, "full", False))
        if getattr(args, "json", False):
            print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
            return 0
        print(f"ЛОКАЛЬНЫЙ ПРИЁМНИК 1С — последние {res['returned']}/{res['total']} (журнал: {res['log']})")
        for e in res["events"]:
            print(_fmt(f"{e['received_at']} · case {e['case_id']} · {e['event_type']} · {e['payload_profile']}", [
                ("summary", json.dumps(e["payload_summary"], ensure_ascii=False)),
                ("keys", ", ".join(e["payload_keys"] or [])),
            ]))
        return 0
    if sub == "send-case":
        if not getattr(args, "confirm", False):
            print("Нужен --confirm для отправки в локальный приёмник 1С (реальная внешняя 1С НЕ вызывается).")
            return 1
        res = local_1c.send_case(int(args.id), profile=args.profile)
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
        return 0 if res.get("ok") else 1
    if sub == "clear":
        if not getattr(args, "confirm", False):
            print("Нужен --confirm для очистки журнала локального приёмника.")
            return 1
        print(json.dumps(local_1c.clear_events(confirm=True), ensure_ascii=False))
        return 0
    return 1


def cmd_ai_smoke(args: argparse.Namespace) -> int:
    """Контролируемый AI smoke-test (manual, --confirm). НЕ auto-apply, НЕ создаёт outbox."""
    _bootstrap_db()
    from app import ai_smoke
    kwargs = {"confirm": getattr(args, "confirm", False), "mock": getattr(args, "mock", False),
              "max_output_tokens": getattr(args, "max_output_tokens", 1024)}
    if args.target == "case":
        kwargs["case_id"] = int(args.id)
    else:
        kwargs["raw_email_id"] = int(args.id)
    res = ai_smoke.run_ai_smoke(**kwargs)
    if getattr(args, "json", False):
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
        return 0
    s = res.get("ai_settings") or {}
    print(_fmt("AI SMOKE — настройки", [
        ("provider/model", f"{s.get('provider')} / {s.get('model')}"),
        ("api_key_present", s.get("api_key_present")), ("max_output_tokens(smoke)", kwargs["max_output_tokens"]),
        ("response_format", s.get("response_format")), ("cache", s.get("cache_enabled")),
    ]))
    if not res.get("ok") and res.get("error") == "confirm_required":
        print("\n⚠️  Без --confirm AI не вызывается. Добавьте --confirm (или --mock для проверки без вызова).")
        return 1
    print(f"\nВызов: called={res.get('called')} mock={getattr(args,'mock',False)} "
          f"auto_applied={res.get('auto_applied')} tokens={res.get('prompt_tokens')}/{res.get('completion_tokens')}")
    if res.get("error"):
        print(f"  ошибка: {res['error']}")
    print("  DIFF (current vs AI):")
    for k, d in (res.get("diff") or {}).items():
        mark = "≠" if d.get("changed") else "="
        print(f"    {k:16} {mark} current={d.get('current')!r}  ai={d.get('ai')!r}")
    bg = res.get("brand_guard") or {}
    if bg and not bg.get("ok"):
        print(f"  ⚠️  BRAND GUARD: {bg.get('warning')}")
    print(f"  sink: {res.get('sink')}")
    return 0 if res.get("ok") else 1


def cmd_ai_settings(args: argparse.Namespace) -> int:
    """Показать текущие AI-настройки (секреты маскируются) + где менять на Qwen."""
    _bootstrap_db()
    from app import ai_smoke
    from app.config import settings as _s
    info = ai_smoke.current_ai_settings()
    if getattr(args, "json", False):
        print(json.dumps(info, ensure_ascii=False, indent=2, default=str))
        return 0
    base = info.get("base_url") or ""
    masked = (base[:18] + "…") if len(base) > 20 else base
    print(_fmt("AI SETTINGS (read-only)", [
        ("enabled", info["enable_ai"]),
        ("text model", info["model"]),
        ("vision model", getattr(_s, "ai_vision_model", "")),
        ("vision enabled", getattr(_s, "ai_vision_enabled", False)),
        ("provider", info["provider"]),
        ("base_url (masked)", masked),
        ("api_key_present", info["api_key_present"]),
        ("max_output_tokens", info["max_output_tokens"]),
        ("context_mode", info["context_mode"]),
        ("conserve_tokens", info["conserve_tokens"]),
        ("cache_enabled", info["cache_enabled"]),
        ("defect_doc_ai_read", getattr(_s, "defect_doc_ai_read", False)),
    ]))
    print("\nГде менять модель (app_settings / .env):")
    print("  текст:   AI_MODEL=…            (или ROUTERAI_DEFAULT_MODEL=…)")
    print("  vision:  AI_VISION_MODEL=qwen/qwen2.5-vl-7b-instruct  (vision НЕ включать автоматически)")
    return 0


def cmd_demo_plan(args: argparse.Namespace) -> int:
    """Печать готового сценария демонстрации."""
    lines = [
        "СЦЕНАРИЙ ДЕМО (readmail_v2)",
        "  1. python3 scripts/readmailctl.py status",
        "  2. python3 scripts/readmailctl.py search \"00000230135\"      # поиск по № претензии → case 40853",
        "  3. python3 scripts/readmailctl.py trace case 40853           # письмо→кейс→поля",
        "  4. python3 scripts/readmailctl.py payload case 41972 --profile standard   # чистый 1С payload",
        "  5. python3 scripts/readmailctl.py local-1c send-case 41972 --profile standard --confirm",
        "  6. python3 scripts/readmailctl.py local-1c last             # журнал локального приёмника 1С",
        "  7. python3 scripts/readmailctl.py search \"82412\"            # поиск по документу",
        "  8. python3 scripts/readmailctl.py search \"прайс\"           # мусор НЕ стал возвратом (info_only)",
        "  9. python3 scripts/readmailctl.py trace case 41936           # followup не уйдёт в 1С",
        " 10. python3 scripts/readmailctl.py ai-smoke case 40853 --confirm   # (опц.) контролируемый AI",
        " 11. python3 scripts/readmailctl.py shell                     # /doctor /search /trace /payload /delivery demo last",
    ]
    print("\n".join(lines))
    return 0


def cmd_decision(args: argparse.Namespace) -> int:
    """Decision trace для case/raw (read-only, AI/1С не вызываются)."""
    _bootstrap_db()
    from app.db import connect
    from app import visual_accounting as va
    with connect() as con:
        res = va.decision_for_case(con, int(args.id)) if args.target == "case" \
            else va.decision_for_raw(con, int(args.id))
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    return 0 if res.get("ok") else 1


def cmd_folder_report(args: argparse.Namespace) -> int:
    """Complete read-only operator folder accounting."""
    _bootstrap_db()
    from app.db import connect
    from app import folder_accounting as fa
    with connect() as con:
        result = fa.build_folder_accounting(con, include_items=False)
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(fa.render_folder_report(result))
    return 0 if result["unaccounted"] == 0 else 2


def cmd_hidden_summary(args: argparse.Namespace) -> int:
    """Раздел «Обработанные / не требуют действия» (read-only)."""
    _bootstrap_db()
    from app.db import connect
    from app import processed_hidden as ph
    with connect() as con:
        s = ph.build_processed_hidden_summary(con)
    if getattr(args, "json", False):
        print(json.dumps(s, ensure_ascii=False, indent=2, default=str))
        return 0 if s["accounted_ok"] else 2
    print(ph.render_hidden_summary(s))
    return 0 if s["accounted_ok"] else 2


def cmd_hidden_list(args: argparse.Namespace) -> int:
    """Список писем скрытого раздела (опц. --group), read-only."""
    _bootstrap_db()
    from app.db import connect
    from app import processed_hidden as ph
    with connect() as con:
        res = ph.list_processed_hidden_items(con, group=getattr(args, "group", None),
                                             page=getattr(args, "page", 1),
                                             page_size=getattr(args, "limit", 50),
                                             q=getattr(args, "q", "") or "")
    if getattr(args, "json", False):
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
        return 0
    print(f"СКРЫТЫЙ РАЗДЕЛ — группа={res['group'] or 'все'} — показано {res['shown_from']}-{res['shown_to']} из {res['total']}")
    for it in res["items"]:
        print(_fmt(f"#{it['raw_email_id']} case={it['case_id']} [{it['folder_name']}]", [
            ("subject", (it["subject"] or "")[:70]), ("buyer", it["buyer_code"]),
            ("why_hidden", it["why_hidden"]), ("next_action", it["next_action"]),
            ("trace", f"decision {it['trace_target']} {it['trace_id']}")]))
    return 0


def cmd_pipeline_report(args: argparse.Namespace) -> int:
    """Canonical pipeline: 6 routes + reason breakdown (read-only)."""
    _bootstrap_db()
    from app.db import connect
    from app import canonical_pipeline as cp
    with connect() as con:
        acc = cp.build_pipeline_accounting(con)
    if getattr(args, "json", False):
        print(json.dumps(acc, ensure_ascii=False, indent=2, default=str))
        return 0 if acc["unaccounted"] == 0 else 2
    print(cp.render_pipeline_report(acc))
    return 0 if acc["unaccounted"] == 0 else 2


def cmd_pipeline_list(args: argparse.Namespace) -> int:
    """Письма pipeline по route/reason (read-only)."""
    _bootstrap_db()
    from app.db import connect
    from app import canonical_pipeline as cp
    with connect() as con:
        res = cp.list_pipeline_items(con, route=getattr(args, "route", None),
                                     reason=getattr(args, "reason", None),
                                     page=getattr(args, "page", 1),
                                     page_size=getattr(args, "limit", 20), q=getattr(args, "q", "") or "")
    if getattr(args, "json", False):
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
        return 0
    print(f"PIPELINE — route={res['route'] or 'все'} reason={res['reason'] or 'все'} — "
          f"показано {res['shown_from']}-{res['shown_to']} из {res['total']}")
    for it in res["items"]:
        print(_fmt(f"#{it['raw_email_id']} case={it['case_id']} [{it['canonical_route']}/{it['reason_group']}]", [
            ("subject", (it["subject"] or "")[:66]), ("buyer", it["buyer_code"]),
            ("next_action", it["next_action"]), ("parent", it.get("parent_case_id")),
            ("can_send_1c", it["can_send_to_1c"])]))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Readmail terminal control")
    sub = ap.add_subparsers(dest="command", required=True)
    p_st = sub.add_parser("status"); p_st.add_argument("--json", action="store_true"); p_st.set_defaults(func=cmd_status)
    p_pause = sub.add_parser("pause"); p_pause.add_argument("worker", nargs="?", default="all"); p_pause.add_argument("--json", action="store_true"); p_pause.set_defaults(func=cmd_pause)
    p_resume = sub.add_parser("resume"); p_resume.add_argument("worker", nargs="?", default="all"); p_resume.add_argument("--json", action="store_true"); p_resume.set_defaults(func=cmd_resume)
    p_tui = sub.add_parser("tui", help="живой монитор (--refresh N), меню или --shell")
    p_tui.add_argument("--refresh", type=int, default=0, help="авто-обновление каждые N секунд (0 = меню)")
    p_tui.add_argument("--shell", action="store_true", help="запустить интерактивную shell")
    p_tui.set_defaults(func=cmd_tui)
    sub.add_parser("shell", help="интерактивная консоль управления").set_defaults(func=cmd_shell)
    sub.add_parser("sh", help="алиас для shell").set_defaults(func=cmd_shell)
    p_search = sub.add_parser("search", help="единый поиск по письмам/кейсам/outbox/клиентам")
    p_search.add_argument("query")
    p_search.add_argument("--scope", default="all", choices=["all", "emails", "cases", "outbox", "clients"])
    p_search.add_argument("--limit", type=int, default=20)
    p_search.add_argument("--json", action="store_true")
    p_search.set_defaults(func=cmd_search)
    p_trace = sub.add_parser("trace", help="трассировка цепочки raw/case/outbox")
    p_trace.add_argument("entity_type", choices=["raw_email", "case", "outbox"])
    p_trace.add_argument("entity_id", type=int)
    p_trace.add_argument("--json", action="store_true")
    p_trace.add_argument("--compact", action="store_true")
    p_trace.set_defaults(func=cmd_trace)
    p_ob = sub.add_parser("outbox", help="безопасный просмотр outbox (read-only)")
    ob_sub = p_ob.add_subparsers(dest="outbox_cmd", required=True)
    ob_s = ob_sub.add_parser("summary"); ob_s.add_argument("--json", action="store_true")
    ob_p = ob_sub.add_parser("preview")
    ob_p.add_argument("--limit", type=int, default=5)
    ob_p.add_argument("--ids", help="список id через запятую, напр. 1,2,3")
    ob_p.add_argument("--profile", choices=["minimal", "standard", "debug"], default=None)
    ob_p.add_argument("--json", action="store_true")
    p_ob.set_defaults(func=cmd_outbox)
    p_br = sub.add_parser("bucket-report", help="визуальная бухгалтерия писем (12 buckets, sum=total)")
    p_br.add_argument("--json", action="store_true")
    p_br.add_argument("--legacy", action="store_true", help="старый детальный accounting raw/case/UI")
    p_br.set_defaults(func=cmd_bucket_report)
    p_fr = sub.add_parser("folder-report", help="рабочие папки писем (sum folders = total raw)")
    p_fr.add_argument("--json", action="store_true")
    p_fr.set_defaults(func=cmd_folder_report)
    p_hs = sub.add_parser("hidden-summary", help="раздел «Обработанные / не требуют действия» (read-only)")
    p_hs.add_argument("--json", action="store_true"); p_hs.set_defaults(func=cmd_hidden_summary)
    p_pr = sub.add_parser("pipeline-report", help="canonical pipeline: routes + reason (read-only)")
    p_pr.add_argument("--json", action="store_true"); p_pr.set_defaults(func=cmd_pipeline_report)
    p_pl = sub.add_parser("pipeline-list", help="письма pipeline (--route --reason, read-only)")
    p_pl.add_argument("--route", default=None); p_pl.add_argument("--reason", default=None)
    p_pl.add_argument("--limit", type=int, default=20); p_pl.add_argument("--page", type=int, default=1)
    p_pl.add_argument("--q", default=""); p_pl.add_argument("--json", action="store_true")
    p_pl.set_defaults(func=cmd_pipeline_list)
    p_hl = sub.add_parser("hidden-list", help="письма скрытого раздела (--group, read-only)")
    p_hl.add_argument("--group", default=None); p_hl.add_argument("--limit", type=int, default=20)
    p_hl.add_argument("--page", type=int, default=1); p_hl.add_argument("--q", default="")
    p_hl.add_argument("--json", action="store_true"); p_hl.set_defaults(func=cmd_hidden_list)
    p_server = sub.add_parser("server"); p_server.add_argument("--with-workers", action="store_true"); p_server.set_defaults(func=cmd_server)
    sub.add_parser("worker").set_defaults(func=cmd_worker)
    sub.add_parser("diagnostic").set_defaults(func=cmd_diagnostic)
    sub.add_parser("reconcile").set_defaults(func=cmd_reconcile)
    p_bf = sub.add_parser("backfill"); p_bf.add_argument("--from-missing", action="store_true"); p_bf.add_argument("--apply", action="store_true"); p_bf.set_defaults(func=cmd_backfill)
    p_wt = sub.add_parser("worker-test")
    p_wt.add_argument("--stage", default="all", choices=["import", "sorter", "stage2", "outbox-preview", "all"])
    p_wt.add_argument("--limit", type=int, default=20)
    p_wt.add_argument("--dry-run", action="store_true", help="всегда read-only (флаг для совместимости)")
    p_wt.set_defaults(func=cmd_worker_test)
    sub.add_parser("open-url").set_defaults(func=cmd_open_url)
    # demo AI + 1C
    p_pl = sub.add_parser("payload", help="payload от case/outbox (read-only, без создания outbox)")
    p_pl.add_argument("target", choices=["case", "outbox"]); p_pl.add_argument("id")
    p_pl.add_argument("--profile", default="standard", choices=["minimal", "standard", "debug"])
    p_pl.add_argument("--json", action="store_true"); p_pl.set_defaults(func=cmd_payload)
    def _add_local_1c_sub(parser: argparse.ArgumentParser) -> None:
        ssub = parser.add_subparsers(dest="local_cmd", required=True)
        ssub.add_parser("status")
        s_last = ssub.add_parser("last"); s_last.add_argument("--limit", type=int, default=10)
        s_last.add_argument("--full", action="store_true"); s_last.add_argument("--json", action="store_true")
        s_send = ssub.add_parser("send-case"); s_send.add_argument("id")
        s_send.add_argument("--profile", default="standard", choices=["minimal", "standard", "debug"])
        s_send.add_argument("--confirm", action="store_true")
        s_clr = ssub.add_parser("clear"); s_clr.add_argument("--confirm", action="store_true")
        parser.set_defaults(func=cmd_local_1c)
    p_l1 = sub.add_parser("local-1c", help="Локальный приёмник 1С (контрольная точка обмена, НЕ реальная 1С)")
    _add_local_1c_sub(p_l1)
    p_d1 = sub.add_parser("demo-1c")  # hidden back-compat alias
    _add_local_1c_sub(p_d1)
    p_as = sub.add_parser("ai-smoke", help="контролируемый AI smoke (manual, --confirm)")
    p_as.add_argument("target", choices=["case", "raw"]); p_as.add_argument("id")
    p_as.add_argument("--confirm", action="store_true"); p_as.add_argument("--mock", action="store_true")
    p_as.add_argument("--model", default="current"); p_as.add_argument("--max-output-tokens", type=int, default=1024, dest="max_output_tokens")
    p_as.add_argument("--json", action="store_true"); p_as.set_defaults(func=cmd_ai_smoke)
    p_aiset = sub.add_parser("ai-settings", help="текущие AI-настройки (read-only)")
    p_aiset.add_argument("--json", action="store_true"); p_aiset.set_defaults(func=cmd_ai_settings)
    sub.add_parser("demo-plan", help="сценарий демонстрации").set_defaults(func=cmd_demo_plan)
    p_dec = sub.add_parser("decision", help="decision trace для case/raw (read-only)")
    p_dec.add_argument("target", choices=["case", "raw"]); p_dec.add_argument("id")
    p_dec.add_argument("--json", action="store_true"); p_dec.set_defaults(func=cmd_decision)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
