"""Dashboard overview: один агрегирующий снимок для экрана «Пульт».

Read-only. Не падает при отсутствии части snapshot'ов. Каждая секция помечена source (live/snapshot)
и временем (checked_at/generated_at) + stale-флагом. Секреты НЕ включаются.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings

ROOT = Path(__file__).resolve().parent.parent
AUDIT_DIR = ROOT / "audit_out"
DATA_DIR = ROOT / "data"
STALE_MINUTES = 60

# Какие event_type считаем «бизнес» (возвратные заявки) vs «контрольные».
BUSINESS_EVENT_TYPES = {"return_ready", "duplicate_or_repeat_ready", "pre_delivery_refusal"}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _snapshot_meta(path: Path, stale_minutes: int = STALE_MINUTES) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "mtime": None, "age_minutes": None, "stale": True, "source": "snapshot"}
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    age = (datetime.now(timezone.utc) - mtime).total_seconds() / 60.0
    return {
        "exists": True,
        "mtime": mtime.replace(microsecond=0).isoformat(),
        "age_minutes": round(age, 1),
        "stale": age > stale_minutes,
        "source": "snapshot",
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def build_overview() -> dict[str, Any]:
    from . import runtime_control, server_core, ai_cost_ledger
    from .db import connect

    overview: dict[str, Any] = {"ok": True, "generated_at": _now()}

    # ── Server ──
    try:
        overview["server"] = {**server_core.public_status(), "source": "live", "status": "running"}
    except Exception as exc:
        overview["server"] = {"status": "unknown", "error": str(exc), "source": "live"}

    # ── Auth ──
    try:
        from . import auth as auth_mod
        overview["auth"] = {
            "required": server_core.auth_required(),
            "enforced": server_core.auth_required(),
            "bootstrap_required": auth_mod.bootstrap_required(),
            "admin_configured": auth_mod.admin_configured(),
            "source": "live",
        }
    except Exception as exc:
        overview["auth"] = {"error": str(exc), "source": "live"}

    # ── Workers ──
    try:
        overview["workers"] = {**runtime_control.get_runtime_status(), "source": "live"}
    except Exception as exc:
        overview["workers"] = {"error": str(exc), "source": "live"}

    # ── Mail (snapshot reconcile + live quarantine/skipped) ──
    recon_path = AUDIT_DIR / "imap_reconcile_summary.json"
    recon = _read_json(recon_path)
    mail: dict[str, Any] = {
        "server_total": recon.get("server_total"),
        "local_raw_total": recon.get("local_raw_total"),
        "missing_local": recon.get("missing_local_total"),
        "fetch_failed": recon.get("fetch_failed_total"),
        "checked_at": recon.get("checked_at"),
        **_snapshot_meta(recon_path),
    }
    try:
        with connect() as con:
            mail["quarantine"] = int(con.execute(
                "SELECT COUNT(*) FROM import_uid_failures WHERE status='quarantined'").fetchone()[0])
            mail["skipped_before_start"] = int(con.execute(
                "SELECT COUNT(*) FROM import_uid_failures WHERE status='skipped'").fetchone()[0])
            job = con.execute(
                "SELECT job_id, status, mode, finished_at, imported_count, failed_count "
                "FROM import_jobs ORDER BY id DESC LIMIT 1").fetchone()
            mail["last_import_job"] = dict(job) if job else None
    except Exception as exc:
        mail["live_error"] = str(exc)
    overview["mail"] = mail

    # ── Processing (live cases) ──
    proc: dict[str, Any] = {"source": "live"}
    try:
        with connect() as con:
            proc["raw_without_case"] = int(con.execute(
                "SELECT COUNT(*) FROM raw_emails r LEFT JOIN cases c ON c.raw_email_id=r.id WHERE c.id IS NULL"
            ).fetchone()[0])
            by_state = {str(r["state"]): int(r["n"]) for r in con.execute(
                "SELECT state, COUNT(*) n FROM cases GROUP BY state")}
            proc["by_state"] = by_state
            proc["cases_total"] = sum(by_state.values())
            proc["ready_to_1c"] = by_state.get("ready_to_1c", 0)
            proc["needs_review"] = by_state.get("needs_review", 0)
            proc["return_claim"] = int(con.execute(
                "SELECT COUNT(*) FROM cases WHERE event_type='new_return'").fetchone()[0])
    except Exception as exc:
        proc["error"] = str(exc)
    # quick/human review — из snapshot evidence summary, если есть
    ev_path = AUDIT_DIR / "full_dry_run_summary.json"
    ev = _read_json(ev_path)
    if ev:
        classes = ev.get("by_final_dry_run_class") or {}
        proc["quick_review_snapshot"] = classes.get("quick_review") or ev.get("quick_review")
        proc["human_review_snapshot"] = classes.get("human_review") or ev.get("human_review")
        proc["evidence_snapshot"] = _snapshot_meta(ev_path)
    overview["processing"] = proc

    # ── Outbox (live) + explanation ──
    overview["outbox"] = _outbox_overview()

    # ── AI ──
    ai: dict[str, Any] = {"enabled": bool(getattr(settings, "enable_ai", False)),
                          "text_workers": getattr(settings, "ai_text_workers", None),
                          "vision_workers": getattr(settings, "ai_vision_workers", None),
                          "source": "live"}
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        month = today[:7]
        agg = ai_cost_ledger.aggregate(by="day")
        ai["calls_today"] = int((agg["groups"].get(today) or {}).get("calls") or 0)
        ai["cost_today"] = float((agg["groups"].get(today) or {}).get("total_cost") or 0.0)
        ai["cost_month"] = round(sum(float(v.get("total_cost") or 0.0)
                                     for k, v in agg["groups"].items() if str(k).startswith(month)), 6)
    except Exception:
        ai["calls_today"] = 0
        ai["cost_today"] = 0.0
        ai["cost_month"] = 0.0
    overview["ai"] = ai

    # ── Recent events ──
    overview["recent"] = _recent_events()
    return overview


def _outbox_overview() -> dict[str, Any]:
    from .db import connect
    out: dict[str, Any] = {"source": "live", "by_status": {}, "control_events": 0, "business_events": 0,
                           "delivery_enabled": bool(getattr(settings, "auto_deliver_outbox", False))}
    try:
        with connect() as con:
            for r in con.execute("SELECT status, COUNT(*) n FROM outbox GROUP BY status"):
                out["by_status"][str(r["status"])] = int(r["n"])
            for r in con.execute("SELECT event_type, COUNT(*) n FROM outbox GROUP BY event_type"):
                et = str(r["event_type"] or "")
                if et in BUSINESS_EVENT_TYPES:
                    out["business_events"] += int(r["n"])
                else:
                    out["control_events"] += int(r["n"])
            err = con.execute(
                "SELECT id, case_id, last_error FROM outbox WHERE status='error' "
                "ORDER BY id DESC LIMIT 1").fetchone()
            out["last_error"] = dict(err) if err else None
    except Exception as exc:
        out["error"] = str(exc)
    new_count = out["by_status"].get("new", 0)
    # Человекочитаемое объяснение «почему висит».
    if new_count and not out["delivery_enabled"]:
        out["explanation"] = (
            f"В очереди {new_count} событий (status=new). Автодоставка в 1С ВЫКЛЮЧЕНА "
            f"(auto_deliver_outbox=false) — поэтому они ждут, это НЕ ошибка 1С. "
            f"Из них контрольных/SLA-событий: {out['control_events']}, возвратных заявок: {out['business_events']}. "
            f"Контрольные (напр. sla_overdue) — это напоминания, а не заявки на возврат."
        )
    elif out["by_status"].get("error"):
        out["explanation"] = "Есть ошибки доставки в 1С — см. last_error и outbox_attempts."
    else:
        out["explanation"] = "Очередь доставки пуста или доставляется штатно."
    return out


def _recent_events(limit: int = 5) -> dict[str, Any]:
    from .db import connect
    recent: dict[str, Any] = {"source": "live"}
    try:
        with connect() as con:
            recent["import_jobs"] = [dict(r) for r in con.execute(
                "SELECT job_id, status, mode, finished_at, imported_count, failed_count "
                "FROM import_jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
            recent["outbox_errors"] = [dict(r) for r in con.execute(
                "SELECT id, case_id, last_error, last_attempt_at FROM outbox WHERE status='error' "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
            recent["quarantine"] = [dict(r) for r in con.execute(
                "SELECT mailbox, uid, error_type, last_seen_at FROM import_uid_failures "
                "WHERE status='quarantined' ORDER BY last_seen_at DESC LIMIT ?", (limit,)).fetchall()]
    except Exception as exc:
        recent["error"] = str(exc)
    return recent
