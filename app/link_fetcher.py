"""link_fetcher.py — скачивает и парсит данные по доверенным внешним ссылкам.

Поддерживает:
- avtoto.ru/personnel_claim/... — HTML-страница рекламации
- storage.yandexcloud.net/... — прямые ссылки на фото/файлы
- claim-transfer.parterra.ru/... — фото от Партерры

Новые неизвестные домены → карантин (не открываем, только логируем).
"""
from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import settings

# Паттерны ссылок по типам
LINK_TYPE_PATTERNS: list[tuple[str, str]] = [
    (r"avtoto\.ru/personnel_claim/", "avtoto_claim"),
    (r"avtoto\.ru/nondelivery/", "avtoto_nondelivery"),
    (r"storage\.yandexcloud\.net/", "yandex_storage"),
    (r"claim-transfer\.parterra\.ru/", "parterra_photo"),
    (r"pr-lg\.ru/", "prlg_portal"),
    (r"disk\.yandex\.ru/", "yandex_disk"),
    (r"mail\.ru/disk/", "mailru_disk"),
]

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".heic"}
DOC_EXTENSIONS = {".pdf", ".xls", ".xlsx", ".doc", ".docx", ".zip"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}


def classify_link(url: str) -> str:
    """Вернуть тип ссылки или 'unknown'."""
    for pattern, link_type in LINK_TYPE_PATTERNS:
        if re.search(pattern, url, re.I):
            return link_type
    return "unknown"


def is_trusted(url: str) -> bool:
    """Проверить, относится ли ссылка к доверенным доменам."""
    try:
        domain = urlparse(url).netloc.lower()
        for trusted in settings.trusted_link_domain_list:
            if domain == trusted or domain.endswith("." + trusted):
                return True
    except Exception:
        pass
    return False


def is_video_link(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in VIDEO_EXTENSIONS)


def is_photo_link(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in PHOTO_EXTENSIONS)


_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
}


def _fetch_url(url: str, timeout: int = 20) -> tuple[bytes | None, str, int]:
    """Скачать URL браузерными заголовками. Возвращает (bytes|None, content_type, status_code).

    Опц. прокси из настроек (settings.link_fetch_proxy) — нужен, когда серверный IP забанен
    источником (avtoto банит дата-центр-IP; домашний VPN проходит). status_code=403 → IP-блок.
    Cookie сессии (settings.link_fetch_cookie) добавляется в заголовки, если задан.
    """
    proxy = getattr(settings, "link_fetch_proxy", None) or None
    cookie = getattr(settings, "link_fetch_cookie", None) or None
    headers = dict(_BROWSER_HEADERS)
    if cookie:
        headers["Cookie"] = cookie
    try:
        kwargs: dict[str, Any] = {"timeout": timeout, "follow_redirects": True, "headers": headers}
        if proxy:
            kwargs["proxy"] = proxy
        with httpx.Client(**kwargs) as client:
            r = client.get(url, headers={"Referer": f"https://{urlparse(url).netloc}/"})
            if r.status_code == 200:
                return r.content, r.headers.get("content-type", ""), 200
            return None, r.headers.get("content-type", ""), r.status_code
    except Exception:
        return None, "", 0


