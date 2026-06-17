from __future__ import annotations

import hashlib
import json
import uuid
import re
from typing import Any

import httpx

from .config import settings
from .db import dumps, get_ai_cache, put_ai_cache, record_ai_usage, utcnow
from .email_parser import clean_ws, visible_body
from .evidence_signals import derive_evidence_signals
from .sender_profiles import sender_hint


class ModelNotFoundError(RuntimeError):
    """Raised when the AI endpoint was reached but the requested model does not exist."""
    def __init__(self, message: str, *, url: str = "", status: int = 0, response_text: str = "", model: str = ""):
        super().__init__(message)
        self.url = url
        self.status = status
        self.response_text = response_text
        self.model = model


SYSTEM_PROMPT = """Извлекаешь данные из писем о возвратах автозапчастей (опт). Отвечай ТОЛЬКО валидным JSON по схеме return_json — без markdown и текста вне JSON. Чего нет — null. Разбирай ВЕРХНИЙ новый текст; цитаты ниже (после «>», «От:», «Кому:», «Original») — это контекст, не новые данные.

ПОЛЯ:
- part_number — артикул детали (латиница+цифры HP1731/RF5161S, иногда только цифры 011227/553206). Метки «Артикул»/«Арт.»/«Код:». НЕ слова артикул/товар/цена/причина/кол-во.
- brand — производитель рядом с артикулом (SANGSIN, KRAUF, FEBEST, CTR). НЕ из email/домена, НЕ марка авто.
- product_name — короткое имя детали (Тяга рулевая, Колодки). Тип детали (Амортизатор/Насос/Рычаг) — это product_name, не brand.
- document_number — номер документа реализации/поступления: рядом с «накладная/УПД/реализация/счёт-фактура №», «по документу №», «поступление №». Часто в ТЕЛЕ письма («УПД №83904 от 15 июня»). НЕ номер заявки/претензии/акта/возврата/сумма/дата. ВАЖНО: №Э…/№В… из ТЕМЫ (напр. №Э00022168) — это return_number, НЕ document_number; document_number ищи отдельно по «УПД/накладная №».
- document_date — дата документа: «№X от ДАТА» или дата рядом со счётом-фактурой/поступлением (может стоять ПЕРЕД номером). Верни DD.MM.YYYY (ISO 2026-06-09 → 09.06.2026). НЕ дата письма/заявки.
- claim_number — «Претензия №»/«Рекламация №»/№ акта ТОРГ-2. client_request_number — «Заявка №»/«Запрос на возврат N»/«Подтверждение №». return_number — «Возврат поставщику №».
- quantity — число. comment — 1-3 слова причины.

МУЛЬТИПОЗИЦИИ: несколько деталей → items[]=[{part_number,brand,product_name,quantity}] КАЖДАЯ; fields=первая; документ/дата общие. Не выбрасывай 2-ю/3-ю.

ВЛОЖЕНИЯ/ССЫЛКИ (даны в payload): имя файла «наряд установки/снятия», «акт/ТОРГ-2/заключение» → defect_documents_status. Ссылки на фото/файлы/видео доказательств (storage.yandexcloud, claim-transfer, disk.yandex, dropmefiles, minio, reclamation/personnel_claim/*.jpg, youtube — видео приёмки) → mentions_photo=true и mentions_return_link=true (по ним оператор смотрит доказательства). Если есть фото-доказательства И причина про брак/дефект → claim_kind=defect, defect_documents_status=partial+.

ПОДСКАЗКИ В PAYLOAD (не данные письма, а навигация — используй, но проверяй по тексту):
- email.sender_hint — типовой формат этого отправителя (домен). Если есть — доверяй структуре, но факты бери из письма.
- email.evidence_signals — сигналы ПО ИМЕНАМ файлов/ссылок (без чтения содержимого): signals[], claim_kind_hint, mentions_*. defect_by_name → скорее claim_kind=defect; act_torg2_doc/upd_document → defect_documents_status≥partial; photo_evidence/return_portal_link → mentions_photo/mentions_return_link=true; position_table (xls) → позиции в приложенной таблице. Эти сигналы — фолбэк, текст письма приоритетнее.
- В email.text может быть блок «[ТЕКСТ ВЛОЖЕНИЙ/АКТА]» — это РАСПОЗНАННЫЙ текст из xls/акта (строки через «|»). Если в самом письме позиций нет, а там есть — бери артикул/бренд/наименование/кол-во и № документа ОТТУДА (полноценные данные). Каждая строка-позиция → отдельный элемент items[]. Шапки/итого/печати игнорируй.

event_type — КУДА пойдёт кейс:
- new_return — ВОЗВРАТ в 1С: брак(defect), некондиция(nonconforming), пересорт(wrong_item), отказ клиента(quality_refusal), НЕДОВОЗ/недопоставка(shortage), некомплект, излишек. Нужны № документа+дата+артикул+причина.
- pre_delivery_refusal — отказ ДО поставки («Запрос на снятие», «просим снять/не поставлять»). Ключ: № заявки/подтверждения + артикул (без документа реализации). Тоже в 1С.
- marking_request — маркировка/ТНВЭД/ЭДО. ВАЖНО: если ПРИЧИНА именно маркировка («Отсутствует/Повреждена контрольная марка», «Честный Знак/ЧЗ не в обороте/не сходится дата», «нет кода/ДатаМатрикс») → event_type=marking_request ДАЖЕ при слове «возврат».
- correction_request — «Корректировка поступления»/УКД/КСФ. number_replacement — замена артикула производителем.
- supplier_decision — решение поставщика («согласовано»). followup_reminder — напоминание/«ожидаем ответ». followup_dialog — иной ответ в переписке.
- ready_to_ship — товар готов к выдаче/забрать. shortage_link_event — недопоставка только по ссылке без позиции.
- problem_notice — УВЕДОМЛЕНИЕ о возможной будущей проблеме, НЕ запрос на возврат. Триггеры: «товар принят с дефектами», «принято до клиента», «клиент принимает решение», «НЕ является запросом на возврат», «в случае отказа (будет предоставлена информация)», «ведётся видеофиксация», «отсутствует/повреждена упаковка», «товар без упаковки», «будет предоставлена дополнительная информация». Если есть такие признаки И НЕТ прямого «оформить возврат/претензия/рекламация/прошу принять возврат» → problem_notice (не в 1С, но извлеки накладную+артикул, requires_action=наблюдать).
- supplier_report — прайс/прайс-лист/x4-прайс/остатки/наличие/stock/отчёт поставщика/автоматическая выгрузка (часто Excel/CSV/ZIP без возвратного запроса). НЕ возврат, даже если внутри «информация об изменениях». info_only — информационное уведомление без действия и без возвратного запроса. unknown — непонятно.

ПРОДОЛЖЕНИЕ ДИАЛОГА: есть In-Reply-To/References ИЛИ тема Re:/RE: → followup (НЕ new_return), даже если ниже цитата с данными (это данные исходного письма; поля бери из цитаты для привязки). «Просьба УТОЧНИТЬ отгрузку» без явного отказа/брака → followup_dialog (уточнение), НЕ возврат. new_return — только ПЕРВОЕ письмо без reply-заголовков.

НЕОПРЕДЕЛЁННОСТЬ (заготовка — НЕ выдумывай): если не можешь уверенно определить тип или ключевые поля — НЕ подгоняй. Поставь самый вероятный event_type (или unknown), confidence<0.5, requires_action=true, next_action=что именно неясно и что проверить оператору (напр. «не видно № документа — проверить вложение/ссылку»), cannot_export_reason=чего не хватает для 1С. Лучше честный «не уверен» с подсказкой, чем выдуманные данные. Пустые поля = null.

ПРИМЕРЫ:
[ТЕМА «Претензия №00000232318 … по документу №83124 от 04.06.2026» ТЕЛО «… | KDN9130YU | Krauf | Клапан … | 1 | Отказ клиента»] → {"event_type":"new_return","claim_kind":"quality_refusal","fields":{"claim_number":"00000232318","document_number":"83124","document_date":"04.06.2026","part_number":"KDN9130YU","brand":"Krauf","product_name":"Клапан компрессора кондиционера","quantity":"1","comment":"отказ клиента"}}
[«Запрос на снятие. Отказ клиента … Подтверждение № 3531392 Код: 011227 Производитель: METELLI … просим снять»] → {"event_type":"pre_delivery_refusal","claim_kind":"quality_refusal","fields":{"client_request_number":"3531392","part_number":"011227","brand":"METELLI","quantity":"4","comment":"отказ клиента"}}
[«Просьба согласовать возврат: накл. 83904 Арт. VWAB035 FEBEST Сайлентблок 2шт. Арт. GB10290 FENOX 2шт»] → {"event_type":"new_return","claim_kind":"quality_refusal","fields":{"document_number":"83904","part_number":"VWAB035","brand":"FEBEST","quantity":"2"},"items":[{"part_number":"VWAB035","brand":"FEBEST","quantity":"2"},{"part_number":"GB10290","brand":"FENOX","quantity":"2"}]}
"""


