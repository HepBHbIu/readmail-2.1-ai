from app.inbox_sorter import classify_inbox


def _email(subject: str, body: str = "", **extra):
    return {
        "id": 1,
        "mailbox": "INBOX",
        "subject": subject,
        "body_text": body,
        "from_addr": "sender@example.com",
        "attachments": [],
        **extra,
    }


def test_supplier_report_with_excel_is_not_return():
    result = classify_inbox(_email(
        "Ежедневный отчёт по остаткам",
        "Автоматическая выгрузка",
        attachments=[{"filename": "report.xlsx"}],
    ))
    assert result["inbox_bucket"] == "supplier_report"


def test_price_list_attachment_is_supplier_report():
    result = classify_inbox(_email(
        "Актуальные данные",
        "Добрый день",
        attachments=[{"filename": "price_list_2026.xlsx"}],
    ))
    assert result["inbox_bucket"] == "supplier_report"
    assert result["next_action"] == "ignore_report"


def test_stock_availability_csv_is_supplier_report():
    result = classify_inbox(_email(
        "Наличие и остатки",
        attachments=[{"filename": "stock.csv"}],
    ))
    assert result["inbox_bucket"] == "supplier_report"


def test_return_requires_business_evidence():
    result = classify_inbox(_email("Возврат", "Артикул ABC123, количество 1 шт."))
    assert result["inbox_bucket"] == "return_claim"


def test_return_with_generic_excel_remains_return_claim():
    result = classify_inbox(_email(
        "Претензия по браку",
        "Артикул ABC123, количество 1 шт.",
        attachments=[{"filename": "claim.xlsx"}],
    ))
    assert result["inbox_bucket"] == "return_claim"


def test_return_word_without_evidence_goes_to_review():
    result = classify_inbox(_email("Возврат", "Добрый день, просьба посмотреть."))
    assert result["inbox_bucket"] == "unknown_needs_review"


def test_followup_is_linked_before_new_claim():
    result = classify_inbox(_email(
        "Re: Претензия",
        "По артикулу ABC123",
        in_reply_to="<parent@example>",
    ))
    assert result["inbox_bucket"] == "return_followup"


def test_empty_references_json_does_not_make_new_claim_followup():
    result = classify_inbox(_email(
        "Претензия №123 по документу №456",
        "Артикул ABC123, количество 1 шт.",
        references_json="[]",
    ))
    assert result["inbox_bucket"] == "return_claim"


def test_duplicate_has_highest_priority():
    result = classify_inbox(_email(
        "Брак, артикул ABC123",
        duplicate_of_raw_email_id=10,
    ))
    assert result["inbox_bucket"] == "duplicate_or_linked"


def test_photo_alone_does_not_make_return_claim():
    result = classify_inbox(_email(
        "Фото во вложении",
        attachments=[{"filename": "photo.jpg"}],
    ))
    assert result["inbox_bucket"] == "unknown_needs_review"
