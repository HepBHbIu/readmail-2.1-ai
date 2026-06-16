from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .config import settings


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").lower().replace("ё", "е")).strip()


def _email_domain(value: Any) -> str:
    matches = re.findall(r"@([a-z0-9.-]+\.[a-z]{2,})", _norm(value), re.I)
    return matches[-1].rstrip(".") if matches else ""


def _config_dir() -> Path:
    configured = Path(settings.buyer_config_dir)
    if configured.exists():
        return configured
    return Path(__file__).resolve().parent.parent / "config" / "buyers"


def _alias_registry_path() -> Path:
    config_root = _config_dir().parent
    return config_root / "counterparty_aliases.yaml"


@lru_cache(maxsize=8)
def _load_alias_registry_cached(path: str, modified_ns: int) -> dict[str, dict[str, Any]]:
    del modified_ns
    registry_path = Path(path)
    if not registry_path.exists():
        return {}
    try:
        data = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return {
        str(code): dict(config)
        for code, config in data.items()
        if isinstance(config, dict)
    }


def _alias_registry() -> dict[str, dict[str, Any]]:
    path = _alias_registry_path()
    modified_ns = path.stat().st_mtime_ns if path.exists() else 0
    return _load_alias_registry_cached(str(path.resolve()), modified_ns)


