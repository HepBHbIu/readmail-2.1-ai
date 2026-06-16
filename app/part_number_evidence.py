from __future__ import annotations

import html
import re
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import yaml

from .config import settings


PART_LABEL = (
    r"(?:арт(?:икул)?\.?|код(?:\s+товара)?|номенклатура|"
    r"part\s*number|sku|каталожн(?:ый)?\s+номер|oem|p\s*/?\s*n|номер\s+детали)"
)
QUANTITY_LABEL = r"(?:количество|кол-во|кол\s*во|к-во|qty|шт\.?|штук(?:а|и)?)"
DOCUMENT_WORDS = (
    "документ", "упд", "накладн", "реализац", "счет-фактур", "счёт-фактур",
)
CLAIM_WORDS = ("претензи", "заявк", "обращени", "рекламаци", "claim")
ITEM_WORDS = ("позици", "детал", "товар", "номенклатур")
TABLE_PART_HEADERS = (
    "артикул", "арт.", "код", "код товара", "номенклатура",
    "part number", "sku", "oem", "номер детали",
)


def _compact(value: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def _norm(value: Any) -> str:
    return _compact(value).lower().replace("ё", "е")


def _snippet(text: str, start: int, end: int, radius: int = 100) -> str:
    return _compact(text[max(0, start - radius):min(len(text), end + radius)])[:360]


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


def part_number_contract(buyer_code: Any) -> dict[str, Any]:
    path = _contracts_path()
    modified_ns = path.stat().st_mtime_ns if path.exists() else 0
    contracts = _load_contracts_cached(str(path.resolve()), modified_ns)
    default = contracts.get("default") or {}
    supplier = contracts.get(str(buyer_code or "").strip()) or {}
    default_part = default.get("part_number_evidence") or {}
    supplier_part = supplier.get("part_number_evidence") or {}
    return {**default_part, **supplier_part}


def invalid_part_number_shape(value: Any) -> str:
    raw = _compact(value)
    compact = re.sub(r"\s+", "", raw)
    if not compact:
        return "missing"
    if "@" in compact:
        return "email"
    if re.fullmatch(r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}", compact):
        return "date"
    # «телефон» — ТОЛЬКО со структурой (+/скобки/пробелы/дефисы). Чистый ряд цифр НЕ телефон:
    # реальные OEM-артикулы бывают целиком числовыми (Bosch 1609073180, BMW 51117421850).
    if re.fullmatch(r"\+?\d[\d\s().\-]{8,}", raw) and re.search(r"[+\s().\-]", raw):
        return "phone"
    if re.fullmatch(r"\d{1,5}", compact):
        return "short_numeric"
    return ""


def _value_pattern(value: Any) -> str:
    raw = _compact(value)
    escaped = re.escape(raw).replace(r"\ ", r"\s*")
    return rf"(?<![A-Za-zА-Яа-я0-9]){escaped}(?![A-Za-zА-Яа-я0-9])"


def _source_texts(raw_email: dict[str, Any], fallback_text: str) -> list[tuple[str, str]]:
    values = [
        ("subject", raw_email.get("subject")),
        ("visible_text", raw_email.get("visible_text")),
        ("body", raw_email.get("body_text")),
        ("html", raw_email.get("body_html")),
    ]
    result = [(source, str(value or "")) for source, value in values if str(value or "").strip()]
    return result or [("body", fallback_text or "")]


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


def _html_rows(value: Any) -> list[list[str]]:
    parser = _TableParser()
    try:
        parser.feed(str(value or ""))
    except Exception:
        return []
    return parser.rows


def _pipe_rows(raw_email: dict[str, Any], fallback_text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for _, text in _source_texts(raw_email, fallback_text):
        for line in str(text).splitlines():
            if line.count("|") >= 2:
                cells = [_compact(cell) for cell in line.split("|")]
                if any(cells):
                    rows.append(cells)
    return rows


def _table_evidence(
    value: Any,
    raw_email: dict[str, Any],
    fallback_text: str,
    product_name: Any,
    require_header: bool = False,
) -> dict[str, Any] | None:
    needle = _norm(value)
    product = _norm(product_name)
    rows = _html_rows(raw_email.get("body_html")) + _pipe_rows(raw_email, fallback_text)
    headers: list[str] = []
    for row in rows:
        normalized = [_norm(cell) for cell in row]
        if any(any(header in cell for header in TABLE_PART_HEADERS) for cell in normalized):
            headers = normalized
            continue
        matching_indexes = [index for index, cell in enumerate(normalized) if cell == needle]
        for index in matching_indexes:
            header_match = bool(
                headers
                and index < len(headers)
                and any(header in headers[index] for header in TABLE_PART_HEADERS)
            )
            product_match = bool(product and any(product in cell or cell in product for cell in normalized if cell))
            product_cells = [
                cell for cell in normalized
                if cell and cell != needle and re.search(r"[а-яa-z]{3,}", cell, re.I)
            ]
            if header_match or (
                not require_header and (product_match or len(product_cells) >= 2)
            ):
                return {
                    "status": "confirmed_by_table_column",
                    "source": "table",
                    "evidence_snippet": " | ".join(row)[:360],
                    "method": "table_column",
                    "warnings": [],
                }
    return None


def _dangerous_context(snippet: str) -> bool:
    normalized = _norm(snippet)
    has_document = any(word in normalized for word in DOCUMENT_WORDS)
    has_claim = any(word in normalized for word in CLAIM_WORDS)
    has_product = (
        bool(re.search(PART_LABEL, normalized, re.I))
        or any(word in normalized for word in ITEM_WORDS)
    )
    return (has_document or has_claim) and not has_product


def evaluate_part_number_evidence(
    value: Any,
    product_name: Any,
    quantity: Any,
    buyer_code: Any,
    raw_email: dict[str, Any] | None = None,
    original_text: str = "",
) -> dict[str, Any]:
    raw_email = dict(raw_email or {})
    contract = part_number_contract(buyer_code)
    shape_problem = invalid_part_number_shape(value)
    raw = _compact(value)
    if shape_problem == "missing":
        return {
            "status": "missing_processed", "source": "none",
            "evidence_snippet": "", "method": "", "warnings": [],
        }
    pattern = _value_pattern(raw)

    safe_for_context_confirmation = shape_problem not in {"phone", "date", "email"}
    if safe_for_context_confirmation and (
        contract.get("prefer_table_column") or contract.get("allow_table_column", True)
    ):
        table = _table_evidence(
            raw,
            raw_email,
            original_text,
            product_name,
            require_header=shape_problem == "short_numeric",
        )
        if table:
            return table

    occurrences: list[tuple[str, str, re.Match[str]]] = []
    for source, text in _source_texts(raw_email, original_text):
        occurrences.extend((source, text, match) for match in re.finditer(pattern, text, re.I))
    if not occurrences:
        return {
            "status": "not_found", "source": "none",
            "evidence_snippet": "", "method": "", "warnings": [],
        }

    if safe_for_context_confirmation and contract.get("allow_part_label", True):
        for source, text, match in occurrences:
            before = text[max(0, match.start() - 55):match.start()]
            if re.search(rf"(?i){PART_LABEL}\s*[:=—\-| ]*\*?\s*$", before):
                evidence = _snippet(text, match.start(), match.end(), 80)
                return {
                    "status": "confirmed_by_part_label",
                    "source": source,
                    "evidence_snippet": evidence,
                    "method": "part_label",
                    "warnings": [],
                }

    if (
        not shape_problem
        and contract.get("allow_compact_item_line", True)
        and quantity not in (None, "")
    ):
        quantity_text = re.escape(str(quantity).strip()).replace(r"\.", r"[.,]")
        for source, text, match in occurrences:
            evidence = _snippet(text, match.start(), match.end(), 100)
            has_quantity = bool(re.search(
                rf"(?i)(?:{QUANTITY_LABEL}\s*[:=,—\- ]*\s*{quantity_text}(?!\d)|"
                rf"(?<!\d){quantity_text}\s*(?:шт\.?|штук(?:а|и)?))",
                evidence,
            ))
            has_item_signal = bool(re.search(PART_LABEL, evidence, re.I)) or bool(
                re.search(r"[A-Za-zА-Яа-я]{3,}", evidence)
            )
            if has_quantity and has_item_signal and not _dangerous_context(evidence):
                return {
                    "status": "confirmed_by_compact_item_line",
                    "source": source,
                    "evidence_snippet": evidence,
                    "method": "compact_item_line",
                    "warnings": [],
                }

    if (
        not shape_problem
        and contract.get("allow_product_context", True)
        and _compact(product_name)
    ):
        max_distance = int(contract.get("max_distance_to_product_name") or 120)
        product_pattern = re.escape(_compact(product_name)).replace(r"\ ", r"\s+")
        for source, text, match in occurrences:
            window_start = max(0, match.start() - max_distance)
            window_end = min(len(text), match.end() + max_distance)
            window = text[window_start:window_end]
            if re.search(product_pattern, window, re.I):
                evidence = _snippet(text, match.start(), match.end(), max_distance)
                if not _dangerous_context(evidence):
                    return {
                        "status": "confirmed_by_product_context",
                        "source": source,
                        "evidence_snippet": evidence,
                        "method": "product_context",
                        "warnings": [],
                    }

    evidence = _snippet(occurrences[0][1], occurrences[0][2].start(), occurrences[0][2].end())
    warning = {
        "phone": "part_number_looks_like_phone",
        "date": "part_number_looks_like_date",
        "email": "part_number_looks_like_email",
        "short_numeric": "part_number_short_numeric_without_label",
    }.get(shape_problem, "part_number_without_product_context")
    return {
        "status": "weak_found",
        "source": occurrences[0][0],
        "evidence_snippet": evidence,
        "method": "",
        "warnings": [warning],
    }
