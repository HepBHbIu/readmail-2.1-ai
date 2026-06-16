from __future__ import annotations

from app.final_case_sorter import build_final_sorting, summarize_final_sorting


def _case(case_id: int, dry_class: str, state: str = "needs_review", **extra):
    return {
        "case_id": case_id,
        "raw_email_id": case_id + 100,
        "buyer_code": "supplier",
        "current_state": state,
        "event_type": extra.pop("event_type", "new_return"),
        "final_dry_run_class": dry_class,
        "blocking_reasons": extra.pop("blocking_reasons", []),
        "warning_reasons": extra.pop("warning_reasons", []),
        "second_gate": {"passed": dry_class.startswith("auto_export"), "field_audit": {}},
        **extra,
    }


def test_staged_safe_is_final_and_forbids_real_export():
    rows = build_final_sorting(
        [_case(1, "auto_export_safe")],
        safe_previews=[{"case_id": 1, "safety_class": "auto_export_safe"}],
        staging_items=[{"case_id": 1, "safety_class": "auto_export_safe", "status": "staged"}],
    )
    item = rows[0]
    assert item["final_bucket"] == "auto_safe_staged"
    assert item["next_action"] == "await_staging_approval"
    assert "send_to_1c" in item["forbidden_actions"]
    assert "write_real_outbox" in item["forbidden_actions"]


def test_warning_never_becomes_staged_bucket():
    rows = build_final_sorting(
        [_case(2, "auto_export_with_warning")],
        warning_previews=[{"case_id": 2, "safety_class": "auto_export_with_warning"}],
        staging_items=[{"case_id": 2, "safety_class": "auto_export_with_warning", "status": "staged"}],
    )
    assert rows[0]["final_bucket"] == "auto_warning_candidate"
    assert "stage_automatically" in rows[0]["forbidden_actions"]


def test_one_click_and_choice_are_distinguished():
    cases = [_case(3, "quick_review"), _case(4, "quick_review")]
    tasks = [
        {"case_id": 3, "review_id": "3:q", "one_click": True, "review_type": "confirm_safe_suggestion"},
        {"case_id": 4, "review_id": "4:q", "one_click": False, "review_type": "claim_kind_choice"},
    ]
    rows = {row["case_id"]: row for row in build_final_sorting(cases, tasks)}
    assert rows[3]["final_bucket"] == "quick_review_one_click"
    assert rows[4]["final_bucket"] == "quick_review_choice"


def test_send_to_human_and_linked_terminal_precedence():
    cases = [
        _case(5, "quick_review"),
        _case(6, "blocked", state="linked_event", event_type="followup_dialog"),
        _case(7, "human_review", state="needs_link"),
        _case(8, "blocked", state="ignored_info_only"),
    ]
    tasks = [{"case_id": 5, "review_id": "5:q", "review_type": "send_to_human_review", "one_click": False}]
    rows = {row["case_id"]: row for row in build_final_sorting(cases, tasks)}
    assert rows[5]["final_bucket"] == "human_review"
    assert rows[6]["final_bucket"] == "duplicate_or_followup"
    assert rows[7]["final_bucket"] == "needs_link"
    assert rows[8]["final_bucket"] == "terminal_non_export"
    assert "send_to_1c" in rows[6]["forbidden_actions"]


def test_repeated_blocker_becomes_needs_rule():
    cases = [
        _case(index, "blocked", blocking_reasons=["part_number:weak_found"])
        for index in range(10, 15)
    ]
    rows = build_final_sorting(cases)
    assert all(row["final_bucket"] == "blocked_needs_rule" for row in rows)
    summary = summarize_final_sorting(rows)
    assert summary["by_bucket"]["blocked_needs_rule"] == 5


def test_ledger_decision_changes_next_step_but_not_case():
    rows = build_final_sorting(
        [_case(20, "quick_review")],
        quick_review_tasks=[{"case_id": 20, "review_id": "20:q", "one_click": True, "review_type": "confirm"}],
        learning_decisions=[{"case_id": 20, "review_id": "20:q", "field": "quantity", "new_value": 2}],
    )
    assert rows[0]["final_bucket"] == "quick_review_one_click"
    assert rows[0]["next_action"] == "decision_recorded_awaiting_rebuild"
    assert rows[0]["learning_ledger_status"]["decisions_count"] == 1
