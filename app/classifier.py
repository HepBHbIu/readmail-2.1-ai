from __future__ import annotations

import hashlib
import html as _html
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parseaddr
from pathlib import Path
from typing import Any

import yaml

from .config import settings
from .db import utcnow
from .email_parser import clean_ws, select_visible_text, visible_body


def norm(text: str | None) -> str:
    return (text or "").replace("\u00a0", " ").lower()


# \u2500\u2500 HTML-table \u044d\u043a\u0441\u0442\u0440\u0430\u043a\u0442\u043e\u0440 (\u0441\u0435\u043c\u0435\u0439\u0441\u0442\u0432\u043e A): \u0441\u0442\u0440\u0443\u043a\u0442\u0443\u0440\u0430 \u0441\u0442\u0440\u043e\u043a/\u043a\u043e\u043b\u043e\u043d\u043e\u043a, \u0430 \u043d\u0435 \u043f\u043b\u043e\u0441\u043a\u0438\u0439 regex \u2500\u2500
# \u0421\u0438\u043d\u043e\u043d\u0438\u043c\u044b \u043d\u0430\u0437\u0432\u0430\u043d\u0438\u0439 \u043a\u043e\u043b\u043e\u043d\u043e\u043a \u2192 \u043f\u043e\u043b\u0435. \u041f\u043e\u0440\u044f\u0434\u043e\u043a \u0432\u0430\u0436\u0435\u043d (\u0441\u043f\u0435\u0446\u0438\u0444\u0438\u0447\u043d\u044b\u0435 \u0440\u0430\u043d\u044c\u0448\u0435 \u043e\u0431\u0449\u0438\u0445).
_TABLE_COLUMN_MAP = [
    ("part_number", ("\u0430\u0440\u0442\u0438\u043a\u0443\u043b", "\u0430\u0440\u0442.", "\u043a\u043e\u0434 \u0442\u043e\u0432\u0430\u0440\u0430", "\u043a\u043e\u0434 \u0434\u0435\u0442\u0430\u043b\u0438", "\u043a\u0430\u0442\u0430\u043b\u043e\u0436\u043d", "\u043d\u043e\u043c\u0435\u043d\u043a\u043b\u0430\u0442\u0443\u0440\u043d\u044b\u0439 \u043d\u043e\u043c\u0435\u0440", "oem", "p/n", "sku")),
    ("brand", ("\u0431\u0440\u0435\u043d\u0434", "\u043c\u0430\u0440\u043a\u0430", "\u043f\u0440\u043e\u0438\u0437\u0432\u043e\u0434\u0438\u0442\u0435\u043b\u044c", "\u0438\u0437\u0433\u043e\u0442\u043e\u0432\u0438\u0442\u0435\u043b\u044c")),
    ("product_name", ("\u043d\u0430\u0438\u043c\u0435\u043d\u043e\u0432\u0430\u043d\u0438\u0435", "\u043d\u0430\u0437\u0432\u0430\u043d\u0438\u0435", "\u043d\u043e\u043c\u0435\u043d\u043a\u043b\u0430\u0442\u0443\u0440\u0430", "\u0442\u043e\u0432\u0430\u0440", "\u0434\u0435\u0442\u0430\u043b\u044c")),
    ("quantity", ("\u043a\u043e\u043b-\u0432\u043e", "\u043a\u043e\u043b \u0432\u043e", "\u043a\u043e\u043b\u0438\u0447\u0435\u0441\u0442\u0432\u043e", "\u043a-\u0432\u043e", "qty")),
    ("document_number", ("\u0434\u043e\u043a\u0443\u043c\u0435\u043d\u0442", "\u0443\u043f\u0434", "\u043d\u0430\u043a\u043b\u0430\u0434\u043d", "\u0441\u0447\u0435\u0442-\u0444\u0430\u043a\u0442\u0443\u0440", "\u0441\u0447\u0451\u0442-\u0444\u0430\u043a\u0442\u0443\u0440", "\u0440\u0435\u0430\u043b\u0438\u0437\u0430\u0446\u0438")),
    ("price", ("\u0446\u0435\u043d\u0430",)),
    ("sum", ("\u0441\u0442-\u0442\u044c", "\u0441\u0442 \u0442\u044c", "\u0441\u0442\u043e\u0438\u043c\u043e\u0441\u0442\u044c", "\u0441\u0443\u043c\u043c\u0430")),
    ("comment", ("\u043f\u0440\u0438\u0447\u0438\u043d\u0430", "\u043f\u0440\u0438\u043c\u0435\u0447\u0430\u043d\u0438\u0435", "\u043a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440\u0438\u0439")),
    ("part_number", ("\u043a\u043e\u0434",)),  # \u00ab\u041a\u043e\u0434\u00bb \u2014 \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u0438\u043c, \u0447\u0442\u043e\u0431\u044b \u043d\u0435 \u043f\u0435\u0440\u0435\u0431\u0438\u0442\u044c \u00ab\u041a\u043e\u0434 \u0442\u043e\u0432\u0430\u0440\u0430\u00bb \u0438 \u043d\u0435 \u0441\u0445\u0432\u0430\u0442\u0438\u0442\u044c \u043b\u0438\u0448\u043d\u0435\u0435
]


def _table_field_for_header(cell: str) -> str | None:
    c = (cell or "").lower().strip()
    if not c or len(c) > 40:
        return None
    for field, syns in _TABLE_COLUMN_MAP:
        if any(s in c for s in syns):
            return field
    return None


def _parse_html_rows(body_html: str) -> list[list[str]]:
    """\u0412\u0441\u0435 \u0441\u0442\u0440\u043e\u043a\u0438 \u0442\u0430\u0431\u043b\u0438\u0446 \u043a\u0430\u043a \u0441\u043f\u0438\u0441\u043a\u0438 \u044f\u0447\u0435\u0435\u043a (<tr> \u2192 [<td|th> ...]). \u0421\u0442\u0440\u0443\u043a\u0442\u0443\u0440\u0430 \u0441\u043e\u0445\u0440\u0430\u043d\u0435\u043d\u0430."""
    rows: list[list[str]] = []
    body_html = (body_html or "")[:200000]  # \u043a\u0430\u043f: \u043d\u0435 \u043f\u0430\u0440\u0441\u0438\u043c \u043c\u0435\u0433\u0430\u0431\u0430\u0439\u0442\u043d\u044b\u0435 layout-\u0442\u0430\u0431\u043b\u0438\u0446\u044b
    for rm in re.finditer(r"(?is)<tr\b[^>]*>(.*?)</tr>", body_html):
        cells: list[str] = []
        for cm in re.finditer(r"(?is)<t[dh]\b[^>]*>(.*?)</t[dh]>", rm.group(1)):
            txt = re.sub(r"(?is)<[^>]+>", " ", cm.group(1))
            txt = _html.unescape(txt)
            txt = re.sub(r"\s+", " ", txt).strip()
            cells.append(txt)
        if cells:
            rows.append(cells)
    return rows


def _split_document_cell(value: str) -> tuple[str | None, str | None]:
    """\u00ab\u2116 81407 \u043e\u0442 15.05.2026\u00bb \u2192 (81407, 15.05.2026)."""
    v = value or ""
    num = None
    nm = re.search(r"\u2116?\s*([0-9][0-9a-z\u0430-\u044f\-_/]{2,})\b", v, re.I)
    if nm:
        num = nm.group(1)
    dm = re.search(r"(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}|\d{4}-\d{2}-\d{2})", v)
    return num, (dm.group(1) if dm else None)


def extract_product_table(body_html: str) -> list[dict[str, Any]]:
    """\u0418\u0437\u0432\u043b\u0435\u0447\u044c \u0442\u043e\u0432\u0430\u0440\u043d\u044b\u0435 \u043f\u043e\u0437\u0438\u0446\u0438\u0438 \u0438\u0437 HTML-\u0442\u0430\u0431\u043b\u0438\u0446\u044b: \u0448\u0430\u043f\u043a\u0430 (\u043a\u043e\u043b\u043e\u043d\u043a\u0430\u2192\u043f\u043e\u043b\u0435) + \u0441\u0442\u0440\u043e\u043a\u0438 \u0434\u0430\u043d\u043d\u044b\u0445.

    \u041d\u0435\u0441\u043a\u043e\u043b\u044c\u043a\u043e \u0441\u0442\u0440\u043e\u043a \u0434\u0430\u043d\u043d\u044b\u0445 \u2192 \u043d\u0435\u0441\u043a\u043e\u043b\u044c\u043a\u043e \u043f\u043e\u0437\u0438\u0446\u0438\u0439 (\u043c\u0443\u043b\u044c\u0442\u0438\u043f\u043e\u0437\u0438\u0446\u0438\u044f). \u041d\u0430\u0434\u0451\u0436\u043d\u0435\u0435 regex \u043f\u043e \u043f\u043b\u043e\u0441\u043a\u043e\u043c\u0443
    \u0442\u0435\u043a\u0441\u0442\u0443: \u043a\u043e\u043b\u043e\u043d\u043a\u0430 \u0438 \u0437\u043d\u0430\u0447\u0435\u043d\u0438\u0435 \u0432\u044b\u0440\u043e\u0432\u043d\u0435\u043d\u044b \u0441\u0442\u0440\u0443\u043a\u0442\u0443\u0440\u043e\u0439 \u0442\u0430\u0431\u043b\u0438\u0446\u044b, \u0430 \u043d\u0435 \u043f\u043e\u0437\u0438\u0446\u0438\u0435\u0439 \u0432 \u043f\u043e\u0442\u043e\u043a\u0435.
    """
    rows = _parse_html_rows(body_html)
    if not rows:
        return []
    header_idx, col_map = -1, {}
    for i, row in enumerate(rows):
        m: dict[int, str] = {}
        for j, cell in enumerate(row):
            f = _table_field_for_header(cell)
            if f and f not in m.values():
                m[j] = f
        if len(m) >= 3 and ("part_number" in m.values() or "product_name" in m.values()):
            header_idx, col_map = i, m
            break
    if header_idx < 0:
        return []
    ncols = len(rows[header_idx])
    items: list[dict[str, Any]] = []
    for row in rows[header_idx + 1:]:
        if len(row) != ncols:
            continue
        item: dict[str, Any] = {}
        for j, field in col_map.items():
            if j >= len(row):
                continue
            val = (row[j] or "").strip()
            if not val:
                continue
            if field == "document_number":
                dn, dd = _split_document_cell(val)
                if dn:
                    item["document_number"] = dn
                if dd:
                    item["document_date"] = _normalize_date(dd) or dd
            elif field == "document_date":
                item["document_date"] = _normalize_date(val) or val
            else:
                item[field] = val
        if item.get("part_number") or item.get("product_name"):
            items.append(item)
    # Защита от layout/служебных таблиц: реальный возврат редко >20 позиций. Если строк
    # неправдоподобно много — это не товарный список (рассылка/вёрстка), берём только
    # первую позицию (без мульти-split), чтобы не плодить сотни кейсов из одного письма.
    if len(items) > 20:
        return items[:1]
    return items








def normalize_subject(subject: str | None) -> str:
    s = norm(subject)
    s = re.sub(r"\b(re|fw|fwd|ответ|пересл):\s*", "", s, flags=re.I)
    s = re.sub(r"[#№]?\b\d{5,12}\b", " ", s)
    s = re.sub(r"[^0-9a-zа-яё]+", " ", s)
    stop = {
        "цена", "сумма", "причина", "товар", "товара", "артикул", "количество",
        "номер", "документ", "заявка", "поставщик", "покупатель", "добрый", "день",
        "здравствуйте", "возврат", "претензия", "рекламация", "счет", "счёт",
        "накладная", "таблица", "позиция", "детали", "ответ", "вопрос",
    }
    words = [w for w in s.split() if len(w) > 2 and w not in stop]
    return " ".join(words[:8]) or "no_subject"


# ── Константы для фильтрации типов писем ──
READY_TO_SHIP_PHRASES = [
    "готов к отгрузке", "готова к отгрузке", "готовы к отгрузке",
    "можете забрать", "ожидает на складе", "товар на складе",
    "ждёт вас на складе", "ждет вас на складе",
    "готово к выдаче", "готовы к выдаче", "готов к выдаче",
    "можно забрать", "к получению",
    "товар готов", "груз готов", "заказ готов",
    "возвраты готовы", "возврат готов", "позиции готовы к",
    "ready for pickup", "ready for shipment",
]
INFO_ONLY_PHRASES = [
    "информируем вас", "уведомляем", "сообщаем о",
    "статус заявки", "заявка обработана", "заявка принята",
    "ваша заявка", "по вашему запросу",
]
SERVICE_REPORT_PHRASES = [
    "отчет об автоматической загрузке прайс-листа",
    "отчёт об автоматической загрузке прайс-листа",
    "автоматической загрузке прайс-листа",
    "считано из файла",
    "добавлено в прайс",
    "не удалось добавить в прайс",
    "загрузка прайс-листа",
]
SPAM_PROMO_WORDS = [
    "скидка", "распродажа", "акция", "специальное предложение",
    "купить", "закажите", "подпишитесь",
]
PRODUCT_WORDS = [
    "масло", "фильтр", "колодки", "диск", "подшипник",
    "ремень", "насос", "датчик", "свеча", "амортизатор",
    "прокладка", "сальник", "ремень", "цепь", "ролик",
]


_RU_MONTH_MAP = {
    "янв": "01", "фев": "02", "мар": "03", "апр": "04",
    "май": "05", "мая": "05", "июн": "06", "июл": "07",
    "авг": "08", "сен": "09", "окт": "10", "ноя": "11", "дек": "12",
}


