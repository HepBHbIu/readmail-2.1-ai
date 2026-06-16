from __future__ import annotations

import unittest

from app.claim_kind_evidence import evaluate_claim_kind_evidence


class ClaimKindEvidenceTest(unittest.TestCase):
    def evaluate(self, kind: str, text: str, buyer: str = "avtoto_ru"):
        return evaluate_claim_kind_evidence(
            value=kind,
            buyer_code=buyer,
            raw_email={"visible_text": text},
            original_text=text,
        )

    def test_explicit_refusal_confirms(self):
        result = self.evaluate("quality_refusal", "Причина возврата: отказ клиента")
        self.assertIn(result["status"], {"confirmed_by_reason_label", "confirmed_by_explicit_reason"})

    def test_plain_refusal_confirms(self):
        # Решение владельца 2026-06-12: «просто отказ» = «отказ клиента» = одна суть,
        # подтверждённый quality_refusal (а не weak). Раньше падал в weak_generic_refusal.
        result = self.evaluate("quality_refusal", "Причина: отказ")
        self.assertIn(result["status"], {"confirmed_by_reason_label", "confirmed_by_explicit_reason"})

    def test_nadlezhashchego_kachestva_confirms(self):
        result = self.evaluate("quality_refusal", "Тип рекламации: Новый товар надлежащего качества")
        self.assertIn(result["status"], {"confirmed_by_reason_label", "confirmed_by_explicit_reason"})

    def test_defect_does_not_confirm_refusal(self):
        result = self.evaluate("quality_refusal", "Причина возврата: брак")
        self.assertEqual(result["status"], "conflict_reason_detected")

    def test_shortage_does_not_confirm_defect(self):
        result = self.evaluate("defect", "Причина: недовоз")
        self.assertEqual(result["status"], "conflict_reason_detected")

    def test_wrong_item_does_not_confirm_shortage(self):
        result = self.evaluate("shortage", "Причина: пересорт")
        self.assertEqual(result["status"], "conflict_reason_detected")

    def test_marking_does_not_confirm_refusal(self):
        result = self.evaluate("quality_refusal", "Не переданы маркировки ЧЗ")
        self.assertEqual(result["status"], "conflict_reason_detected")

    def test_mixed_refusal_and_defect_is_conflict(self):
        result = self.evaluate("quality_refusal", "Отказ клиента. При осмотре обнаружен брак.")
        self.assertEqual(result["status"], "conflict_reason_detected")

    def test_html_reason_column_confirms(self):
        result = evaluate_claim_kind_evidence(
            value="defect",
            buyer_code="auto_sputnik",
            raw_email={
                "body_html": (
                    "<table><tr><th>Артикул</th><th>Причина претензии</th></tr>"
                    "<tr><td>HP4465</td><td>Брак</td></tr></table>"
                )
            },
            original_text="",
        )
        self.assertEqual(result["status"], "confirmed_by_table_reason_column")


if __name__ == "__main__":
    unittest.main()
