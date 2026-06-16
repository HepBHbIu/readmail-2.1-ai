from __future__ import annotations

import html
import re
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import yaml

from .config import settings


QUANTITY_LABEL = (
    # «количеств\w*» — склонения: количество / в количестве 1 / количества
    # (avtoformula: «...в количестве 1 по документу» без «шт»).
    r"(?:количеств\w*(?:\s+(?:претензи[ия]|возврат[а]?))?|"
    r"кол-во|кол\.\s*|к-во|qty|quantity)"
)
PIECE_UNIT = r"(?:шт\.?|штук(?:а|и)?|ед\.)"
PART_LABEL = r"(?:арт(?:икул)?\.?|код(?:\s+товара)?|номенклатура|sku|part\s*number)"
TABLE_HEADERS = (
    "количество", "кол-во", "количество претензия",
    "количество претензии", "количество возврата", "qty", "quantity",
)
PRICE_WORDS = ("цена", "стоимость", "сумма", "итого", "руб", "ндс")
DOCUMENT_WORDS = (
    "упд", "документ", "накладн", "счет-фактур", "счёт-фактур",
    "претензи", "заявк", "обращени", "заказ",
)
DATE_RE = re.compile(
    r"(?<!\d)(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}-\d{1,2}-\d{1,2})(?!\d)"
)