@lru_cache(maxsize=4)
def _load_rules_cached(config_dir: str) -> tuple[dict[str, Any], ...]:
    rules: list[dict[str, Any]] = []
    for path in sorted(Path(config_dir).glob("*.yml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        buyer = data.get("buyer") if isinstance(data.get("buyer"), dict) else {}
        aliases = buyer.get("aliases") if isinstance(buyer.get("aliases"), dict) else {}
        code = str(buyer.get("code") or "").strip()
        if not code or not buyer.get("enabled", True):
            continue
        rules.append(
            {
                "code": code,
                "name": str(buyer.get("name") or code).strip(),
                "config": path.name,
                "domains": [
                    _norm(item).lstrip("@")
                    for item in aliases.get("domains") or []
                    if str(item or "").strip()
                ],
                "senders": [
                    _norm(item)
                    for item in aliases.get("senders") or []
                    if str(item or "").strip()
                ],
                "folders": [
                    _norm(item)
                    for item in aliases.get("folders") or []
                    if str(item or "").strip()
                ],
                "subject_contains": [
                    _norm(item)
                    for item in aliases.get("subject_contains") or []
                    if len(_norm(item)) >= 4
                ],
                "body_contains": [
                    _norm(item)
                    for item in aliases.get("body_contains") or []
                    if len(_norm(item)) >= 4
                ],
            }
        )
    return tuple(rules)


def _rules() -> tuple[dict[str, Any], ...]:
    return _load_rules_cached(str(_config_dir().resolve()))


def _domain_matches(domain: str, configured: str) -> bool:
    return bool(domain and configured and (domain == configured or domain.endswith("." + configured)))


def _route_matches(rule: dict[str, Any], mailbox: str) -> bool:
    if any(folder and folder in mailbox for folder in rule["folders"]):
        return True
    return any(domain and domain in mailbox for domain in rule["domains"])


def _explicit_text_counterparties(subject: str, body: str) -> list[dict[str, str]]:
    subject_norm = _norm(subject)
    body_norm = _norm(body)
    combined = f"{subject_norm}\n{body_norm}"
    found: dict[str, dict[str, str]] = {}
    for rule in _rules():
        signals: list[str] = []
        name = _norm(rule["name"])
        name_tokens = [
            name,
            name.replace(" / ", " "),
            name.replace("-", " "),
        ]
        if any(token and len(token) >= 5 and token in combined for token in name_tokens):
            signals.append("name")
        for domain in rule["domains"]:
            if domain and domain in combined:
                signals.append(f"domain:{domain}")
        if signals:
            found[rule["code"]] = {
                "code": rule["code"],
                "name": rule["name"],
                "signal": ",".join(dict.fromkeys(signals)),
            }
    return list(found.values())


def _relation_class(relation: str) -> str:
    normalized = _norm(relation)
    if "nested" in normalized or "supplier" in normalized:
        return "known_nested_counterparty"
    if "signature" in normalized:
        return "known_signature"
    return "known_alias"


def classify_counterparty_mismatch(
    buyer_code: Any,
    detected_text_counterparties: list[dict[str, Any]] | None,
    evidence_meta: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    code = str(buyer_code or "").strip()
    evidence_meta = dict(evidence_meta or {})
    registry_entry = _alias_registry().get(code) or {}
    allowed = {
        _norm(value)
        for value in registry_entry.get("allowed_text_counterparties") or []
        if str(value or "").strip()
    }
    relation = str(registry_entry.get("relation") or "alias")
    configured_severity = str(registry_entry.get("severity") or "info").lower()
    route_codes = {
        str(item.get("code") or "")
        for item in evidence_meta.get("route_counterparties") or []
        if item.get("code")
    }
    selected_source = str(evidence_meta.get("source") or "")
    selected_status = str(evidence_meta.get("status") or "")
    results: list[dict[str, Any]] = []
    for item in detected_text_counterparties or []:
        detected_code = str(item.get("code") or "")
        detected_name = str(item.get("name") or detected_code or "").strip()
        signal = str(item.get("signal") or "")
        is_allowed = _norm(detected_name) in allowed
        authoritative_profile_conflict = (
            detected_code
            and detected_code != code
            and detected_code in route_codes
            and (
                selected_source == "pattern_detector"
                or selected_status == "confirmed_by_parser"
            )
        )
        if is_allowed:
            mismatch_class = _relation_class(relation)
            severity = configured_severity if configured_severity in {"info", "warning"} else "warning"
        elif authoritative_profile_conflict:
            mismatch_class = "dangerous_profile_conflict"
            severity = "error"
        else:
            mismatch_class = "unknown_mismatch"
            severity = "warning"
        results.append(
            {
                "buyer_code": code,
                "detected_code": detected_code,
                "detected_name": detected_name,
                "signal": signal,
                "mismatch_class": mismatch_class,
                "severity": severity,
                "relation": relation if is_allowed else "",
                "registry_allowed": is_allowed,
                "requires_business_confirmation": mismatch_class in {
                    "known_nested_counterparty",
                    "unknown_mismatch",
                },
            }
        )
    return results


def build_buyer_evidence(
    buyer_code: Any,
    raw_email: dict[str, Any] | None,
    case_data: dict[str, Any] | None,
) -> dict[str, Any]:
    raw_email = dict(raw_email or {})
    case_data = dict(case_data or {})
    code = str(buyer_code or "").strip()
    from_addr = str(raw_email.get("from_addr") or "")
    from_domain = _email_domain(from_addr)
    mailbox = _norm(raw_email.get("mailbox"))
    subject = str(raw_email.get("subject") or "")
    body = "\n".join(
        str(raw_email.get(key) or "")
        for key in ("visible_text", "body_text", "snippet")
    )
    payload = case_data.get("payload") if isinstance(case_data.get("payload"), dict) else {}
    processing = case_data.get("processing") if isinstance(case_data.get("processing"), dict) else {}
    reasons = payload.get("reasons") if isinstance(payload.get("reasons"), dict) else {}
    buyer_reasons = [str(item) for item in reasons.get("buyer") or []]
    processing_source = str(
        payload.get("processing_source")
        or processing.get("source")
        or ""
    ).strip()
    parser = str(
        payload.get("parser")
        or payload.get("parser_id")
        or processing.get("parser")
        or ""
    ).strip()
    pattern_id = str(
        payload.get("pattern_id")
        or processing.get("pattern_id")
        or ""
    ).strip()

    rules = _rules()
    selected = next((rule for rule in rules if rule["code"] == code), None)
    sender_match = False
    domain_match = False
    route_match = False
    if selected:
        sender_norm = _norm(from_addr)
        sender_match = any(sender and sender in sender_norm for sender in selected["senders"])
        domain_match = any(_domain_matches(from_domain, domain) for domain in selected["domains"])
        route_match = _route_matches(selected, mailbox)

    reason_domain = next((item.split(":", 1)[1] for item in buyer_reasons if item.startswith("domain:")), "")
    learned_email = next((item.split(":", 1)[1] for item in buyer_reasons if item.startswith("learned_email:")), "")
    learned_domain = next((item.split(":", 1)[1] for item in buyer_reasons if item.startswith("learned_domain:")), "")
    pattern_reasons = [
        item for item in buyer_reasons
        if item == "sender" or item.startswith(("subject:", "body:"))
    ]

    status = ""
    source = ""
    matched_rule = ""
    if sender_match or learned_email:
        status, source = "confirmed_by_sender", "sender"
        matched_rule = learned_email or "configured_sender"
    elif domain_match or learned_domain or (reason_domain and reason_domain == from_domain):
        status, source = "confirmed_by_domain", "from_domain"
        matched_rule = learned_domain or reason_domain or "configured_domain"
    elif route_match:
        status, source = "confirmed_by_route", "import_route"
        matched_rule = mailbox
    elif parser:
        status, source = "confirmed_by_parser", parser
        matched_rule = parser
    elif processing_source == "pattern" and selected and (pattern_reasons or processing.get("pattern_before_ai_available")):
        status, source = "confirmed_by_pattern", "pattern_detector"
        matched_rule = pattern_id or ",".join(pattern_reasons) or selected["config"]

    route_counterparties: list[dict[str, str]] = []
    for rule in rules:
        signals: list[str] = []
        if any(_domain_matches(from_domain, domain) for domain in rule["domains"]):
            signals.append(f"from_domain:{from_domain}")
        if any(sender and sender in _norm(from_addr) for sender in rule["senders"]):
            signals.append("sender")
        if _route_matches(rule, mailbox):
            signals.append("route")
        if signals:
            route_counterparties.append(
                {
                    "code": rule["code"],
                    "name": rule["name"],
                    "signal": ",".join(dict.fromkeys(signals)),
                }
            )

    text_counterparties = _explicit_text_counterparties(subject, body)
    mismatches = [
        item
        for item in [*route_counterparties, *text_counterparties]
        if item["code"] != code
    ]
    unique_mismatches: dict[str, dict[str, str]] = {}
    for item in mismatches:
        existing = unique_mismatches.get(item["code"])
        if not existing:
            unique_mismatches[item["code"]] = dict(item)
            continue
        signals = [
            signal
            for signal in (existing.get("signal", "") + "," + item.get("signal", "")).split(",")
            if signal
        ]
        existing["signal"] = ",".join(dict.fromkeys(signals))
    result = {
        "status": status,
        "source": source,
        "matched_rule": matched_rule,
        "pattern_id": pattern_id,
        "parser": parser,
        "processing_source": processing_source,
        "from": from_addr,
        "from_domain": from_domain,
        "mailbox": str(raw_email.get("mailbox") or ""),
        "buyer_reasons": buyer_reasons,
        "route_counterparties": route_counterparties,
        "text_counterparties": text_counterparties,
        "mismatches": list(unique_mismatches.values()),
    }
    result["mismatch_classifications"] = classify_counterparty_mismatch(
        code,
        result["mismatches"],
        result,
    )
    result["dangerous_profile_conflict"] = any(
        item.get("mismatch_class") == "dangerous_profile_conflict"
        for item in result["mismatch_classifications"]
    )
    return result
