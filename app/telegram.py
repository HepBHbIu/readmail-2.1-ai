from __future__ import annotations

import threading
import time
import httpx
from typing import Any

from .config import settings

CLAIM_KIND_RU = {
    "defect": "Брак",
    "nonconforming": "Некондиция",
    "number_replacement": "Замена артикула",
    "wrong_item": "Пересорт",
    "shortage": "Недовоз",
    "overdelivery": "Излишек",
    "incomplete_set": "Некомплект",
    "correction_request": "Корректировка",
    "marking_request": "Маркировка",
    "quality_refusal": "Отказ клиента",
}


def _chat_ids() -> list[str]:
    return [c.strip() for c in (settings.tg_chat_ids or "").split(",") if c.strip()]


def _is_allowed(chat_id: str) -> bool:
    if not settings.tg_whitelist_enabled:
        return True
    allowed = _chat_ids()
    return str(chat_id) in allowed


def send_message(text: str, *, chat_id: str | None = None, parse_mode: str = "HTML") -> dict[str, Any]:
    """Send a message to one chat or all configured chats."""
    token = settings.tg_bot_token
    if not token:
        return {"ok": False, "error": "TG_BOT_TOKEN not configured"}
    targets = [chat_id] if chat_id else _chat_ids()
    if not targets:
        return {"ok": False, "error": "No TG_CHAT_IDS configured"}
    results = []
    for cid in targets:
        if not _is_allowed(cid):
            results.append({"chat_id": cid, "ok": False, "error": "not_in_whitelist"})
            continue
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            r = httpx.post(url, json={
                "chat_id": cid,
                "text": text[:4096],
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }, timeout=10)
            data = r.json()
            results.append({"chat_id": cid, "ok": data.get("ok", False), "error": data.get("description")})
        except Exception as exc:
            results.append({"chat_id": cid, "ok": False, "error": str(exc)[:200]})
    ok = any(r.get("ok") for r in results)
    return {"ok": ok, "results": results}


def token_stats(period: str = "today") -> dict[str, Any]:
    """Точная статистика токенов/времени/денег из ai_usage. БЕЗ режимов (паттернов нет).

    Термины владельца: ВЫХОД = наш запрос (prompt) ↑, ВХОД = ответ сервера (completion) ↓.
    Деньги считаем по таблице pricing.PRICES_RUB_PER_1M (по каждой модели отдельно).
    period: 'today' (с начала суток МСК) или 'all'. Разбивка по kind (text/vision).
    """
    from .db import connect
    from .pricing import cost_rub
    conds = ["ok=1"]
    params: list[Any] = []
    if period == "today":
        from datetime import datetime, timezone, timedelta
        msk_now = datetime.now(timezone(timedelta(hours=3)))
        start_utc = (msk_now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=3))
        conds.append("created_at >= ?")
        params.append(start_utc.strftime("%Y-%m-%dT%H:%M:%S"))
    where = "WHERE " + " AND ".join(conds)
    out: dict[str, Any] = {"period": period, "by_kind": {}, "total": {}}
    try:
        with connect() as con:
            rows = con.execute(
                f"""SELECT COALESCE(kind,'text') kind, model,
                           COUNT(*) n, SUM(prompt_tokens) pt, SUM(completion_tokens) ct, SUM(duration_ms) dur
                    FROM ai_usage {where}
                    GROUP BY COALESCE(kind,'text'), model""",
                params,
            ).fetchall()
    except Exception as exc:
        return {"error": str(exc)}

    def _blank() -> dict[str, Any]:
        return {"emails": 0, "out_tokens": 0, "in_tokens": 0, "out_rub": 0.0, "in_rub": 0.0,
                "total_rub": 0.0, "sec": 0.0}

    agg: dict[str, dict[str, Any]] = {}
    tot = _blank()
    for r in rows:
        d = dict(r)
        kind = d["kind"]; model = d["model"]
        n = int(d["n"] or 0); pt = int(d["pt"] or 0); ct = int(d["ct"] or 0); dur = int(d["dur"] or 0)
        c = cost_rub(model, pt, ct)
        k = agg.setdefault(kind, _blank())
        for tgt in (k, tot):
            tgt["emails"] += n
            tgt["out_tokens"] += pt          # ВЫХОД = запрос (prompt)
            tgt["in_tokens"] += ct           # ВХОД = ответ (completion)
            tgt["out_rub"] += c["out_rub"]
            tgt["in_rub"] += c["in_rub"]
            tgt["total_rub"] += c["total_rub"]
            tgt["sec"] += dur / 1000.0
    for k in (*agg.values(), tot):
        n = k["emails"]
        k["avg_tokens"] = round((k["out_tokens"] + k["in_tokens"]) / n) if n else 0
        k["avg_sec"] = round(k["sec"] / n, 1) if n else 0
        k["avg_rub"] = round(k["total_rub"] / n, 4) if n else 0
        for f in ("out_rub", "in_rub", "total_rub"):
            k[f] = round(k[f], 2)
    out["by_kind"] = agg
    out["total"] = tot
    return out


