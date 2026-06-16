from __future__ import annotations

import html
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .config import settings


DEFAULT_POSITIVE = (
    "универсальный передаточный документ",
    "документ реализации",
    "счет-фактура",
    "счёт-фактура",
    "счет-фактур",
    "счёт-фактур",
    "накладная",
    "накладн",
    "накл.",
    "накл",
    "реализация",
    "реализац",
    "торг-12",
    "invoice",
    "waybill",
    "упд",
    "с/ф",
    "сф",
    "документ",
    "док.",
    # ТН = товарная накладная (avtoto/autorus/ixora: «ТН № 82669», «№ ТН 82150»,
    # «отгруженной по 81627»). Якорим к № чтобы НЕ задеть «ТН ВЭД» (таможенный код).
    "товарная накладная",
    "товарной накладной",
    "тн №",
    "№ тн",
    "отгружен",
)
DEFAULT_NEGATIVE = (
    "претензия", "заявка", "обращение", "рекламация",
    "claim", "request", "ticket",
)
DEFAULT_AMBIGUOUS = (
    "письмо", "заказ", "возврат",
)
DATE_RE = re.compile(
    r"(?<!\d)(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}-\d{1,2}-\d{1,2})(?!\d)"
)
NUMBER_TOKEN_RE = re.compile(r"(?<![\w])(?:№|#|no\.?|n)?\s*([A-Za-zА-Яа-я0-9][A-Za-zА-Яа-я0-9./_-]{2,30})", re.I)


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
def _load_contracts_cached(path: str, modified_ns: int) -> dict[str, Any]:
    del modified_ns
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def document_contract(buyer_code: Any) -> dict[str, Any]:
    path = _config_root() / "supplier_contracts.yaml"
    modified_ns = path.stat().st_mtime_ns if path.exists() else 0
    raw = _load_contracts_cached(str(path.resolve()), modified_ns)
    default = raw.get("default") if isinstance(raw.get("default"), dict) else {}
    supplier = raw.get(str(buyer_code or "")) if isinstance(raw.get(str(buyer_code or "")), dict) else {}
    default_doc = default.get("document_number_evidence") if isinstance(default.get("document_number_evidence"), dict) else {}
    supplier_doc = supplier.get("document_number_evidence") if isinstance(supplier.get("document_number_evidence"), dict) else {}
    return {**default_doc, **supplier_doc}


def _sources(raw_email: dict[str, Any], fallback: str) -> list[tuple[str, str]]:
    values = [
        ("subject", raw_email.get("subject")),
        ("visible_text", raw_email.get("visible_text")),
        ("body", raw_email.get("body_text")),
        ("html", raw_email.get("body_html")),
    ]
    result = [(name, str(value or "")) for name, value in values if str(value or "").strip()]
    return result or [("body", fallback or "")]


def _value_pattern(value: Any) -> re.Pattern[str]:
    escaped = re.escape(_compact(value)).replace(r"\ ", r"\s*")
    return re.compile(
        rf"(?<![A-Za-zА-Яа-я0-9]){escaped}(?![A-Za-zА-Яа-я0-9])",
        re.I,
    )


def _bad_shape(value: Any) -> str:
    raw = _compact(value)
    if not raw:
        return "missing"
    if "@" in raw:
        return "email"
    if DATE_RE.fullmatch(raw):
        return "date"
    if re.fullmatch(r"\+?\d[\d\s().-]{8,}", raw) and len(re.sub(r"\D", "", raw)) >= 10:
        return "phone"
    return ""


def _nearest_distance(text: str, position: int, phrases: list[str], radius: int = 90) -> tuple[int, str]:
    window_start = max(0, position - radius)
    window_end = min(len(text), position + radius)
    normalized = _norm(text[window_start:window_end])
    local_position = min(radius, position)
    best = (10_000, "")
    for phrase in phrases:
        normalized_phrase = _norm(phrase)
        start = 0
        while True:
            found = normalized.find(normalized_phrase, start)
            if found < 0:
                break
            distance = min(
                abs(local_position - found),
                abs(local_position - (found + len(normalized_phrase))),
            )
            if distance < best[0]:
                best = (distance, phrase)
            start = found + max(1, len(normalized_phrase))
    return best


def _context_status(phrase: str) -> str:
    normalized = _norm(phrase)
    if normalized in {"упд", "универсальный передаточный документ"}:
        return "confirmed_by_upd_context"
    if normalized in {
        "счет-фактура", "счёт-фактура", "счет-фактур", "счёт-фактур",
        "с/ф", "сф", "invoice",
    }:
        return "confirmed_by_invoice_context"
    if "накладн" in normalized or normalized in {"накл.", "накл", "торг-12", "waybill"}:
        return "confirmed_by_waybill_context"
    if "реализац" in normalized:
        return "confirmed_by_realization_context"
    return "confirmed_by_document_label"


def _snippet(text: str, start: int, end: int, radius: int = 90) -> str:
    return _compact(text[max(0, start - radius):min(len(text), end + radius)])[:360]