def _compact(value: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def _norm(value: Any) -> str:
    return _compact(value).lower().replace("ё", "е")


def _number(value: Any) -> float | None:
    try:
        return float(str(value).strip().replace(",", "."))
    except Exception:
        return None


def _number_pattern(value: Any) -> str:
    number = _number(value)
    if number is None:
        return re.escape(_compact(value))
    if number.is_integer():
        return rf"{int(number)}(?:[.,]0+)?"
    return re.escape(format(number, "g")).replace(r"\.", r"[.,]")


def _value_pattern(value: Any) -> re.Pattern[str]:
    return re.compile(rf"(?<![\d.,]){_number_pattern(value)}(?![\d.,])", re.I)


def _snippet(text: str, start: int, end: int, radius: int = 100) -> str:
    return _compact(text[max(0, start - radius):min(len(text), end + radius)])[:380]


def _contracts_path() -> Path:
    configured = Path(settings.buyer_config_dir).parent / "supplier_contracts.yaml"
    if configured.exists():
        return configured
    return Path(__file__).resolve().parent.parent / "config" / "supplier_contracts.yaml"


@lru_cache(maxsize=8)
def _load_contracts_cached(path: str, modified_ns: int) -> dict[str, dict[str, Any]]:
    del modified_ns
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return {
        str(code): dict(config)
        for code, config in data.items()
        if isinstance(config, dict)
    }


def quantity_contract(buyer_code: Any) -> dict[str, Any]:
    path = _contracts_path()
    modified_ns = path.stat().st_mtime_ns if path.exists() else 0
    contracts = _load_contracts_cached(str(path.resolve()), modified_ns)
    default = contracts.get("default") or {}
    supplier = contracts.get(str(buyer_code or "").strip()) or {}
    return {
        **(default.get("quantity_evidence") or {}),
        **(supplier.get("quantity_evidence") or {}),
    }


def _source_texts(raw_email: dict[str, Any], fallback: str) -> list[tuple[str, str]]:
    values = (
        ("subject", raw_email.get("subject")),
        ("visible_text", raw_email.get("visible_text")),
        ("body", raw_email.get("body_text")),
        ("html", raw_email.get("body_html")),
    )
    result = [(source, str(value or "")) for source, value in values if str(value or "").strip()]
    return result or [("body", fallback or "")]


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.lower() == "tr":
            self._row = []
        elif tag.lower() in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self._row is not None and self._cell is not None:
            self._row.append(_compact(" ".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(self._row):
                self.rows.append(self._row)
            self._row = None


def _table_rows(raw_email: dict[str, Any], fallback: str) -> list[list[str]]:
    parser = _TableParser()
    try:
        parser.feed(str(raw_email.get("body_html") or ""))
    except Exception:
        pass
    rows = list(parser.rows)
    for _, text in _source_texts(raw_email, fallback):
        for line in text.splitlines():
            if line.count("|") >= 2:
                row = [_compact(cell) for cell in line.split("|")]
                if any(row):
                    rows.append(row)
    return rows


def _cell_matches_quantity(cell: str, value: Any) -> bool:
    return bool(re.fullmatch(rf"\s*{_number_pattern(value)}\s*(?:{PIECE_UNIT})?\s*", cell, re.I))


def _table_evidence(
    value: Any,
    part_number: Any,
    product_name: Any,
    raw_email: dict[str, Any],
    fallback: str,
) -> dict[str, Any] | None:
    headers: list[str] = []
    part = _norm(part_number)
    product = _norm(product_name)
    for row in _table_rows(raw_email, fallback):
        normalized = [_norm(cell) for cell in row]
        if any(any(header in cell for header in TABLE_HEADERS) for cell in normalized):
            headers = normalized
            continue
        for index, cell in enumerate(row):
            if not _cell_matches_quantity(cell, value):
                continue
            header_match = bool(
                headers
                and index < len(headers)
                and any(header in headers[index] for header in TABLE_HEADERS)
            )
            product_match = bool(
                (part and any(part == item or part in item for item in normalized))
                or (product and any(product in item or item in product for item in normalized if item))
            )
            if header_match or product_match:
                return {
                    "status": "confirmed_by_table_quantity_column",
                    "source": "table",
                    "evidence_snippet": " | ".join(row)[:380],
                    "method": "table_quantity_column",
                    "warnings": [],
                }
    return None


def _dangerous_number_context(snippet: str, value: Any) -> bool:
    normalized = _norm(snippet)
    pattern = _number_pattern(value)
    explicit_quantity = bool(re.search(
        rf"(?i)(?:{QUANTITY_LABEL}\s*[:=—\-| ]*{pattern}|"
        rf"{PIECE_UNIT}\s*[:=—\-| ]*{pattern}|{pattern}\s*{PIECE_UNIT})",
        snippet,
    ))
    if explicit_quantity:
        return False
    if DATE_RE.search(snippet):
        date = DATE_RE.search(snippet)
        if date and re.search(pattern, date.group()):
            return True
    if re.search(r"\+?\d[\d\s().-]{8,}", snippet):
        phone = re.search(r"\+?\d[\d\s().-]{8,}", snippet)
        if phone and re.search(pattern, phone.group()):
            return True
    if any(word in normalized for word in PRICE_WORDS):
        return True
    return False


def _candidate_conflict(
    snippet: str,
    selected: Any,
    part_number: Any,
    product_name: Any,
) -> bool:
    label_candidates = {
        number
        for match in re.finditer(
            rf"(?i){QUANTITY_LABEL}\s*[:=—\-| ]*\s*(\d+(?:[.,]\d+)?)",
            snippet,
        )
        if (number := _number(match.group(1))) is not None
    }
    if label_candidates:
        candidates = label_candidates
    else:
        local = snippet
        for anchor in (_compact(part_number), _compact(product_name)):
            if not anchor:
                continue
            match = re.search(re.escape(anchor), snippet, re.I)
            if match:
                local = snippet[max(0, match.start() - 30):match.end() + 90]
                break
        candidates = {
            number
            for match in re.finditer(
                rf"(?i)(\d+(?:[.,]\d+)?)\s*{PIECE_UNIT}(?=$|\W)",
                local,
            )
            if (number := _number(match.group(1))) is not None
        }
    selected_number = _number(selected)
    return len(candidates) > 1 and selected_number in candidates


def evaluate_quantity_evidence(
    value: Any,
    part_number: Any,
    product_name: Any,
    document_number: Any,
    buyer_code: Any,
    raw_email: dict[str, Any] | None = None,
    original_text: str = "",
) -> dict[str, Any]:
    raw_email = dict(raw_email or {})
    contract = quantity_contract(buyer_code)
    number = _number(value)
    if value in (None, "") or number is None:
        return {
            "status": "missing_processed", "source": "none",
            "evidence_snippet": "", "method": "", "warnings": [],
        }

    if contract.get("allow_table_quantity_column", True):
        table = _table_evidence(
            value, part_number, product_name, raw_email, original_text
        )
        if table:
            return table

    occurrences: list[tuple[str, str, re.Match[str]]] = []
    pattern = _value_pattern(value)
    for source, text in _source_texts(raw_email, original_text):
        occurrences.extend((source, text, match) for match in pattern.finditer(text))
    if not occurrences:
        return {
            "status": "not_found", "source": "none",
            "evidence_snippet": "", "method": "", "warnings": [],
        }

    part = _compact(part_number)
    product = _compact(product_name)
    document = _compact(document_number)
    strong: list[dict[str, Any]] = []
    for source, text, match in occurrences:
        evidence = _snippet(text, match.start(), match.end(), 110)
        if _dangerous_number_context(evidence, value):
            continue
        has_part = bool(
            part and re.search(re.escape(part), evidence, re.I)
        )
        has_product = bool(
            product and len(product) >= 4 and _norm(product) in _norm(evidence)
        )
        quantity_label = bool(re.search(
            rf"(?i){QUANTITY_LABEL}\s*[:=—\-| ]*\s*{_number_pattern(value)}(?![\d.,])",
            evidence,
        ))
        piece_after = bool(re.search(
            rf"(?i)(?<![\d.,]){_number_pattern(value)}\s*{PIECE_UNIT}(?=$|\W)",
            evidence,
        ))
        unit_before_match = re.search(
            rf"(?i){PIECE_UNIT}\s*[:=—\-| ]*{_number_pattern(value)}(?![\d.,])",
            evidence,
        )
        unit_before = bool(
            unit_before_match
            and not re.search(
                r"\d\s*$",
                evidence[max(0, unit_before_match.start() - 16):unit_before_match.start()],
            )
        )
        piece_unit = piece_after or unit_before
        part_pair = (has_part or has_product) and (quantity_label or piece_unit)
        compact_line = bool(
            has_part
            and re.search(PART_LABEL, evidence, re.I)
            and (quantity_label or piece_unit)
        )
        if not (quantity_label or piece_unit or part_pair):
            continue
        if document and document == str(int(number) if number.is_integer() else number):
            continue
        if _candidate_conflict(evidence, value, part_number, product_name):
            return {
                "status": "conflict_quantity_candidates",
                "source": source,
                "evidence_snippet": evidence,
                "method": "multiple_equal_context_candidates",
                "warnings": ["quantity_multiple_candidates"],
            }
        if compact_line and contract.get("allow_compact_item_line", True):
            status, method, score = "confirmed_by_compact_item_line", "compact_item_line", 50
        elif part_pair:
            status, method, score = "confirmed_by_part_quantity_pair", "part_quantity_pair", 45
        elif quantity_label and contract.get("allow_quantity_label", True):
            status, method, score = "confirmed_by_quantity_label", "quantity_label", 35
        elif piece_unit and contract.get("allow_piece_unit", True):
            status, method, score = "confirmed_by_piece_unit", "piece_unit", 30
        else:
            continue
        if number == 1 and contract.get("require_product_context_for_single_number", True):
            if not (has_part or has_product or quantity_label or piece_unit):
                continue
        strong.append({
            "status": status, "source": source, "evidence_snippet": evidence,
            "method": method, "warnings": [], "score": score,
        })

    if strong:
        best = max(strong, key=lambda item: item["score"])
        best.pop("score", None)
        return best

    first_source, first_text, first_match = occurrences[0]
    return {
        "status": "weak_number_without_quantity_context",
        "source": first_source,
        "evidence_snippet": _snippet(first_text, first_match.start(), first_match.end(), 70),
        "method": "unlabeled_number",
        "warnings": [
            "quantity_1_without_context" if number == 1 else "quantity_without_context"
        ],
    }