def _fmt_n(x: int) -> str:
    return f"{int(x):,}".replace(",", " ")


def _token_stats_lines(period: str = "today") -> list[str]:
    """Строки для ТГ: ВЫХОД(запрос)/ВХОД(ответ) токены + ₽, по text/vision, среднее на письмо."""
    st = token_stats(period)
    if st.get("error") or not st.get("total", {}).get("emails"):
        return []
    t = st["total"]; bk = st["by_kind"]
    label = "сегодня" if period == "today" else "всего"
    lines = [
        f"💰 <b>Токены/деньги ({label})</b>: писем {t['emails']} · итого <b>{t['total_rub']}₽</b>",
        f"  ↑ ВЫХОД (наш запрос): {_fmt_n(t['out_tokens'])} ток · {t['out_rub']}₽",
        f"  ↓ ВХОД (ответ сервера): {_fmt_n(t['in_tokens'])} ток · {t['in_rub']}₽",
    ]
    for key, ic, nm in (("text", "✍️", "текст"), ("vision", "🖼", "визуал")):
        b = bk.get(key)
        if b and b["emails"]:
            lines.append(f"  {ic} {nm}: {b['emails']} · ↑{_fmt_n(b['out_tokens'])}/↓{_fmt_n(b['in_tokens'])} ток · {b['total_rub']}₽ · ⌀{b['avg_sec']}с")
    lines.append(f"  ⌀ письмо: {t['avg_tokens']} ток · {t['avg_rub']}₽ · {t['avg_sec']}с")
    return lines


def notify_cycle_done(result: dict[str, Any]) -> None:
    """Send a cycle summary after /api/autopilot/cycle completes."""
    if not settings.tg_notify_on_cycle or not settings.tg_bot_token:
        return
    imp    = result.get("import") or {}
    ai     = result.get("ai") or {}
    queue  = result.get("queue") or {}
    deliv  = result.get("delivery") or {}
    stats  = result.get("stats") or {}

    imported  = imp.get("imported", 0)
    ai_done   = ai.get("applied", 0)
    sent      = deliv.get("sent", 0)
    queued    = queue.get("queued", 0)
    errors    = stats.get("outbox_error", 0)

    icon = "✅" if result.get("ok") else "⚠️"
    lines = [
        f"{icon} <b>Readmail — цикл завершён</b>",
        f"📥 Новых писем: <b>{imported}</b>",
        f"🤖 AI обработал: <b>{ai_done}</b>",
        f"📤 Отправлено в 1С: <b>{sent}</b>",
    ]
    if queued:
        lines.append(f"📦 В очереди: <b>{queued}</b>")
    if errors:
        lines.append(f"❌ Ошибок доставки: <b>{errors}</b>")
    if not result.get("ok") and result.get("error"):
        lines.append(f"⚠️ {result['error'][:200]}")
    try:
        tl = _token_stats_lines("today")
        if tl:
            lines.append("")
            lines.extend(tl)
    except Exception:
        pass
    try:
        send_message("\n".join(lines))
    except Exception:
        pass


def notify_unresolved(cases: list[dict[str, Any]]) -> None:
    """Alert about unresolved cases that couldn't be processed."""
    if not settings.tg_notify_unresolved or not settings.tg_bot_token:
        return
    if len(cases) < settings.tg_unresolved_min:
        return
    lines = [f"❓ <b>Readmail — {len(cases)} неразобранных писем</b>"]
    for c in cases[:10]:
        buyer = c.get("buyer_name") or c.get("buyer_code") or "Неизвестный"
        subj  = (c.get("subject") or "—")[:60]
        miss  = ", ".join(c.get("missing") or []) or "?"
        lines.append(f"\n• <b>{buyer}</b>: {subj}\n  ↳ Не хватает: {miss}")
    if len(cases) > 10:
        lines.append(f"\n...и ещё {len(cases)-10}")
    try:
        send_message("\n".join(lines))
    except Exception:
        pass


def notify_delivery_error(errors: list[dict[str, Any]]) -> None:
    """Alert about 1C delivery failures."""
    if not settings.tg_notify_errors or not settings.tg_bot_token or not errors:
        return
    lines = [f"🔴 <b>Readmail — ошибки доставки в 1С: {len(errors)}</b>"]
    for e in errors[:5]:
        lines.append(f"• Кейс #{e.get('case_id')}: {(e.get('last_error') or '?')[:100]}")
    try:
        send_message("\n".join(lines))
    except Exception:
        pass


