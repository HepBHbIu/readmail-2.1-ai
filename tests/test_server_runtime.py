from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from app.config import settings


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """Изолированная БД для runtime_control / ledger."""
    db = tmp_path / "rt.sqlite3"
    monkeypatch.setattr(settings, "database_path", db, raising=False)
    return db


# ── Worker pause/resume (Ф6) ──────────────────────────────────────────

def test_pause_resume_worker(tmp_db):
    import app.runtime_control as rc
    monkey_enable_ai(True)
    assert rc.can_run("import") is True
    rc.pause("import")
    assert rc.is_paused("import") is True
    assert rc.can_run("import") is False
    rc.resume("import")
    assert rc.can_run("import") is True


def test_global_pause_blocks_all(tmp_db):
    import app.runtime_control as rc
    rc.resume("all")
    rc.pause("all")
    st = rc.get_runtime_status()
    assert st["global_paused"] is True
    assert rc.is_paused("stage2") is True
    rc.resume("all")
    assert rc.get_runtime_status()["global_paused"] is False


def test_delivery_paused_by_default(tmp_db):
    import app.runtime_control as rc
    # доставка в 1С по умолчанию на паузе (безопасный дефолт)
    assert rc.is_paused("delivery") is True


def test_unknown_worker_rejected(tmp_db):
    import app.runtime_control as rc
    assert rc.pause("nonsense")["ok"] is False


def monkey_enable_ai(value: bool):
    # хелпер: enable_ai влияет на worker_enabled('ai')
    setattr(settings, "enable_ai", value)


# ── AI cost ledger: static_only = 0 токенов (Ф8) ──────────────────────

def test_static_only_writes_no_cost(tmp_path, monkeypatch):
    import app.ai_cost_ledger as led
    ledger = tmp_path / "ledger.jsonl"
    res = led.record_ai_cost(pipeline_mode="static_only", ai_task="missing_fields",
                             input_tokens=999, output_tokens=999, path=ledger)
    assert res.get("skipped") is True
    assert not ledger.exists()  # static_only не создаёт записей


def test_ai_assist_writes_ledger_row(tmp_path, monkeypatch):
    import app.ai_cost_ledger as led
    monkeypatch.setattr(settings, "ai_text_input_per_1k", 1.0, raising=False)
    monkeypatch.setattr(settings, "ai_text_output_per_1k", 2.0, raising=False)
    monkeypatch.setattr(settings, "ai_vision_per_image", 0.0, raising=False)
    monkeypatch.setattr(settings, "ai_call_base_price", 0.0, raising=False)
    ledger = tmp_path / "ledger.jsonl"
    row = led.record_ai_cost(pipeline_mode="static_plus_ai_assist", ai_task="missing_fields",
                             case_id=1, buyer_code="avtoto_ru", input_tokens=1000, output_tokens=500,
                             fields_accepted_by_evidence=["part_number"], path=ledger)
    assert ledger.exists()
    assert row["input_cost"] == 1.0 and row["output_cost"] == 1.0
    assert row["total_cost"] == 2.0
    assert row["unknown_cost"] is False
    rows = led.read_ledger(ledger)
    assert len(rows) == 1 and rows[0]["buyer_code"] == "avtoto_ru"


def test_vision_counts_images(tmp_path, monkeypatch):
    import app.ai_cost_ledger as led
    monkeypatch.setattr(settings, "ai_vision_per_image", 0.5, raising=False)
    monkeypatch.setattr(settings, "ai_text_input_per_1k", 0.0, raising=False)
    monkeypatch.setattr(settings, "ai_text_output_per_1k", 0.0, raising=False)
    monkeypatch.setattr(settings, "ai_call_base_price", 0.0, raising=False)
    ledger = tmp_path / "v.jsonl"
    row = led.record_ai_cost(pipeline_mode="defect_vision_check", ai_task="defect_vision",
                             image_count=3, path=ledger)
    assert row["image_count"] == 3 and row["vision_cost"] == 1.5 and row["total_cost"] == 1.5


def test_missing_pricing_sets_unknown_cost(tmp_path, monkeypatch):
    import app.ai_cost_ledger as led
    for attr in ("ai_text_input_per_1k", "ai_text_output_per_1k", "ai_vision_per_image", "ai_call_base_price"):
        monkeypatch.setattr(settings, attr, 0.0, raising=False)
    cost = led.compute_cost(1000, 1000, 2)
    assert cost["unknown_cost"] is True and cost["total_cost"] == 0.0  # не падаем


