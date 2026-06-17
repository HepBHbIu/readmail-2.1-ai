"""Сигналы доказательств ПО ИМЕНАМ — без скачивания файлов и рендера ссылок.

Идея владельца: тип возврата часто понятен уже по имени вложения и пути ссылки
(reclamation/.../recl_*.jpg → фото брака; торг-2*.xlsx → акт; упд*.pdf → документ).
Этот слой дёшев (regex по тексту/именам), даёт ИИ готовую сводку и работает
как детерминированный фолбэк, когда ИИ не уверен.
"""
from __future__ import annotations

import re
from typing import Any

_URL_RE = re.compile(r"https?://[^\s\"<>)\]]+")

# Путь/имя → claim_kind-подсказка (брак/некондиция/недовоз/документ).
_DEFECT_PAT = ("реклам", "reclam", "брак", "defect", "damage", "деф", "битый", "скол", "трещ")
_DOC_PAT = ("торг-2", "торг2", "torg", "акт", "akt", "заключ", "наряд", "экспертиз")
_UPD_PAT = ("упд", "счёт-факт", "счет-факт", "invoice", "поступлен")
_RETURN_PAT = ("возврат", "vozvrat", "return", "претенз", "claim")
_SHORTAGE_PAT = ("недов", "nondeliver", "недопоставк", "расхожд", "излиш")
_PHOTO_EXT = (".jpg", ".jpeg", ".png", ".gif", ".heic", ".bmp")
_TABLE_EXT = (".xls", ".xlsx", ".csv")
_DOC_EXT = (".pdf", ".doc", ".docx")

# Провайдеры файлов-доказательств (фото/акты/видео лежат за ссылкой).
_EVID_HOSTS = ("storage.yandexcloud", "claim-transfer", "disk.yandex", "dropmefiles",
               "minio", "reclamation", "personnel_claim")
_VIDEO_HOSTS = ("youtube", "youtu.be")
_NOISE = ("w3.org", ".dtd", "schemas.", "avatars.mds", "mds.yandex", "trk.mail.ru",
          "e.mail.ru", "vk.com", "/track", "/pixel", "googletagmanager", "doubleclick",
          "facebook.com", "unsubscribe", ".css", ".js", "/font", "logo")


def _hit(text: str, pats: tuple[str, ...]) -> bool:
    return any(p in text for p in pats)


def derive_evidence_signals(email_data: dict[str, Any]) -> dict[str, Any]:
    """Дешёвая сводка доказательств по именам вложений и путям ссылок.

    Возвращает компактный dict для payload + детерминированных фолбэков.
    """
    atts = email_data.get("attachments") or []
    names = [str(a.get("filename") or "").lower() for a in atts]
    blob_names = " ".join(names)

    body = " ".join(str(email_data.get(k) or "") for k in
                    ("visible_text", "body_text", "snippet", "body_html"))
    evid_urls: list[str] = []
    for u in _URL_RE.findall(body):
        low = u.lower().rstrip(".,;)]}\"'")
        if any(n in low for n in _NOISE):
            continue
        if any(h in low for h in _EVID_HOSTS + _VIDEO_HOSTS):
            evid_urls.append(low)
    url_blob = " ".join(evid_urls)
    haystack = blob_names + " " + url_blob

    photo_files = sum(1 for n in names if n.endswith(_PHOTO_EXT))
    table_files = [n for n in names if n.endswith(_TABLE_EXT)]
    doc_files = [n for n in names if n.endswith(_DOC_EXT)]

    signals: list[str] = []
    claim_hint: str | None = None
    if _hit(haystack, _DEFECT_PAT):
        signals.append("defect_by_name"); claim_hint = "defect"
    if _hit(haystack, _SHORTAGE_PAT):
        signals.append("shortage_by_name"); claim_hint = claim_hint or "shortage"
    if _hit(blob_names, _DOC_PAT):
        signals.append("act_torg2_doc")
    if _hit(blob_names, _UPD_PAT):
        signals.append("upd_document")
    if photo_files or any(h in url_blob for h in ("reclamation", "claim-transfer", "/photo")):
        signals.append("photo_evidence")
    if any(h in url_blob for h in _VIDEO_HOSTS):
        signals.append("video_evidence")
    if _hit(url_blob, _RETURN_PAT) or _hit(url_blob, ("nondelivery",)):
        signals.append("return_portal_link")
    if table_files:
        signals.append("position_table")  # xls/csv = список позиций → парсить как текст

    return {
        "signals": signals,
        "claim_kind_hint": claim_hint,
        "photo_files": photo_files,
        "evidence_links": evid_urls[:8],
        "table_files": table_files[:5],
        "doc_files": doc_files[:5],
        "mentions_photo": bool(photo_files) or "photo_evidence" in signals,
        "mentions_return_link": bool(evid_urls) and ("return_portal_link" in signals
                                                     or "photo_evidence" in signals),
        "mentions_service_document": bool(doc_files) or "act_torg2_doc" in signals
                                     or "upd_document" in signals,
    }