def notify_unresolved_immediate(case: dict[str, Any]) -> None:
    """Immediate alert: one case just couldn't be identified at all."""
    if not settings.tg_notify_unresolved or not settings.tg_bot_token:
        return
    buyer = case.get("buyer_name") or case.get("buyer_code") or "Неизвестный клиент"
    subj  = (case.get("subject") or "—")[:80]
    miss  = ", ".join(case.get("missing") or []) or "нет данных"
    lines = [
        "🚨 <b>Readmail — не удалось разобрать письмо</b>",
        f"👤 Клиент: <b>{buyer}</b>",
        f"📋 Тема: {subj}",
        f"❌ Не хватает: <b>{miss}</b>",
        f"🔗 Откройте панель → Неразобранные",
    ]
    try:
        send_message("\n".join(lines))
    except Exception:
        pass


def send_hourly_report(con: Any) -> None:
    """Collect stats for the last hour and send summary to Telegram."""
    from .db import loads

    if not settings.tg_notify_on_cycle or not settings.tg_bot_token:
        return
    try:
        # Cases created in the last hour
        rows = con.execute(
            """
            SELECT c.claim_kind, c.state, c.event_type, c.buyer_name,
                   c.missing_json, e.subject
            FROM cases c JOIN raw_emails e ON e.id=c.raw_email_id
            WHERE c.created_at >= datetime('now', '-1 hour')
            ORDER BY c.id DESC
            """,
        ).fetchall()

        if not rows:
            return  # Nothing happened — don't spam

        total = len(rows)
        ready = sum(1 for r in rows if r["state"] == "ready_to_1c")
        needs_review = sum(1 for r in rows if r["state"] == "needs_review")
        unresolved = sum(1 for r in rows if r["state"] == "needs_review" and "event_type" in (loads(r["missing_json"] or "[]")))

        # Count by claim_kind
        kind_counts: dict[str, int] = {}
        for r in rows:
            k = r["claim_kind"] or "unknown"
            kind_counts[k] = kind_counts.get(k, 0) + 1

        # Outbox stats
        sent_row = con.execute(
            "SELECT COUNT(*) c FROM outbox WHERE sent_at >= datetime('now', '-1 hour')"
        ).fetchone()
        sent = int(sent_row["c"] if sent_row else 0)

        lines = ["📊 <b>Readmail — часовой отчёт</b>"]
        lines.append(f"📥 Обработано писем: <b>{total}</b>")
        lines.append(f"✅ Готово к 1С: <b>{ready}</b>")
        if sent:
            lines.append(f"📤 Отправлено в 1С: <b>{sent}</b>")
        if needs_review:
            lines.append(f"🔍 На проверке AI: <b>{needs_review}</b>")

        if kind_counts and getattr(settings, "tg_report_include_reasons", True):
            lines.append("")
            lines.append("📋 <b>По причинам:</b>")
            for kind, cnt in sorted(kind_counts.items(), key=lambda x: -x[1]):
                label = CLAIM_KIND_RU.get(kind, kind)
                lines.append(f"  • {label}: <b>{cnt}</b>")

        # Unresolved cases — list them
        unresolved_cases = [r for r in rows if r["state"] == "needs_review" and not r["claim_kind"]]
        if unresolved_cases:
            lines.append("")
            lines.append(f"❓ <b>Неразобранных: {len(unresolved_cases)}</b>")
            for r in unresolved_cases[:5]:
                buyer = r["buyer_name"] or "?"
                subj = (r["subject"] or "—")[:50]
                lines.append(f"  ⚠️ {buyer}: {subj}")
            if len(unresolved_cases) > 5:
                lines.append(f"  ...и ещё {len(unresolved_cases)-5}")

        send_message("\n".join(lines))
    except Exception:
        pass


