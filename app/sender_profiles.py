"""Лёгкие пресеты под @домен отправителя — короткая подсказка в payload.

На основе анализа корпуса 2.1: у каждого поставщика устойчивая структура темы и
типовой event_type. Подсказка снимает неоднозначность (точнее) и позволяет ИИ не
гадать формат (немного экономит на «рассуждении»). НЕ дублирует общие правила —
только специфика отправителя одной строкой.

Используется в ai_client._chat_payload: добавляется как `email.sender_hint`.
Расширять — просто дописать домен в PROFILES.
"""
from __future__ import annotations

import re
from typing import Any

_DOMAIN_RE = re.compile(r"@([\w.-]+)")

# domain → краткая подсказка. Формат: что обычно шлёт + где ключи.
PROFILES: dict[str, str] = {
    "parterra.ru": "обычно «Корректировка поступления ГРУТ-N» = problem_notice/корректировка (НЕ всегда возврат); "
                   "фото брака за ссылкой claim-transfer.parterra.ru. «Возврат Россько» = new_return.",
    "ixora-auto.ru": "«Отчёт о загрузке прайс-листа» = supplier_report (терминально, не возврат). "
                     "«Возврат товара N / N» и «Запрос на возврат» = new_return; номер из темы → return_number.",
    "avtoto.ru": "«Новое сообщение по рекламации N» / «ТМС/ДТА/ЖАС запрос на возврат N» = new_return; "
                 "claim_number из темы; позиции/кол-во часто по ссылке avtoto.ru/nondelivery → shortage_link_event.",
    "avtoformula.ru": "«Запрос на возврат товара надлежащего качества» = nonconforming; "
                      "«Акт ТОРГ-2 №А-...» = defect с документом; код детали в формате KR...M.",
    "auto-sputnik.ru": "«Претензия № N от DD.MM.YYYY поставщику Питстоп» = claim; claim_number из темы; поставщик=Питстоп.",
    "trinity-parts.ru": "ВАЖНО: №Э… в теме (напр. №Э00022168) = return_number (номер возврата), "
                        "НЕ document_number! document_number бери из тела — «УПД №XXXXX от ДАТА» (напр. 83904). "
                        "«Товар принят с дефектами/Тычки» = defect; «Отказ от товара» = quality_refusal; "
                        "«Заявка на недовоз» = shortage; «не является запросом на возврат» → problem_notice.",
    "autoeuro.ru": "«Запрос на снятие/возврат. Отказ клиента» до отгрузки = pre_delivery_refusal; "
                   "Код: → part_number; «Подтверждение» в теме без данных = followup_dialog.",
    "favorit-parts.ru": "«(N ДМД) Заявка на возврат поставщику № N» = new_return; "
                       "«Возвраты готовы к выдаче» = ready_to_ship; номер заявки → return_number.",
    "autorus.ru": "«Расхождение при поставке по УПД N» = shortage/overdelivery; document_number=№УПД; "
                  "Re: в теме = followup_dialog по тому же УПД.",
    "pr-lg.ru": "Профит-Лига: «Возврат готов к выдаче ПЛ N» = ready_to_ship; «Запрос КСФ по возврату» = correction_request; "
                "«деталь устанавливалась» = defect (был монтаж).",
    "shate-m.com": "Шате-М: запрос на возврат/претензия = new_return; номер из темы.",
    "motexc.ru": "Мотексперт: «Согласование возврата <АРТИКУЛЫ>» = new_return, артикулы прямо в теме "
                 "(часто несколько → items[] мультипозиция, бренд по тексту/null); «Не получили УКД» = correction_request; «недопоставка» = shortage.",
    "berg.ru": "Берг: запрос на возврат/согласование = new_return; номер и артикул из темы/тела.",
}

# алиасы доменов (www / поддомены) → канон
_ALIASES = {
    "www.avtoto.ru": "avtoto.ru",
    "shate-m.ru": "shate-m.com",
}


def sender_domain(from_addr: str | None) -> str | None:
    if not from_addr:
        return None
    m = _DOMAIN_RE.search(from_addr)
    if not m:
        return None
    d = m.group(1).lower()
    return _ALIASES.get(d, d)


def sender_hint(from_addr: str | None) -> str | None:
    """Короткая подсказка под домен отправителя (или None — общий промт)."""
    d = sender_domain(from_addr)
    if not d:
        return None
    if d in PROFILES:
        return PROFILES[d]
    # частичное совпадение по корню домена (поддомены: wsdw.autoeuro.ru → autoeuro.ru)
    for dom, hint in PROFILES.items():
        root = dom.split(".")[0]
        if root and root in d:
            return hint
    return None
