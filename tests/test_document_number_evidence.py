from __future__ import annotations

import unittest

from app.document_number_evidence import (
    evaluate_document_number_evidence,
    find_best_document_candidate,
)


class DocumentNumberEvidenceTest(unittest.TestCase):
    def evaluate(self, value: str, text: str, date: str | None = None):
        return evaluate_document_number_evidence(
            value=value,
            document_date=date,
            buyer_code="ixora_auto_ru",
            raw_email={"visible_text": text},
            original_text=text,
        )

    def test_tn_abbreviation_is_document_context(self):
        # ТН = товарная накладная (avtoto/autorus): «ТН № 82669», «№ ТН 82150»
        self.assertEqual(self.evaluate("82669", "ТН № 82669 от 29.05.2026")["status"], "confirmed_by_document_label")
        self.assertEqual(self.evaluate("82150", "№ ТН 82150 от 24.05.2026")["status"], "confirmed_by_document_label")
        self.assertEqual(self.evaluate("81627", "отгруженной по 81627 от 18.05.2026")["status"], "confirmed_by_document_label")

    def test_tn_ved_is_not_document_context(self):
        # «ТН ВЭД» (таможенный код) НЕ должно считаться док-меткой
        self.assertEqual(self.evaluate("1234567", "код ТН ВЭД 1234567 маркировка")["status"], "weak_no_document_context")

    def test_claim_number_is_conflict(self):
        result = self.evaluate("1352831", "Претензия №1352831 от 19.05.2026")
        self.assertEqual(result["status"], "conflict_claim_or_request_number")

    def test_request_number_is_conflict(self):
        result = self.evaluate("98095393", "Заявка №98095393")
        self.assertEqual(result["status"], "conflict_claim_or_request_number")

    def test_phone_is_not_document(self):
        result = self.evaluate("+7 999 123-45-67", "Телефон +7 999 123-45-67")
        self.assertEqual(result["status"], "weak_no_document_context")

    def test_order_without_document_context_is_weak(self):
        result = self.evaluate("12345", "Заказ №12345")
        self.assertEqual(result["status"], "weak_no_document_context")
        self.assertIn("document_number_without_document_context", result["warnings"])

    def test_upd_context_confirms(self):
        result = self.evaluate("80000", "УПД №80000 от 04.05.2026", "04.05.2026")
        self.assertEqual(result["status"], "confirmed_by_upd_context")
        self.assertTrue(result["date_near"])

    def test_realization_document_confirms(self):
        result = self.evaluate("12345", "Документ реализации №12345")
        self.assertEqual(result["status"], "confirmed_by_realization_context")

    def test_inflected_waybill_context_confirms(self):
        result = self.evaluate("82667", "По Вашей накладной №82667 от 29.05.26")
        self.assertEqual(result["status"], "confirmed_by_waybill_context")

    def test_abbreviated_waybill_context_confirms(self):
        result = self.evaluate("83156", "04.06.2026 накл. 83156 Арт. 1901")
        self.assertEqual(result["status"], "confirmed_by_waybill_context")

    def test_inflected_invoice_context_confirms(self):
        result = self.evaluate("81068", "По счёт-фактуре №81068 от 13.05.2026")
        self.assertEqual(result["status"], "confirmed_by_invoice_context")

    def test_best_candidate_prefers_document_over_claim(self):
        text = "Претензия №998877. УПД №80000 от 04.05.2026."
        result = find_best_document_candidate(
            {"visible_text": text}, text, "ixora_auto_ru"
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["value"], "80000")


if __name__ == "__main__":
    unittest.main()