CLAIM_HINT_WORDS = [
    "возврат", "претенз", "рекламац", "брак", "дефект", "недовоз", "недопостав", "пересорт",
    "некондиц", "некомплект", "не комплект", "отказ", "маркиров", "корректиров", "упд",
]


def _strip_thinking(text: str) -> str:
    """Remove common reasoning wrappers used by local Qwen/MLX chat UIs."""
    text = (text or "").strip()
    # Qwen often emits <think>...</think> before the answer.  It is useful for humans,
    # but poisonous for strict JSON extraction.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.I | re.S).strip()
    text = re.sub(r"^```(?:json|JSON)?\s*", "", text).strip()
    text = re.sub(r"\s*```$", "", text).strip()
    return text


def _try_json_obj(fragment: str) -> dict[str, Any]:
    try:
        obj = json.loads(fragment)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _extract_json(text: str) -> dict[str, Any]:
    """Best-effort JSON extraction from local LLM output.

    Older versions used first "{" + last "}", which fails when the model emits
    reasoning, examples, or two JSON blocks.  v1.17 scans for the first valid
    balanced JSON object and accepts markdown/thinking wrappers.
    """
    text = _strip_thinking(text or "")
    if not text:
        return {}
    direct = _try_json_obj(text)
    if direct:
        return direct

    # Try fenced json blocks anywhere in the text.
    for m in re.finditer(r"```(?:json|JSON)?\s*(\{.*?\})\s*```", text, flags=re.S):
        obj = _try_json_obj(m.group(1))
        if obj:
            return obj

    # Scan every opening brace and let JSONDecoder stop at the end of the first object.
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _end = decoder.raw_decode(text[idx:])
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue

    # Last-resort: first brace to last brace, with tiny repairs for trailing commas.
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        frag = text[start:end + 1]
        obj = _try_json_obj(frag)
        if obj:
            return obj
        repaired = re.sub(r",\s*([}\]])", r"\1", frag)
        obj = _try_json_obj(repaired)
        if obj:
            return obj
    return {}


def _body_for_ai(email_data: dict[str, Any]) -> str:
    visible = str(email_data.get("visible_text") or visible_body(email_data.get("body_text"), email_data.get("body_html")))
    if settings.ai_context_mode == "full_visible":
        body = visible
    else:
        # Token saver: first screen usually contains the actual new request/reminder.
        body = visible[: max(500, int(settings.ai_max_chars))]
    return body[: max(500, int(settings.ai_max_chars))]


def _is_reply_email(email_data: dict[str, Any]) -> bool:
    """Быстрая проверка: является ли письмо ответом по техзаголовкам и теме."""
    if email_data.get("in_reply_to"):
        return True
    if email_data.get("references"):
        return True
    subject = email_data.get("subject") or ""
    if re.match(r"\s*(re|re\[|ответ|пересл|fw|fwd|aw|ant|sv|vs|ref)\s*:", subject, re.I):
        return True
    return False


def _buyer_ai_prompt(buyer_code: str | None) -> str:
    """Индивидуальные ИИ-подсказки клиента из его YAML-профиля (поле ai_prompt). '' если нет."""
    if not buyer_code:
        return ""
    try:
        from .classifier import load_buyer_rules
        for r in load_buyer_rules():
            if r.code == buyer_code:
                return (r.ai_prompt or "").strip()
    except Exception:
        return ""
    return ""


_PROMPT_URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.I)


