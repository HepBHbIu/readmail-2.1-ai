"""Folder Accounting v2: every raw email has exactly one operator folder."""
from __future__ import annotations

import importlib.util
import json
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import folder_accounting as fa
from app import visual_accounting as va
from app.classifier import detect_event_type
from app.config import settings

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = json.loads((ROOT / "tests" / "fixtures" / "search_cases.json").read_text("utf-8"))


def _raw(subject: str = "Тест", body: str = "") -> dict:
    return {
        "id": 1,
        "subject": subject,
        "visible_text": body,
        "body_text": body,
        "from_addr": "sender@example.test",
        "status": "imported",
        "duplicate_of_raw_email_id": None,
    }


def _case(**values) -> dict:
    case = {
        "id": 1,
        "raw_email_id": 1,
        "event_type": "new_return",
        "claim_kind": "quality_refusal",
        "state": "needs_review",
        "status": "needs_review",
        "ready_for_export": 0,
        "needs_ai": 0,
        "missing_json": "[]",
        "payload_json": "{}",
        "has_ai_suggestion": 0,
    }
    case.update(values)
    return case


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "folders.sqlite3", raising=False)
    from app.db import connect, dumps, init_db

    init_db()
    with connect() as con:
        for raw in FIXTURES["raw_emails"]:
            con.execute(
                f"INSERT INTO raw_emails({','.join(raw)}) VALUES ({','.join('?' * len(raw))})",
                tuple(raw.values()),
            )
        for source in FIXTURES["cases"]:
            case = dict(source)
            case["fields_json"] = dumps(case.get("fields_json") or {})
            case["payload_json"] = dumps(case.get("payload_json") or {})
            con.execute(
                f"INSERT INTO cases({','.join(case)}) VALUES ({','.join('?' * len(case))})",
                tuple(case.values()),
            )
        for outbox in FIXTURES["outbox"]:
            con.execute(
                f"INSERT INTO outbox({','.join(outbox)}) VALUES ({','.join('?' * len(outbox))})",
                tuple(outbox.values()),
            )
        con.commit()
    yield


def test_folder_sum_equals_total_and_every_raw_has_name(db):
    from app.db import connect
    with connect() as con:
        result = fa.build_folder_accounting(con, include_items=True)
    assert result["accounted"] == result["total_raw"]
    assert result["unaccounted"] == 0
    assert sum(result["by_folder"].values()) == result["total_raw"]
    assert all(item["folder_name"] for item in result["items"])


@pytest.mark.parametrize(
    ("raw", "case", "expected"),
    [
        (_raw("ЗАМЕНА НОМЕРА/БРЕНДА (Отмена при приемке)"), _case(claim_kind="number_replacement"), fa.FOLDER_NUMBER_REPLACEMENT),
        (_raw("Код ТНВЭД", "Подлежащий маркировке"), _case(event_type="marking_request", claim_kind="marking_request"), fa.FOLDER_MARKING),
        (_raw("Запрос на снятие", "Не поставлять товар"), _case(event_type="pre_delivery_refusal"), fa.FOLDER_PRE_DELIVERY),
        (_raw("Недопоставка", "https://www.avtoto.ru/nondelivery/123/"), _case(claim_kind="shortage"), fa.FOLDER_SHORTAGE_LINK),
        (_raw(), _case(event_type="correction_request", claim_kind="correction_request"), fa.FOLDER_CORRECTION),
        (_raw(), _case(event_type="ready_to_ship", state="linked_event"), fa.FOLDER_READY_TO_SHIP),
        (_raw(), _case(event_type="followup_dialog", state="linked_event"), fa.FOLDER_LINKS_COMPLETED),
        (_raw("Прайс-лист"), _case(event_type="info_only", state="ignored_info_only"), fa.FOLDER_REPORTS),
    ],
)
def test_special_folder_mapping(raw, case, expected):
    assert fa.folder_for(raw, case)["folder_name"] == expected


def test_number_replacement_is_service_event_not_new_return():
    event_type, _, _ = detect_event_type(
        {"subject": "ЗАМЕНА НОМЕРА/БРЕНДА", "visible_text": ""},
        "ЗАМЕНА НОМЕРА/БРЕНДА (Отмена при приемке)",
        "number_replacement",
        {},
        "inbound_customer",
    )
    assert event_type == "number_replacement"
    assert event_type != "new_return"


def test_folder_report_does_not_change_outbox(db):
    from app.db import connect
    with connect() as con:
        before = con.execute("SELECT COUNT(*) c FROM outbox").fetchone()["c"]
        fa.build_folder_accounting(con)
        after = con.execute("SELECT COUNT(*) c FROM outbox").fetchone()["c"]
    assert before == after


def _load_readmailctl():
    spec = importlib.util.spec_from_file_location("readmailctl_folders", ROOT / "scripts" / "readmailctl.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shell_folders_command(db):
    result = _load_readmailctl().dispatch_shell_command("/folders")
    assert "TOTAL RAW" in result.text
    assert "UNACCOUNTED: 0" in result.text
    assert fa.FOLDER_MARKING in result.text


def test_folder_accounting_api(db, monkeypatch):
    from app.db import connect
    import app.main as main

    @contextmanager
    def fake_connect():
        with connect() as con:
            yield con

    monkeypatch.setattr(main, "connect", fake_connect)
    response = TestClient(main.app).get("/api/stats/folder-accounting")
    assert response.status_code == 200
    payload = response.json()
    assert payload["accounted"] == payload["total_raw"]
    assert payload["unaccounted"] == 0


def test_folder_module_has_no_ai_or_1c_calls():
    source = (ROOT / "app" / "folder_accounting.py").read_text(encoding="utf-8")
    for forbidden in ("run_ai", "ai_client", "deliver_outbox", "queue_case_event", "httpx"):
        assert forbidden not in source