def send_daily_report(con: Any) -> None:
    """Суточный отчёт: получено / обработано / проведено / проблемные (где система не справилась)."""
    from .db import loads
    if not settings.tg_bot_token:
        return
    try:
        # Получено писем за сутки
        received = int((con.execute(
            "SELECT COUNT(*) c FROM raw_emails WHERE imported_at >= datetime('now','-1 day')"
        ).fetchone() or {"c": 0})["c"])
        # Обработано (кейсы созданы за сутки)
        rows = con.execute(
            """SELECT c.claim_kind, c.state, c.event_type, c.buyer_name, c.fields_json, e.subject
               FROM cases c JOIN raw_emails e ON e.id=c.raw_email_id
               WHERE c.created_at >= datetime('now','-1 day')"""
        ).fetchall()
        processed = len(rows)
        ready = sum(1 for r in rows if r["state"] == "ready_to_1c")
        sent = int((con.execute(
            "SELECT COUNT(*) c FROM outbox WHERE sent_at >= datetime('now','-1 day')"
        ).fetchone() or {"c": 0})["c"])

        # Проблемные = new_return, где система НЕ справилась (нет артикула / номера / причины).
        prob = con.execute(
            """SELECT c.id, c.buyer_name, c.claim_kind, c.fields_json, e.subject
               FROM cases c JOIN raw_emails e ON e.id=c.raw_email_id
               WHERE c.event_type='new_return' AND c.state='needs_review'
               ORDER BY c.id DESC"""
        ).fetchall()
        problems = []
        for r in prob:
            f = loads(r["fields_json"] or "{}")
            miss = []
            if not f.get("part_number"):
                miss.append("артикул")
            if not (f.get("document_number") or f.get("return_number") or f.get("client_request_number")):
                miss.append("номер")
            if not r["claim_kind"]:
                miss.append("причина")
            if miss:
                problems.append((r["buyer_name"] or "?", (r["subject"] or "—")[:45], ", ".join(miss)))

        kind_counts: dict[str, int] = {}
        for r in rows:
            k = r["claim_kind"] or "unknown"
            kind_counts[k] = kind_counts.get(k, 0) + 1

        lines = ["🗓 <b>Readmail — суточный отчёт</b>"]
        lines.append(f"📥 Получено писем: <b>{received}</b>")
        lines.append(f"⚙️ Обработано: <b>{processed}</b>")
        lines.append(f"✅ Готово к 1С: <b>{ready}</b>")
        lines.append(f"📤 Проведено в 1С: <b>{sent}</b>")
        lines.append(f"⚠️ Проблемных (система не справилась): <b>{len(problems)}</b>")
        if kind_counts and getattr(settings, "tg_report_include_reasons", True):
            lines.append("\n📋 <b>По причинам:</b>")
            for kind, cnt in sorted(kind_counts.items(), key=lambda x: -x[1]):
                lines.append(f"  • {CLAIM_KIND_RU.get(kind, kind)}: <b>{cnt}</b>")
        if problems and getattr(settings, "tg_daily_report_problems", True):
            lim = int(getattr(settings, "tg_daily_report_problems_limit", 15) or 15)
            lines.append(f"\n🔴 <b>Проблемные кейсы (нет нужных полей):</b>")
            for buyer, subj, miss in problems[:lim]:
                lines.append(f"  • {buyer}: {subj} — нет: {miss}")
            if len(problems) > lim:
                lines.append(f"  …и ещё {len(problems)-lim}")
        send_message("\n".join(lines))
    except Exception:
        pass


# ── Фоновый поток часового отчёта ──────────────────────────────────
_HOURLY_THREAD: threading.Thread | None = None
_HOURLY_STOP = threading.Event()
_LAST_DAILY_DATE: str | None = None


def _hourly_loop() -> None:
    """Send Telegram summary on the configured interval. Runs as a daemon thread."""
    while not _HOURLY_STOP.is_set():
        interval_minutes = max(1, int(getattr(settings, "tg_report_interval_minutes", 60) or 60))
        slices = max(1, int(interval_minutes * 60 / 30))
        for _ in range(slices):
            if _HOURLY_STOP.is_set():
                return
            time.sleep(30)
        # Send report
        try:
            from .db import connect
            from .runtime_settings import apply_runtime_settings
            apply_runtime_settings()
            with connect() as con:
                send_hourly_report(con)
                # Суточный отчёт — раз в день в заданный час.
                global _LAST_DAILY_DATE
                if getattr(settings, "tg_daily_report_enabled", True):
                    from datetime import datetime
                    now = datetime.now()
                    today = now.strftime("%Y-%m-%d")
                    if now.hour == int(getattr(settings, "tg_daily_report_hour", 9) or 9) and _LAST_DAILY_DATE != today:
                        send_daily_report(con)
                        _LAST_DAILY_DATE = today
        except Exception:
            pass


def start_hourly_reporter() -> None:
    """Start the background hourly report thread (call once on app startup)."""
    global _HOURLY_THREAD
    if _HOURLY_THREAD and _HOURLY_THREAD.is_alive():
        return
    _HOURLY_STOP.clear()
    _HOURLY_THREAD = threading.Thread(target=_hourly_loop, name="readmail-tg-hourly", daemon=True)
    _HOURLY_THREAD.start()


def test_connection() -> dict[str, Any]:
    """Send a test message to verify bot + chat configuration."""
    return send_message("✅ <b>Readmail</b> — тест соединения. Бот настроен корректно.")
