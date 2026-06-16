from __future__ import annotations

import unittest

from app.part_number_evidence import evaluate_part_number_evidence


class PartNumberEvidenceTest(unittest.TestCase):
    def evaluate(self, value: str, text: str, **kwargs):
        return evaluate_part_number_evidence(
            value=value,
            product_name=kwargs.get("product_name"),
            quantity=kwargs.get("quantity"),
            buyer_code=kwargs.get("buyer_code", "avtoto_ru"),
            raw_email={"visible_text": text},
            original_text=text,
        )

    def test_numeric_oem_part_not_phone_shape(self):
        from app.part_number_evidence import invalid_part_number_shape as bad
        # реальные числовые OEM-артикулы НЕ должны считаться телефоном
        self.assertEqual(bad("1609073180"), "")   # Bosch
        self.assertEqual(bad("51117421850"), "")  # BMW
        self.assertEqual(bad("070903107"), "")    # VAG
        # телефон со структурой — остаётся phone
        self.assertEqual(bad("+7 495 374-88-88"), "phone")
        self.assertEqual(bad("8(495)374-88-88"), "phone")

    def test_part_label_confirms(self):
        result = self.evaluate("HP4465", "Возврат. Арт. HP4465, шт. 1", quantity=1)
        self.assertEqual(result["status"], "confirmed_by_part_label")

    def test_compact_item_line_confirms(self):
        result = self.evaluate(
            "HP4465", "Товар HP4465 фильтр масляный, шт. 1",
            quantity=1,
        )
        self.assertEqual(result["status"], "confirmed_by_compact_item_line")

    def test_compact_item_line_with_document_context_confirms(self):
        result = self.evaluate(
            "SP2165",
            "По накладной 82938 выписана позиция SP2165 в количестве 1 шт.",
            quantity=1,
        )
        self.assertEqual(result["status"], "confirmed_by_compact_item_line")

    def test_product_context_confirms(self):
        result = self.evaluate(
            "HP4465", "HP4465 Фильтр масляный двигателя",
            product_name="Фильтр масляный двигателя",
        )
        self.assertEqual(result["status"], "confirmed_by_product_context")

    def test_phone_is_not_confirmed(self):
        result = self.evaluate("+7 999 123-45-67", "Телефон +7 999 123-45-67")
        self.assertEqual(result["status"], "weak_found")

    def test_date_is_not_confirmed(self):
        result = self.evaluate("27.05.2026", "Дата документа 27.05.2026")
        self.assertEqual(result["status"], "weak_found")

    def test_document_number_without_product_context_is_not_confirmed(self):
        result = self.evaluate("82412", "По документу №82412 от 27.05.2026")
        self.assertEqual(result["status"], "weak_found")

    def test_quantity_is_not_confirmed_as_part_number(self):
        result = self.evaluate("1", "Количество: 1 шт.", quantity=1)
        self.assertEqual(result["status"], "weak_found")


if __name__ == "__main__":
    unittest.main()
