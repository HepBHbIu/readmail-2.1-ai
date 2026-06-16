"""UI rollback / cleanup (2026-06-10): один operator-shell, pipeline только в dev.

Тесты статические (контент файлов) + ui/mode, т.к. браузера в CI нет. Проверяют, что
наслоения убраны и инженерный конвейер гейтится по режиму.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "app/web/static/style.css").read_text(encoding="utf-8")
HTML = (ROOT / "app/web/index.html").read_text(encoding="utf-8")
JS = (ROOT / "app/web/static/app.js").read_text(encoding="utf-8")


# ── Ф3: pipeline-bar только в developer mode ──────────────────────────

def test_pipeline_visible_for_operator_after_restore():
    # restore: верхний конвейер снова виден оператору (gating убран)
    assert "body:not(.developer-mode) .pipeline-bar { display: none; }" not in CSS
    assert 'id="pipeline-bar"' in HTML

def test_js_toggles_developer_mode_class():
    assert 'classList.toggle("developer-mode"' in JS
    assert 'classList.toggle("operator-mode"' in JS


# ── Ф4: CSS без конфликтных слоёв ─────────────────────────────────────

def test_no_layered_control_shell_patch():
    # старый дублирующий патч-блок «Control Shell v1» убран
    assert "Control Shell v1: жёсткая защита" not in CSS
    assert "Operator Control Center — единый shell" in CSS

def test_topbar_not_force_wrapped_unconditionally():
    # не должно быть безусловного force-wrap шапки в cleanup-блоке (он ломал layout)
    cleanup = CSS.split("Operator Control Center — единый shell")[1]
    assert ".topbar { flex-wrap: wrap" not in cleanup
    assert ".tabs.nav-groups { flex-wrap: wrap" not in cleanup

def test_horizontal_overflow_guard_present():
    assert "overflow-x: hidden" in CSS


# ── Ф6: навигация — пустые группы схлопываются ────────────────────────

def test_js_collapses_empty_nav_groups():
    assert ".nav-group" in JS and "allHidden" in JS

def test_js_falls_back_to_emails_when_active_hidden():
    assert 'activateTab("emails"' in JS


# ── Ф5: сохранённое полезное ──────────────────────────────────────────

def test_dashboard_tab_kept():
    assert 'data-tab="dashboard"' in HTML

def test_logout_kept():
    assert 'id="btn-logout"' in HTML and "doLogout" in JS

def test_patterns_zero_tokens_text_kept():
    assert "0 токенов (без AI)" in JS


# ── ui/mode: operator не видит инженерку ──────────────────────────────

def test_ui_mode_operator_excludes_engineering(monkeypatch):
    from app.config import settings
    import app.server_core as sc
    monkeypatch.setattr(settings, "developer_mode", True, raising=False)
    vis = sc.ui_mode("operator")["visible_tabs"]
    for t in ("evidence", "ai_trace", "defect_audit"):
        assert t not in vis