def _candidate_from_occurrence(
    source: str,
    text: str,
    start: int,
    end: int,
    positive: list[str],
    negative: list[str],
    document_date: Any,
) -> dict[str, Any]:
    positive_distance, positive_phrase = _nearest_distance(text, start, positive)
    negative_distance, negative_phrase = _nearest_distance(text, start, negative)
    snippet = _snippet(text, start, end)
    date_near = bool(
        document_date
        and _norm(document_date) in _norm(snippet)
    ) or bool(DATE_RE.search(snippet))
    score = 0
    if positive_phrase:
        score += 100 - min(positive_distance, 90)
    if date_near:
        score += 15
    if negative_phrase and (not positive_phrase or negative_distance + 8 < positive_distance):
        score -= 100 - min(negative_distance, 90)
    return {
        "source": source,
        "snippet": snippet,
        "positive_phrase": positive_phrase,
        "positive_distance": positive_distance if positive_phrase else None,
        "negative_phrase": negative_phrase,
        "negative_distance": negative_distance if negative_phrase else None,
        "date_near": date_near,
        "score": score,
    }


def find_best_document_candidate(
    raw_email: dict[str, Any] | None,
    original_text: str = "",
    buyer_code: Any = "",
) -> dict[str, Any] | None:
    """Return the strongest labeled document number, ignoring claim/request ids."""
    raw_email = dict(raw_email or {})
    contract = document_contract(buyer_code)
    positive = list(dict.fromkeys([
        *DEFAULT_POSITIVE,
        *(contract.get("positive_context") or []),
    ]))
    negative = list(dict.fromkeys([
        *DEFAULT_NEGATIVE,
        *(contract.get("negative_context") or []),
    ]))
    candidates: list[dict[str, Any]] = []
    for source, text in _sources(raw_email, original_text):
        for match in NUMBER_TOKEN_RE.finditer(text):
            value = match.group(1)
            if not re.search(r"\d", value) or _bad_shape(value):
                continue
            candidate = _candidate_from_occurrence(
                source, text, match.start(1), match.end(1), positive, negative, None
            )
            candidate["value"] = value
            if candidate["positive_phrase"]:
                candidates.append(candidate)
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item["score"], -int(item["positive_distance"] or 9999)))


def evaluate_document_number_evidence(
    value: Any,
    document_date: Any,
    buyer_code: Any,
    raw_email: dict[str, Any] | None = None,
    original_text: str = "",
) -> dict[str, Any]:
    raw_email = dict(raw_email or {})
    shape = _bad_shape(value)
    if shape == "missing":
        candidate = find_best_document_candidate(raw_email, original_text, buyer_code)
        return {
            "status": "missing_processed",
            "source": candidate.get("source", "none") if candidate else "none",
            "evidence_snippet": candidate.get("snippet", "") if candidate else "",
            "method": "",
            "matched_context": candidate.get("positive_phrase", "") if candidate else "",
            "date_near": bool(candidate and candidate.get("date_near")),
            "suggested_value": candidate.get("value") if candidate else None,
            "warnings": [],
        }
    if shape:
        return {
            "status": "weak_no_document_context",
            "source": "none",
            "evidence_snippet": "",
            "method": "",
            "matched_context": "",
            "date_near": False,
            "suggested_value": None,
            "warnings": ["document_number_bad_shape"],
        }

    contract = document_contract(buyer_code)
    positive = list(dict.fromkeys([
        *DEFAULT_POSITIVE,
        *(contract.get("positive_context") or []),
    ]))
    negative = list(dict.fromkeys([
        *DEFAULT_NEGATIVE,
        *(contract.get("negative_context") or []),
    ]))
    ambiguous = list(DEFAULT_AMBIGUOUS)
    occurrences: list[dict[str, Any]] = []
    pattern = _value_pattern(value)
    for source, text in _sources(raw_email, original_text):
        for match in pattern.finditer(text):
            occurrences.append(_candidate_from_occurrence(
                source, text, match.start(), match.end(), positive, negative, document_date
            ))
    if not occurrences:
        return {
            "status": "not_found", "source": "none", "evidence_snippet": "",
            "method": "", "matched_context": "", "date_near": False,
            "suggested_value": None, "warnings": [],
        }

    best_positive = max(
        (item for item in occurrences if item["positive_phrase"]),
        key=lambda item: item["score"],
        default=None,
    )
    if best_positive and (
        not best_positive["negative_phrase"]
        or int(best_positive["positive_distance"] or 999) <= int(best_positive["negative_distance"] or 999) + 8
    ):
        phrase = str(best_positive["positive_phrase"])
        return {
            "status": _context_status(phrase),
            "source": best_positive["source"],
            "evidence_snippet": best_positive["snippet"],
            "method": "document_context",
            "matched_context": phrase,
            "date_near": best_positive["date_near"],
            "suggested_value": None,
            "warnings": [],
        }

    best_negative = min(
        (item for item in occurrences if item["negative_phrase"]),
        key=lambda item: int(item["negative_distance"] or 9999),
        default=None,
    )
    if best_negative:
        return {
            "status": "conflict_claim_or_request_number",
            "source": best_negative["source"],
            "evidence_snippet": best_negative["snippet"],
            "method": "negative_context",
            "matched_context": best_negative["negative_phrase"],
            "date_near": best_negative["date_near"],
            "suggested_value": (
                (find_best_document_candidate(raw_email, original_text, buyer_code) or {}).get("value")
            ),
            "warnings": ["possible_claim_number_not_document_number"],
        }

    first = occurrences[0]
    normalized_snippet = _norm(first["snippet"])
    ambiguous_phrase = next(
        (phrase for phrase in ambiguous if _norm(phrase) in normalized_snippet),
        "",
    )
    return {
        "status": "weak_no_document_context",
        "source": first["source"],
        "evidence_snippet": first["snippet"],
        "method": "unlabeled_number",
        "matched_context": ambiguous_phrase,
        "date_near": first["date_near"],
        "suggested_value": None,
        "warnings": ["document_number_without_document_context"],
    }