def parse_avtoto_claim(html: str) -> dict[str, Any]:
    """Извлечь данные из HTML-страницы рекламации avtoto.

    Реальная структура (серверный рендер): артикул в <b class="code value">…</b>,
    описание/документ/причина в .claim_text, фото — files.avtoto.ru/file/<hash>,
    документы — files.avtoto.ru/file/… с расширением (pdf/xls). Несколько деталей —
    несколько .code value → собираем все позиции."""
    result: dict[str, Any] = {"source": "avtoto_claim", "photos": [], "documents": [], "fields": {}, "items": []}
    if not html:
        return result

    def _txt(s: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()

    # ── Фото и документы (files.avtoto.ru/file/<hash>) ──
    files = re.findall(r'https://files\.avtoto\.ru/file/[a-f0-9]+(?:\.[a-z0-9]{2,5})?', html, re.I)
    files = list(dict.fromkeys(files))
    for u in files:
        ext = (urlparse(u).path.rsplit(".", 1)[-1].lower() if "." in urlparse(u).path else "")
        if ext in {"pdf", "xls", "xlsx", "doc", "docx", "zip"}:
            result["documents"].append(u)
        else:
            result["photos"].append(u)  # без расширения = фото (avtoto отдаёт картинку)
    # прямые img src как фолбэк
    for p in re.findall(r'src=["\']([^"\']+\.(?:jpg|jpeg|png|webp))["\']', html, re.I):
        if "placeholder" not in p.lower() and "/build/img/" not in p and p not in result["photos"]:
            result["photos"].append(p)
    result["photos"] = result["photos"][:30]
    result["documents"] = result["documents"][:20]

    # ── Артикулы (позиции): <b class="code value">ART</b> ──
    arts = re.findall(r'class="code value"[^>]*>\s*([A-Z0-9][A-Z0-9._/\-]{2,40})\s*<', html, re.I)
    for a in dict.fromkeys(arts):
        result["items"].append({"part_number": a.strip()})
    if arts:
        result["fields"]["part_number"] = arts[0].strip()

    # ── Текст рекламации (.claim_text) → документ/дата/кол-во/причина ──
    claim = " ".join(_txt(m) for m in re.findall(r'class="claim_text"[^>]*>(.*?)</', html, re.I | re.S))
    if not claim:
        claim = _txt(html)
    m = re.search(r'(?:накладн[аяой]+|документ[ау]?|упд)\s*№?\s*([0-9]{4,})\s+от\s+(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})', claim, re.I)
    if not m:
        m = re.search(r'\b([0-9]{4,})\s+от\s+(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})', claim)
    if m:
        result["fields"]["document_number"] = m.group(1)
        result["fields"]["document_date"] = m.group(2)
    m = re.search(r'(?:в\s+количеств[еа]|кол-?во)\s*[:]?\s*(\d+)', claim, re.I)
    if m:
        result["fields"]["quantity"] = m.group(1)
    m = re.search(r'причин[аы]\s+обращени[яей]\s+([А-ЯЁа-яё][^.\n<]{2,60})', claim, re.I)
    if m:
        result["fields"]["comment"] = m.group(1).strip()
    return result


def fetch_external_link(url: str) -> dict[str, Any]:
    """Основная функция — скачать и распарсить внешнюю ссылку.

    Возвращает словарь с ключами:
    - ok: bool
    - link_type: str
    - trusted: bool
    - quarantine: bool  (если домен неизвестен)
    - photos: list[str]  (URL фото)
    - fields: dict  (извлечённые поля)
    - raw_bytes_size: int
    - content_type: str
    - error: str | None
    """
    result: dict[str, Any] = {
        "ok": False,
        "url": url,
        "link_type": classify_link(url),
        "trusted": is_trusted(url),
        "quarantine": False,
        "photos": [],
        "fields": {},
        "raw_bytes_size": 0,
        "content_type": "",
        "error": None,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    # Видео — пропускаем
    if is_video_link(url):
        result["error"] = "video_skipped"
        return result

    # Известный claim/фото-тип ссылки доверяем по самому паттерну URL (точное совпадение домена+пути).
    KNOWN_TRUSTED_TYPES = ("avtoto_claim", "avtoto_nondelivery", "yandex_storage",
                           "parterra_photo", "prlg_portal", "yandex_disk", "mailru_disk")
    if not result["trusted"] and result["link_type"] not in KNOWN_TRUSTED_TYPES:
        result["quarantine"] = True
        result["error"] = "untrusted_domain"
        return result

    # Прямая ссылка на фото
    if is_photo_link(url):
        result["photos"] = [url]
        result["ok"] = True
        return result

    # Скачиваем страницу
    raw, content_type, status = _fetch_url(url)
    if raw is None:
        # 403 = IP-блок источника (серверный дата-центр-IP забанен; нужен прокси/VPN-выход).
        result["error"] = "ip_blocked_403" if status == 403 else (f"http_{status}" if status else "fetch_failed")
        result["status_code"] = status
        return result

    result["raw_bytes_size"] = len(raw)
    result["content_type"] = content_type

    # HTML-страница
    if "html" in content_type.lower() or result["link_type"] in ("avtoto_claim", "avtoto_nondelivery"):
        try:
            html = raw.decode("utf-8", errors="replace")
        except Exception:
            html = ""
        parsed = parse_avtoto_claim(html)
        result["photos"] = parsed.get("photos", [])
        result["documents"] = parsed.get("documents", [])
        result["items"] = parsed.get("items", [])
        result["fields"] = parsed.get("fields", {})
        result["ok"] = True
        return result

    # PDF или Excel — отдадим на обработку AI
    path_lower = urlparse(url).path.lower()
    if path_lower.endswith(".pdf"):
        result["content_type"] = "application/pdf"
        result["raw_bytes"] = raw  # type: ignore[assignment]
        result["ok"] = True
        return result

    if any(path_lower.endswith(e) for e in (".xls", ".xlsx")):
        result["content_type"] = "application/vnd.ms-excel"
        result["raw_bytes"] = raw  # type: ignore[assignment]
        result["ok"] = True
        return result

    result["ok"] = True
    return result


def extract_links_from_email(subject: str | None, body_text: str | None, body_html: str | None) -> list[dict[str, Any]]:
    """Найти все ссылки в письме и классифицировать их."""
    text = " ".join([subject or "", body_text or "", body_html or ""])
    urls = re.findall(r'https?://[^\s<>\'\")\]]+', text)
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for url in urls:
        url = url.rstrip(".,;)\"]}\\")
        if url in seen:
            continue
        seen.add(url)
        if is_video_link(url):
            continue
        result.append({
            "url": url,
            "link_type": classify_link(url),
            "trusted": is_trusted(url),
            "is_photo": is_photo_link(url),
        })
    return result[:20]


def read_email_links(subject: str | None, body_text: str | None, body_html: str | None) -> dict[str, Any]:
    """Обработать ВСЕ ссылки письма (1, 2, 3… в одном письме): скачать, распарсить, слить.

    Возвращает агрегат: объединённые поля, все фото и документы, позиции, и пер-ссылочный отчёт.
    Для брака документы/фото со страницы — это доказательная база (там же, по ссылке)."""
    links = extract_links_from_email(subject, body_text, body_html)
    claim_links = [l for l in links if not l["is_photo"]
                   and l["link_type"] in ("avtoto_claim", "avtoto_nondelivery", "parterra_photo", "prlg_portal")]
    photo_links = [l for l in links if l["is_photo"] or l["link_type"] in ("yandex_storage", "parterra_photo")]

    agg: dict[str, Any] = {"ok": False, "processed": 0, "blocked": 0, "fields": {}, "photos": [],
                           "documents": [], "items": [], "per_link": []}
    for l in claim_links:
        r = fetch_external_link(l["url"])
        agg["per_link"].append({"url": l["url"], "ok": r.get("ok"), "error": r.get("error"),
                                "status": r.get("status_code"), "fields": r.get("fields"),
                                "photos": len(r.get("photos") or []), "documents": len(r.get("documents") or [])})
        if r.get("error") == "ip_blocked_403":
            agg["blocked"] += 1
        if r.get("ok"):
            agg["processed"] += 1
            for k, v in (r.get("fields") or {}).items():
                if v and not agg["fields"].get(k):  # первый непустой выигрывает
                    agg["fields"][k] = v
            agg["photos"] += [p for p in (r.get("photos") or []) if p not in agg["photos"]]
            agg["documents"] += [d for d in (r.get("documents") or []) if d not in agg["documents"]]
            agg["items"] += (r.get("items") or [])
    # прямые фото-ссылки
    for l in photo_links:
        if l["url"] not in agg["photos"]:
            agg["photos"].append(l["url"])
    agg["ok"] = agg["processed"] > 0 or bool(agg["photos"])
    return agg


def parse_saved_claim_html(html: str) -> dict[str, Any]:
    """Фолбэк, когда серверный IP забанен: оператор сохранил страницу рекламации (HTML/webarchive),
    мы парсим её тем же парсером, что и авто-загрузку. Возвращает {fields, photos, documents, items}."""
    return parse_avtoto_claim(html or "")