def _normalize_date(value: str | None) -> str | None:
    """Normalize any recognized date format to DD.MM.YYYY."""
    if not value:
        return None
    v = value.strip()
    # Срезаем время у datetime-значений: "2026-05-08 00:00:00" / "18.05.2026 13:52" -> дата.
    v = re.sub(r"[ T]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?$", "", v).strip()

    # ISO format: YYYY-MM-DD
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", v)
    if m:
        return f"{m.group(3)}.{m.group(2)}.{m.group(1)}"

    # Numeric with separator: DD.MM.YY or DD.MM.YYYY or DD-MM-YYYY or DD/MM/YYYY
    m = re.fullmatch(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})", v)
    if m:
        d, mo, y = m.group(1).zfill(2), m.group(2).zfill(2), m.group(3)
        if len(y) == 2:
            y = "20" + y
        return f"{d}.{mo}.{y}"

    # Russian text month: "26 мая 2026" or "26 мая 2026 года"
    m = re.match(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", v, re.I)
    if m:
        d = m.group(1).zfill(2)
        month_raw = m.group(2)[:3].lower()
        mo = _RU_MONTH_MAP.get(month_raw)
        y = m.group(3)
        if mo:
            return f"{d}.{mo}.{y}"

    return v  # return as-is if no pattern matched


def _validate_price(v: str) -> tuple[bool, str]:
    clean = re.sub(r'[₽\s]|руб\.?|RUB', '', v, flags=re.I).strip()
    if re.search(r'[а-яёa-z]', clean, re.I):
        return False, f"price содержит буквы: {v}"
    try:
        num = float(clean.replace(',', '.').replace(' ', ''))
        if num <= 0:
            return False, f"price <= 0: {v}"
        if num > 10_000_000:
            return False, f"price слишком велика: {v}"
        return True, ""
    except Exception:
        return False, f"price не число: {v}"

def _validate_document_number(v: str) -> tuple[bool, str]:
    if len(v) > 40:
        return False, f"document_number слишком длинный (возможно наименование): {v[:40]}"
    if not re.search(r'\d', v):
        return False, f"document_number без цифр: {v}"
    v_lower = v.lower()
    for word in PRODUCT_WORDS:
        if word in v_lower:
            return False, f"document_number похож на наименование: {v}"
    if re.fullmatch(r'\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}', v):
        return False, f"document_number похож на дату: {v}"
    return True, ""

def _validate_quantity(v: str) -> tuple[bool, str]:
    clean = re.sub(r'(шт\.?|pcs\.?|ед\.?)', '', v, flags=re.I).strip()
    try:
        num = float(clean.replace(',', '.'))
        if num > 1000:
            return False, f"quantity > 1000 (возможно цена): {v}"
        return True, ""
    except Exception:
        if re.search(r'[а-яa-z]', clean, re.I):
            return False, f"quantity содержит буквы: {v}"
        return True, ""

def sanity_check_fields(fields: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Проверка и очистка полей перед экспортом в 1С."""
    cleaned = dict(fields)
    errors: list[str] = []

    # price
    raw_price = cleaned.get("price") or cleaned.get("sum") or cleaned.get("amount")
    if raw_price is not None:
        ok, err = _validate_price(str(raw_price))
        if not ok:
            errors.append(err)
            cleaned.pop("price", None)
            cleaned.pop("sum", None)
            cleaned.pop("amount", None)
        else:
            try:
                num = float(str(raw_price).replace(',', '.').replace(' ', '').replace('₽', '').replace('руб', '').strip())
                cleaned["price"] = num
            except Exception:
                cleaned["price"] = raw_price

    # document_number
    raw_doc = cleaned.get("document_number")
    if raw_doc is not None:
        ok, err = _validate_document_number(str(raw_doc))
        if not ok:
            errors.append(err)
            cleaned["document_number"] = None

    # quantity
    raw_qty = cleaned.get("quantity")
    if raw_qty is not None:
        ok, err = _validate_quantity(str(raw_qty))
        if not ok:
            errors.append(err)
            cleaned["quantity"] = None
        else:
            try:
                cleaned["quantity"] = int(float(str(raw_qty).replace(',', '.')))
            except Exception:
                cleaned["quantity"] = raw_qty

    return cleaned, errors


def stable_hash(value: str, prefix: str = "h") -> str:
    return f"{prefix}:{hashlib.sha1(value.encode('utf-8', errors='ignore')).hexdigest()[:16]}"


@dataclass
class BuyerRule:
    code: str
    name: str
    domains: list[str]
    senders: list[str]
    subject_contains: list[str]
    body_contains: list[str]
    statuses: dict[str, list[str]]
    processing_types: dict[str, list[str]]
    regex: dict[str, list[str]]
    # Мультиполевые шаблоны клиента (декларативно, из YAML): один regex → несколько полей.
    # Это переносит «вшитые в код» форматные шаблоны в профиль клиента. Каждый элемент:
    #   {name, require?, pattern, flags?, fields:{поле: № группы}, only_if_empty?}
    templates: list[dict[str, Any]] = field(default_factory=list)
    # Шаблоны ПОЗИЦИЙ (мультипозиция): один regex с finditer → N товарных строк.
    # Формат строки повторяется (ПИТСТОП: «АРТ БРЕНД ИМЯ N шт. цена= ref=»). Каждый элемент:
    #   {name, require?, pattern, flags?, fields:{поле: № группы}} — поля позиции
    #   (part_number/brand/product_name/quantity/price/comment). Результат → table_items.
    item_templates: list[dict[str, Any]] = field(default_factory=list)
    # Дефолты клиента, напр. {"claim_kind": "quality_refusal"} — причина по умолчанию для
    # new_return этого клиента, когда из текста причину определить нельзя (ixora «Информируем»).
    defaults: dict[str, Any] = field(default_factory=dict)
    # Индивидуальные подсказки для ИИ по этому клиенту (добавляются к общему SYSTEM_PROMPT).
    ai_prompt: str = ""


def _safe_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return []


def load_buyer_rules(config_dir: Path | None = None) -> list[BuyerRule]:
    config_dir = config_dir or settings.buyer_config_dir
    rules: list[BuyerRule] = []
    if not config_dir.exists():
        return rules
    for path in sorted(config_dir.glob("*.yml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            buyer = data.get("buyer") or {}
            if buyer.get("enabled", True) is False:
                continue
            aliases = buyer.get("aliases") or {}
            fields = data.get("fields") or {}
            rules.append(
                BuyerRule(
                    code=str(buyer.get("code") or path.stem),
                    name=str(buyer.get("name") or buyer.get("code") or path.stem),
                    domains=[d.lower().lstrip("@") for d in _safe_list(aliases.get("domains"))],
                    senders=[s.lower() for s in _safe_list(aliases.get("senders"))],
                    subject_contains=_safe_list(aliases.get("subject_contains")),
                    body_contains=_safe_list(aliases.get("body_contains")),
                    statuses={str(k): _safe_list(v) for k, v in (data.get("statuses") or {}).items()},
                    processing_types={str(k): _safe_list(v) for k, v in (data.get("processing_types") or {}).items()},
                    regex={str(k): _safe_list(v) for k, v in (fields.get("regex") or {}).items()},
                    templates=[t for t in (data.get("templates") or []) if isinstance(t, dict) and t.get("pattern")],
                    item_templates=[t for t in (data.get("item_templates") or []) if isinstance(t, dict) and t.get("pattern")],
                    defaults=dict(data.get("defaults") or {}),
                    ai_prompt=str(data.get("ai_prompt") or "").strip(),
                )
            )
        except Exception:
            continue
    return rules


# ── Контекст переписки ────────────────────────────────────────────────────
# Отличительные признаки того, что письмо НЕ является первым в переписке.
# Помимо технических заголовков (In-Reply-To, References), есть речевые маркеры,
# характерные для ответов, напоминаний и продолжений диалога.

REPLY_CONTEXT_WORDS = [
    "вы писали", "вы написали", "в ответ на", "в ответе на ваше",
    "на ваше письмо", "на ваше обращение", "на ваш запрос",
    "как и обсуждали", "как договаривались", "продолжая",
    "в продолжение", "относительно вашего", "относительно нашего",
    "по вашему письму", "по нашему разговору",
    "как мы обсуждали", "в развитие темы",
    "отвечаю на ваше", "отвечаю на ваш",
    "ссылаясь на", "во исполнение",
]

SUBJECT_REPLY_MARKERS = re.compile(
    r"^(re|re[\[\(]?\d+[\]\)]?|fw|fwd|odp|aw|ant|sv|vs|ref|r\s*e\s*:"
    r"|ответ|пересл|переслан)", re.I
)

# Количество часов, после которых письмо от того же отправителя
# с той же темой считается отдельным новым обращением, а не продолжением.


def is_first_contact(email_data: dict[str, Any]) -> tuple[bool, list[str]]:
    """Определяет, является ли письмо ПЕРВЫМ в переписке (не ответом/продолжением).

    Возвращает (is_first: bool, reasons: list[str]).
    Первое = True = нет истории переписки.
    """
    reasons: list[str] = []
    in_reply_to = email_data.get("in_reply_to")
    references = email_data.get("references") or []
    subject = norm(email_data.get("subject"))
    body = norm(email_data.get("visible_text") or email_data.get("snippet") or "")

    # 1. Технические заголовки
    if in_reply_to:
        reasons.append(f"has_in_reply_to:{in_reply_to[:40]}")
        return False, reasons
    if references:
        reasons.append(f"has_references:count={len(references)}")
        return False, reasons

    # 2. Маркеры темы (Re:, Ответ:, Fwd:)
    if SUBJECT_REPLY_MARKERS.match(subject):
        reasons.append("subject_reply_marker")
        return False, reasons

    # 3. Речевые маркеры продолжения в теле письма
    has_context = _contains_any(body, REPLY_CONTEXT_WORDS)
    if has_context:
        reasons.append("reply_context_words")
        return False, reasons

    # 4. Цитаты (наличие quoted text — сильный признак ответа)
    quote_markers = int(email_data.get("quote_markers") or 0)
    if quote_markers >= 2:
        reasons.append(f"quote_markers:{quote_markers}")
        return False, reasons

    reasons.append("first_contact")
    return True, reasons


def detect_first_contact(
    email_data: dict[str, Any],
    existing_cases: list[dict[str, Any]] | None = None,
) -> tuple[bool, list[str]]:
    """Определяет первое ли это письмо от данного клиента по данной теме.

    Отличается от is_first_contact тем, что учитывает историю сообщений
    в БД: если от того же отправителя с похожей темой уже были кейсы,
    это может быть продолжением старой темы.

    Возвращает (is_first: bool, reasons: list[str]).
    """
    is_first, reasons = is_first_contact(email_data)

    if is_first and existing_cases:
        # Проверяем по базе: были ли письма от этого отправителя с похожей темой
        from_addr = norm(email_data.get("from_addr"))
        subject_template = normalize_subject(email_data.get("subject"))
        if from_addr and subject_template and subject_template != "no_subject":
            recent = [
                c for c in existing_cases
                if norm(c.get("from_addr", "")) == from_addr
                and c.get("subject_template") == subject_template
            ]
            if recent:
                reasons.append(f"existing_thread:{len(recent)}_prior_emails")
                return False, reasons

    return is_first, reasons


# ── Известные бренды для разделения склеенных артикулов ──
# AI часто путает: ETZ1107MRKrauf → это не один артикул, а ETZ1107MR (артикул) + Krauf (бренд).




# ── Константы для фильтрации типов писем ──
DEFAULT_KIND_PATTERNS: dict[str, list[str]] = {
    "defect": ["брак", "дефект", "неисправ", "сломал", "не работает", "вышел из строя", "перестал работать", "заводск"],
    "incomplete_set": ["некомплект", "не комплект", "не хватает комплект", "комплектность", "отсутствует в комплекте"],
    "nonconforming": ["некондиц", "нетоварный вид", "без маркиров", "без тары", "упаковк", "царап", "мят", "поврежд", "вмятин", "скол", "потерт", "потёрт", "разбит", "бой", "помят"],
    "number_replacement": [
        "замена номера", "замена артикула", "замена номера производителем",
        "замена номера изготовителем", "замена производителя", "замена бренда",
        "номер заменен", "номер заменён", "артикул заменен", "артикул заменён",
    ],
    "wrong_item": ["пересорт", "не тот", "другой артикул", "неверный товар", "ошибка подбора", "не соответствует"],
    "shortage": ["недовоз", "недопостав", "не довез", "не поставлен", "не хват", "отсутствует", "меньше чем", "не пришло", "недостача"],
    "overdelivery": ["излишек", "лишний товар", "лишняя позиция", "перевоз"],
    "correction_request": ["корректиров", "ксф", "укд", "счет-фактур", "счёт-фактур", "исправительный"],
    "marking_request": ["маркировк", "честный знак", "честного знак", "честным знак", "коды честного",
                          "код маркировки", "коды маркировки", "коды эдо не совпад", "знака не совпад",
                          "коды честного знака", "честного знака не совпад"],
    "quality_refusal": ["отказ клиента", "отказ покупателя", "отказывается от товара", "отказ от товара", "до получения", "отказ от детали", "отказ конечного покупателя", "не понадобился", "товар надлежащего качества", "не верный подбор", "неверный подбор", "клиент отказал", "отказался от товар", "от которого клиент отказ"],
}

NEW_RETURN_WORDS = ["возврат", "рекламац", "претензи", "прошу согласовать", "заявляем", "вернуть", "возврату"]
REMINDER_WORDS = [
    "напомина", "повторно", "срочно", "есть решение", "рассмотрели", "ждем ответ", "ждём ответ",
    "статус", "ответьте", "когда будет", "алло", "актуально", "что по", "повторная просьба",
]
STRONG_REMINDER_WORDS = [
    "повторно", "напомина", "рассмотрели", "рассмотрен", "ждем ответ", "ждём ответ",
    "ответьте", "когда будет", "что по", "актуально", "повторная просьба", "просьба дать ответ",
]
SUPPLIER_DECISION_WORDS = ["согласовано", "принято", "отказано", "одобрено", "решение", "можете вернуть", "возврат разрешен", "возврат разрешён"]
# Отказ ДО отгрузки — товар не отгружали, документа/даты документа нет
PRE_DELIVERY_WORDS = [
    "запрос на снятие", "снять этот товар", "не поставлять", "снятие позиции",
    "до отгрузки", "не отгружать", "отмена заказа", "отмена позиции", "снять с заказа",
    "отказ от поставки", "не успели отгрузить",
    # отказ ДО поставки / до прихода / до документа реализации
    "отказная позиция", "до привоза", "до поставки", "не привозить", "отменить заказ",
    "клиент отказался до получения", "отказ до получения", "отказ клиента на заказанный",
    "отказ на заказанный", "снятие отказа",
    "отказ от клиента на заказанный", "не поставлять к нам на склад",
    "во избежание формирования лишних возвратов",
    "в случае поставки будет сформирован акт возврата",
]
# Слова-признаки, что отгрузка БЫЛА и есть документ реализации → это обычный возврат,
# даже если в тексте есть «отказ». Используется в _detect_pre_delivery_refusal.
SHIPMENT_DOCUMENT_WORDS = [
    "упд", "накладн", "счёт-фактур", "счет-фактур", "реализаци", "торг-12", "торг12",
    "товарная накладная", "документ реализации", "по накладной", "по упд",
]


def _has_shipment_document(full_text: str, fields: dict[str, Any]) -> bool:
    """Есть ли признак свершившейся поставки/документа реализации.

    Если документ есть — это обычный возврат, НЕ pre_delivery_refusal.
    """
    # A date alone is not proof of shipment: pre-delivery templates often contain
    # "Дата поставки" for the planned order while explicitly asking not to ship it.
    if fields.get("document_number"):
        return True
    return _contains_any(full_text or "", SHIPMENT_DOCUMENT_WORDS)


def _detect_pre_delivery_refusal(full_text: str, event_type: str, fields: dict[str, Any]) -> bool:
    """Отказ клиента ДО поставки/до документа реализации.

    True только если: это возвратное письмо (new_return), есть фраза-признак отказа до поставки,
    и НЕТ признаков свершившейся отгрузки/документа реализации. Если документ есть → обычный возврат.
    """
    if event_type != "new_return":
        return False
    if not _contains_any(full_text or "", PRE_DELIVERY_WORDS):
        return False
    if _has_shipment_document(full_text, fields):
        return False
    return True


def detect_marking_subcategory(text: str) -> str | None:
    """Return a deterministic marking/TNVED subtype for operator routing."""
    value = norm(text or "")
    if not _contains_any(value, ["маркировк", "честн", "тнвэд", "тн вэд", "код тн"]):
        return None
    if _contains_any(value, ["исправьте в своей системе", "поправьте в своей системе"]):
        return "marking.need_supplier_fix"
    if _contains_any(value, ["в нашей системе указан код", "поправить код", "исправить код в нашей системе"]):
        return "marking.need_our_system_fix"
    if _contains_any(value, ["не подлежащий маркировке", "не подлежит маркировке"]):
        return "marking.not_required"
    if _contains_any(value, ["тнвэд", "тн вэд", "код тн"]) and _contains_any(
        value, ["не совпад", "неверн", "поправ", "исправ", "указан код"]
    ):
        return "marking.tnved_mismatch"
    if _contains_any(value, ["код маркировки", "коды маркировки", "честного знака"]) and _contains_any(
        value, ["не совпад", "неверн", "ошиб"]
    ):
        return "marking.code_mismatch"
    return "marking.required"


def detect_shortage_link_only(
    text: str,
    claim_kind: str | None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Identify an Avtoto nondelivery link as a trusted shortage intake."""
    if claim_kind != "shortage" and "недопостав" not in norm(text or ""):
        return None
    links = list((evidence or {}).get("all_links") or [])
    trusted = [
        link for link in links
        if re.search(r"https?://(?:www\.)?avtoto\.ru/nondelivery(?:/|$)", str(link), re.I)
    ]
    if not trusted:
        return None
    return {
        "subcategory": "shortage.link_only",
        "has_external_links": True,
        "trusted_link_domain": True,
        "trusted_domain": "avtoto.ru",
        "external_links": trusted[:10],
        "part_number_required_initially": False,
    }
INTERNAL_FORWARD_HINTS = ["fw:", "fwd:", "пересл:", "пересланное сообщение", "forwarded message"]

RUSSIAN_MONTHS = r"(?:янв(?:аря?)?|фев(?:раля?)?|мар(?:та?)?|апр(?:еля?)?|ма[йя]|июн[ья]?|июл[ья]?|авг(?:уста?)?|сен(?:тября?)?|окт(?:ября?)?|ноя(?:бря?)?|дек(?:абря?)?)"

FIELD_REGEX: dict[str, list[str]] = {
    "claim_number": [
        r"(?:претензи[яи]|рекламаци[яи]|claim)\s*(?:№|#|номер|n)\s*[:#№\- ]*([a-zа-я0-9\-_/]{4,40})",
    ],
    "client_request_number": [
        r"(?:заявк[аи]|обращени[ея]|request)\s*(?:№|#|номер|n)\s*[:#№\- ]*([a-zа-я0-9\-_/]{4,40})",
        r"(?:отказ|вх\.?|вход[а-я]+)\s*(?:№|#|номер|n)[\s:]*([a-zа-я0-9\-_/]{3,40})",
    ],
    "return_number": [
        r"(?:возврат|rma)\s*(?:№|#|номер|n)\s*[:#№\- ]*([a-zа-я0-9\-_/]{4,40})",
        r"(?:вх\.?|вход[а-я]*)\s*(?:№|#|номер|n)[\s:]*([0-9a-zа-я\-_/]{4,40})",
        r"(?:отказ)\s+вх\.?\s+(?:номер|№|#|n)[\s:]*([0-9]{4,12})",
        # ixora/avtoto: «Запрос на возврат 1329582/97592493», «Рекламация № 1698137».
        # Хвост «/97592493» (№ заказа) обрежет _clean_extracted_field.
        r"(?:запрос\s+на\s+возврат|возврат\s+товара|рекламаци[яи])\s*№?\s*(\d{6,9}(?:/\d{6,})?)\b",
        r"\bticketid\s*:?\s*(\d{6,9})\b",
        # autorus «Возврат товаров №В5062025504», profit «Заявка на возврат №1661706».
        r"(?:возврат\s+товаров?|заявк[аи]\s+на\s+возврат)\s*№\s*([a-zа-я0-9\-_/]{4,40})",
    ],
    "document_number": [
        r"\bнакл\.?\s*(?:№|#|номер|n)?\s*[:#№\- ]*([0-9]{4,12})\b",
        # «накладная 830156», «товарная накладная 830156», «УПД 830156», «реализация 830156»,
        # «документ 830156» — БЕЗ обязательного № между меткой и числом (частый формат поставщиков).
        r"(?:товарн\w*\s+)?накладн\w*\s+(?:№|#|n)?\s*[:#№\- ]*([0-9]{4,12})\b",
        r"\bупд\s+(?:№|#|n)?\s*[:#№\- ]*([0-9]{4,12})\b",
        r"(?:реализаци[яи]|документ[ау]?)\s+(?:№|#|n)?\s*[:#№\- ]*([0-9]{4,12})\b",
        r"(?:упд|у\s*п\s*д|сч[её]т[- ]?фактур[аые]?|накладн[аяой]|документ[ау]?|реализаци[яи]|заказ)\s*(?:№|#|номер|n)\s*[:#№\- ]*([0-9a-zа-я\-_/]{4,40})",
        r"(?:упд|накладн[аяой])\s*№?\s*([0-9а-я\-_/]{4,40})\s+от",
        r"№\s*([0-9]{5,12})\s*(?:от\s*\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})",
        r"\b(грут[- ]?\d{3,12})\b",
        # с/ф, сф — счёт-фактура сокращённо: «Номер с/ф: 81688», «по сф 77489»
        r"(?:номер\s+)?с[/\\]?ф\s*(?:№|номер)?\s*[:#№\- ]*([0-9]{4,12})",
    ],
    "document_date": [
        # ПРИОРИТЕТ: дата рядом с номером документа «83245 от 05 июня 2026» / «83245 от 05.06.2026».
        # Номер документа = 4-6 цифр, НЕ перед ним № (иначе это № заявки/претензии, напр. «№ 10178179 от …»).
        r"(?<![№\d])\b\d{4,6}\s+от\s+(\d{1,2}\s+" + RUSSIAN_MONTHS + r"\s+20\d{2})",
        r"(?<![№\d])\b\d{4,6}\s+от\s+(\d{1,2}[.\-/]\d{1,2}[.\-/]20\d{2})",
        # С префиксом «от/дата» — приоритет (это дата документа, а не письма)
        r"(?:от|дата)(?:\s*с[/\\]?ф)?\s*[: ]*(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})",
        r"(?:от|дата)\s*[: ]*(\d{1,2}\s+" + RUSSIAN_MONTHS + r"\s+\d{4})",
        r"(?:от|дата)\s*[: ]*(\d{4}-\d{2}-\d{2})",
        # Текстовый месяц без префикса
        r"(\d{1,2}\s+" + RUSSIAN_MONTHS + r"\s+20\d{2})\b",
        # ISO без префикса (в таблицах/колонках)
        r"\b(20\d{2}-\d{2}-\d{2})\b",
        # Полная дата с 4-значным годом без префикса (в таблицах через | или пробел)
        r"(?:\||\s)(\d{1,2}[.\-/]\d{1,2}[.\-/]20\d{2})(?:\||\s|$)",
    ],
    "part_number": [
        # Метка артикула (+ синонимы Код/OEM/P/N/кат.номер) → значение. Разделитель допускает '|'
        # (расклеенная таблица «Артикул: | HP1731», «Код: | GDB1550DTE»).
        r"(?:артикул|арт\.?|код товара|код детали|кат\.?\s*номер|каталожн(?:ый)? номер|номер детали|номенклатур[аы]|oem|p\s*[/\\]\s*n|part(?: number)?|sku|код)\s*[:#№\-\.|  ]+([a-z0-9][a-z0-9._/\-]{2,50})",
        r"(?:покупали)\s*[:|  ]+([a-z0-9][a-z0-9._/\-]{3,50})",
        r"\bарт\.?\s+([a-z0-9][a-z0-9._/\-]{2,40})",
        # Последовательность «<контекст> БРЕНД(латиница) АРТИКУЛ»: «детали PATRON PC3460»,
        # «Деталь – DIAMOND 89550623702», «Отказ GSP 5409040PK». Берём 2-й токен (артикул).
        r"(?:возврат\w*\s+детал\w+|детал[ьияей]+|вернуть|отказ)\s*[–\-—:.]*\s*[A-Za-z][A-Za-z\-]{1,15}\s+([A-Za-z]*\d[A-Za-z0-9._/\-]{2,40})\b",
    ],
    "quantity": [
        # Только в контексте количества (НЕ из любых чисел — частая ошибка по аудиту).
        r"(?:кол[- ]?во|количество|qty)\s*[:=|  ]+(\d+(?:[,.]\d+)?)",
        r"в\s+количестве\s+(\d+(?:[,.]\d+)?)",
        r"(\d+(?:[,.]\d+)?)\s*(?:шт\.?|штук[аи]?|pcs)\b",
    ],
}

# Optional fields for the 1C JSON. They are useful when present but do not form a strong key.
FIELD_REGEX.setdefault("brand", [
    r"(?:бренд|марка|производитель)\s*[:#№\-|  ]+([a-zа-я0-9 ._\-/]{2,50})",
    # Бренд из последовательности «<контекст> БРЕНД АРТИКУЛ»: «детали PATRON PC3460» → PATRON
    r"(?:возврат\w*\s+детал\w+|детал[ьияей]+|вернуть|отказ)\s*[–\-—:.]*\s*([A-Za-z][A-Za-z\-]{1,15})\s+[A-Za-z]*\d[A-Za-z0-9._/\-]{2,}",
])
FIELD_REGEX.setdefault("product_name", [
    r"(?:наименование|товар|деталь)\s*[:#№\-|  ]+([а-яёa-z][а-яёa-z0-9 .,/()\-]{3,80})",
])
FIELD_REGEX.setdefault("comment", [
    r"(?:комментарий|описание|причина\s+(?:возврата|обращения|брака|недовоза|претензии)?|причина клиента|неисправность|дефект)\s*[:#№\- ]{0,2}([а-яёa-z0-9][^\n\r]{3,140})",
    r"(?:клиент\s+(?:указал|пишет|сообщает)[,: ]+)([а-яёa-z0-9][^\n\r]{3,140})",
])

DEADLINE_DAYS = {
    "shortage": 3,
    "number_replacement": 3,
    "wrong_item": 3,
    "nonconforming": 5,
    "defect": 5,
    "overdelivery": 3,
    "incomplete_set": 3,
    "correction_request": 2,
    "marking_request": 2,
    "quality_refusal": 3,
}

SLA_KIND_ATTRS = {
    "shortage": "sla_shortage_days",
    "number_replacement": "sla_wrong_item_days",
    "wrong_item": "sla_wrong_item_days",
    "incomplete_set": "sla_incomplete_set_days",
    "nonconforming": "sla_nonconforming_days",
    "defect": "sla_defect_days",
    "overdelivery": "sla_overdelivery_days",
    "quality_refusal": "sla_quality_refusal_days",
    "correction_request": "sla_correction_request_days",
    "marking_request": "sla_marking_request_days",
}

CLAIM_KIND_LABELS = {
    "defect": "Брак",
    "nonconforming": "Некондиция",
    "number_replacement": "Замена артикула",
    "wrong_item": "Пересорт",
    "shortage": "Недовоз/недопоставка",
    "overdelivery": "Излишек",
    "incomplete_set": "Некомплект",
    "correction_request": "Корректировка документов",
    "marking_request": "Маркировка",
    "quality_refusal": "Отказ клиента",
}

BAD_FIELD_VALUES = {
    "цена", "сумма", "причина", "товар", "товара", "артикул", "количество", "поставщик", "покупатель",
    "номер", "документ", "дата", "шт", "руб", "рублей", "итого", "наличие", "наименование", "брак", "дефект", "недовоз", "пересорт", "некондиция", "замена артикула",
    "описание", "комментарий", "проблема", "артикул товара", "код", "наименование товара",
}




def _contains_any(text: str, words: list[str]) -> bool:
    t = norm(text)
    return any(norm(w) in t for w in words if w)


def _is_service_report(email_data: dict[str, Any], text: str) -> bool:
    combined = "\n".join([
        str(email_data.get("subject") or ""),
        str(email_data.get("from_addr") or ""),
        text or "",
    ])
    t = norm(combined)
    if _contains_any(t, SERVICE_REPORT_PHRASES):
        return True
    return "price@" in t and ("прайс" in t or "price" in t)


def _score_words(text: str, words: list[str]) -> int:
    t = norm(text)
    return sum(1 for w in words if w and norm(w) in t)


def _email_domain(addr_value: str | None) -> str:
    _, addr = parseaddr(addr_value or "")
    if "@" not in addr:
        return ""
    return addr.rsplit("@", 1)[-1].lower()


def _all_addresses(*values: str | None) -> list[str]:
    result: list[str] = []
    for value in values:
        if not value:
            continue
        for _display, addr in getaddresses([value]):
            if addr:
                result.append(addr.lower())
    return result


def _is_company_addr(addr: str) -> bool:
    addr = (addr or "").lower()
    if not addr:
        return False
    if addr in settings.company_email_list:
        return True
    if "@" in addr:
        domain = addr.rsplit("@", 1)[-1]
        return any(domain == d or domain.endswith("." + d) for d in settings.company_domain_list)
    return False


def detect_direction(email_data: dict[str, Any]) -> tuple[str, list[str]]:
    subject = norm(email_data.get("subject"))
    body = norm(email_data.get("visible_text") or "")
    mailbox = norm(email_data.get("mailbox"))
    from_addrs = _all_addresses(email_data.get("from_addr"))
    rcpt_addrs = _all_addresses(email_data.get("to_addr"), email_data.get("cc_addr"))
    from_company = any(_is_company_addr(a) for a in from_addrs)
    to_company = any(_is_company_addr(a) for a in rcpt_addrs)
    has_company_config = bool(settings.company_email_list or settings.company_domain_list)
    sent_folder = any(x in mailbox for x in ["sent", "отправ", "исходящ"])
    forward_marker = _contains_any(subject + "\n" + body[:1000], settings.internal_forward_marker_list or INTERNAL_FORWARD_HINTS)
    reasons: list[str] = []
    if sent_folder:
        reasons.append("sent_folder")
    if from_company:
        reasons.append("from_company")
    if to_company:
        reasons.append("to_company")
    if forward_marker:
        reasons.append("forward_marker")

    if has_company_config:
        if from_company and to_company:
            return ("internal_forward" if forward_marker else "internal_thread"), reasons
        if from_company:
            return "outbound_company", reasons
        if to_company:
            return "inbound_customer", reasons
        return "external_unknown", reasons

    # Fallback when company domains are not configured yet. In v1.2 the safe default is
    # focused-folder mode: explicitly configured folders are customer intake folders.
    if sent_folder:
        return "outbound_company", reasons
    if forward_marker and not settings.configured_folders_are_customer:
        return "possible_internal_forward", reasons
    if settings.configured_folders_are_customer:
        reasons.append("configured_folder_customer_mode")
        return "inbound_customer", reasons
    if forward_marker:
        return "possible_internal_forward", reasons
    return "inbound_customer", reasons


def detect_buyer(
    email_data: dict[str, Any],
    buyer_rules: list[BuyerRule],
    learned_identities: list[dict[str, Any]] | None = None,
) -> tuple[str | None, str | None, float, list[str]]:
    from_addr = norm(email_data.get("from_addr"))
    domain = _email_domain(email_data.get("from_addr"))
    subject = norm(email_data.get("subject"))
    body = norm(email_data.get("visible_text") or visible_body(email_data.get("body_text"), email_data.get("body_html")))
    best: tuple[str | None, str | None, float, list[str]] = (None, None, 0.0, [])

    # Learned identities are allowed to identify the buyer, but not to make the case export-ready by themselves.
    # They are written only after manual confirmation or validated AI+human review.
    for ident in learned_identities or []:
        itype = str(ident.get("identity_type") or "").lower()
        ivalue = str(ident.get("identity_value") or "").lower()
        if not ivalue:
            continue
        if itype == "email" and ivalue in from_addr:
            score = min(0.96, float(ident.get("confidence") or 0.9) + 0.03)
            cand = (str(ident.get("buyer_code") or ""), ident.get("buyer_name"), score, [f"learned_email:{ivalue}"])
            if cand[2] > best[2]:
                best = cand
        elif itype == "domain" and domain and (domain == ivalue or domain.endswith("." + ivalue)):
            score = min(0.90, float(ident.get("confidence") or 0.85))
            cand = (str(ident.get("buyer_code") or ""), ident.get("buyer_name"), score, [f"learned_domain:{ivalue}"])
            if cand[2] > best[2]:
                best = cand

    for rule in buyer_rules:
        score = 0.0
        reasons: list[str] = []
        if domain and any(domain == d or domain.endswith("." + d) for d in rule.domains):
            score += 0.65
            reasons.append(f"domain:{domain}")
        if any(sender in from_addr for sender in rule.senders):
            score += 0.75
            reasons.append("sender")
        subj_hits = _score_words(subject, rule.subject_contains)
        body_hits = _score_words(body, rule.body_contains)
        if subj_hits:
            score += min(0.25, subj_hits * 0.08)
            reasons.append(f"subject:{subj_hits}")
        if body_hits:
            score += min(0.20, body_hits * 0.04)
            reasons.append(f"body:{body_hits}")
        if score > best[2]:
            best = (rule.code, rule.name, min(score, 1.0), reasons)
    return best


def _looks_like_price(v: str) -> bool:
    """Эвристика: выглядит ли строка как цена (число с пробелами тысяч или запятой копеек)."""
    # "2 164", "1 618", "12 500,00"
    clean = v.replace(" ", "").replace("\u00a0", "")
    if re.fullmatch(r"\d{2,8}[,\.]\d{2}", clean):
        return True
    if re.fullmatch(r"\d{4,8}", clean) and " " in v:
        return True
    # Число с разделителями цены (пробел тысяч ИЛИ десятичная запятая/точка) и >= 100 — цена.
    # ВАЖНО: голая последовательность цифр без разделителей (4801012010) — это OEM-артикул,
    # а НЕ цена. Многие оригинальные номера чисто цифровые. Не бракуем их.
    has_price_sep = bool(re.search(r"\d[\s ]\d", v) or re.search(r"\d[,.]\d{2}\b", v))
    if has_price_sep:
        try:
            num = float(clean.replace(",", "."))
            if num >= 100 and re.fullmatch(r"[\d\s,.  ]+", v.strip()):
                return True
        except Exception:
            pass
    return False


def _date_grounded_in_text(date_str: str, text: str) -> bool:
    """True, если дата (в любом распространённом формате) реально встречается в тексте письма.

    Защита от «document_date_not_found_in_email_text» (527 расхождений в аудите): regex
    иногда вытягивает чужое число как дату. Если нормализованной даты нет в тексте — не верим ей.
    """
    if not date_str or not text:
        return False
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", str(date_str).strip())
    if not m:
        return True  # не наш нормализованный формат — не вмешиваемся
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    yy = y % 100
    t = text.lower()
    cands = [
        f"{d:02}.{mo:02}.{y}", f"{d}.{mo}.{y}", f"{d:02}.{mo:02}.{yy:02}", f"{d}.{mo}.{yy:02}",
        f"{y}-{mo:02}-{d:02}", f"{d:02}-{mo:02}-{y}", f"{d:02}-{mo:02}-{yy:02}",
        f"{d:02}/{mo:02}/{y}", f"{d}/{mo}/{y}",
    ]
    if any(c in t for c in cands):
        return True
    months = ["январ", "феврал", "март", "апрел", "мая", "июн", "июл", "август", "сентябр", "октябр", "ноябр", "декабр"]
    mon = months[mo - 1] if 1 <= mo <= 12 else ""
    if mon and re.search(rf"\b{d}\s+{mon}", t):
        return True
    return False


def _date_anchored_to_document(date_str: str, doc: str, text: str) -> bool:
    """True, если дата стоит РЯДОМ с номером документа (это дата документа), а не случайная
    дата (оферты/подписи/поставки) из тела. Контроль: в письме часто несколько дат."""
    if not date_str or not doc or not text:
        return True
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", str(date_str).strip())
    if not m:
        return True
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    yy = y % 100
    variants = [f"{d:02}.{mo:02}.{y}", f"{d}.{mo}.{y}", f"{d:02}.{mo:02}.{yy:02}", f"{d}.{mo}.{yy:02}", f"{y}-{mo:02}-{d:02}"]
    t = text.lower()
    for dm in re.finditer(re.escape(str(doc).lower()), t):
        window = t[max(0, dm.start() - 15): dm.end() + 60]
        if any(v in window for v in variants):
            return True
    return False


def _distinct_date_count(text: str) -> int:
    """Сколько РАЗНЫХ дат в тексте (для решения, неоднозначна ли дата документа)."""
    return len(set(re.findall(r"\b\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}\b", text or "")))


# bad-context: значение поля стоит в «запрещённом» окружении (заявка/маркировка/телефон…),
# значит это НЕ то поле (правдоподобный мусор). Регексы из mail_sorter recommendations/static_max.
# Границы слов ОБЯЗАТЕЛЬНЫ: без них «тел» ловится в «покупаТЕЛя», «инн» в «длИННый» → ложные флаги.
_DOC_NUMBER_BAD_RE = re.compile(r"\b(заявк\w*|заказ\w*|тикет\w*|обращени\w*|reclam\w*|телефон\w*|инн|кпп|трек\w*)\b", re.I | re.U)
_PART_BAD_RE = re.compile(r"\b(маркировк\w*|честн\w*\s+знак\w*|код\s+маркировк\w*)\b", re.I | re.U)


def _value_only_in_bad_context(value: str, text: str, bad_re: "re.Pattern", width: int = 22) -> bool:
    """True, если ВСЕ вхождения значения предварены запрещённой меткой (заявка/тикет/маркировка…)
    и нет ни одного «чистого». Метка стоит ПЕРЕД значением, поэтому смотрим левый контекст.
    Тогда поле извлечено из неправильного места — не пускаем в 1С автоматом, в Сверку."""
    if not value or not text:
        return False
    occ = list(re.finditer(re.escape(str(value)), text, re.I))
    if not occ:
        return False
    for m in occ:
        left = text[max(0, m.start() - width): m.start()]
        if not bad_re.search(left):
            return False  # есть хотя бы одно вхождение с нормальной (не запрещённой) меткой слева
    return True


_EXPLICIT_LABEL_RE = {
    "quantity": re.compile(r"(?:кол[- ]?во|количество)\s*[:=|  ]+(\d+)", re.I),
    "part_number": re.compile(r"(?:артикул(?:\s+заказа)?|код товара)\s*[:#№\-\.|  —]+\*?\s*([a-z0-9][a-z0-9._/\-]{2,50})", re.I),
}


def apply_explicit_labels(fields: dict[str, Any], text: str) -> list[tuple[str, str, str]]:
    """Автокоррекция: если в письме есть ЯВНАЯ метка (Кол-во: X / Артикул: Y), она авторитетнее
    извлечённого паттерном значения — ставим её АВТОМАТИЧЕСКИ (не дёргаем оператора).

    Чинит самый опасный класс аудита (mismatch_with_explicit_label) без ручной проверки.
    Возвращает список исправлений (поле, было, стало) — для лога.
    """
    fixes: list[tuple[str, str, str]] = []
    t = text or ""
    for key, rx in _EXPLICIT_LABEL_RE.items():
        m = rx.search(t)
        if not m:
            continue
        explicit = str(m.group(1)).strip()
        if not explicit or _looks_like_bad_value(explicit, key):
            continue
        got = str(fields.get(key) or "").strip()
        if got.lower() != explicit.lower():
            fixes.append((key, got, explicit))
            fields[key] = explicit
    return fixes


def _looks_like_bad_value(value: Any, field: str) -> bool:
    v = norm(str(value or "")).strip(" .,:;№#")
    if not v:
        return True
    if v in BAD_FIELD_VALUES:
        return True
    if field == "part_number":
        if len(v) < int(settings.part_number_min_len) or len(v) > int(settings.part_number_max_len):
            return True
        if re.fullmatch(r"\d+/\d+/?", v):
            return True
        # Чисто кириллица (одно ИЛИ несколько слов: «начала разгрузки», «детали») — НЕ артикул.
        if re.fullmatch(r"[а-яё]+(?:\s+[а-яё]+)*", v, re.I):
            return True
        # Доменный хвост из подписи (trinity-parts.ru → «s.ru») или email-фрагмент — НЕ артикул.
        if re.search(r"\.(?:ru|com|рф|net|org|su)$", v, re.I) or "@" in v:
            return True
        # Чисто латиница без единой цифры и ≤6 символов — это БРЕНД (DEPO/MILES/VIKA/ELF/VKD),
        # а не артикул. Настоящий артикул почти всегда содержит цифру.
        if re.fullmatch(r"[a-z]{2,6}", v, re.I):
            return True
        if re.fullmatch(r"\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}", v):
            return True
        if re.fullmatch(r"\d+[,.]\d{2}", v):
            return True
        # Артикул не может быть просто числом >= 100 с пробелами (это цена)
        if _looks_like_price(v):
            return True
        # Article can be numeric, but a random 1-2 digit number is almost surely quantity.
        if re.fullmatch(r"\d{1,2}", v):
            return True
        # Mercedes/OEM-артикул с пробелами («A 271 030 09 63», «271 030 09 63») — это ВАЛИДНЫЙ
        # артикул, а не «3+ слова мусор». Опц. буква + группы цифр через пробел.
        if re.fullmatch(r"[A-Za-z]{0,2}\s*\d{3}(?:\s+\d{2,3}){2,3}", v):
            return False
        # 3+ слова = бренд+склейка значений ("STELLOX 2998821SX 29-98821-sx"). 1-2 токена допустимы.
        if len(v.split()) > 2:
            return True
        # Если 2 токена и первый — словобренд (только буквы, ≥4) — это "БРЕНД артикул", берём только артикул-часть отдельным паттерном, а склейку отвергаем
        parts = v.split()
        if len(parts) == 2 and re.fullmatch(r"[A-Za-zА-Яа-яЁё]{4,}", parts[0]) and re.search(r"\d", parts[1]):
            return True
    if field == "quantity":
        try:
            q = float(v.replace(",", "."))
            if q <= 0 or q > 100000:
                return True
        except Exception:
            return True
    if field in {"claim_number", "client_request_number", "return_number"}:
        if len(v) < 4 or len(v) > 40:
            return True
        if not re.search(r"\d", v):
            return True
    if field == "document_number":
        if len(v) < int(settings.document_number_min_len) or len(v) > int(settings.document_number_max_len):
            return True
        if not re.search(r"\d", v):
            return True
        if re.fullmatch(r"\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}", v):
            return True
        if re.fullmatch(r"\d+[,.]\d{2}", v):
            return True
        # Номер документа ≠ артикул. SKU-форма «латиница+цифры слитно» (RF80541SMPB, ETZ1107MR)
        # — это part_number, а не номер документа. Реальные номера: чистые цифры (82933),
        # с дефисом/кириллическим префиксом (А-260603-00072), но не латинский SKU.
        if re.fullmatch(r"[A-Za-z]{2,}\d{2,}[A-Za-z0-9]*", v):
            return True
    if field == "comment":
        # Комментарий должен быть осмысленным текстом, а не дампом данных
        if len(v) < 4 or len(v) > 240:
            return True
        if v in {"добрый", "здравствуйте", "уведомляем", "с уважением", "запрос на в"}:
            return True
        if re.fullmatch(r"[а-яё]{2,5}\s+добр(?:ый)?", v, re.I):
            return True
        if v in {"выписана по", "при оприход", "от вас по н", "в количеств"}:
            return True
        # Если много цифр без пробелов — это скорее всего мусор (конкатенация артикулов/цен)
        digits = sum(1 for c in v if c.isdigit())
        letters = sum(1 for c in v if c.isalpha())
        if digits > 20 and digits > letters * 2:
            return True
        # Цена/количество в середине текста — признак мусора
        if re.search(r'\d{2,3}\s{0,1}\d{3}[.,]?\d{0,2}', v):
            return True
        # Слишком много слов — захватили не комментарий, а параграф
        words = v.split()
        if len(words) > 20:
            return True
    if field == "brand":
        # Бренд не может содержать слова-заголовки таблицы (захват шапки)
        header_words = ("артикул", "кол-во", "кол во", "количество", "цена", "описание",
                        "наименование", "группа", "стоимость", "производитель", "номер",
                        "сумма", "ед. изм", "ед.изм", "вес", "кратность", "документ", "название")
        if any(h in v for h in header_words):
            return True
        if any(h in v for h in ("avtoto", "vozvra", "mail.ru", "www.", "http")):
            return True
        if v in {"mai", "mail"}:
            return True
        if len(v) <= 2:
            return True
        if len(v) > 25:
            return True
        if re.fullmatch(r"[a-zа-яё]\d{1,2}", v, re.I):
            return True
        if re.search(r"\d", v) and re.fullmatch(r"[a-z0-9._/-]{3,30}", v, re.I):
            return True
        # Бренд из одних цифр — мусор
        if re.fullmatch(r"[\d\s.,]+", v):
            return True
    if field == "product_name":
        if len(v) < 3 or len(v) > 80:
            return True
        low = v.lower()
        # Наименование — это НАЗВАНИЕ ДЕТАЛИ, а НЕ фраза/шапка письма. Это главный источник
        # мусора в аудите (1163+86): «Запрос на возврат», «надлежащего качества», «Добрый день…».
        EMAIL_PHRASES = (
            "запрос на возврат", "надлежащего качества", "добрый день", "здравствуйте",
            "с уважением", "получили отказ", "прошу", "просьба", "рассмотрет", "согласовать",
            "по причине", "отказ клиент", "отказ конечного", "отказ покупател", "уведомление",
            "при приемке", "при приёмке", "возврат товара", "вернуть товар", "хотим вернуть",
            "сообщение по рекламации", "новое сообщение", "обнаружено", "при приём",
            "будет отправлена клиенту", "если клиент откажется", "повторное письмо",
            "отдел рекламаций автото", "отправлено из почты", "от кого",
            "получен заказ",
            # ПИТСТОП-подвал акта: «Товар сдал/принял», «Поставщик», «Приходная накладная»,
            # «Составлен акт», «по факту приемки» — это служебный текст, не название детали.
            "товар сдал", "товар принял", "поставщик", "приходная накладн",
            "составлен акт", "по факту приемки", "по факту приёмки", "мнение комисси",
            # Шапка формы ТОРГ-2/ТОРГ-12 («наименование товара (груза) или номера вагонов
            # (контейнеров, автофургонов…)») — служебный текст бланка, не название детали.
            "вагонов", "контейнеров", "автофургонов", "груза)", "номера вагон",
        )
        # Одиночные обрывки служебных строк («сдал», «принял», «комиссия»).
        if low in {"сдал", "принял", "комиссия", "поставщик", "не указано"}:
            return True
        if any(p in low for p in EMAIL_PHRASES):
            return True
        if low.startswith("артикул ") or " накладная" in low:
            return True
        # Обрезанный «хвост» письма: заканчивается коротким огрызком слова (Зд, Добр, то)
        # после нормального слова — признак обрезки фразы, а не названия детали.
        _toks = v.split()
        if len(_toks) >= 3 and len(_toks[-1]) <= 3 and _toks[-1][:1].isupper():
            return True
        # Наименование не должно содержать слова-заголовки
        if any(h in v for h in ("кол-во", "количество", "цена без ндс", "стоимость с ндс", "сумма ндс", "ед. изм")):
            return True
        # Наименование не должно состоять в основном из цифр
        digits = sum(1 for c in v if c.isdigit())
        letters = sum(1 for c in v if c.isalpha())
        if digits > 0 and letters == 0:
            return True
        if digits > 10 and digits > letters * 2:
            return True
        # Цена внутри наименования — явный мусор
        if re.search(r'\d{2,3}\s+\d{3}', v):
            return True
    return False


def _clean_extracted_field(key: str, value: str) -> str:
    value = str(value or "").strip(" .,:;№#|")
    if key in {"claim_number", "client_request_number", "return_number"}:
        value = re.sub(r"/\d{6,}/?$", "", value).strip(" .,:;№#/")
    if key in {"brand", "product_name", "comment"}:
        # Расклеенная таблица оставляет «| » в начале/конце значения — срезаем.
        value = value.strip(" |").strip(" .,:;№#")
        # Срезаем хвост на разделителе колонки таблицы («Краsuf | 1 | 2 674»).
        value = re.split(r"\s*\|\s*", value, maxsplit=1)[0].strip()
        # Stop greedy optional fields before the next labelled value in compact emails.
        value = re.split(
            r"\s+(?:артикул|арт\.?|кол[- ]?во|количество|qty|бренд|марка|производитель|"
            r"наименование|номенклатура|товар|деталь|упд|накладн[аяой]|дата|причина|комментарий)"
            r"(?:\s*[:#№=\- ]|\b)",
            value,
            maxsplit=1,
            flags=re.I,
        )[0].strip(" .,:;№#")
        value = re.split(
            r"\s+(?:в\s+случае\s+возникновения\s+вопросов|подтвердите\s+готовность|"
            r"ознакомиться\s+с\s+рекламацией|с\s+уважением|>{2,}|отдел\s+рекламаций\s+автото)",
            value,
            maxsplit=1,
            flags=re.I,
        )[0].strip(" .,:;№#")
        if key == "comment":
            value = re.sub(r"^(?:клиент[а-яё]*|возврат[а-яё]*|обращени[а-яё]*|брака|претензи[а-яё]*)\s*[:#№=\- ]+", "", value, flags=re.I).strip(" .,:;№#")
            # Обрезаем мусорный хвост: ссылки на файлы/фото, шаблонные фразы.
            value = re.split(
                r"\s*(?:ссылк[аи]\s+на|https?://|перейти\s+к\s+заявке|просьба\s+согласовать|"
                r"для\s+подтверждения|не\s+отвечайте)",
                value, maxsplit=1, flags=re.I,
            )[0].strip(" .,:;№#/*>-")
    return value














def extract_fields(text: str, buyer_rule: BuyerRule | None = None) -> dict[str, Any]:
    """v2.1 AI-only: паттерн-извлечение полей убрано целиком — поля заполняет ИИ."""
    return {}


# Вердикт акта ТОРГ-2: «мнение комиссии о причинах их образования» → ниже «<АРТ> - <причина>».
# Это АВТОРИТЕТНАЯ причина (документ важнее тела письма-шаблона «обнаружено несоответствие»).
# Текст акта расклеен через « | » с сотнями пустых ячеек, поэтому берём ОКНО после заголовка
# (до «заключение комиссии») и ищем ключевые слова причины в нём.
def claim_kind_from_act(text: str) -> str | None:
    """claim_kind из АВТОРИТЕТНОГО источника-документа:
      • акт ТОРГ-2 «мнение комиссии о причинах»;
      • колонка «Тип возврата» в Excel-перечне (autorus «Возврат товаров №В…»).
    Это перебивает ложный correction_request от слова «счёт-фактура» (это лишь колонка-ссылка)."""
    t = norm(text or "")
    idx = t.find("мнение комисси")
    win = 2000
    if idx < 0:
        idx = t.find("тип возврата")  # Excel-перечень возврата (autorus): колонка с причиной
    if idx < 0:
        # autoeuro «Уведомление»: реальная причина в «Комментарий: <Пересорт/Брак/…>»,
        # а generic-фраза «детали с дефектами забракованы» — шаблон (не показатель).
        idx = t.find("комментарий")
        win = 70  # тесное окно — только значение комментария, без чужого текста
    if idx < 0:
        return None
    v = t[idx: idx + win]
    cut = v.find("заключени")          # дальше — общий boilerplate-вывод, его не берём
    if cut > 40:
        v = v[:cut]
    if not v.strip():
        return None
    if "недопоставк" in v or "недостач" in v:
        return "shortage"
    if "честн" in v or "маркировк" in v or ("код" in v and "не совпад" in v):
        return "marking_request"
    if "пересорт" in v or "не верный подбор" in v or "неверный подбор" in v:
        return "wrong_item"
    if "излиш" in v:
        return "overdelivery"
    if "отказ" in v or "октаз" in v:  # «октаз» — частая опечатка «отказ»
        return "quality_refusal"
    if any(w in v for w in ("сломан", "не работает", "неисправ", "разбит", "треснут", "трещин",
                            "отсутств", "некомплект", "поврежд", "бой", "скол", "вмятин", "деформ",
                            "брак", "дефект", "замят", "погнут", "не герметич")):
        return "defect"
    return None


def detect_kind(text: str, buyer_rule: BuyerRule | None = None) -> tuple[str | None, float, list[str]]:
    t = norm(text)
    candidates: dict[str, int] = {}
    reasons: list[str] = []
    for kind, words in DEFAULT_KIND_PATTERNS.items():
        n = _score_words(t, words)
        if n:
            candidates[kind] = candidates.get(kind, 0) + n
            reasons.append(f"{kind}:{n}")
    if buyer_rule:
        for kind, words in buyer_rule.statuses.items():
            n = _score_words(t, words)
            if n:
                candidates[kind] = candidates.get(kind, 0) + n + 1
                reasons.append(f"buyer_status:{kind}:{n}")
    if not candidates:
        return None, 0.0, reasons
    # «defect» (брак) требует пакет из 3 документов — ставим его осторожно: при равенстве
    # очков уступает более лёгким типам (некондиция/недовоз/пересорт), а не выигрывает по алфавиту.
    kind = sorted(candidates.items(), key=lambda kv: (-kv[1], 1 if kv[0] == "defect" else 0, kv[0]))[0][0]
    confidence = min(0.85, 0.45 + candidates[kind] * 0.12)
    return kind, confidence, reasons


def detect_event_type(email_data: dict[str, Any], text: str, claim_kind: str | None, fields: dict[str, Any], direction: str, *, is_first_contact_result: tuple[bool, list[str]] | None = None) -> tuple[str, bool, list[str]]:
    subject = norm(email_data.get("subject"))
    body = norm(text)
    has_reply_headers = bool(email_data.get("in_reply_to") or email_data.get("references"))
    subject_is_reply = bool(SUBJECT_REPLY_MARKERS.match(subject))
    reminder = _contains_any(text, REMINDER_WORDS)
    strong_reminder = _contains_any(text, STRONG_REMINDER_WORDS)
    supplier_decision = _contains_any(text, SUPPLIER_DECISION_WORDS)
    new_words = _contains_any(text, NEW_RETURN_WORDS)
    reasons: list[str] = []

    # Определяем, является ли письмо первым контактом
    if is_first_contact_result is not None:
        is_first, first_reasons = is_first_contact_result
    else:
        is_first, first_reasons = is_first_contact(email_data)
    if not is_first:
        reasons.extend(first_reasons)
        reasons.append("not_first_contact")

    # Проверка новых типов (не-возвраты)
    if _is_service_report(email_data, text):
        reasons.append("service_report")
        return "supplier_report", False, reasons

    if _contains_any(text, SPAM_PROMO_WORDS) and not fields.get("part_number") and not fields.get("document_number"):
        reasons.append("spam_promo")
        return "spam_promo", False, reasons

    if _contains_any(text, READY_TO_SHIP_PHRASES):
        reasons.append("ready_to_ship_phrases")
        return "ready_to_ship", False, reasons

    # info_only — ТОЛЬКО когда нет данных возврата. Иначе «Информируем Вас о необходимости
    # вернуть товар: Бренд X Артикул Y Документ Z» ложно уходил в «Не по теме».
    _has_return_data = bool(
        (fields.get("part_number") and (fields.get("document_number") or fields.get("return_number") or fields.get("claim_number") or fields.get("client_request_number")))
        or _contains_any(text, ["вернуть товар", "на возврат", "возврату подлеж", "необходимость вернуть"])
    )
    if not claim_kind and not _has_return_data and _contains_any(text, INFO_ONLY_PHRASES):
        reasons.append("info_only_phrases")
        return "info_only", False, reasons

    # Объявление о переходе на ЭДО / смене документооборота — это инфо, НЕ возврат.
    # «Росско. Возвраты. Переход на систему ЭДО в Диадок» — слово «возврат» в теме ложно
    # затягивало в new_return. Признак: ЭДО/Диадок/СБИС + глагол-объявление, и нет артикула.
    if (not fields.get("part_number")
            and _contains_any(text, ["переход на эдо", "переход на систему эдо", "электронный документооборот",
                                     "оператор эдо", "оператора эдо", "по эдо", "диадок", "сбис"])
            and _contains_any(text, ["сообщаем", "доработан", "переход на", "просим вас уведомить",
                                     "по какой системе", "необходима ли вам"])):
        reasons.append("edo_announcement")
        return "info_only", False, reasons

    # Акт сверки / «прошу сверить и подписать» — сверка по МНОГИМ документам, а не один новый
    # возврат. Это продолжение/напоминание по существующим возвратам → followup_reminder.
    if _contains_any(text, ["акт сверки", "прошу сверить", "просьба сверить", "во вложении сверка",
                            "во вложении сверку", "сверить и подписать", "сверка за 20"]):
        reasons.append("reconciliation_request")
        return "followup_reminder", True, reasons

    if reminder:
        reasons.append("reminder_words")
    if strong_reminder:
        reasons.append("strong_reminder_words")
    if has_reply_headers:
        reasons.append("reply_headers")
    if subject_is_reply:
        reasons.append("reply_subject")
    if supplier_decision:
        reasons.append("supplier_decision_words")
    if direction not in {"inbound_customer", "external_unknown"}:
        reasons.append(f"direction:{direction}")
        return direction, True, reasons

    # Маркировка/ТНВЭД и корректировки — самостоятельные служебные типы даже в reply-chain.
    # Иначе они растворяются в общем followup_dialog и оператор не видит реальную причину.
    if claim_kind in {"correction_request", "marking_request", "number_replacement"}:
        reasons.append(f"service_type:{claim_kind}")
        return claim_kind, bool(has_reply_headers or subject_is_reply), reasons

    # ── Письма с историей переписки ─────────────────────────────────────
    # Если письмо НЕ является первым контактом — оно НЕ может быть new_return.
    if not is_first:
        if strong_reminder:
            return "followup_reminder", True, reasons
        if supplier_decision and (has_reply_headers or subject_is_reply):
            return "supplier_decision", True, reasons
        if has_reply_headers or subject_is_reply:
            return "followup_dialog", True, reasons
        # Даже без техзаголовков, но с речевыми маркерами ответа
        if reminder:
            return "followup_reminder", True, reasons
        reasons.append("not_first_fallback_followup")
        return "followup_dialog", True, reasons

    # ── Первый контакт (нет истории переписки) ──────────────────────────
    if strong_reminder:
        # Без истории переписки напоминание маловероятно — но проверим
        reasons.append("first_contact_strong_reminder_unlikely")
    if supplier_decision and (has_reply_headers or subject_is_reply):
        return "supplier_decision", True, reasons
    # Признак возврата: явные слова (возврат/рекламация/претензия/вернуть) ИЛИ
    # извлечённая товарная строка возврата (документ+артикул, либо № заявки).
    # Под-тип (claim_kind) тут может быть НЕ определён — для generic «возврата» в
    # DEFAULT_KIND_PATTERNS нет слова-триггера. Это НЕ повод ронять письмо в
    # «неразобранные»: это явный new_return, под-тип уточнит AI/оператор.
    return_signal = (
        new_words
        or bool(fields.get("part_number") and fields.get("document_number"))
        or bool(fields.get("return_number"))
    )
    if claim_kind or return_signal:
        if not claim_kind:
            reasons.append("new_return_without_subkind")
        return "new_return", False, reasons
    if has_reply_headers or subject_is_reply:
        return "followup_dialog", True, reasons
    return "unknown", False, reasons


def make_strong_key(buyer_code: str | None, fields: dict[str, Any]) -> str | None:
    # A business key without buyer is not strong enough for linking/export:
    # document/part/request numbers can collide between customers.
    if not buyer_code:
        return None
    buyer = buyer_code
    for key in ("claim_number", "client_request_number", "return_number"):
        value = fields.get(key)
        if value and not _looks_like_bad_value(value, key):
            return f"{key}:{buyer}:{str(value).lower()}"
    doc = fields.get("document_number")
    part = fields.get("part_number")
    if doc and part and not _looks_like_bad_value(doc, "document_number") and not _looks_like_bad_value(part, "part_number"):
        return f"doc_part:{buyer}:{str(doc).lower()}:{str(part).lower()}"
    if doc and not _looks_like_bad_value(doc, "document_number"):
        return f"document:{buyer}:{str(doc).lower()}"
    return None


def make_thread_key(email_data: dict[str, Any], buyer_code: str | None, strong_key: str | None) -> tuple[str, str | None]:
    if strong_key:
        return f"strong:{strong_key}", None
    refs = email_data.get("references") or []
    in_reply_to = email_data.get("in_reply_to")
    if refs:
        return f"message:{refs[0]}", None
    if in_reply_to:
        return f"message:{in_reply_to}", None
    template = normalize_subject(email_data.get("subject"))
    weak = f"subject:{buyer_code or 'unknown'}:{template}"
    return stable_hash(weak, "weak_thread"), weak


def parse_received_at(email_data: dict[str, Any]) -> datetime:
    value = email_data.get("received_at")
    if value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            pass
    return datetime.now(timezone.utc)


def deadline_for(kind: str | None, event_type: str, received_at: datetime) -> str | None:
    if event_type == "followup_reminder":
        hours = int(getattr(settings, "sla_followup_hours", 4) or 4)
        return (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(microsecond=0).isoformat()
    if event_type == "supplier_decision":
        hours = int(getattr(settings, "sla_supplier_decision_hours", 4) or 4)
        return (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(microsecond=0).isoformat()
    if event_type in {"outbound_company", "internal_forward", "internal_thread", "possible_internal_forward"}:
        return None
    attr = SLA_KIND_ATTRS.get(kind or "")
    days = int(getattr(settings, attr, 0) or 0) if attr else 0
    if days <= 0:
        days = int(DEADLINE_DAYS.get(kind or "", settings.default_deadline_days) or settings.default_deadline_days)
    return (received_at + timedelta(days=days)).replace(microsecond=0).isoformat()


def priority_for(deadline_at: str | None, text: str, is_followup: bool) -> str:
    if _contains_any(text, ["срочно", "повторно", "крайний срок", "просроч", "горит"]):
        return "critical"
    if not deadline_at:
        return "normal"
    try:
        deadline = datetime.fromisoformat(deadline_at.replace("Z", "+00:00"))
        hours = (deadline - datetime.now(timezone.utc)).total_seconds() / 3600
    except Exception:
        return "normal"
    critical_hours = float(getattr(settings, "sla_critical_hours", 0) or 0)
    warning_hours = float(getattr(settings, "sla_warning_hours", 24) or 24)
    if hours <= critical_hours:
        return "critical"
    if hours <= warning_hours:
        return "high"
    if is_followup or hours <= max(72, warning_hours * 2):
        return "medium"
    return "normal"


URL_RE = re.compile(r"https?://[^\s<>'\"]+", re.I)
PHOTO_EXT_RE = re.compile(r"\.(?:jpg|jpeg|png|heic|webp|bmp|gif)$", re.I)
DOC_EXT_RE = re.compile(r"\.(?:pdf|doc|docx|xls|xlsx|zip|rar|7z)$", re.I)
DEFECT_DOC_WORDS = [
    "акт", "акт возврата", "заказ-наряд", "заказ наряд", "установк", "сняти", "заключение",
    "заключение сервиса", "сервисн", "диагност", "дефектовк", "рекламационный акт",
]
# Три документа пакета брака (актуально ТОЛЬКО для claim_kind=defect). Детект по имени файла + по
# содержимому (extracted_text из Excel/PDF). Включена латиница в именах (akt/act/torg = акт ТОРГ-2).
DEFECT_DOC_TYPES = {
    "install_order": (["установк", "монтаж", "install"], "заказ-наряд на установку"),
    "removal_order": (["сняти", "снят", "демонтаж", "removal"], "заказ-наряд на снятие"),
    "service_act": (["акт", "заключени", "сервис", "дефектовк", "диагност", "экспертиз",
                     "akt", "act", "torg", "торг-2", "торг 2"], "акт/заключение сервиса"),
}
# Маркеры в СОДЕРЖИМОМ документа (Excel/PDF извлечён в extracted_text) → это акт/заключение брака.
_DEFECT_CONTENT_MARKERS = {
    "service_act": re.compile(r"торг[- ]?2|унифицированная\s+форма\s*№?\s*торг|акт\s+о\s+|акт\s+об\s+установлен|"
                              r"заключени[ея]|дефектовочн|рекламацион|акт\s+экспертиз", re.I | re.U),
    "install_order": re.compile(r"заказ[- ]?наряд.{0,30}установк|наряд[- ]?заказ.{0,30}монтаж", re.I | re.U),
    "removal_order": re.compile(r"заказ[- ]?наряд.{0,30}сняти|демонтаж", re.I | re.U),
}


def classify_defect_documents(attachments: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Какие из 3 документов брака приложены — по ИМЕНАМ файлов И СОДЕРЖИМОМУ (extracted_text). Только для брака.

    Excel/PDF читается на импорте (extracted_text). Акт ТОРГ-2 распознаётся по содержимому даже если имя
    файла нейтральное/латиницей. По позиции/маркерам, НЕ по конкретному значению.
    """
    present = {"install_order": False, "removal_order": False, "service_act": False}
    for att in attachments or []:
        name = norm(str(att.get("filename") or ""))
        content = str(att.get("extracted_text") or "")
        for key, (words, _label) in DEFECT_DOC_TYPES.items():
            if name and any(w in name for w in words):
                present[key] = True
            elif content and _DEFECT_CONTENT_MARKERS.get(key) and _DEFECT_CONTENT_MARKERS[key].search(content):
                present[key] = True
    missing = [label for key, (_w, label) in DEFECT_DOC_TYPES.items() if not present[key]]
    return {"present": present, "missing": missing, "complete": not missing}


def defect_documents_flag(attachments: list[dict[str, Any]] | None) -> dict[str, Any]:
    """МЕХАНИЧЕСКИЙ флаг полноты документов брака (без ИИ, по вложениям+именам).

    Состояния (state):
      - "absent"             — вложений нет → документов брака нет;
      - "present_unverified" — файлы есть, но тип по именам НЕ опознан (нужен ИИ-vision);
      - "partial"            — по именам опознана ЧАСТЬ из 3 документов;
      - "complete"           — по именам опознаны ВСЕ 3 (установка+снятие+акт сервиса).
    `needs_ai_vision=True` помечает кейсы, где точный флаг даст только чтение фото/скана ИИ.
    Документы брака: заказ-наряд на установку, заказ-наряд на снятие, акт/заключение сервиса.
    """
    atts = attachments or []
    docs = classify_defect_documents(atts)
    present = docs["present"]
    n_present = sum(1 for v in present.values() if v)
    photos = [a for a in atts if PHOTO_EXT_RE.search(str(a.get("filename") or ""))
              or "image" in str(a.get("content_type") or "").lower()]
    docfiles = [a for a in atts if DOC_EXT_RE.search(str(a.get("filename") or ""))]
    if not atts:
        state = "absent"
    elif n_present >= 3:
        state = "complete"
    elif n_present >= 1:
        state = "partial"
    else:
        state = "present_unverified"
    vision_allowed = bool(
        getattr(settings, "defect_doc_ai_read", False)
        and getattr(settings, "defect_vision_enabled", False)
        and getattr(settings, "ai_vision_enabled", False)
    )
    if vision_allowed:
        documents_status = state
    elif atts:
        documents_status = "metadata_only"
    else:
        documents_status = "unknown_not_read"
    return {
        "state": state,
        "defect_documents_status": documents_status,
        "present": present,
        "missing": docs["missing"],
        "attachments_count": len(atts),
        "has_attachments": bool(atts),
        "has_images": bool(photos),
        "photos_count": len(photos),
        "docfiles_count": len(docfiles),
        "needs_ai_vision": vision_allowed and state == "present_unverified" and bool(photos or docfiles),
        "operator_attention": not vision_allowed or state != "complete",
        "attachment_strategy": getattr(settings, "defect_attachment_strategy", "metadata_only"),
        "read_pdf_first": bool(getattr(settings, "defect_read_pdf_first", True)),
        "images_order": getattr(settings, "defect_read_images_order", "first_last_then_inner"),
        "max_images": max(0, int(getattr(settings, "max_defect_images_per_case", 2) or 0)),
        "vision_enabled": vision_allowed,
    }


RETURN_LINK_WORDS = ["возврат", "претенз", "рекламац", "claim", "return", "rma", "photo", "фото", "акт"]
PHOTO_WORDS = ["фото", "фотограф", "снимок", "влож", "прикреп", "скан", "изображен"]
SERVICE_DOC_NEGATIVE_RE = re.compile(r"(?:нет|без|отсутств|не\s+прилож|не\s+готов|пока\s+не\s+готов|не\s+предоставлен).{0,40}(?:акт|заказ[- ]?наряд|заключение|сервисн|дефектовк)|(?:акт|заказ[- ]?наряд|заключение).{0,40}(?:нет|отсутств|не\s+готов|пока\s+не\s+готов|не\s+прилож)", re.I | re.U)


def _extract_links(text: str | None) -> list[str]:
    result: list[str] = []
    for url in URL_RE.findall(text or ""):
        clean = url.rstrip(".,;)\"]}")
        if clean not in result:
            result.append(clean)
    return result[:20]


def _attachment_name(att: dict[str, Any]) -> str:
    return norm(" ".join([str(att.get("filename") or ""), str(att.get("content_type") or "")]))


def evidence_summary(text: str, attachments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Return proof checklist used by validator and by 1C JSON.

    It is intentionally conservative: external return links count as proof only when the
    corresponding setting is enabled, because the system cannot always open partner portals.
    """
    attachments = attachments or []
    t = norm(text)
    links = _extract_links(text)
    photos = []
    docs = []
    service_docs = []
    for att in attachments:
        name = _attachment_name(att)
        filename = str(att.get("filename") or "")
        ctype = str(att.get("content_type") or "").lower()
        is_photo = bool(PHOTO_EXT_RE.search(filename)) or ctype.startswith("image/")
        is_doc = bool(DOC_EXT_RE.search(filename)) or any(x in ctype for x in ["pdf", "word", "excel", "spreadsheet", "zip", "rar"])
        is_service = any(w in name for w in DEFECT_DOC_WORDS)
        if is_photo:
            photos.append({"filename": filename, "content_type": att.get("content_type"), "size_bytes": att.get("size_bytes")})
        if is_doc:
            docs.append({"filename": filename, "content_type": att.get("content_type"), "size_bytes": att.get("size_bytes")})
        if is_service:
            service_docs.append({"filename": filename, "content_type": att.get("content_type"), "size_bytes": att.get("size_bytes")})
    text_has_service_doc = any(w in t for w in DEFECT_DOC_WORDS) and not SERVICE_DOC_NEGATIVE_RE.search(t)
    text_mentions_photo = any(w in t for w in PHOTO_WORDS)
    return_links = []
    for link in links:
        l = norm(link)
        if any(w in l for w in RETURN_LINK_WORDS) or any(w in t for w in ["по ссылке", "ссылка на возврат", "возвратная ссылка", "фото по ссылке"]):
            return_links.append(link)
    link_counts = bool(return_links and settings.return_link_counts_as_evidence)
    return {
        "attachments_count": len(attachments),
        "photos_count": len(photos),
        "documents_count": len(docs),
        "service_documents_count": len(service_docs),
        "links_count": len(links),
        "return_links_count": len(return_links),
        "has_photo": bool(photos or (text_mentions_photo and link_counts)),
        "has_document": bool(docs or service_docs or link_counts),
        "has_service_document": bool(service_docs or text_has_service_doc or link_counts),
        "has_return_link": bool(return_links),
        "photos": photos[:10],
        "documents": docs[:10],
        "service_documents": service_docs[:10],
        "return_links": return_links[:10],
        "all_links": links[:10],
        "text_mentions_photo": text_mentions_photo,
        "text_mentions_service_document": text_has_service_doc,
    }


def required_evidence_for(claim_kind: str | None) -> dict[str, Any]:
    kind = claim_kind or "unknown"
    if kind == "defect":
        return {
            "photo": bool(settings.require_photo_proof),
            "service_document": bool(settings.require_defect_documents),
            "label": "Брак: нужны фото + акт/заказ-наряд/заключение сервиса",
        }
    if kind in {"nonconforming", "wrong_item", "shortage", "incomplete_set"}:
        return {
            "photo": bool(settings.require_photo_proof),
            "service_document": False,
            "label": "Нужны фото/ссылка с доказательствами",
        }
    if kind in {"quality_refusal", "number_replacement", "overdelivery", "correction_request", "marking_request"}:
        return {"photo": False, "service_document": False, "label": "Доказательства желательны, но не блокируют экспорт"}
    return {"photo": False, "service_document": False, "label": "Нет отдельного чек-листа доказательств"}


def quality_check(
    event_type: str,
    claim_kind: str | None,
    fields: dict[str, Any],
    strong_key: str | None,
    direction: str,
    buyer_code: str | None,
    evidence: dict[str, Any] | None = None,
    pre_delivery: bool = False,
    shortage_link_only: bool = False,
) -> tuple[list[str], list[dict[str, Any]]]:
    missing: list[str] = []
    issues: list[dict[str, Any]] = []
    evidence = evidence or {}

    def issue(level: str, code: str, text: str) -> None:
        issues.append({"level": level, "code": code, "message": text})

    for key, value in fields.items():
        if _looks_like_bad_value(value, key):
            issue("error", f"bad_{key}", f"Поле {key} похоже на мусор: {value}")

    if direction == "inbound_customer" and not buyer_code:
        missing.append("buyer")
        issue("warning", "unknown_buyer", "Не определён покупатель")

    if event_type in {"new_return", "pre_delivery_refusal"}:
        if not claim_kind:
            # Под-тип возврата не определён — НЕ блокируем. Минимум для 1С = номер+артикул
            # (см. спецификацию). Причину уточнит AI/оператор. Мягкое предупреждение.
            issue("warning", "unconfirmed_claim_kind", "Под-тип возврата не определён (уточнит AI/оператор)")
        if not strong_key and not shortage_link_only:
            missing.append("strong_key")
            issue("error", "missing_strong_key", "Нет надёжного ключа: номер претензии/заявки/возврата или документ+артикул")

        # Минимум для 1С: номер обращения/документа + артикул + дата документа + причина.
        # Причина хранится в claim_kind; подробный клиентский текст остается bonus-полем comment.
        has_doc_num = bool(
            fields.get("document_number") or
            fields.get("claim_number") or
            fields.get("return_number") or
            fields.get("client_request_number")
        )
        has_part = bool(fields.get("part_number"))
        has_date = bool(fields.get("document_date"))

        # ── Особый случай: отказ ДО отгрузки (запрос на снятие). ──
        # Товар не отгружали → документа/даты документа физически нет.
        # Достаточно номера обращения/заявки клиента + артикул.
        # Признак: pre_delivery=True (определяется по тексту в classify_email).
        if shortage_link_only:
            issue(
                "warning",
                "shortage_link_requires_stage2",
                "Недопоставка принята по доверенной ссылке; позицию нужно получить на следующем этапе",
            )
        elif pre_delivery:
            has_request_num = bool(
                fields.get("client_request_number") or
                fields.get("claim_number") or
                fields.get("return_number")
            )
            if not has_request_num:
                missing.append("client_request_number")
                issue("error", "missing_request_number",
                      "Снятие до отгрузки: нужен номер заявки/обращения клиента")
            if not has_part:
                missing.append("part_number")
                issue("error", "missing_part_number", "Нет артикула — обязательно для 1С")
            # document_number и document_date НЕ требуем — отгрузки не было
        else:
            if not has_doc_num:
                missing.append("document_number")
                issue("error", "missing_document_number",
                      "Нет номера документа (УПД/накладная/претензия) — обязательно для 1С")
            if not has_part:
                missing.append("part_number")
                issue("error", "missing_part_number",
                      "Нет артикула — обязательно для 1С")
            # Дата документа ЖЕЛАТЕЛЬНА, но не блокирует: есть клиенты, присылающие
            # только номер+артикул. Не валим такие письма в AI из-за отсутствия даты.
            if not has_date:
                issue("warning", "missing_document_date",
                      "Нет даты документа (желательно)")

        if fields.get("quantity") and _looks_like_bad_value(fields.get("quantity"), "quantity"):
            missing.append("valid_quantity")
        requirements = required_evidence_for(claim_kind)
        if settings.strict_evidence_validation:
            if requirements.get("photo") and not evidence.get("has_photo"):
                missing.append("photo_evidence")
                issue("error", "missing_photo_evidence", f"Для типа '{claim_kind}' нужны фото или возвратная ссылка с доказательствами")
            if requirements.get("service_document") and not evidence.get("has_service_document"):
                missing.append("service_document")
                issue("error", "missing_service_document", "Для брака нужен акт/заказ-наряд/заключение сервиса или ссылка на карточку возврата")
        elif requirements.get("photo") and not evidence.get("has_photo"):
            issue("warning", "missing_photo_evidence", f"Желательны фото для типа '{claim_kind}'")
    elif event_type in {"followup_reminder", "followup_dialog", "supplier_decision"}:
        if not (strong_key or fields.get("claim_number") or fields.get("client_request_number") or fields.get("return_number")):
            issue("warning", "weak_followup_link", "Это похоже на продолжение/напоминание, но нет надёжного бизнес-ключа для связи")
    elif event_type == "unknown":
        missing.append("event_type")
        issue("warning", "unknown_event", "Не понятно, что это за письмо")
    return missing, issues


def control_summary(
    *,
    event_type: str,
    state: str,
    claim_kind: str | None,
    priority: str,
    deadline_at: str | None,
    missing: list[str],
    quality: list[dict[str, Any]],
    ready_for_export: bool,
    strong_key: str | None,
) -> dict[str, Any]:
    """Business-control status for the operator and 1C.

    This is intentionally separate from parser state. Parser state says what the
    letter is; control status says what the business must do now.
    """
    missing_set = set(missing or [])
    issue_codes = {str(q.get("code")) for q in (quality or []) if isinstance(q, dict)}
    now = datetime.now(timezone.utc)
    overdue = False
    hours_left: float | None = None
    overdue_hours: float | None = None
    if deadline_at:
        try:
            deadline = datetime.fromisoformat(str(deadline_at).replace("Z", "+00:00"))
            delta = (deadline - now).total_seconds() / 3600
            hours_left = round(delta, 2)
            if delta < 0:
                overdue = True
                overdue_hours = round(abs(delta), 2)
        except Exception:
            pass

    if ready_for_export and state == "ready_to_1c":
        status = "ready_to_1c"
        action = "send_to_1c"
        owner = "system"
    elif event_type == "followup_reminder":
        status = "customer_reminder"
        action = "raise_priority_existing_case" if strong_key else "link_reminder_to_case"
        owner = "operator_or_1c"
    elif event_type == "supplier_decision":
        status = "supplier_decision_received"
        action = "append_decision_and_update_case"
        owner = "operator_or_1c"
    elif event_type in {"followup_dialog"}:
        status = "dialog_update"
        action = "append_to_case_history" if strong_key else "link_dialog_to_case"
        owner = "operator_or_1c"
    elif {"photo_evidence", "service_document"} & missing_set or {"missing_photo_evidence", "missing_service_document"} & issue_codes:
        status = "waiting_customer_documents"
        action = "request_missing_evidence"
        owner = "customer"
    elif "strong_key" in missing_set:
        status = "needs_business_key"
        action = "extract_or_request_document_number"
        owner = "system_then_operator"
    elif "buyer" in missing_set:
        status = "needs_buyer_identification"
        action = "identify_customer_or_ask_ai"
        owner = "system_then_operator"
    elif state == "needs_link":
        status = "needs_link"
        action = "link_to_existing_case"
        owner = "operator_or_1c"
    elif state == "needs_review":
        status = "needs_review"
        action = "review_or_run_ai"
        owner = "system_then_operator"
    elif state == "linked_event":
        status = "under_control"
        action = "keep_in_case_history"
        owner = "system"
    elif state == "context_sent":
        status = "waiting_counterparty_or_1c"
        action = "wait_for_response"
        owner = "counterparty"
    elif state == "ignored_internal":
        status = "ignored_internal"
        action = "no_business_action"
        owner = "system"
    else:
        status = "needs_review"
        action = "review_unknown_event"
        owner = "system_then_operator"

    if overdue and status not in {"ready_to_1c", "ignored_internal"}:
        status = "overdue_" + status
        action = "escalate_" + action

    return {
        "status": status,
        "action": action,
        "owner": owner,
        "claim_kind_label": CLAIM_KIND_LABELS.get(claim_kind or "", claim_kind or "unknown"),
        "deadline_at": deadline_at,
        "hours_left": hours_left,
        "overdue": overdue,
        "overdue_hours": overdue_hours,
        "priority": priority,
        "missing": missing or [],
    }

def _clean_comment_for_export(raw_comment: str | None) -> str | None:
    """Проверяет и чистит comment перед экспортом в 1С."""
    if not raw_comment:
        return None
    v = str(raw_comment).strip(" .,:;№#\n\r\t")
    if not v:
        return None
    # Если больше 240 символов — обрезаем
    if len(v) > 200:
        v = v[:200]
    # Если много цифр подряд без пробелов — это мусор (конкатенация артикулов/цен)
    if re.search(r'\d{8,}', v.replace(' ', '')):
        return None
    # Если больше половины символов — цифры, это мусор
    digits = sum(1 for c in v if c.isdigit())
    letters = sum(1 for c in v if c.isalpha())
    total = digits + letters
    if total > 0 and digits > 0 and digits / total > 0.5:
        return None
    return v


def build_export_json(case_id: int | None, email_data: dict[str, Any], case_data: dict[str, Any]) -> dict[str, Any]:
    fields = case_data.get("fields") or {}
    payload = case_data.get("payload") or {}
    evidence = payload.get("evidence") or {}
    source_method = payload.get("processing_source") or ("ai" if payload.get("ai_overlay") else "pattern")

    def _num(v):
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return v
        try:
            return int(str(v).strip())
        except Exception:
            try:
                return float(str(v).replace(" ", "").replace(",", "."))
            except Exception:
                return None

    # Мультипозиция: если в payload есть таблица позиций (>1) — выгружаем ВСЕ строки,
    # шапка документа общая, у каждой позиции свои артикул/кол-во/цена/причина.
    items = []
    rows = payload.get("table_items") or []
    if isinstance(rows, list) and len(rows) > 1:
        for ti in rows:
            it = {}
            if ti.get("part_number"):
                it["part_number"] = str(ti["part_number"]).strip()
            if ti.get("received_part_number"):
                it["received_part_number"] = str(ti["received_part_number"]).strip()  # пересорт: приехавший факт
            if ti.get("brand"):
                it["brand"] = str(ti["brand"]).strip()
            if ti.get("product_name"):
                it["product_name"] = str(ti["product_name"]).strip()
            q = _num(ti.get("quantity"))
            if q is not None:
                it["quantity"] = q
            p = _num(ti.get("price"))
            if p is not None:
                it["price"] = float(p)
            if ti.get("comment"):
                it["reason"] = str(ti["comment"]).strip()  # причина может быть у каждой позиции
            if it.get("part_number") or it.get("product_name"):
                items.append(it)

    # Одна позиция (или таблица не дала строк) — собираем из плоских fields.
    item = {}
    try:
        if not items and fields.get("part_number"):
            item["part_number"] = str(fields["part_number"]).strip()
        if fields.get("received_part_number"):
            item["received_part_number"] = str(fields["received_part_number"]).strip()  # пересорт: приехавший факт
        if fields.get("brand"):
            item["brand"] = str(fields["brand"]).strip()
        if fields.get("product_name"):
            item["product_name"] = str(fields["product_name"]).strip()
        if fields.get("quantity") is not None:
            try:
                item["quantity"] = int(fields["quantity"])
            except (ValueError, TypeError):
                try:
                    item["quantity"] = float(str(fields["quantity"]).replace(",", "."))
                except Exception:
                    item["quantity"] = None
        price = fields.get("price")
        if price is not None:
            if isinstance(price, (int, float)):
                item["price"] = float(price)
            elif isinstance(price, str):
                try:
                    item["price"] = float(price.replace(",", ".").replace(" ", "").replace("₽", "").replace("руб", ""))
                except Exception:
                    item["price"] = None
            else:
                item["price"] = None
        if not items and (item.get("part_number") or item.get("product_name") or item.get("brand")):
            items.append(item)
    except Exception:
        pass

    # Цена/комментарий в позициях — по настройке (отсекаем при необходимости).
    if not settings.one_c_include_price:
        for it in items:
            it.pop("price", None)

    out = {
        "case_id": case_id,
        "source": "readmail_v2",
        "created_at": utcnow(),

        "buyer": {
            "code": case_data.get("buyer_code"),
            "name": case_data.get("buyer_name"),
        },

        "document": {
            "number": fields.get("document_number"),
            "date": fields.get("document_date"),
            "type": fields.get("document_type", "УПД"),
        },

        # Один номер обращения + его тип (вместо 4 дублирующих полей с null).
        "claim": {
            "number": fields.get("claim_number") or fields.get("client_request_number") or fields.get("return_number"),
            "number_type": ("claim_number" if fields.get("claim_number")
                            else "client_request_number" if fields.get("client_request_number")
                            else "return_number" if fields.get("return_number") else None),
            "kind": case_data.get("claim_kind"),
            "kind_label": CLAIM_KIND_LABELS.get(case_data.get("claim_kind") or "", ""),
            "event_type": case_data.get("event_type"),
            "priority": case_data.get("priority"),
        },

        "items": items,
    }
    # comment в claim — по настройке.
    if settings.one_c_include_comment:
        out["claim"]["comment"] = _clean_comment_for_export(fields.get("comment"))
    # Служебные секции meta/processing — по настройке (по умолчанию ВЫКЛ, это «мусор» для 1С).
    if settings.one_c_include_meta:
        out["meta"] = {
            "confidence": case_data.get("confidence"),
            "ai_used": bool(payload.get("ai_overlay")),
            "classifier_version": "2.0",
            "source_email_id": email_data.get("email_id") or email_data.get("id"),
            "deadline_at": case_data.get("deadline_at"),
            "strong_key": case_data.get("strong_key"),
        }
        if settings.one_c_include_evidence_flags:
            out["meta"]["has_attachments"] = bool(email_data.get("attachments"))
    if settings.one_c_include_processing:
        out["processing"] = {
            "source": source_method,
            "mode": payload.get("processing_mode") or "auto",
            "classifier": payload.get("classifier"),
            "manual_gate": bool(payload.get("manual_gate")),
            "ai_overlay": bool(payload.get("ai_overlay")),
        }
    # Флаги документов брака → в 1С (для брака/некондиции): какие из 3 документов есть.
    if case_data.get("claim_kind") in ("defect", "nonconforming"):
        ddf = payload.get("defect_doc_flag") or payload.get("defect_documents") or {}
        present = ddf.get("present") or {}
        if ddf:
            out["defect_documents"] = {
                "state": ddf.get("state"),
                "complete": bool(ddf.get("complete")) or ddf.get("state") in ("complete", "docs_complete"),
                "install_order": bool(present.get("install_order")),
                "removal_order": bool(present.get("removal_order")),
                "service_act": bool(present.get("service_act")),
                "n_present": ddf.get("n_present"),
                "source": ddf.get("mode"),
            }
    return out


def classify_email(
    email_data: dict[str, Any],
    buyer_rules: list[BuyerRule] | None = None,
    case_id: int | None = None,
    learned_identities: list[dict[str, Any]] | None = None,
    existing_cases: list[dict[str, Any]] | None = None,
    force_item: int = 0,
) -> dict[str, Any]:
    buyer_rules = buyer_rules if buyer_rules is not None else load_buyer_rules()
    # Текст для извлечения. Приоритет: уже расклеенный visible_text (с " | ").
    # Если его нет/он склеенный, но в HTML есть таблица — расклеиваем из body_html.
    visible = select_visible_text(
        email_data.get("body_text"), email_data.get("body_html"), email_data.get("visible_text")
    )
    # Защита от зависания regex (катастрофический backtracking) на огромных телах:
    # ограничиваем текст для классификации. Товарная строка/реквизиты всегда в начале.
    if len(visible) > 40000:
        visible = visible[:40000]
        email_data = {**email_data, "visible_text": visible}
    full_text = "\n".join([str(email_data.get("subject") or ""), visible])
    direction, direction_reasons = detect_direction(email_data)
    buyer_code, buyer_name, buyer_score, buyer_reasons = detect_buyer(email_data, buyer_rules, learned_identities)
    buyer_rule = next((r for r in buyer_rules if r.code == buyer_code), None)
    fields = extract_fields(full_text, buyer_rule)
    # Добивка из ДОКУМЕНТА-вложения (Excel/PDF: УПД/счёт-фактура/акт ТОРГ-2). Если в теле письма нет
    # document_number/document_date — берём их из СОДЕРЖИМОГО документа («Счёт-фактура № N от DATE»).
    # По шаблону/маркерам, не по значению. Заполняем ТОЛЬКО пустые поля, не перетираем извлечённое из тела.
    if not (fields.get("document_number") and fields.get("document_date")):
        for att in (email_data.get("attachments") or []):
            doc_text = str(att.get("extracted_text") or "")
            low = str(att.get("filename") or "").lower()
            if not doc_text or not (low.endswith((".xls", ".xlsx", ".pdf"))):
                continue
            doc_fields = extract_fields(doc_text[:8000], buyer_rule)
            for k in ("document_number", "document_date"):
                if not fields.get(k) and doc_fields.get(k):
                    fields[k] = doc_fields[k]
            if fields.get("document_number") and fields.get("document_date"):
                break
    # HTML-таблица (семейство A): структурное извлечение колонок — авторитетнее regex по
    # плоскому тексту (колонка↔значение выровнены структурой). Заполняет/исправляет поля.
    _bh_for_table = email_data.get("body_html") or ""
    # Скорость: тяжёлый разбор таблицы только когда в письме реально есть <table>.
    table_items = extract_product_table(_bh_for_table) if "<table" in _bh_for_table.lower() else []
    # v2.1 AI-only: паттерн-мультипозиция (| -таблицы, item_templates) убрана — позиции раскладывает ИИ.
    # Дедуп одинаковых позиций: акт ТОРГ-2 в Excel нередко двоит строку товара (форма+итог),
    # из-за чего одна позиция превращалась в N идентичных. Схлопываем по (артикул, кол-во, цена).
    if len(table_items) > 1:
        _seen: set = set()
        _uniq: list[dict[str, Any]] = []
        for _it in table_items:
            _key = (
                str(_it.get("part_number") or "").strip().lower(),
                str(_it.get("quantity") or "").strip(),
                str(_it.get("price") or "").strip(),
                str(_it.get("product_name") or "").strip().lower(),
            )
            if _key in _seen:
                continue
            _seen.add(_key)
            _uniq.append(_it)
        table_items = _uniq
    multi_item_count = len(table_items)
    if table_items:
        _pick = table_items[force_item] if 0 <= force_item < len(table_items) else table_items[0]
        for _k, _v in _pick.items():
            if _v and not _looks_like_bad_value(_v, _k):
                fields[_k] = _v
    # Grounding даты: если document_date не встречается в тексте письма — это чужое число,
    # не доверяем (аудит: 527 «дата не из текста»). Дата необязательна — просто убираем.
    if fields.get("document_date") and not _date_grounded_in_text(fields.get("document_date"), full_text):
        fields.pop("document_date", None)
    # Автокоррекция по явной метке (Кол-во:/Артикул:) — метка авторитетнее паттерна. Автомат, не оператор.
    apply_explicit_labels(fields, full_text)
    evidence = evidence_summary(full_text, email_data.get("attachments") or [])
    claim_kind, kind_score, kind_reasons = detect_kind(full_text, buyer_rule)

    # Приоритет ДОКУМЕНТА над телом письма: если есть акт ТОРГ-2 с вердиктом «мнение комиссии
    # о причинах» — берём причину ОТТУДА (тело письма-шаблон «несоответствие» вторично).
    # Текст акта дописан в full_text при импорте (.xls/.pdf), либо в теле письма.
    _act_kind = claim_kind_from_act(full_text)
    if _act_kind and _act_kind != claim_kind:
        claim_kind = _act_kind
        kind_score = max(kind_score, 0.9)
        kind_reasons.append("act_verdict_override")

    # Определяем, является ли письмо первым контактом
    is_first_contact_result = is_first_contact(email_data)
    # Проверяем по базе, если есть existing_cases
    if is_first_contact_result[0] and existing_cases:
        is_first_contact_result = detect_first_contact(email_data, existing_cases)

    event_type, is_followup, event_reasons = detect_event_type(
        email_data, full_text, claim_kind, fields, direction,
        is_first_contact_result=is_first_contact_result,
    )

    # Дефолт причины из профиля клиента: это возврат, но под-тип из текста не определён →
    # берём claim_kind по умолчанию (ixora «Информируем» без причины → отказ клиента).
    if not claim_kind and event_type == "new_return" and buyer_rule and buyer_rule.defaults.get("claim_kind"):
        claim_kind = str(buyer_rule.defaults["claim_kind"])
        kind_reasons.append("buyer_default_claim_kind")

    # Доверие к event_type выше для писем с историей
    if not is_first_contact_result[0]:
        event_reasons.append("reply_context_validated")
    strong_key = make_strong_key(buyer_code, fields)
    thread_key, weak_key = make_thread_key(email_data, buyer_code, strong_key)
    received = parse_received_at(email_data)
    deadline_at = deadline_for(claim_kind, event_type, received)
    priority = priority_for(deadline_at, full_text, is_followup)
    pre_delivery = _detect_pre_delivery_refusal(full_text, event_type, fields)
    if pre_delivery:
        event_type = "pre_delivery_refusal"
        event_reasons.append("pre_delivery_refusal")
    shortage_link = detect_shortage_link_only(full_text, claim_kind, evidence)
    if shortage_link:
        event_type = "shortage_link_event"
        event_reasons.append("shortage_link_event")
    missing, quality = quality_check(
        event_type,
        claim_kind,
        fields,
        strong_key,
        direction,
        buyer_code,
        evidence,
        pre_delivery=pre_delivery,
        shortage_link_only=bool(shortage_link),
    )

    # Санитарная проверка полей
    fields, field_sanity_errors = sanity_check_fields(fields)
    for err in field_sanity_errors:
        quality.append({"level": "error", "code": "sanity_fail", "message": err})

    # КОНТРОЛЬ ДАТЫ ДОКУМЕНТА: если в письме несколько разных дат, а извлечённая дата НЕ стоит
    # рядом с номером документа — это, вероятно, чужая дата (оферты/подписи). Не отправляем
    # такой кейс в 1С автоматом, а помечаем на Сверку. Поле НЕ меняем — решает оператор.
    if (
        event_type == "new_return"  # только экспортные кейсы; followup-поля унаследованы и не якорятся
        and fields.get("document_date")
        and fields.get("document_number")
        and _distinct_date_count(full_text) >= 2
        and not _date_anchored_to_document(fields["document_date"], fields["document_number"], full_text)
    ):
        quality.append({
            "level": "error",
            "code": "date_not_anchored",
            "message": f"Дата {fields['document_date']} не привязана к № документа {fields['document_number']} (в письме несколько дат) — проверить вручную",
        })

    # bad-context: номер документа / артикул извлечены из «запрещённого» окружения
    # (заявка/тикет/телефон/маркировка) → это чужое значение. В 1С не пускаем, в Сверку.
    if event_type == "new_return":
        _dn = fields.get("document_number")
        if _dn and _value_only_in_bad_context(str(_dn), full_text, _DOC_NUMBER_BAD_RE):
            quality.append({"level": "error", "code": "document_number_bad_context",
                            "message": f"№ документа {_dn} похож на номер заявки/тикета/телефона — проверить"})
        _pn = fields.get("part_number")
        if _pn and _value_only_in_bad_context(str(_pn), full_text, _PART_BAD_RE):
            quality.append({"level": "error", "code": "part_number_bad_context",
                            "message": f"Артикул {_pn} похож на маркировку/Честный знак — проверить"})

    exportable_event = event_type in {"new_return", "pre_delivery_refusal"}
    hard_errors = [q for q in quality if q.get("level") == "error"]

    # Минимально необходимые поля для экспорта в 1С:
    # номер обращения/документа + артикул + дата документа + причина (claim_kind).
    has_business_number = bool(
        fields.get("document_number")
        or fields.get("claim_number")
        or fields.get("return_number")
        or fields.get("client_request_number")
    )
    # Минимум для 1С (УТОЧНЕНО владельцем 2026-06-04):
    #  обычный new_return — ОСНОВА из 5 полей: дата документа + номер документа + артикул +
    #  причина (claim_kind). Количество — «если есть» (НЕ блок, владелец 2026-06-05).
    #  pre_delivery (снятие до поставки) — № заявки + артикул (док/дата/кол-во не нужны).
    #  Бренд/наименование/цена — желательны, НЕ блок.
    if shortage_link:
        # Link-only is accepted into its own queue, but never auto-exported before Stage 2
        # resolves the linked positions.
        has_min_fields = bool(buyer_code and claim_kind == "shortage")
    elif pre_delivery:
        has_min_fields = bool(
            (fields.get("return_number") or fields.get("client_request_number") or fields.get("claim_number"))
            and fields.get("part_number")
        )
    else:
        has_min_fields = bool(
            fields.get("document_number")
            and fields.get("document_date")
            and fields.get("part_number")
            and claim_kind
        )

    # Вычислить confidence ПЕРЕД needs_ai
    confidence = min(1.0, 0.1 + buyer_score * 0.35 + kind_score * 0.35 + (0.2 if strong_key else 0.0))
    if event_type in {"followup_reminder", "followup_dialog", "supplier_decision"} and (email_data.get("references") or email_data.get("in_reply_to")):
        confidence = max(confidence, 0.75)
    if event_type in {"internal_forward", "internal_thread", "outbound_company"}:
        confidence = max(confidence, 0.85)

    # Под-тип возврата (claim_kind) не определён сам по себе НЕ повод гнать в AI:
    # если есть покупатель + номер + артикул (strong_key), паттерны справились.
    if exportable_event and has_min_fields and buyer_code and strong_key and not hard_errors:
        confidence = max(confidence, 0.82)

    # AI обязателен если: низкая уверенность ИЛИ нет минимальных полей ИЛИ нет покупателя
    needs_ai = (
        confidence < 0.80
        or not has_min_fields
        or not buyer_code
    ) if exportable_event else False

    # Дополнительный буст для готовых к экспорту
    if exportable_event and not needs_ai and has_min_fields and not hard_errors and not missing:
        confidence = max(confidence, 0.82)

    # Пересчёт ready_for_export после финального needs_ai
    ready_for_export = (
        exportable_event
        and not missing
        and not hard_errors
        and direction == "inbound_customer"
        and has_min_fields
        and not needs_ai
    )
    if shortage_link:
        ready_for_export = False
        needs_ai = False

    if event_type in {"internal_forward", "internal_thread", "possible_internal_forward"}:
        state = "ignored_internal"
    elif event_type in {"info_only", "supplier_report", "spam_promo"}:
        state = "ignored_info_only" if event_type == "supplier_report" else "ignored_" + event_type
    elif event_type == "outbound_company":
        state = "context_sent"
    elif event_type in {"followup_reminder", "followup_dialog", "ready_to_ship"}:
        state = "needs_link" if not strong_key else "linked_event"
    elif event_type == "supplier_decision":
        state = "linked_event" if strong_key or email_data.get("references") else "needs_link"
    elif event_type in {"new_return", "pre_delivery_refusal"} and ready_for_export:
        state = "ready_to_1c"
    elif event_type in {"new_return", "pre_delivery_refusal"}:
        state = "needs_review"
    elif event_type in {"correction_request", "marking_request", "number_replacement"}:
        state = "linked_event" if strong_key or email_data.get("references") else "needs_link"
    elif event_type == "shortage_link_event":
        state = "needs_link"
    elif event_type == "problem_notice":
        state = "problem_notice"
    else:
        state = "needs_review"

    control = control_summary(event_type=event_type, state=state, claim_kind=claim_kind, priority=priority, deadline_at=deadline_at, missing=missing, quality=quality, ready_for_export=ready_for_export, strong_key=strong_key)
    payload = {
        "reasons": {
            "direction": direction_reasons,
            "buyer": buyer_reasons,
            "kind": kind_reasons,
            "event": event_reasons,
        },
        "direction": direction,
        "classifier": "deterministic_v2.0",
        "processing_source": "pattern",
        "processing_mode": "auto",
        "quote_markers": email_data.get("quote_markers") or 0,
        "evidence": evidence,
        "evidence_requirements": required_evidence_for(claim_kind),
        # Пакет документов брака (только для defect): какие из 3 приложены по именам файлов.
        "defect_documents": classify_defect_documents(email_data.get("attachments")) if claim_kind in ("defect", "nonconforming") else None,
        # Механический флаг полноты документов брака (3 состояния + хук на ИИ-vision).
        "defect_doc_flag": defect_documents_flag(email_data.get("attachments")) if claim_kind in ("defect", "nonconforming") else None,
        "table_items": table_items if multi_item_count > 1 else None,
        "multi_item_count": multi_item_count,
        "control": control,
        "learning_policy": "human_or_validated_ai_before_trust",
        "visible_text_only": True,
        "ai_policy": "ai_off_by_default_validate_before_export",
        "is_first_contact": is_first_contact_result[0],
        "is_first_contact_reasons": is_first_contact_result[1],
        "pre_delivery_refusal": bool(pre_delivery),
        "classification_subcategory": (
            (shortage_link or {}).get("subcategory")
            or detect_marking_subcategory(full_text)
        ),
        "shortage_link": shortage_link,
    }
    # v2.1 «обезврежен скелет»: в AI-only детерминированный сортер НЕ раскидывает по
    # терминальным папкам по ключевым словам (отсюда промахи: претензия→корректировка,
    # ЧЗ→возврат, Re:→new_return). Всё уходит «ОЖИДАЕТ ИИ» (needs_review+needs_ai), а
    # event_type/claim_kind/маршрут ставит ТОЛЬКО ИИ. Ключи связок/direction уже посчитаны
    # выше и сохраняются. event_type скелета остаётся лишь подсказкой для ИИ (skeleton_guess).
    if getattr(settings, "ai_only", False):
        # ТЗ: ДО AI не назначаем бизнес-claim. Догадку скелета храним отдельно (static_hint),
        # финальные event_type/claim_kind = pending/null. Бизнес-статус ставит ТОЛЬКО AI.
        payload["static_hint"] = {
            "draft_event_type": event_type,
            "draft_claim_kind": claim_kind,
        }
        payload["processing_source"] = "static_skeleton"
        payload["ai_checked"] = False
        payload["ai_applied"] = False
        event_type = "unknown"   # отображаемый статус «Проверяется AI», не бизнес-причина
        claim_kind = None
        state = "needs_review"
        needs_ai = True
        ready_for_export = False
    case_data = {
        "buyer_code": buyer_code,
        "buyer_name": buyer_name,
        "event_type": event_type,
        "pre_delivery_refusal": bool(pre_delivery),
        "claim_kind": claim_kind,
        "status": claim_kind,
        "priority": priority,
        "confidence": round(confidence, 3),
        "deadline_at": deadline_at,
        "thread_key": thread_key,
        "strong_key": strong_key,
        "weak_key": weak_key,
        "is_followup": is_followup,
        "ready_for_export": ready_for_export,
        "needs_review": not ready_for_export and state not in {"ignored_internal", "context_sent", "ignored_info_only", "ignored_spam_promo", "problem_notice"},
        "needs_ai": needs_ai,
        "has_min_fields": has_min_fields,
        "state": state,
        "fields": fields,
        "missing": missing,
        "quality": quality,
        "payload": payload,
    }
    case_data["export"] = build_export_json(case_id, email_data, case_data)
    return case_data

VALID_EVENT_TYPES = {
    "new_return",
    "pre_delivery_refusal",
    "followup_reminder",
    "followup_dialog",
    "supplier_decision",
    "correction_request",
    "marking_request",
    "number_replacement",
    "shortage_link_event",
    "ready_to_ship",
    "supplier_report",
    "problem_notice",
    "info_only",
    "unknown",
}
VALID_CLAIM_KINDS = {
    "defect", "nonconforming", "number_replacement", "wrong_item", "shortage", "overdelivery", "incomplete_set",
    "correction_request", "marking_request", "quality_refusal",
}


def apply_ai_overlay(email_data: dict[str, Any], case_data: dict[str, Any], ai_response: dict[str, Any]) -> dict[str, Any]:
    """Merge a validated AI suggestion into a deterministic case and recompute all gates.

    This does not mark a case trusted. It only repairs missing fields and still runs
    quality_check before ready_for_export can become true.
    """
    merged = dict(case_data)
    fields = dict(case_data.get("fields") or {})
    ai_fields = ai_response.get("fields") if isinstance(ai_response.get("fields"), dict) else {}
    # «Мягкие» поля не входят в минимум для 1С и часто ловятся паттернами с мусором
    # (бренд из email vozvrat@..., наименование-простыня). AI с новым промтом по ним
    # надёжен: если AI явно вернул пусто — старое значение паттерна считаем мусором и чистим.
    _SOFT_FIELDS = {"brand", "product_name", "comment"}
    # v2.1 AI-only: ИИ — АВТОРИТЕТ. Скелет — лишь черновик-подсказка. Если ИИ вернул валидное
    # значение поля, оно ПОБЕЖДАЕТ скелет (раньше скелет с кривым «не-битым» значением, напр.
    # №Э00022168 в document_number у Trinity, перетирал верное 83904 от ИИ — системный косяк).
    _ai_only = bool(getattr(settings, "ai_only", False))
    for key in ("claim_number", "client_request_number", "return_number", "document_number", "document_date", "part_number", "quantity", "brand", "product_name", "comment"):
        value = ai_fields.get(key)
        if value in (None, "", [], {}):
            # AI ничего не нашёл по полю. Для мягких полей затираем старый мусор паттерна.
            if key in _SOFT_FIELDS and fields.get(key):
                del fields[key]
            continue
        value = str(value).strip(" .,:;№#")
        if key == "document_date":
            value = _normalize_date(value) or value
        if not _looks_like_bad_value(value, key):
            old = fields.get(key)
            # AI-only: ИИ перетирает скелет. Иначе — не трогаем хороший паттерн (старое поведение).
            if _ai_only or key in _SOFT_FIELDS or not old or _looks_like_bad_value(old, key):
                fields[key] = value

    buyer_code = merged.get("buyer_code")
    buyer_name = merged.get("buyer_name")
    ai_buyer_code = ai_response.get("buyer_code")
    if not buyer_code and ai_buyer_code:
        safe_code = re.sub(r"[^a-z0-9_\-]+", "_", str(ai_buyer_code).strip().lower())[:48].strip("_")
        if safe_code:
            buyer_code = safe_code
            buyer_name = str(ai_response.get("buyer_name") or ai_buyer_code)[:120]

    event_type = str(ai_response.get("event_type") or merged.get("event_type") or "unknown").strip()
    if event_type not in VALID_EVENT_TYPES:
        event_type = merged.get("event_type") or "unknown"
    claim_kind = ai_response.get("claim_kind") or merged.get("claim_kind")
    if claim_kind not in VALID_CLAIM_KINDS:
        claim_kind = merged.get("claim_kind") if merged.get("claim_kind") in VALID_CLAIM_KINDS else None

    direction = (merged.get("payload") or {}).get("direction") or detect_direction(email_data)[0]
    evidence = evidence_summary("\n".join([str(email_data.get("subject") or ""), str(email_data.get("visible_text") or visible_body(email_data.get("body_text"), email_data.get("body_html")))]), email_data.get("attachments") or [])
    strong_key = make_strong_key(buyer_code, fields)
    thread_key, weak_key = make_thread_key(email_data, buyer_code, strong_key)
    received = parse_received_at(email_data)
    deadline_at = deadline_for(claim_kind, event_type, received)
    is_followup = event_type in {"followup_reminder", "followup_dialog", "supplier_decision"}
    _ai_full_text = "\n".join([str(email_data.get("subject") or ""), str(email_data.get("visible_text") or email_data.get("snippet") or "")])
    # Grounding даты и после AI: дата обязана быть в тексте письма.
    if fields.get("document_date") and not _date_grounded_in_text(fields.get("document_date"), _ai_full_text):
        fields.pop("document_date", None)
    apply_explicit_labels(fields, _ai_full_text)
    priority = priority_for(deadline_at, _ai_full_text, is_followup)
    # Доверяем вердикту ИИ: если он явно классифицировал отказ ДО поставки — это pre_delivery
    # (формат АвтоЕвро «Запрос на снятие» эвристика не ловит, но 1С-минимум у него свой:
    # № заявки/подтверждения + артикул, без документа реализации).
    pre_delivery = _detect_pre_delivery_refusal(_ai_full_text, event_type, fields) or \
        str(ai_response.get("event_type") or "") == "pre_delivery_refusal"
    if pre_delivery:
        event_type = "pre_delivery_refusal"
    shortage_link = detect_shortage_link_only(_ai_full_text, claim_kind, evidence)
    if shortage_link:
        event_type = "shortage_link_event"
    missing, quality = quality_check(
        event_type,
        claim_kind,
        fields,
        strong_key,
        direction,
        buyer_code,
        evidence,
        pre_delivery=pre_delivery,
        shortage_link_only=bool(shortage_link),
    )
    hard_errors = [q for q in quality if q.get("level") == "error"]
    has_business_number = bool(
        fields.get("document_number")
        or fields.get("claim_number")
        or fields.get("return_number")
        or fields.get("client_request_number")
    )
    # Минимум для 1С (УТОЧНЕНО владельцем 2026-06-04):
    #  обычный new_return — ОСНОВА из 5 полей: дата документа + номер документа + артикул +
    #  причина (claim_kind). Количество — «если есть» (НЕ блок, владелец 2026-06-05).
    #  pre_delivery (снятие до поставки) — № заявки + артикул (док/дата/кол-во не нужны).
    #  Бренд/наименование/цена — желательны, НЕ блок.
    if shortage_link:
        has_min_fields = bool(buyer_code and claim_kind == "shortage")
    elif pre_delivery:
        has_min_fields = bool(
            (fields.get("return_number") or fields.get("client_request_number") or fields.get("claim_number"))
            and fields.get("part_number")
        )
    else:
        has_min_fields = bool(
            fields.get("document_number")
            and fields.get("document_date")
            and fields.get("part_number")
            and claim_kind
        )
    ready_for_export = (
        event_type in {"new_return", "pre_delivery_refusal"}
        and has_min_fields
        and not missing
        and not hard_errors
        and direction == "inbound_customer"
    )
    if shortage_link:
        ready_for_export = False

    if event_type in {"followup_reminder", "followup_dialog"}:
        state = "needs_link" if not strong_key else "linked_event"
    elif event_type == "supplier_decision":
        state = "linked_event" if strong_key or email_data.get("references") else "needs_link"
    elif event_type in {"new_return", "pre_delivery_refusal"} and ready_for_export:
        state = "ready_to_1c"
    elif event_type in {"new_return", "pre_delivery_refusal"}:
        state = "needs_review"
    elif event_type in {"correction_request", "marking_request", "number_replacement"}:
        state = "linked_event" if strong_key or email_data.get("references") else "needs_link"
    elif event_type == "ready_to_ship":
        # «Готово к выдаче/забрать возврат» — в связки, не в ручной (ветки не было в AI-пути).
        state = "linked_event" if strong_key or email_data.get("references") else "needs_link"
    elif event_type == "shortage_link_event":
        state = "needs_link"
    elif event_type == "problem_notice":
        state = "problem_notice"
    elif event_type in {"info_only", "supplier_report"}:
        state = "ignored_info_only"
    else:
        state = "needs_review"

    ai_conf = ai_response.get("confidence")
    try:
        ai_conf_f = float(ai_conf)
    except Exception:
        ai_conf_f = 0.0
    confidence = max(float(merged.get("confidence") or 0), min(0.90, ai_conf_f))
    control = control_summary(event_type=event_type, state=state, claim_kind=claim_kind, priority=priority, deadline_at=deadline_at, missing=missing, quality=quality, ready_for_export=ready_for_export, strong_key=strong_key)
    payload = dict(merged.get("payload") or {})
    payload["ai_overlay"] = {
        "applied": True,
        "confidence": round(ai_conf_f, 3),
        "evidence": ai_response.get("evidence") or {},
        "requires_action": ai_response.get("requires_action"),
        "next_action": ai_response.get("next_action"),
        "cannot_export_reason": ai_response.get("cannot_export_reason"),
        "defect_documents_status": ai_response.get("defect_documents_status"),
    }
    payload["classifier"] = "deterministic_v1.9_plus_ai_overlay"
    payload["processing_source"] = "ai"
    payload["ai_checked"] = True
    payload["ai_applied"] = True
    # final_claim_kind = что принято после AI (для карточки/аудита). static_hint остаётся как был.
    payload["final_claim_kind"] = claim_kind
    payload["processing_mode"] = payload.get("processing_mode") or "auto"
    payload["evidence"] = evidence
    payload["evidence_requirements"] = required_evidence_for(claim_kind)
    payload["control"] = control
    payload["pre_delivery_refusal"] = bool(pre_delivery)
    payload["classification_subcategory"] = (
        (shortage_link or {}).get("subcategory")
        or detect_marking_subcategory(_ai_full_text)
    )
    payload["shortage_link"] = shortage_link

    # Мультипозиция от ИИ: items[] → table_items. build_export_json соберёт ВСЕ строки
    # (шапка документа общая), иначе вторая/третья деталь терялась. >1 позиции = мультикейс.
    ai_items = ai_response.get("items")
    if isinstance(ai_items, list) and ai_items:
        norm_items: list[dict[str, Any]] = []
        for it in ai_items:
            if not isinstance(it, dict):
                continue
            row: dict[str, Any] = {}
            for k in ("part_number", "brand", "product_name", "quantity", "price", "comment"):
                v = it.get(k)
                if v not in (None, ""):
                    row[k] = v
            if row.get("part_number") or row.get("product_name"):
                norm_items.append(row)
        # дедуп по артикулу+наименованию (ИИ иногда повторяет первую позицию в items)
        seen: set[tuple] = set()
        uniq: list[dict[str, Any]] = []
        for row in norm_items:
            key = (str(row.get("part_number") or "").lower(), str(row.get("product_name") or "").lower())
            if key in seen:
                continue
            seen.add(key)
            uniq.append(row)
        if len(uniq) > 1:
            payload["table_items"] = uniq
            payload["multi_item_count"] = len(uniq)

    merged.update(
        {
            "buyer_code": buyer_code,
            "buyer_name": buyer_name,
            "event_type": event_type,
            "pre_delivery_refusal": bool(pre_delivery),
            "claim_kind": claim_kind,
            "status": claim_kind,
            "priority": priority,
            "confidence": round(confidence, 3),
            "deadline_at": deadline_at,
            "thread_key": thread_key,
            "strong_key": strong_key,
            "weak_key": weak_key,
            "is_followup": is_followup,
            "ready_for_export": ready_for_export,
            "needs_review": not ready_for_export and state != "problem_notice",
            "state": state,
            "fields": fields,
            "missing": missing,
            "quality": quality,
            "payload": payload,
        }
    )
    merged["export"] = build_export_json(None, email_data, merged)
    return merged


def force_operator_review(case_data: dict[str, Any]) -> dict[str, Any]:
    """Mark a ready case as awaiting the operator's manual «Старт», WITHOUT
    dropping its export readiness.

    Раньше эта функция СРЕЗАЛА ready_for_export/state у готовых писем — из-за
    чего ни ручной «Старт», ни авто не находили, что слать в 1С (1С был всегда
    пустой). Теперь письмо ОСТАЁТСЯ ready_to_1c/ready_for_export, но помечается
    manual_gate: оно ждёт ручного «Старт» оператора и САМО в 1С не уходит
    (can_auto_send это учитывает). Автопилот этот гейт не вешает — его письма
    летят в 1С автоматически, минуя сверку.
    """
    data = dict(case_data or {})
    if data.get("state") != "ready_to_1c" and not data.get("ready_for_export"):
        return data

    payload = dict(data.get("payload") or {})
    payload["manual_gate"] = True
    payload["operator_review_required"] = True
    payload["processing_mode"] = "manual"
    data["payload"] = payload
    # В export (1С JSON) служебные флаги НЕ кладём — это внутреннее (мусор для 1С).
    return data