def _attachments_for_prompt(email_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Список вложений для первого прогона: имя+тип+размер, чтобы промт сам
    распознал тип документа брака по имени файла, не гоняя vision."""
    out: list[dict[str, Any]] = []
    for a in (email_data.get("attachments") or [])[:30]:
        out.append({
            "filename": a.get("filename"),
            "content_type": a.get("content_type"),
            "size": a.get("size_bytes") if a.get("size_bytes") is not None else a.get("size"),
        })
    return out


def _links_for_prompt(email_data: dict[str, Any]) -> list[str]:
    """Ссылки из письма ВКЛЮЧАЯ HTML — там фото-доказательства брака и порталы возврата
    (storage.../reclamation/*.jpg), которые visible_text срезает. Мусор-схемы отсеиваем,
    фото/документы выносим вперёд (это доказательства)."""
    text = " ".join(str(email_data.get(k) or "") for k in ("visible_text", "body_text", "snippet", "subject", "body_html"))
    # Шум по корпусу v2: трекеры/аватары/подписи/соцсети — НЕ доказательства.
    NOISE = ("w3.org", ".dtd", "schemas.", "/font", "unsubscribe", ".css", ".js",
             "avatars.mds", "mds.yandex", "trk.mail.ru", "e.mail.ru", "vk.com",
             "/track", "/pixel", "googletagmanager", "doubleclick", "facebook.com",
             "t.me/", "wa.me/", "skype")
    # Провайдеры доказательств/файлов по корпусу v2 — В НАЧАЛО (фото/акты/видео по ссылке).
    EVID = ("storage.yandexcloud", "claim-transfer", "disk.yandex", "dropmefiles", "minio",
            "reclamation", "personnel_claim", "claim", "vozvrat", "return", "nondelivery",
            "/photo", ".jpg", ".jpeg", ".png", ".pdf", "youtube", "youtu.be")
    seen: list[str] = []
    for m in _PROMPT_URL_RE.findall(text):
        u = m.rstrip(".,;)]}\"'")
        low = u.lower()
        if any(x in low for x in NOISE):
            continue
        if u not in seen:
            seen.append(u)
        if len(seen) >= 25:
            break
    seen.sort(key=lambda u: 0 if any(x in u.lower() for x in EVID) else 1)
    return seen


# v2.1: компактного промта нет — «запрос дешевле ответа», поэтому всю конкретику
# (правила, примеры, вложения, ссылки) кладём в полный промт для любого purpose.
def _chat_payload(email_data: dict[str, Any], case_data: dict[str, Any], purpose: str = "case_extract") -> tuple[list[dict[str, str]], str, int]:
    body = _body_for_ai(email_data)

    # LEAN user-payload: правила и форматы — в SYSTEM-промте, здесь НЕ дублируем
    # (раньше был массив rules[] + business_context + minimum_export — копия system,
    # промт пух вдвое). Тут только: подсказка скелета, само письмо, reply-флаги, схема.
    prompt = {
        "purpose": purpose,
        "skeleton_guess": {  # черновик статики (static_hint) — ПОДСКАЗКА, AI решает сам
            "event_type": (((case_data.get("payload") or {}).get("static_hint") or {}).get("draft_event_type")
                           or case_data.get("event_type")),
            "claim_kind": (((case_data.get("payload") or {}).get("static_hint") or {}).get("draft_claim_kind")
                           or case_data.get("claim_kind")),
            "buyer_code": case_data.get("buyer_code"),
            "buyer_name": case_data.get("buyer_name"),
            "fields": case_data.get("fields") or {},
        },
        "email": {
            "subject": email_data.get("subject"),
            "from": email_data.get("from_addr"),
            "to": email_data.get("to_addr"),
            "received_at": email_data.get("received_at"),
            "text": body,
            "attachments": _attachments_for_prompt(email_data),
            "links": _links_for_prompt(email_data),
            # Дешёвые сигналы ПО ИМЕНАМ (брак/акт/фото/таблица) — без скачивания.
            "evidence_signals": derive_evidence_signals(email_data),
            # Подсказка под отправителя (типовой формат домена) — снимает неоднозначность.
            "sender_hint": sender_hint(email_data.get("from_addr")),
        },
        "reply_context": {
            "has_in_reply_to": bool(email_data.get("in_reply_to")),
            "has_references": bool(email_data.get("references")),
            "subject_is_reply": _is_reply_email(email_data),
        },
        "return_json": {  # СХЕМА ответа — верни строго это
            "buyer_code": "str|null", "buyer_name": "str|null",
            "event_type": "new_return|pre_delivery_refusal|followup_reminder|followup_dialog|supplier_decision|correction_request|marking_request|number_replacement|shortage_link_event|ready_to_ship|supplier_report|problem_notice|info_only|unknown",
            "claim_kind": "defect|nonconforming|number_replacement|wrong_item|shortage|overdelivery|incomplete_set|correction_request|marking_request|quality_refusal|null",
            "fields": {"claim_number": "·", "client_request_number": "·", "return_number": "·",
                       "document_number": "·", "document_date": "·", "part_number": "·",
                       "brand": "·", "product_name": "·", "quantity": "·", "comment": "·"},
            "items": "ВСЕ позиции письма [{part_number,brand,product_name,quantity}]; fields=первая",
            "evidence": {"mentions_photo": "bool", "mentions_return_link": "bool", "mentions_service_document": "bool"},
            "confidence": "0..1", "requires_action": "bool", "next_action": "str|null",
            "cannot_export_reason": "str|null",
            "defect_documents_status": "complete|partial|metadata_only|unknown_not_read|not_applicable",
        },
    }
    user = json.dumps(prompt, ensure_ascii=False)
    # Индивидуальный промт клиента из его профиля (ai_prompt) → добавляем к общему SYSTEM_PROMPT.
    # Если у клиента подсказок нет — работает только общий промт (фолбэк).
    sys_prompt = SYSTEM_PROMPT
    extra = _buyer_ai_prompt(case_data.get("buyer_code"))
    if extra:
        sys_prompt = SYSTEM_PROMPT + "\n\n# ОСОБЕННОСТИ ЭТОГО КЛИЕНТА (приоритет над общими правилами):\n" + extra
    prompt_hash = hashlib.sha256((settings.ai_provider + settings.ai_model + sys_prompt + user).encode("utf-8")).hexdigest()[:32]
    return ([{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}], prompt_hash, len(user) + len(sys_prompt))



def _routerai_headers() -> dict[str, str]:
    """Return auth headers for RouterAI."""
    key = settings.routerai_api_key or settings.ai_api_key or ""
    if key:
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    return {"Content-Type": "application/json"}


def _routerai_models() -> list[dict[str, str]]:
    """Fetch available models from RouterAI."""
    try:
        base = settings.routerai_base_url.rstrip("/")
        with httpx.Client(timeout=10) as client:
            r = client.get(base + "/v1/models", headers=_routerai_headers())
            if r.status_code < 400:
                data = r.json()
                models = []
                for m in (data.get("data") or []):
                    mid = m.get("id") or ""
                    if mid:
                        models.append({"id": mid, "name": m.get("name") or m.get("id") or mid})
                return models
    except Exception:
        pass
    return []

def _gigachat_token() -> str:
    # If AI_API_KEY already looks like a Bearer access token, use it directly.
    if settings.ai_api_key and settings.ai_api_key not in {"local", "none"} and not settings.gigachat_auth_key:
        return settings.ai_api_key
    if not settings.gigachat_auth_key:
        raise RuntimeError("GIGACHAT_AUTH_KEY is empty")
    headers = {
        "Authorization": f"Basic {settings.gigachat_auth_key}",
        "RqUID": str(uuid.uuid4()),
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    with httpx.Client(timeout=settings.ai_timeout_seconds, verify=False) as client:
        r = client.post(settings.gigachat_oauth_url, headers=headers, data={"scope": settings.gigachat_scope})
        r.raise_for_status()
        data = r.json()
        token = data.get("access_token")
        if not token:
            raise RuntimeError("GigaChat token response has no access_token")
        return str(token)


def _base_variants(base_url: str) -> list[str]:
    """Return safe candidate base URLs for local OpenAI-compatible servers.

    In Docker on macOS, 127.0.0.1/localhost points to the container, not the Mac.
    Users often paste http://127.0.0.1:8010/v1 from the MLX/vMLX panel, so we also
    try host.docker.internal automatically.
    """
    raw = (base_url or "").strip().rstrip("/")
    if not raw:
        raw = "http://host.docker.internal:8010/v1"
    variants: list[str] = []
    def add(u: str) -> None:
        u = u.strip().rstrip("/")
        if u and u not in variants:
            variants.append(u)
    if "127.0.0.1" in raw:
        add(raw.replace("127.0.0.1", "host.docker.internal"))
    if "localhost" in raw:
        add(raw.replace("localhost", "host.docker.internal"))
    add(raw)
    return variants


def _model_urls(base_url: str) -> list[str]:
    urls: list[str] = []
    for base in _base_variants(base_url):
        if base.endswith("/v1"):
            candidates = [base + "/models"]
        else:
            candidates = [base + "/v1/models", base + "/models"]
        for u in candidates:
            if u not in urls:
                urls.append(u)
    return urls



def _join_url(base: str, path: str) -> str:
    base = (base or "").strip().rstrip("/")
    path = (path or "").strip()
    if not path:
        return base
    if path.startswith("http://") or path.startswith("https://"):
        return path.rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return (base + path).rstrip("/")


def _root_variants(base_url: str) -> list[str]:
    out: list[str] = []
    def add(u: str) -> None:
        u = u.strip().rstrip("/")
        if u and u not in out:
            out.append(u)
    for base in _base_variants(base_url):
        if base.endswith("/v1"):
            add(base[:-3].rstrip("/"))
        add(base)
    return out


def _endpoint_candidates(base_url: str) -> list[tuple[str, str]]:
    """Endpoint candidates for local model servers.

    Native MLX/vMLX panels are not always OpenAI-compatible even when their UI shows
    a /v1 base URL.  v1.12.3 therefore probes OpenAI-style, Ollama-like and simple
    prompt endpoints.  If AI_ENDPOINT_PATH is set, it is tried first.
    """
    out: list[tuple[str, str]] = []
    mode = (getattr(settings, "ai_endpoint_mode", "auto") or "auto").strip().lower()
    explicit_path = (getattr(settings, "ai_endpoint_path", "") or "").strip()

    def add(url: str, kind: str) -> None:
        url = url.rstrip("/")
        item = (url, kind)
        if url and item not in out:
            out.append(item)

    def kinds_for_mode() -> list[str]:
        if mode == "openai_chat":
            return ["chat"]
        if mode == "openai_completion":
            return ["completion"]
        if mode == "ollama_chat":
            return ["ollama_chat"]
        if mode == "ollama_generate":
            return ["ollama_generate"]
        if mode == "simple_generate":
            return ["simple_generate"]
        return ["chat", "completion", "ollama_chat", "ollama_generate", "simple_generate"]

    # Explicit path first.  This is the escape hatch for a custom MLX Chat project.
    if explicit_path:
        for base in _root_variants(base_url):
            for kind in kinds_for_mode():
                add(_join_url(base, explicit_path), kind)

    for base in _base_variants(base_url):
        if base.endswith("/v1"):
            root = base[:-3].rstrip("/")
            if mode in {"auto", "openai_chat"}:
                add(base + "/chat/completions", "chat")
                add(root + "/chat/completions", "chat")
            if mode in {"auto", "openai_completion"}:
                add(base + "/completions", "completion")
                add(root + "/completions", "completion")
        else:
            if mode in {"auto", "openai_chat"}:
                add(base + "/v1/chat/completions", "chat")
                add(base + "/chat/completions", "chat")
            if mode in {"auto", "openai_completion"}:
                add(base + "/v1/completions", "completion")
                add(base + "/completions", "completion")

    # Common local server / chat UI routes.  These are not assumed safe for export;
    # they are only used to get a JSON suggestion and still go through validator.
    if mode == "auto":
        for root in _root_variants(base_url):
            for path, kinds in [
                ("/api/chat", ["ollama_chat", "simple_generate"]),
                ("/api/generate", ["ollama_generate", "simple_generate"]),
                ("/api/v1/chat", ["ollama_chat", "simple_generate"]),
                ("/api/v1/generate", ["ollama_generate", "simple_generate"]),
                ("/generate", ["ollama_generate", "simple_generate"]),
                ("/chat", ["ollama_chat", "simple_generate"]),
                ("/completion", ["completion", "simple_generate"]),
                ("/predict", ["simple_generate"]),
            ]:
                for kind in kinds:
                    add(_join_url(root, path), kind)

    return out


def _messages_to_prompt(messages: list[dict[str, str]]) -> str:
    parts: list[str] = []
    for m in messages:
        role = str(m.get("role") or "user").upper()
        content = str(m.get("content") or "")
        parts.append(f"{role}:\n{content}")
    parts.append("ASSISTANT: верни только JSON")
    return "\n\n".join(parts)




def _payload_for_kind(kind: str, messages: list[dict[str, str]], model: str) -> dict[str, Any]:
    max_tokens = int(settings.ai_max_output_tokens)
    prompt = _messages_to_prompt(messages)
    if kind == "chat":
        payload: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0.0, "max_tokens": max_tokens, "stream": False}
        if settings.ai_response_format == "json_object":
            payload["response_format"] = {"type": "json_object"}
        return payload
    if kind == "completion":
        return {"model": model, "prompt": prompt, "temperature": 0.0, "max_tokens": max_tokens, "stream": False}
    if kind == "ollama_chat":
        return {"model": model, "messages": messages, "stream": False, "options": {"temperature": 0, "num_predict": max_tokens}}
    if kind == "ollama_generate":
        return {"model": model, "prompt": prompt, "stream": False, "options": {"temperature": 0, "num_predict": max_tokens}}
    # Generic chat-panel/generate endpoint fallback.  Different local UIs use different
    # names; sending prompt/message/text together makes the request understandable for
    # many simple wrappers, while unknown keys are usually ignored.
    return {
        "model": model,
        "prompt": prompt,
        "message": prompt,
        "text": prompt,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "stream": False,
    }


def _response_content(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, dict):
        return json.dumps(raw, ensure_ascii=False)
    try:
        choice = (raw.get("choices") or [])[0]
        if isinstance(choice, dict):
            msg = choice.get("message") or {}
            if isinstance(msg, dict) and msg.get("content") is not None:
                return str(msg.get("content") or "")
            if choice.get("text") is not None:
                return str(choice.get("text") or "")
    except Exception:
        pass
    msg = raw.get("message")
    if isinstance(msg, dict) and msg.get("content") is not None:
        return str(msg.get("content") or "")
    for key in ["response", "content", "text", "output", "result", "generated_text", "completion"]:
        if raw.get(key) is not None:
            val = raw.get(key)
            return val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
    return json.dumps(raw, ensure_ascii=False)


def _resolve_openai_compatible_model(client: httpx.Client, base_url: str, requested: str) -> str:
    if requested and requested != "auto":
        return requested
    for url in _model_urls(base_url):
        try:
            r = client.get(url)
            if r.status_code < 400:
                data = r.json().get("data") or []
                if data and data[0].get("id"):
                    return str(data[0]["id"])
        except Exception:
            pass
    return "local"


_MODEL_NOT_FOUND_PATTERNS = [
    "model not found",
    "model '", "model \"",
    "not found",
    "invalid model",
    "model does not exist",
    "unknown model",
    "model .* not found",
    "model .* not supported",
]


def _is_model_not_found(text: str) -> bool:
    """Check if an error response indicates the model was not found/supported."""
    low = text.lower().replace("\n", " ").replace("'", "").replace('"', "")
    for p in _MODEL_NOT_FOUND_PATTERNS:
        if p.replace("'", "").replace('"', "") in low:
            return True
    # Also check structured OpenAI-style error
    if '"error"' in low and ('"message"' in low or '"code"' in low):
        # Look for model mentions in error
        return True
    return False


# ── Живой лог ИИ «от запроса до вывода» (кольцевой буфер последних вызовов) ──
_AI_LIVE_LOG: list[dict[str, Any]] = []

def _ai_log_push(entry: dict[str, Any]) -> None:
    try:
        from datetime import datetime, timezone, timedelta
        # Москва (UTC+3) — чтобы в ИИ-логе было местное время, а не «раннее утро» UTC.
        entry["at"] = datetime.now(timezone(timedelta(hours=3))).replace(microsecond=0).isoformat()
    except Exception:
        entry["at"] = ""
    _AI_LIVE_LOG.append(entry)
    if len(_AI_LIVE_LOG) > 60:
        del _AI_LIVE_LOG[:-60]

def _req_excerpt(messages: list[dict[str, Any]]) -> str:
    """Короткая выжимка запроса для лога: системка + последнее сообщение пользователя."""
    parts = []
    for m in messages:
        role = m.get("role")
        c = m.get("content")
        if isinstance(c, list):  # vision: текст + картинка
            txt = " ".join(p.get("text", "[image]") if isinstance(p, dict) else str(p) for p in c)
            c = txt
        parts.append(f"[{role}] {str(c)[:900]}")
    return "\n".join(parts)[:1800]

def get_ai_live_log(limit: int = 30) -> list[dict[str, Any]]:
    return list(reversed(_AI_LIVE_LOG[-int(limit):]))


def _request_chat(messages: list[dict[str, str]]) -> tuple[dict[str, Any], str, str, str]:
    """Обёртка с логированием запрос→ответ→токены→время для живого AI-лога."""
    import time as _t
    _t0 = _t.time()
    try:
        raw, provider, model, url = _request_chat_inner(messages)
    except Exception as exc:
        _ai_log_push({"ok": False, "error": str(exc)[:400], "model": settings.ai_model,
                      "request": _req_excerpt(messages), "response": "", "ms": int((_t.time() - _t0) * 1000),
                      "prompt_tokens": 0, "completion_tokens": 0})
        raise
    try:
        content = _response_content(raw)
        _pc = sum(len(str(m.get("content", ""))) for m in messages)
        ptok, ctok = _usage_tokens(raw, _pc, len(content or ""))
        _ai_log_push({"ok": True, "model": model, "provider": provider,
                      "request": _req_excerpt(messages), "response": (content or "")[:1800],
                      "prompt_tokens": ptok, "completion_tokens": ctok, "ms": int((_t.time() - _t0) * 1000)})
    except Exception:
        pass
    return raw, provider, model, url


def _request_chat_inner(messages: list[dict[str, str]]) -> tuple[dict[str, Any], str, str, str]:
    provider = settings.ai_provider.lower().strip()
    requested_model = settings.ai_model or "auto"
    if provider == "gigachat":
        token = _gigachat_token()
        url = settings.gigachat_base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"}
        model = requested_model if requested_model != "auto" else "GigaChat-2-Lite"
        payload: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0.0, "max_tokens": int(settings.ai_max_output_tokens)}
        with httpx.Client(timeout=settings.ai_timeout_seconds, verify=False) as client:
            r = client.post(url, headers=headers, json=payload)
            r.raise_for_status()
            return r.json(), provider, model, url

    headers = {"Content-Type": "application/json"}
    if settings.ai_api_key and settings.ai_api_key not in {"local", "none", ""}:
        headers["Authorization"] = f"Bearer {settings.ai_api_key}"
    errors: list[str] = []
    tried_urls: list[dict[str, Any]] = []
    with httpx.Client(timeout=settings.ai_timeout_seconds) as client:
        model = _resolve_openai_compatible_model(client, settings.ai_base_url, requested_model)
        for url, kind in _endpoint_candidates(settings.ai_base_url):
            payload = _payload_for_kind(kind, messages, model)
            try:
                r = client.post(url, headers=headers, json=payload)
                # Some OpenAI-like local servers reject response_format.
                if r.status_code >= 400 and isinstance(payload, dict) and "response_format" in payload:
                    payload.pop("response_format", None)
                    r = client.post(url, headers=headers, json=payload)
                tried_urls.append({"url": url, "kind": kind, "status": r.status_code})
                if r.status_code in {404, 405}:
                    errors.append(f"{url} [{kind}] -> HTTP {r.status_code}")
                    continue
                if r.status_code >= 400:
                    body = (r.text or "").strip().replace("\n", " ")[:220]
                    # Check for model-not-found specifically
                    if _is_model_not_found(body):
                        raise ModelNotFoundError(
                            f"Endpoint {url} найден, но выбранная модель не найдена. "
                            f"HTTP {r.status_code}: {body}",
                            url=url,
                            status=r.status_code,
                            response_text=body,
                            model=model,
                        )
                    errors.append(f"{url} [{kind}] -> HTTP {r.status_code}: {body}")
                    continue
                try:
                    return r.json(), provider, model, url
                except Exception:
                    return {"text": r.text}, provider, model, url
            except ModelNotFoundError:
                raise
            except Exception as exc:
                errors.append(f"{url} [{kind}] -> {str(exc)[:180]}")
                continue
    joined = " | ".join(errors[-12:])
    raise RuntimeError("AI endpoint не ответил ни на одном поддержанном маршруте. " + joined)


def _usage_tokens(raw: Any, prompt_chars: int, response_chars: int) -> tuple[int, int]:
    """Реальные токены из ответа API (OpenAI-поле usage); если нет — оценка ~3.3 симв/токен (RU)."""
    try:
        u = raw.get("usage") if isinstance(raw, dict) else None
        if isinstance(u, dict):
            pt = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
            ct = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
            if pt or ct:
                return pt, ct
    except Exception:
        pass
    return int((prompt_chars or 0) / 3.3), int((response_chars or 0) / 3.3)


def run_ai_suggestion(
    email_data: dict[str, Any],
    case_data: dict[str, Any],
    *,
    con: Any | None = None,
    case_id: int | None = None,
    purpose: str = "case_extract",
) -> dict[str, Any]:
    if not settings.enable_ai:
        return {"ok": False, "skipped": "ENABLE_AI=false"}
    messages, prompt_hash, prompt_chars = _chat_payload(email_data, case_data, purpose=purpose)
    provider = settings.ai_provider.lower().strip()
    cached = None
    if con is not None and settings.ai_cache_enabled:
        cached = get_ai_cache(con, prompt_hash)
        if cached:
            response = cached.get("response") or {}
            record_ai_usage(
                con,
                case_id=case_id,
                provider=cached.get("provider") or provider,
                model=cached.get("model") or settings.ai_model,
                prompt_hash=prompt_hash,
                prompt_chars=int(cached.get("prompt_chars") or prompt_chars),
                response_chars=int(cached.get("response_chars") or 0),
                cached=True,
                ok=bool(response),
            )
            result = {
                "ok": bool(response),
                "cached": True,
                "provider": cached.get("provider") or provider,
                "model": cached.get("model") or settings.ai_model,
                "prompt_hash": prompt_hash,
                "created_at": utcnow(),
                "response": response,
                "raw_excerpt": cached.get("raw_excerpt"),
                "usage": {"prompt_chars": int(cached.get("prompt_chars") or prompt_chars), "response_chars": int(cached.get("response_chars") or 0)},
            }
            _trace_suggestion(email_data, case_data, result, case_id=case_id, purpose=purpose)
            return result
    try:
        import time as _t
        _t0 = _t.time()
        raw, provider, model, _url = _request_chat(messages)
        _dur_ms = int((_t.time() - _t0) * 1000)
        content = _response_content(raw)
        parsed = _extract_json(content)
        response_chars = len(content or "")
        ptok, ctok = _usage_tokens(raw, prompt_chars, response_chars)
        result = {
            "ok": bool(parsed),
            "cached": False,
            "provider": provider,
            "model": model,
            "prompt_hash": prompt_hash,
            "created_at": utcnow(),
            "response": parsed,
            "raw_excerpt": clean_ws(content, 1000),
            "usage": {"prompt_chars": prompt_chars, "response_chars": response_chars},
        }
        if con is not None:
            # Запись usage/кэша — НЕ ФАТАЛЬНА: «database is locked» при логировании НЕ должна
            # терять оплаченный ответ модели (раньше падало в except → ok=False → деньги впустую).
            try:
                record_ai_usage(
                    con,
                    case_id=case_id,
                    provider=provider,
                    model=model,
                    prompt_hash=prompt_hash,
                    prompt_chars=prompt_chars,
                    response_chars=response_chars,
                    prompt_tokens=ptok,
                    completion_tokens=ctok,
                    cached=False,
                    ok=bool(parsed),
                    duration_ms=_dur_ms,
                )
                if settings.ai_cache_enabled and parsed:
                    put_ai_cache(
                        con,
                        prompt_hash=prompt_hash,
                        provider=provider,
                        model=model,
                        response=parsed,
                        raw_excerpt=result["raw_excerpt"],
                        prompt_chars=prompt_chars,
                        response_chars=response_chars,
                    )
            except Exception:
                pass
        _trace_suggestion(email_data, case_data, result, case_id=case_id, purpose=purpose)
        return result
    except Exception as exc:
        if con is not None:
            record_ai_usage(
                con,
                case_id=case_id,
                provider=provider,
                model=settings.ai_model,
                prompt_hash=prompt_hash,
                prompt_chars=prompt_chars,
                cached=False,
                ok=False,
                error=str(exc)[:300],
            )
        result = {"ok": False, "error": str(exc), "provider": provider, "model": settings.ai_model, "prompt_hash": prompt_hash, "raw_excerpt": ""}
        _trace_suggestion(email_data, case_data, result, case_id=case_id, purpose=purpose)
        return result


def _trace_suggestion(
    email_data: dict[str, Any],
    case_data: dict[str, Any],
    suggestion: dict[str, Any],
    *,
    case_id: int | None,
    purpose: str,
) -> None:
    try:
        from .ai_trace import append_ai_trace, build_trace_entry
        from .classifier import apply_ai_overlay

        ai_result = suggestion.get("response") if isinstance(suggestion.get("response"), dict) else {}
        final_result = apply_ai_overlay(email_data, case_data, ai_result) if ai_result else dict(case_data)
        mode = "fallback" if purpose in {"repair_missing_fields", "first_unknown_customer"} else "overlay"
        entry = build_trace_entry(
            email_data=email_data,
            pattern_result=case_data,
            ai_result=ai_result,
            final_result=final_result,
            provider=str(suggestion.get("provider") or settings.ai_provider),
            model=str(suggestion.get("model") or settings.ai_model),
            mode=mode,
            prompt_hash=str(suggestion.get("prompt_hash") or ""),
            case_id=case_id,
            raw_email_id=case_data.get("raw_email_id"),
            usage=suggestion.get("usage") or {},
            error=suggestion.get("error"),
        )
        append_ai_trace(entry)
    except Exception:
        pass


def trace_freeform_ai_call(
    prompt_text: str,
    *,
    provider: str,
    model: str,
    response_text: str = "",
    usage: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """Trace an explicit model test without attaching it to a processed case."""
    try:
        from .ai_trace import append_ai_trace, build_trace_entry

        ai_result = {"fields": {"comment": clean_ws(response_text, 1000)}} if response_text else {}
        append_ai_trace(build_trace_entry(
            email_data={
                "subject": "Manual AI test",
                "body_text": clean_ws(prompt_text, 500),
                "attachments": [],
            },
            pattern_result={"fields": {}},
            ai_result=ai_result,
            final_result=ai_result or {"fields": {}},
            provider=provider,
            model=model,
            mode="sandbox_replay",
            prompt_hash=hashlib.sha256(str(prompt_text or "").encode("utf-8")).hexdigest()[:32],
            usage=usage or {},
            error=error,
        ))
    except Exception:
        pass


def should_ask_ai(case_data: dict[str, Any], email_data: dict[str, Any] | None = None) -> bool:
    if not settings.enable_ai:
        return False
    if case_data.get("buyer_code") is None and settings.auto_ai_unknown_buyer:
        return True
    if case_data.get("buyer_code") is None and settings.auto_ai_first_unknown_customer:
        if not settings.ai_first_unknown_requires_claim_words:
            return True
        text = "\n".join([str((email_data or {}).get("subject") or ""), str((email_data or {}).get("visible_text") or (email_data or {}).get("snippet") or "")]).lower()
        return any(w in text for w in CLAIM_HINT_WORDS)
    missing = set(case_data.get("missing") or [])
    return bool(missing & {"buyer", "event_type", "claim_kind", "strong_key", "goods_or_document"})


def test_ai_connection() -> dict[str, Any]:
    if not settings.enable_ai:
        return {"ok": False, "skipped": "ENABLE_AI=false", "hint": "Включи ENABLE_AI в панели AI / MLX."}
    messages = [
        {"role": "system", "content": "Ответь только JSON без markdown."},
        {"role": "user", "content": settings.ai_test_prompt},
    ]
    try:
        raw, provider, model, url = _request_chat(messages)
    except ModelNotFoundError as exc:
        result = {
            "ok": False,
            "provider": settings.ai_provider,
            "model": settings.ai_model,
            "base_url": settings.ai_base_url,
            "endpoint_ok": True,
            "model_ok": False,
            "error_type": "model_not_found",
            "endpoint": exc.url,
            "status": exc.status,
            "error": str(exc),
            "message": (
                f"Endpoint найден, но выбранная модель не найдена. "
                f"Загрузите список моделей и выберите модель из списка."
            ),
            "hint": "Используйте «Загрузить модели», чтобы получить список доступных моделей, затем выберите одну из списка.",
            "response_excerpt": exc.response_text[:500] if exc.response_text else "",
        }
        trace_freeform_ai_call(
            settings.ai_test_prompt,
            provider=settings.ai_provider,
            model=settings.ai_model,
            error=str(exc),
        )
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "provider": settings.ai_provider,
            "model": settings.ai_model,
            "base_url": settings.ai_base_url,
            "endpoint_ok": False,
            "model_ok": False,
            "error_type": "endpoint_error",
            "error": str(exc),
            "hint": "v1.12.3 пробует OpenAI, Ollama-like и простые /api/chat /api/generate маршруты. Если это MLX Chat UI, нажми AI probe и поставь найденный path в AI_ENDPOINT_PATH.",
            "endpoint_mode": getattr(settings, "ai_endpoint_mode", "auto"),
            "endpoint_path": getattr(settings, "ai_endpoint_path", ""),
            "tried": [f"{u} [{kind}]" for u, kind in _endpoint_candidates(settings.ai_base_url)],
        }
        trace_freeform_ai_call(
            settings.ai_test_prompt,
            provider=settings.ai_provider,
            model=settings.ai_model,
            error=str(exc),
        )
        return result
    content = _response_content(raw)
    parsed = _extract_json(content)
    ptok, ctok = _usage_tokens(raw, len(settings.ai_test_prompt), len(content or ""))
    result = {
        "ok": bool(parsed),
        "provider": provider,
        "model": model,
        "url": url,
        "response": parsed,
        "raw_excerpt": clean_ws(content, 600),
    }
    trace_freeform_ai_call(
        settings.ai_test_prompt,
        provider=provider,
        model=model,
        response_text=content,
        usage={"prompt_tokens": ptok, "completion_tokens": ctok},
    )
    return result


def probe_ai_server() -> dict[str, Any]:
    """Diagnostic probe for AI server endpoints.

    For RouterAI provider, it only checks GET /v1/models and POST /v1/chat/completions
    (the two endpoints that RouterAI actually supports), avoiding noisy probes of
    Ollama/MLX paths.

    For other providers, it performs the full set of lightweight GET checks and
    POST tests across all supported endpoint shapes.
    """
    provider = settings.ai_provider.lower().strip()
    if provider == "gigachat":
        return {"ok": True, "provider": provider, "note": "probe для GigaChat не нужен; используй AI test"}

    # ── RouterAI: только релевантные эндпоинты ──────────────────────────
    if provider == "routerai":
        results: list[dict[str, Any]] = []
        model = settings.ai_model or "auto"
        headers = {"Content-Type": "application/json"}
        if settings.ai_api_key and settings.ai_api_key not in {"local", "none", ""}:
            headers["Authorization"] = f"Bearer {settings.ai_api_key}"
        with httpx.Client(timeout=min(int(settings.ai_timeout_seconds), 20)) as client:
            # RouterAI base URL уже содержит /v1 (напр. https://routerai.ru/api/v1)
            # поэтому добавляем /models и /chat/completions без префикса /v1
            router_base = (settings.routerai_base_url or settings.ai_base_url).rstrip("/")
            # 1. GET /v1/models — список моделей (всегда 200)
            models_url = router_base + "/models"
            try:
                r = client.get(models_url, headers={"Accept": "application/json", **headers})
                models_text = (r.text or "").strip().replace("\n", " ")[:500]
                results.append({"url": models_url, "method": "GET", "status": r.status_code, "excerpt": models_text, "content_type": r.headers.get("content-type", "")})
            except Exception as exc:
                results.append({"url": models_url, "method": "GET", "error": str(exc)[:220]})

            # 2. POST /v1/chat/completions — проверить выбранную модель
            chat_url = router_base + "/chat/completions"
            payload: dict[str, Any] = {"model": model, "messages": [{"role": "user", "content": "test"}], "max_tokens": 5}
            try:
                r = client.post(chat_url, headers=headers, json=payload)
                chat_text = (r.text or "").strip().replace("\n", " ")[:500]
                ok = r.status_code < 400
                model_ok = False
                if not ok and _is_model_not_found(chat_text):
                    model_ok = False
                elif ok:
                    model_ok = True
                results.append({"url": chat_url, "method": "POST", "status": r.status_code, "ok": ok, "model_ok": model_ok, "excerpt": chat_text})
            except Exception as exc:
                results.append({"url": chat_url, "method": "POST", "error": str(exc)[:220]})

        any_ok = any(r.get("ok") for r in results)
        models_available = any(r.get("status") == 200 for r in results if r.get("method") == "GET")
        return {
            "ok": any_ok,
            "provider": provider,
            "base_url": settings.ai_base_url,
            "model": model,
            "models_endpoint_ok": models_available,
            "diagnosis": (
                "✅ Models endpoint работает — загрузите модели через «Загрузить модели»" if models_available
                else "❌ Models endpoint не отвечает — проверьте Base URL"
            ),
            "results": results,
        }

    # ── Другие провайдеры (полный probe) ────────────────────────────────
    headers = {"Content-Type": "application/json"}
    if settings.ai_api_key and settings.ai_api_key not in {"local", "none", ""}:
        headers["Authorization"] = f"Bearer {settings.ai_api_key}"
    messages = [
        {"role": "system", "content": "Ответь только JSON без markdown."},
        {"role": "user", "content": settings.ai_test_prompt},
    ]
    base_urls = _root_variants(settings.ai_base_url)
    get_paths = ["", "/", "/health", "/api/health", "/docs", "/openapi.json", "/models", "/v1/models", "/api/tags", "/api/models", "/api/v1/models"]
    get_results: list[dict[str, Any]] = []
    post_results: list[dict[str, Any]] = []
    model = settings.ai_model or "local"
    with httpx.Client(timeout=min(int(settings.ai_timeout_seconds), 20)) as client:
        for base in base_urls[:4]:
            for path in get_paths:
                url = _join_url(base, path)
                try:
                    r = client.get(url)
                    text = (r.text or "").strip().replace("\n", " ")[:300]
                    get_results.append({"url": url, "status": r.status_code, "content_type": r.headers.get("content-type", ""), "excerpt": text})
                except Exception as exc:
                    get_results.append({"url": url, "error": str(exc)[:220]})
        for url, kind in _endpoint_candidates(settings.ai_base_url):
            payload = _payload_for_kind(kind, messages, model)
            try:
                r = client.post(url, headers=headers, json=payload)
                text = (r.text or "").strip().replace("\n", " ")[:500]
                ok = r.status_code < 400
                parsed_ok = False
                parsed_response = None
                if ok:
                    try:
                        parsed_response = _extract_json(_response_content(r.json()))
                        parsed_ok = bool(parsed_response)
                    except Exception:
                        parsed_ok = False
                post_results.append({"url": url, "kind": kind, "status": r.status_code, "ok": ok, "json_ok": parsed_ok, "excerpt": text})
                if ok and parsed_ok:
                    break
            except Exception as exc:
                post_results.append({"url": url, "kind": kind, "error": str(exc)[:220]})
    candidates = [x for x in post_results if x.get("ok")]
    return {
        "ok": any(x.get("json_ok") for x in post_results),
        "provider": provider,
        "base_url": settings.ai_base_url,
        "model": settings.ai_model,
        "endpoint_mode": getattr(settings, "ai_endpoint_mode", "auto"),
        "endpoint_path": getattr(settings, "ai_endpoint_path", ""),
        "recommendation": (
            "Если POST ok=true, но json_ok=false — endpoint отвечает, но модель не вернула чистый JSON. "
            "Если везде 404 — это не API-сервер модели, а UI/панель; запусти отдельный mlx_lm.server или укажи реальный AI endpoint path."
        ),
        "post_candidates_ok": candidates,
        "post_results": post_results,
        "get_results": get_results,
    }

def list_ai_models() -> dict[str, Any]:
    """Best-effort model discovery for OpenAI-compatible local panels and GigaChat.

    Returns model list as entries with ``id`` and ``name`` for dropdown display.
    """
    provider = settings.ai_provider.lower().strip()
    if provider == "gigachat":
        try:
            token = _gigachat_token()
            url = settings.gigachat_base_url.rstrip("/") + "/models"
            with httpx.Client(timeout=min(int(settings.ai_timeout_seconds), 20), verify=False) as client:
                r = client.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
                text = (r.text or "").strip()[:1000]
                data = r.json() if r.status_code < 400 else None
                models: list[dict[str, str]] = []
                if isinstance(data, dict):
                    for item in data.get("data") or []:
                        if isinstance(item, dict) and item.get("id"):
                            sid = str(item["id"])
                            models.append({"id": sid, "name": str(item.get("name") or sid)})
                return {"ok": r.status_code < 400, "provider": provider, "url": url, "status": r.status_code, "models": models, "raw_excerpt": text}
        except Exception as exc:
            return {"ok": False, "provider": provider, "error": str(exc)[:500]}

    headers = {"Accept": "application/json"}
    if settings.ai_api_key and settings.ai_api_key not in {"local", "none", ""}:
        headers["Authorization"] = f"Bearer {settings.ai_api_key}"
    results: list[dict[str, Any]] = []
    all_entries: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    urls = []
    for u in _model_urls(settings.ai_base_url):
        if u not in urls:
            urls.append(u)
    for root in _root_variants(settings.ai_base_url):
        for path in ["/api/tags", "/api/models", "/api/v1/models"]:
            u = _join_url(root, path)
            if u not in urls:
                urls.append(u)
    with httpx.Client(timeout=min(int(settings.ai_timeout_seconds), 20)) as client:
        for url in urls:
            try:
                r = client.get(url, headers=headers)
                text = (r.text or "").strip().replace("\n", " ")[:800]
                item: dict[str, Any] = {"url": url, "status": r.status_code, "excerpt": text}
                if r.status_code < 400:
                    try:
                        data = r.json()
                        entries: list[dict[str, str]] = []
                        if isinstance(data, dict):
                            # OpenAI-style: { data: [{ id, name, ... }] }
                            for rec in data.get("data") or []:
                                if isinstance(rec, dict):
                                    mid = rec.get("id") or rec.get("model")
                                    if mid:
                                        sid = str(mid)
                                        entry = {"id": sid, "name": str(rec.get("name") or sid)}
                                        if sid not in seen_ids:
                                            seen_ids.add(sid)
                                            all_entries.append(entry)
                                        entries.append(entry)
                            # Ollama /api/tags uses models: [{ name: ... }]
                            for rec in data.get("models") or []:
                                if isinstance(rec, dict):
                                    sid = str(rec.get("name") or rec.get("id") or rec.get("model") or "")
                                    if sid:
                                        entry = {"id": sid, "name": str(rec.get("name") or sid)}
                                        if sid not in seen_ids:
                                            seen_ids.add(sid)
                                            all_entries.append(entry)
                                        entries.append(entry)
                                elif isinstance(rec, str):
                                    sid = rec
                                    entry = {"id": sid, "name": sid}
                                    if sid not in seen_ids:
                                        seen_ids.add(sid)
                                        all_entries.append(entry)
                                    entries.append(entry)
                        # Also support flat string list
                        if isinstance(data, list):
                            for rec in data:
                                sid = str(rec) if not isinstance(rec, dict) else str(rec.get("id") or rec.get("name") or rec.get("model") or "")
                                if sid:
                                    entry = {"id": sid, "name": sid}
                                    if sid not in seen_ids:
                                        seen_ids.add(sid)
                                        all_entries.append(entry)
                                    entries.append(entry)
                        item["models"] = entries
                    except Exception as exc:
                        item["parse_error"] = str(exc)[:200]
                results.append(item)
            except Exception as exc:
                results.append({"url": url, "error": str(exc)[:300]})
    return {"ok": bool(all_entries), "provider": provider, "base_url": settings.ai_base_url, "models": all_entries, "results": results}


# ─────────────────────────────────────────────
# Vision / мультимодальная обработка
# ─────────────────────────────────────────────

import base64 as _b64


def _downscale_image(raw_bytes: bytes, max_side: int = 1280, quality: int = 80) -> bytes:
    """Сжать фото перед vision: большая сторона ≤ max_side, JPEG q80. Payload падает в разы →
    vision быстрее и дешевле. При любой ошибке возвращаем оригинал."""
    try:
        from PIL import Image
        import io as _io
        im = Image.open(_io.BytesIO(raw_bytes))
        if im.mode in ("RGBA", "P", "LA"):
            im = im.convert("RGB")
        w, h = im.size
        if max(w, h) > max_side:
            scale = max_side / float(max(w, h))
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        out = _io.BytesIO()
        im.save(out, format="JPEG", quality=quality, optimize=True)
        data = out.getvalue()
        return data if data and len(data) < len(raw_bytes) else raw_bytes
    except Exception:
        return raw_bytes


def _image_to_b64(raw_bytes: bytes) -> str:
    return _b64.b64encode(_downscale_image(raw_bytes)).decode("ascii")


def run_vision_on_attachment(
    att_bytes: bytes,
    filename: str,
    content_type: str,
    hint_text: str = "",
) -> dict[str, Any]:
    """Route an attachment (image or PDF) to the right vision handler.

    Returns dict with keys: ok, fields (extracted), source (vision|pdf_text), text.
    """
    if not settings.ai_vision_enabled:
        return {"ok": False, "skipped": "ai_vision_enabled=false", "fields": {}}

    fname_lower = (filename or "").lower()
    ctype_lower = (content_type or "").lower()

    # ── PDF: try text extraction first ──────────────────────────────────
    is_pdf = fname_lower.endswith(".pdf") or "pdf" in ctype_lower
    if is_pdf:
        pdf_text = ""
        try:
            from pdfminer.high_level import extract_text
            import io
            pdf_text = extract_text(io.BytesIO(att_bytes)) or ""
        except Exception:
            pass
        # If we got reasonable text (>100 chars), use text model not vision
        if len(pdf_text.strip()) > 100:
            return {
                "ok": True, "source": "pdf_text",
                "text": pdf_text[:6000],
                "fields": {},  # caller will run through patterns
            }
        # Sparse text → scanned PDF → convert first page to image for vision
        try:
            import io
            from PIL import Image
            # Try pdf2image if available, else skip vision for scanned PDF
            try:
                from pdf2image import convert_from_bytes
                pages = convert_from_bytes(att_bytes, first_page=1, last_page=1, dpi=150)
                if pages:
                    buf = io.BytesIO()
                    pages[0].save(buf, format="JPEG", quality=85)
                    att_bytes = buf.getvalue()
                    content_type = "image/jpeg"
                    is_pdf = False  # Fall through to vision
            except ImportError:
                return {"ok": False, "skipped": "pdf2image_not_installed", "fields": {}}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "fields": {}}

    # ── Image → vision model ─────────────────────────────────────────────
    is_image = (
        any(fname_lower.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".heic"))
        or content_type.startswith("image/")
    )
    if not is_image and not is_pdf is False:
        return {"ok": False, "skipped": "not_image_or_pdf", "fields": {}}

    result = run_vision_extraction(att_bytes, content_type or "image/jpeg", hint_text)
    result["source"] = "vision"
    result["fields"] = result.get("response") or {}
    return result


def run_vision_extraction(
    image_bytes: bytes,
    content_type: str = "image/jpeg",
    hint_text: str = "",
    prompt_text: str | None = None,
    case_id: int | None = None,
) -> dict[str, Any]:
    """Отправить изображение в vision-модель, получить структурированные поля.

    Возвращает тот же формат что run_ai_suggestion — dict с ключом 'response'.
    """
    if not settings.ai_vision_enabled:
        return {"ok": False, "skipped": "ai_vision_enabled=false"}

    b64 = _image_to_b64(image_bytes)
    prompt_text = prompt_text or (
        "На изображении этикетка или документ автозапчасти. "
        "Извлеки JSON без markdown: "
        "{\"part_number\": ..., \"brand\": ..., \"product_name\": ..., "
        "\"quantity\": ..., \"document_number\": ..., \"document_date\": ..., "
        "\"claim_kind\": \"defect|nonconforming|number_replacement|wrong_item|shortage|overdelivery|...\"|null, "
        "\"comment\": ...}. "
        "Не придумывай. Если поля нет — null."
    )
    if hint_text:
        prompt_text += f"\n\nДополнительный контекст из письма:\n{hint_text[:500]}"

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    # _image_to_b64 нормализует в JPEG (сжатие) → тип всегда image/jpeg.
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                },
                {"type": "text", "text": prompt_text},
            ],
        }
    ]

    # Используем vision-провайдер (может отличаться от основного)
    original_provider = settings.ai_provider
    original_model = settings.ai_model
    original_api_key = settings.ai_api_key

    try:
        # Временно переключаем на vision-настройки
        settings.ai_provider = settings.ai_vision_provider  # type: ignore[attr-defined]
        settings.ai_model = settings.ai_vision_model  # type: ignore[attr-defined]
        if settings.ai_vision_api_key:
            settings.ai_api_key = settings.ai_vision_api_key  # type: ignore[attr-defined]

        import time as _tv
        _tv0 = _tv.time()
        raw, provider, model, _url = _request_chat(messages)
        _vdur_ms = int((_tv.time() - _tv0) * 1000)
        content = _response_content(raw)
        parsed = _extract_json(content)
        ptok, ctok = _usage_tokens(raw, len(str(prompt_text or "")), len(content or ""))
        result = {
            "ok": bool(parsed),
            "provider": provider,
            "model": model,
            "response": parsed,
            "raw_excerpt": clean_ws(content, 500),
            "usage": {"prompt_tokens": ptok, "completion_tokens": ctok,
                      "prompt_chars": len(str(prompt_text or "")), "response_chars": len(content or "")},
        }
        try:
            from .db import connect as _connect
            with _connect() as _uc:
                record_ai_usage(_uc, case_id=case_id, provider=provider, model=model,
                                prompt_chars=len(str(prompt_text or "")), response_chars=len(content or ""),
                                prompt_tokens=ptok, completion_tokens=ctok, ok=bool(parsed), kind="vision",
                                duration_ms=_vdur_ms)
        except Exception:
            pass
        _trace_vision(prompt_text, content_type, result)
        return result
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
        try:
            from .db import connect as _connect
            with _connect() as _uc:
                record_ai_usage(_uc, case_id=case_id, provider=settings.ai_vision_provider,
                                model=settings.ai_vision_model, ok=False, error=str(exc)[:300], kind="vision")
        except Exception:
            pass
        _trace_vision(prompt_text, content_type, result)
        return result
    finally:
        settings.ai_provider = original_provider  # type: ignore[attr-defined]
        settings.ai_model = original_model  # type: ignore[attr-defined]
        settings.ai_api_key = original_api_key  # type: ignore[attr-defined]


def _trace_vision(prompt_text: str, content_type: str, result: dict[str, Any]) -> None:
    try:
        from .ai_trace import append_ai_trace, build_trace_entry
        parsed = result.get("response") if isinstance(result.get("response"), dict) else {}
        ai_case = {
            "claim_kind": parsed.get("claim_kind"),
            "fields": {key: value for key, value in parsed.items() if key != "claim_kind"},
        }
        append_ai_trace(build_trace_entry(
            email_data={
                "subject": "Vision attachment",
                "body_text": str(prompt_text or "")[:500],
                "attachments": [{"filename": "vision_input", "content_type": content_type}],
            },
            pattern_result={"fields": {}},
            ai_result=ai_case,
            final_result=ai_case,
            provider=str(result.get("provider") or settings.ai_vision_provider),
            model=str(result.get("model") or settings.ai_vision_model),
            mode="vision",
            prompt_hash=hashlib.sha256(str(prompt_text or "").encode("utf-8")).hexdigest()[:32],
            usage=result.get("usage") or {},
            error=result.get("error"),
        ))
    except Exception:
        pass


# ─────────────────────────────────────────────
# Счётчик токенов / трафика
# ─────────────────────────────────────────────





def _load_ai_price_rules() -> dict[str, dict[str, float]]:
    raw = str(getattr(settings, "ai_price_rules_json", "") or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    rows = data.get("models") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return {}
    rules: dict[str, dict[str, float]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        model = str(row.get("model") or row.get("id") or "").strip()
        if not model:
            continue
        def num(key: str) -> float:
            try:
                return float(str(row.get(key, 0) or 0).replace(",", "."))
            except Exception:
                return 0.0
        rules[model] = {
            "input_per_mtok_rub": num("input_per_mtok_rub"),
            "output_per_mtok_rub": num("output_per_mtok_rub"),
            "image_rub": num("image_rub"),
        }
    return rules


def _rub(value: float) -> float:
    return round(float(value or 0), 4)


def get_token_stats(con: Any) -> dict[str, Any]:
    """Агрегированная статистика по токенам для плашки в UI."""
    try:
        today = __import__("datetime").date.today().isoformat()
        row_today = con.execute(
            "SELECT SUM(prompt_chars) as p, SUM(response_chars) as r, COUNT(*) as n, "
            "COALESCE(SUM(prompt_tokens),0) as pt, COALESCE(SUM(completion_tokens),0) as ct "
            "FROM ai_usage WHERE DATE(created_at) = ?",
            (today,),
        ).fetchone()
        row_total = con.execute(
            "SELECT SUM(prompt_chars) as p, SUM(response_chars) as r, COUNT(*) as n, "
            "COALESCE(SUM(prompt_tokens),0) as pt, COALESCE(SUM(completion_tokens),0) as ct "
            "FROM ai_usage",
        ).fetchone()
        rows_models = con.execute(
            "SELECT provider, model, COUNT(*) as n, COALESCE(SUM(prompt_chars),0) as p, COALESCE(SUM(response_chars),0) as r, "
            "COALESCE(SUM(prompt_tokens),0) as pt, COALESCE(SUM(completion_tokens),0) as ct "
            "FROM ai_usage GROUP BY provider, model ORDER BY n DESC"
        ).fetchall()
        rows_today_models = con.execute(
            "SELECT provider, model, COUNT(*) as n, COALESCE(SUM(prompt_chars),0) as p, COALESCE(SUM(response_chars),0) as r, "
            "COALESCE(SUM(prompt_tokens),0) as pt, COALESCE(SUM(completion_tokens),0) as ct "
            "FROM ai_usage WHERE DATE(created_at) = ? GROUP BY provider, model ORDER BY n DESC",
            (today,),
        ).fetchall()
        rules = _load_ai_price_rules()
        def _safe(row: Any, key: str) -> int:
            try:
                return int(row[key] or 0)
            except Exception:
                return 0
        def _pack(row: Any) -> dict[str, Any]:
            p_chars = _safe(row, "p")
            r_chars = _safe(row, "r")
            req = _safe(row, "n")
            # Реальные токены из ответа API; если их нет (старые записи) — оценка симв/4.
            pt_real = _safe(row, "pt")
            ct_real = _safe(row, "ct")
            in_tokens = pt_real if pt_real > 0 else p_chars // 4
            out_tokens = ct_real if ct_real > 0 else r_chars // 4
            cost = 0.0
            try:
                model_name = str(row["model"] or "")
            except Exception:
                model_name = ""
            used_rule = rules.get(model_name)
            if used_rule:
                cost = (in_tokens / 1_000_000) * used_rule["input_per_mtok_rub"]
                cost += (out_tokens / 1_000_000) * used_rule["output_per_mtok_rub"]
                cost += req * used_rule.get("image_rub", 0.0)
            return {
                "requests": req,
                "prompt_chars": p_chars,
                "response_chars": r_chars,
                "prompt_tokens_approx": in_tokens,
                "response_tokens_approx": out_tokens,
                "tokens_approx": in_tokens + out_tokens,
                "avg_tokens_approx": (in_tokens + out_tokens) // max(1, req),
                "cost_rub": _rub(cost),
                "avg_cost_rub": _rub(cost / max(1, req)),
            }
        # Приблизительно: 1 токен ≈ 4 символа
        today_pack = _pack(row_today)
        total_pack = _pack(row_total)
        models = []
        for r in rows_models:
            pack = _pack(r)
            models.append({
                "provider": r["provider"],
                "model": r["model"],
                **pack,
                "priced": str(r["model"] or "") in rules,
            })
        today_cost = sum(_pack(r)["cost_rub"] for r in rows_today_models)
        total_cost = sum(m["cost_rub"] for m in models)
        today_pack["cost_rub"] = _rub(today_cost)
        today_pack["avg_cost_rub"] = _rub(today_cost / max(1, today_pack["requests"]))
        total_pack["cost_rub"] = _rub(total_cost)
        total_pack["avg_cost_rub"] = _rub(total_cost / max(1, total_pack["requests"]))
        return {
            "today": {**today_pack, "chars": _safe(row_today, "p") + _safe(row_today, "r")},
            "total": {**total_pack, "chars": _safe(row_total, "p") + _safe(row_total, "r")},
            "models": models,
            "pricing_configured": bool(rules),
        }
    except Exception as e:
        return {"error": str(e)}