def test_aggregate_by_mode_and_supplier(tmp_path, monkeypatch):
    import app.ai_cost_ledger as led
    monkeypatch.setattr(settings, "ai_text_input_per_1k", 1.0, raising=False)
    monkeypatch.setattr(settings, "ai_text_output_per_1k", 0.0, raising=False)
    monkeypatch.setattr(settings, "ai_vision_per_image", 0.0, raising=False)
    monkeypatch.setattr(settings, "ai_call_base_price", 0.0, raising=False)
    ledger = tmp_path / "agg.jsonl"
    led.record_ai_cost(pipeline_mode="full_ai_pipeline", ai_task="full_ai", buyer_code="a",
                       input_tokens=1000, fields_accepted_by_evidence=["x"], path=ledger)
    led.record_ai_cost(pipeline_mode="full_ai_pipeline", ai_task="full_ai", buyer_code="b",
                       input_tokens=2000, fields_rejected_by_evidence=["y"], path=ledger)
    agg = led.aggregate(by="supplier", path=ledger)
    assert agg["groups"]["a"]["total_cost"] == 1.0
    assert agg["groups"]["b"]["total_cost"] == 2.0
    assert agg["acceptance_rate"] == 0.5  # 1 accepted / (1+1)


# ── Server core: bind / auth / banner / ui-mode (Ф3/Ф4/Ф10) ───────────

def test_bind_localhost_without_lan(monkeypatch):
    import app.server_core as sc
    monkeypatch.setattr(settings, "server_host", "", raising=False)
    monkeypatch.setattr(settings, "server_allow_lan", False, raising=False)
    host, _ = sc.resolve_bind()
    assert host == "127.0.0.1"


def test_bind_all_interfaces_with_lan(monkeypatch):
    import app.server_core as sc
    monkeypatch.setattr(settings, "server_host", "", raising=False)
    monkeypatch.setattr(settings, "server_allow_lan", True, raising=False)
    host, _ = sc.resolve_bind()
    assert host == "0.0.0.0"


def test_lan_requires_auth(monkeypatch):
    import app.server_core as sc
    monkeypatch.setattr(settings, "server_allow_lan", True, raising=False)
    monkeypatch.setattr(settings, "server_require_auth", False, raising=False)
    assert sc.auth_required() is True  # LAN всегда требует auth


def test_password_not_plain_text_and_verifies():
    import app.server_core as sc
    h = sc.hash_password("s3cret")
    assert "s3cret" not in h and h.startswith("pbkdf2_sha256$")
    assert sc.verify_password("s3cret", h) is True
    assert sc.verify_password("wrong", h) is False


def test_banner_has_no_secrets(monkeypatch):
    import app.server_core as sc
    monkeypatch.setattr(settings, "server_session_secret", "TOPSECRET", raising=False)
    monkeypatch.setattr(settings, "admin_password_hash", "pbkdf2_sha256$1$salt$deadbeef", raising=False)
    banner = sc.startup_banner({"global_paused": False, "workers": {"import": {"state": "running"}}})
    assert "TOPSECRET" not in banner
    assert "deadbeef" not in banner
    assert "Auth:" in banner


def test_developer_tabs_hidden_for_operator(monkeypatch):
    import app.server_core as sc
    monkeypatch.setattr(settings, "developer_mode", True, raising=False)
    operator = sc.ui_mode("operator")
    admin = sc.ui_mode("admin")
    assert "ai_trace" not in operator["visible_tabs"]       # operator не видит инженерку
    assert "ai_trace" in admin["visible_tabs"]              # admin при developer_mode видит
    # developer_mode выключен → даже admin не видит
    monkeypatch.setattr(settings, "developer_mode", False, raising=False)
    assert "ai_trace" not in sc.ui_mode("admin")["visible_tabs"]


def test_public_status_has_no_secrets(monkeypatch):
    import app.server_core as sc
    monkeypatch.setattr(settings, "server_session_secret", "SHHH", raising=False)
    monkeypatch.setattr(settings, "admin_password_hash", "HASHVALUE", raising=False)
    st = sc.public_status()
    flat = str(st)
    assert "SHHH" not in flat and "HASHVALUE" not in flat
    assert "admin_configured" in st  # только факт наличия, не сам хэш
