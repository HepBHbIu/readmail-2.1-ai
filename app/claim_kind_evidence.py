from __future__ import annotations

import html
import re
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import yaml

from .config import settings


KIND_ALIASES = {
    "customer_refusal": "quality_refusal",
    "refusal": "quality_refusal",
    "nonconforming": "defect",
    "wrong_product": "wrong_item",
    "under_delivery": "shortage",
    "marking_request": "marking",
}
REASON_LABELS = (
    "причина", "причина возврата", "причина претензии",
    "комментарий", "тип претензии",
)


def _compact(value: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def _norm(value: Any) -> str:
    return _compact(value).lower().replace("ё", "е")


def _config_root() -> Path:
    configured = Path(settings.buyer_config_dir).parent
    if configured.exists():
        return configured
    return Path(__file__).resolve().parent.parent / "config"


@lru_cache(maxsize=8)
def _load_yaml_cached(path: str, modified_ns: int) -> dict[str, Any]:
    del modified_ns
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _load_yaml(path: Path) -> dict[str, Any]:
    modified_ns = path.stat().st_mtime_ns if path.exists() else 0
    return _load_yaml_cached(str(path.resolve()), modified_ns)


def claim_rules() -> dict[str, dict[str, list[str]]]:
    raw = _load_yaml(_config_root() / "claim_kind_rules.yaml")
    return {
        str(kind): {
            key: [_norm(item) for item in config.get(key) or [] if _norm(item)]
            for key in ("positive", "weak", "negative")
        }
        for kind, config in raw.items()
        if isinstance(config, dict)
    }


def supplier_claim_contract(buyer_code: Any) -> dict[str, Any]:
    raw = _load_yaml(_config_root() / "supplier_contracts.yaml")
    default = raw.get("default") if isinstance(raw.get("default"), dict) else {}
    supplier = raw.get(str(buyer_code or "")) if isinstance(raw.get(str(buyer_code or "")), dict) else {}
    default_claim = default.get("claim_kind_evidence") if isinstance(default.get("claim_kind_evidence"), dict) else {}
    supplier_claim = supplier.get("claim_kind_evidence") if isinstance(supplier.get("claim_kind_evidence"), dict) else {}
    return {**default_claim, **supplier_claim}


def _sources(raw_email: dict[str, Any], fallback: str) -> list[tuple[str, str]]:
    values = [
        ("subject", raw_email.get("subject")),
        ("visible_text", raw_email.get("visible_text")),
        ("body", raw_email.get("body_text")),
        ("html", raw_email.get("body_html")),
    ]
    result = [(name, str(value or "")) for name, value in values if str(value or "").strip()]
    return result or [("body", fallback or "")]


def _snippet(text: str, phrase: str, radius: int = 100) -> str:
    normalized = _norm(text)
    pos = normalized.find(_norm(phrase))
    if pos < 0:
        return _compact(text)[:360]
    return _compact(text[max(0, pos - radius):pos + len(phrase) + radius])[:360]


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


def _table_rows(raw_email: dict[str, Any]) -> list[list[str]]:
    parser = _TableParser()
    try:
        parser.feed(str(raw_email.get("body_html") or ""))
    except Exception:
        pass
    rows = list(parser.rows)
    for key in ("visible_text", "body_text"):
        for line in str(raw_email.get(key) or "").splitlines():
            if line.count("|") >= 2:
                cells = [_compact(cell) for cell in line.split("|")]
                if any(cells):
                    rows.append(cells)
    return rows


def _phrase_matches(text: str, phrases: list[str]) -> list[str]:
    normalized = _norm(text)
    return sorted(
        {phrase for phrase in phrases if phrase and phrase in normalized},
        key=len,
        reverse=True,
    )


def _detected_positive_kinds(text: str, rules: dict[str, dict[str, list[str]]]) -> dict[str, list[str]]:
    return {
        kind: matches
        for kind, config in rules.items()
        if (matches := _phrase_matches(text, config.get("positive") or []))
    }


def _labeled_reason_segments(text: str, column_names: list[str]) -> list[str]:
    labels = sorted(
        {_norm(item) for item in [*REASON_LABELS, *column_names] if _norm(item)},
        key=len,
        reverse=True,
    )
    if not labels:
        return []
    pattern = re.compile(
        rf"(?i)(?:{'|'.join(re.escape(item) for item in labels)})"
        r"\s*[:=—\-| ]{1,12}([^\n\r]{1,180})"
    )
    return [_compact(match.group(1)) for match in pattern.finditer(text)]


def _nearby_conflict(
    text: str,
    current_phrases: list[str],
    other_phrases: list[str],
    radius: int = 160,
) -> str:
    normalized = _norm(text)
    for current in current_phrases:
        start = 0
        while True:
            pos = normalized.find(current, start)
            if pos < 0:
                break
            window = normalized[max(0, pos - radius):pos + len(current) + radius]
            conflict = next((phrase for phrase in other_phrases if phrase in window), "")
            if conflict:
                return conflict
            start = pos + max(1, len(current))
    return ""


def _table_reason(
    raw_email: dict[str, Any],
    current_kind: str,
    rules: dict[str, dict[str, list[str]]],
    column_names: list[str],
) -> dict[str, Any] | None:
    headers: list[str] = []
    names = {_norm(item) for item in [*REASON_LABELS, *column_names] if _norm(item)}
    for row in _table_rows(raw_email):
        normalized = [_norm(cell) for cell in row]
        if any(any(name in cell for name in names) for cell in normalized):
            headers = normalized
            continue
        for index, cell in enumerate(normalized):
            if not cell:
                continue
            header_reason = bool(
                headers and index < len(headers)
                and any(name in headers[index] for name in names)
            )
            if not header_reason:
                continue
            detected = _detected_positive_kinds(cell, rules)
            if current_kind in detected and len(detected) == 1:
                return {
                    "status": "confirmed_by_table_reason_column",
                    "source": "table",
                    "evidence_snippet": " | ".join(row)[:360],
                    "method": "table_reason_column",
                    "matched_phrase": detected[current_kind][0],
                    "detected_kinds": detected,
                    "warnings": [],
                }
            if detected and (current_kind not in detected or len(detected) > 1):
                return {
                    "status": "conflict_reason_detected",
                    "source": "table",
                    "evidence_snippet": " | ".join(row)[:360],
                    "method": "table_reason_column",
                    "matched_phrase": next(iter(next(iter(detected.values()))), ""),
                    "detected_kinds": detected,
                    "warnings": ["claim_kind_conflict"],
                }
    return None


def evaluate_claim_kind_evidence(
    value: Any,
    buyer_code: Any,
    raw_email: dict[str, Any] | None = None,
    original_text: str = "",
) -> dict[str, Any]:
    if value in (None, ""):
        return {
            "status": "missing_processed", "source": "none", "evidence_snippet": "",
            "method": "", "matched_phrase": "", "detected_kinds": {},
            "warnings": [],
        }
    current_kind = KIND_ALIASES.get(str(value), str(value))
    rules = claim_rules()
    current = rules.get(current_kind)
    if not current:
        return {
            "status": "weak_found", "source": "none", "evidence_snippet": "",
            "method": "", "matched_phrase": "", "detected_kinds": {},
            "warnings": ["claim_kind_without_explicit_evidence"],
        }
    raw_email = dict(raw_email or {})
    contract = supplier_claim_contract(buyer_code)
    if contract.get("allow_table_reason_column"):
        table = _table_reason(
            raw_email,
            current_kind,
            rules,
            contract.get("reason_column_names") or [],
        )
        if table:
            return table

    combined = "\n".join(text for _, text in _sources(raw_email, original_text))
    column_names = contract.get("reason_column_names") or []
    for segment in _labeled_reason_segments(combined, column_names):
        segment_detected = _detected_positive_kinds(segment, rules)
        if current_kind in segment_detected and len(segment_detected) == 1:
            phrase = segment_detected[current_kind][0]
            return {
                "status": "confirmed_by_reason_label",
                "source": "body",
                "evidence_snippet": segment[:360],
                "method": "reason_label",
                "matched_phrase": phrase,
                "detected_kinds": segment_detected,
                "warnings": [],
            }
        if segment_detected:
            phrase = next(iter(next(iter(segment_detected.values()))), "")
            return {
                "status": "conflict_reason_detected",
                "source": "body",
                "evidence_snippet": segment[:360],
                "method": "reason_label_conflict",
                "matched_phrase": phrase,
                "detected_kinds": segment_detected,
                "warnings": ["claim_kind_conflict"],
            }

    detected = _detected_positive_kinds(combined, rules)
    current_matches = detected.get(current_kind) or []
    other_detected = {kind: phrases for kind, phrases in detected.items() if kind != current_kind}
    negative_matches = _phrase_matches(combined, current.get("negative") or [])
    other_phrases = [
        phrase for phrases in other_detected.values() for phrase in phrases
    ] + negative_matches
    nearby_conflict = _nearby_conflict(combined, current_matches, other_phrases)
    if (not current_matches and (other_detected or negative_matches)) or nearby_conflict:
        phrase = (
            nearby_conflict
            or (
                next(iter(next(iter(other_detected.values()))), "")
                if other_detected else negative_matches[0]
            )
        )
        return {
            "status": "conflict_reason_detected",
            "source": "body",
            "evidence_snippet": _snippet(combined, phrase),
            "method": "conflict_detection",
            "matched_phrase": phrase,
            "detected_kinds": detected,
            "warnings": ["claim_kind_conflict"],
        }

    supplier_phrases = []
    if current_kind == "quality_refusal":
        supplier_phrases = [
            _norm(item)
            for item in contract.get("quality_refusal_phrases") or []
            if _norm(item)
        ]
    supplier_matches = _phrase_matches(combined, supplier_phrases)
    if supplier_matches:
        phrase = supplier_matches[0]
        return {
            "status": "confirmed_by_supplier_contract",
            "source": "body",
            "evidence_snippet": _snippet(combined, phrase),
            "method": "supplier_contract",
            "matched_phrase": phrase,
            "detected_kinds": {current_kind: supplier_matches},
            "warnings": [],
        }

    if current_matches:
        phrase = current_matches[0]
        return {
            "status": "confirmed_by_explicit_reason",
            "source": "body",
            "evidence_snippet": _snippet(combined, phrase),
            "method": "explicit_reason",
            "matched_phrase": phrase,
            "detected_kinds": {current_kind: current_matches},
            "warnings": [],
        }

    weak_matches = _phrase_matches(combined, current.get("weak") or [])
    if weak_matches:
        phrase = weak_matches[0]
        return {
            "status": "weak_generic_refusal",
            "source": "body",
            "evidence_snippet": _snippet(combined, phrase),
            "method": "generic_reason",
            "matched_phrase": phrase,
            "detected_kinds": {},
            "warnings": ["claim_kind_generic_refusal"],
        }
    return {
        "status": "weak_found",
        "source": "none",
        "evidence_snippet": "",
        "method": "",
        "matched_phrase": "",
        "detected_kinds": {},
        "warnings": ["claim_kind_without_explicit_evidence"],
    }
