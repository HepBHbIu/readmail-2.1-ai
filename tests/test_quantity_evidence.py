from __future__ import annotations

import unittest

from app.quantity_evidence import evaluate_quantity_evidence


class QuantityEvidenceTest(unittest.TestCase):
    def evaluate(
        self,
        value,
        text: str,
        *,
        part_number: str = "HP4465",
        product_name: str = "Фильтр масляный",
        document_number: str = "80000",
        html: str = "",
    ):
        return evaluate_quantity_evidence(
            value=value,
            part_number=part_number,
            product_name=product_name,
            document_number=document_number,
            buyer_code="avtoto_ru",
            raw_email={"visible_text": text, "body_html": html},
            original_text=text,
        )

    def test_date_is_not_quantity(self):
        result = self.evaluate(5, "Дата документа 04.05.2026")
        self.assertNotIn(result["status"], {
            "confirmed_by_quantity_label",
            "confirmed_by_piece_unit",
            "confirmed_by_table_quantity_column",
            "confirmed_by_compact_item_line",
            "confirmed_by_part_quantity_pair",
        })

    def test_phone_is_not_quantity(self):
        result = self.evaluate(123, "Телефон +7 999 123-45-67")
        self.assertEqual(result["status"], "weak_number_without_quantity_context")

    def test_document_number_is_not_quantity(self):
        result = self.evaluate(80000, "УПД №80000 от 04.05.2026")
        self.assertEqual(result["status"], "weak_number_without_quantity_context")

    def test_price_is_not_quantity(self):
        result = self.evaluate(999, "Цена: 999 руб.")
        self.assertEqual(result["status"], "weak_number_without_quantity_context")

    def test_year_is_not_quantity(self):
        result = self.evaluate(2026, "Год выпуска: 2026")
        self.assertEqual(result["status"], "weak_number_without_quantity_context")

    def test_single_one_without_product_context_is_weak(self):
        result = self.evaluate(1, "Получено одно сообщение: 1", part_number="", product_name="")
        self.assertEqual(result["status"], "weak_number_without_quantity_context")

    def test_part_and_piece_unit_confirm(self):
        result = self.evaluate(1, "Арт. HP4465, шт. 1")
        self.assertEqual(result["status"], "confirmed_by_compact_item_line")

    def test_table_claim_quantity_column_confirms(self):
        html = """
        <table>
          <tr><th>Артикул</th><th>Количество претензия</th></tr>
          <tr><td>HP4465</td><td>1</td></tr>
        </table>
        """
        result = self.evaluate(1, "", html=html)
        self.assertEqual(result["status"], "confirmed_by_table_quantity_column")

    def test_equal_quantity_candidates_conflict(self):
        result = self.evaluate(
            1,
            "Арт. HP4465, количество 1 шт.; количество возврата 2 шт.",
        )
        self.assertEqual(result["status"], "conflict_quantity_candidates")

    def test_package_piece_count_does_not_conflict_with_labeled_quantity(self):
        result = self.evaluate(
            1,
            "SKF VKD35036T комплект 2 шт. Количество: 1",
            part_number="VKD35036T",
        )
        self.assertEqual(result["status"], "confirmed_by_part_quantity_pair")

    def test_price_after_piece_unit_is_not_second_quantity(self):
        result = self.evaluate(
            1,
            "Арт. HP4465 Кол-во 1 шт. 3576.00 руб.",
        )
        self.assertNotEqual(result["status"], "conflict_quantity_candidates")


if __name__ == "__main__":
    unittest.main()
