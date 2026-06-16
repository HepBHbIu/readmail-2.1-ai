from __future__ import annotations

from typing import Any

from .config import settings
from .db import get_app_settings, set_app_settings

SETTING_DEFS: list[dict[str, Any]] = [
    {"key":"IMAP_HOST","attr":"imap_host","type":"str","category":"Почта","label":"IMAP host","default":"imap.yandex.com"},
    {"key":"IMAP_PORT","attr":"imap_port","type":"int","category":"Почта","label":"IMAP port","default":993},
    {"key":"IMAP_USERNAME","attr":"imap_username","type":"str","category":"Почта","label":"Email / login","default":""},
    {"key":"IMAP_PASSWORD","attr":"imap_password","type":"secret","category":"Почта","label":"Пароль приложения","default":""},
    {"key":"IMAP_FOLDERS","attr":"imap_folders","type":"str","category":"Почта","label":"Папки через запятую","default":"INBOX"},
    {"key":"IMAP_SEARCH","attr":"imap_search","type":"str","category":"Почта","label":"IMAP search","default":"ALL"},
    {"key":"IMAP_DATE_FROM_ENABLED","attr":"imap_date_from_enabled","type":"bool","category":"Почта","label":"Грузить письма ОТ даты","default":False},
    {"key":"IMAP_DATE_FROM","attr":"imap_date_from","type":"str","category":"Почта","label":"Дата/время ОТ (SINCE)","default":""},
    {"key":"IMAP_DATE_TO_ENABLED","attr":"imap_date_to_enabled","type":"bool","category":"Почта","label":"Грузить письма ДО даты","default":False},
    {"key":"IMAP_DATE_TO","attr":"imap_date_to","type":"str","category":"Почта","label":"Дата/время ДО (BEFORE)","default":""},
    {"key":"IMAP_BATCH_SIZE","attr":"imap_batch_size","type":"int","category":"Почта","label":"UID в одном batch запросе","default":20},
    {"key":"IMAP_LIMIT","attr":"imap_limit","type":"int","category":"Почта","label":"Лимит на папку","default":200},
    {"key":"IMAP_TOTAL_LIMIT","attr":"imap_total_limit","type":"int","category":"Почта","label":"Общий лимит за импорт","default":2000},
    {"key":"SCAN_INTERVAL_SECONDS","attr":"scan_interval_seconds","type":"int","category":"Автоматизация","label":"Автоимпорт каждые N секунд, 0 = выкл","default":0},
    {"key":"CONFIGURED_FOLDERS_ARE_CUSTOMER","attr":"configured_folders_are_customer","type":"bool","category":"Почта","label":"Выбранные папки считаем клиентскими","default":True},
    {"key":"ENABLE_INBOX_SORTER","attr":"enable_inbox_sorter","type":"bool","category":"Обработка","label":"Inbox Sorter (экспериментальный)","default":False},
    {"key":"STORE_RAW_EMAILS","attr":"store_raw_emails","type":"bool","category":"Хранение","label":"Сохранять raw .eml","default":False},
    {"key":"COMPANY_DOMAINS","attr":"company_domains","type":"str","category":"Компания","label":"Домены нашей компании","default":""},
    {"key":"COMPANY_EMAILS","attr":"company_emails","type":"str","category":"Компания","label":"Наши email через запятую","default":""},
    {"key":"DEFAULT_DEADLINE_DAYS","attr":"default_deadline_days","type":"int","category":"SLA","label":"Дедлайн по умолчанию, дней","default":3},
    {"key":"SLA_SHORTAGE_DAYS","attr":"sla_shortage_days","type":"int","category":"SLA","label":"Недовоз: срок, дней","default":3},
    {"key":"SLA_WRONG_ITEM_DAYS","attr":"sla_wrong_item_days","type":"int","category":"SLA","label":"Пересорт: срок, дней","default":3},
    {"key":"SLA_INCOMPLETE_SET_DAYS","attr":"sla_incomplete_set_days","type":"int","category":"SLA","label":"Некомплект: срок, дней","default":3},
    {"key":"SLA_NONCONFORMING_DAYS","attr":"sla_nonconforming_days","type":"int","category":"SLA","label":"Некондиция: срок, дней","default":5},
    {"key":"SLA_DEFECT_DAYS","attr":"sla_defect_days","type":"int","category":"SLA","label":"Брак: срок, дней","default":5},
    {"key":"SLA_QUALITY_REFUSAL_DAYS","attr":"sla_quality_refusal_days","type":"int","category":"SLA","label":"Отказ клиента: срок, дней","default":3},
    {"key":"SLA_FOLLOWUP_HOURS","attr":"sla_followup_hours","type":"int","category":"SLA","label":"Напоминание: реакция, часов","default":4},
    {"key":"SLA_SUPPLIER_DECISION_HOURS","attr":"sla_supplier_decision_hours","type":"int","category":"SLA","label":"Решение/ответ: реакция, часов","default":4},
    {"key":"SLA_WARNING_HOURS","attr":"sla_warning_hours","type":"int","category":"SLA","label":"Предупреждать за N часов","default":24},
    {"key":"SLA_OVERDUE_ESCALATE_HOURS","attr":"sla_overdue_escalate_hours","type":"int","category":"SLA","label":"Просрочка-эскалация через N часов","default":24},
    {"key":"AUTO_QUEUE_SLA_EVENTS","attr":"auto_queue_sla_events","type":"bool","category":"SLA","label":"Автоматически создавать SLA/control events","default":True},
    {"key":"AUTO_LEARN_UNKNOWN_BUYERS","attr":"auto_learn_unknown_buyers","type":"bool","category":"Самообучение","label":"Автоучить новых клиентов","default":True},
    {"key":"AUTO_PROMOTE_UNKNOWN_BUYER_AFTER","attr":"auto_promote_unknown_buyer_after","type":"int","category":"Самообучение","label":"Доверять после N наблюдений","default":3},
    {"key":"AUTO_PROMOTE_MIN_STRUCTURED","attr":"auto_promote_min_structured","type":"int","category":"Самообучение","label":"Минимум структурированных писем","default":2},
    {"key":"AUTO_PROMOTE_CONFIDENCE","attr":"auto_promote_confidence","type":"float","category":"Самообучение","label":"Порог доверия","default":0.72},
    {"key":"ENABLE_AI","attr":"enable_ai","type":"bool","category":"AI / MLX","label":"Включить AI","default":False},
    {"key":"AI_PROVIDER","attr":"ai_provider","type":"select","options":["routerai","openai_compatible","gigachat","yandexgpt"],"category":"AI / MLX","label":"AI provider","default":"openai_compatible"},
    {"key":"AI_BASE_URL","attr":"ai_base_url","type":"str","category":"AI / MLX","label":"OpenAI-compatible URL","default":"http://host.docker.internal:8080/v1"},
    {"key":"AI_API_KEY","attr":"ai_api_key","type":"secret","category":"AI / MLX","label":"AI API key","default":"local"},
    {"key":"AI_MODEL","attr":"ai_model","type":"str","category":"AI / MLX","label":"AI model","default":"auto"},
    {"key":"AI_TIMEOUT_SECONDS","attr":"ai_timeout_seconds","type":"int","category":"AI / MLX","label":"AI timeout, сек","default":90},
    {"key":"AI_ENDPOINT_MODE","attr":"ai_endpoint_mode","type":"select","options":["openai_chat","auto","openai_completion","ollama_chat","ollama_generate","simple_generate"],"category":"AI / MLX","label":"Endpoint mode (openai_chat — без пробинга, быстро)","default":"openai_chat"},
    {"key":"AI_ENDPOINT_PATH","attr":"ai_endpoint_path","type":"str","category":"AI / MLX","label":"Endpoint path, если не OpenAI","default":""},
    {"key":"AI_RESPONSE_FORMAT","attr":"ai_response_format","type":"select","options":["none","json_object"],"category":"AI / MLX","label":"Response format","default":"json_object"},
    {"key":"AI_CONTEXT_MODE","attr":"ai_context_mode","type":"select","options":["visible_top","full_visible"],"category":"AI / MLX","label":"AI контекст","default":"visible_top"},
    {"key":"AI_MAX_CHARS","attr":"ai_max_chars","type":"int","category":"AI / MLX","label":"Максимум символов письма","default":6000},
    {"key":"AI_MAX_OUTPUT_TOKENS","attr":"ai_max_output_tokens","type":"int","category":"AI / MLX","label":"Максимум output токенов","default":900},
    {"key":"AI_CACHE_ENABLED","attr":"ai_cache_enabled","type":"bool","category":"AI / MLX","label":"Кэшировать AI ответы","default":True},
    {"key":"AI_CONSERVE_TOKENS","attr":"ai_conserve_tokens","type":"bool","category":"AI / MLX","label":"Экономить токены","default":True},
    {"key":"AI_PRICE_RULES_JSON","attr":"ai_price_rules_json","type":"str","category":"AI / MLX","label":"Цены моделей AI, JSON","default":""},
    {"key":"AI_ONLY","attr":"ai_only","type":"bool","category":"AI / MLX","label":"AI-only: паттерны отключены, поля только ИИ (v2.1)","default":True},
    {"key":"AI_VISION_ENABLED","attr":"ai_vision_enabled","type":"bool","category":"AI / MLX","label":"Включить Vision AI","default":False},
    {"key":"AI_VISION_PROVIDER","attr":"ai_vision_provider","type":"select","options":["routerai","openai_compatible","gigachat","yandexgpt"],"category":"AI / MLX","label":"Vision AI provider","default":"routerai"},
    {"key":"AI_VISION_MODEL","attr":"ai_vision_model","type":"str","category":"AI / MLX","label":"Vision AI модель","default":"qwen/qwen2.5-vl-7b-instruct"},
    {"key":"AI_VISION_API_KEY","attr":"ai_vision_api_key","type":"secret","category":"AI / MLX","label":"Vision API key","default":""},
    {"key":"AUTO_AI_FIRST_UNKNOWN_CUSTOMER","attr":"auto_ai_first_unknown_customer","type":"bool","category":"AI / MLX","label":"AI сразу для первого письма нового клиента","default":False},
    {"key":"AUTO_APPLY_AI_ON_FIRST_UNKNOWN_CUSTOMER","attr":"auto_apply_ai_on_first_unknown_customer","type":"bool","category":"AI / MLX","label":"Автоприменять AI первого клиента через validator","default":False},
    {"key":"AUTO_APPLY_AI_VALIDATED","attr":"auto_apply_ai_validated","type":"bool","category":"AI / MLX","label":"Автоприменять AI, если прошёл validator","default":False},
    {"key":"YANDEXGPT_FOLDER_ID","attr":"yandexgpt_folder_id","type":"str","category":"AI / YandexGPT","label":"YandexGPT folder ID","default":""},
    {"key":"YANDEXGPT_API_KEY","attr":"yandexgpt_api_key","type":"secret","category":"AI / YandexGPT","label":"YandexGPT API key","default":""},
    {"key":"YANDEXGPT_BASE_URL","attr":"yandexgpt_base_url","type":"str","category":"AI / YandexGPT","label":"YandexGPT base URL","default":"https://llm.api.cloud.yandex.net"},
    {"key":"GIGACHAT_AUTH_KEY","attr":"gigachat_auth_key","type":"secret","category":"AI / GigaChat","label":"GigaChat Authorization key","default":""},
    {"key":"GIGACHAT_SCOPE","attr":"gigachat_scope","type":"str","category":"AI / GigaChat","label":"GigaChat scope","default":"GIGACHAT_API_PERS"},
    {"key":"GIGACHAT_BASE_URL","attr":"gigachat_base_url","type":"str","category":"AI / GigaChat","label":"GigaChat base URL","default":"https://gigachat.devices.sberbank.ru/api/v1"},
    {"key":"TRUSTED_LINK_DOMAINS","attr":"trusted_link_domains","type":"str","category":"Ссылки","label":"Доверенные домены для ссылок","default":"avtoto.ru,storage.yandexcloud.net,claim-transfer.parterra.ru,pr-lg.ru,auto-sputnik.ru"},
    {"key":"LINK_QUARANTINE_ENABLED","attr":"link_quarantine_enabled","type":"bool","category":"Ссылки","label":"Карантин для новых ссылок","default":True},
    {"key":"TUNNEL_PUBLIC_AI_URL","attr":"tunnel_public_ai_url","type":"str","category":"Туннель","label":"Публичный URL к локальной модели","default":""},
    {"key":"REMOTE_AUDIT_MODEL","attr":"remote_audit_model","type":"str","category":"Аудит","label":"Модель для внешнего аудита","default":""},
    {"key":"STRICT_EVIDENCE_VALIDATION","attr":"strict_evidence_validation","type":"bool","category":"Доказательства / 1С","label":"Строгая проверка документов/фото","default":True},
    {"key":"RETURN_LINK_COUNTS_AS_EVIDENCE","attr":"return_link_counts_as_evidence","type":"bool","category":"Доказательства / 1С","label":"Возвратная ссылка считается доказательством","default":True},
    {"key":"REQUIRE_PHOTO_PROOF","attr":"require_photo_proof","type":"bool","category":"Доказательства / 1С","label":"Требовать фото там, где это нужно","default":True},
    {"key":"REQUIRE_DEFECT_DOCUMENTS","attr":"require_defect_documents","type":"bool","category":"Доказательства / 1С","label":"Для брака требовать акт/заказ-наряд/заключение","default":True},
    {"key":"DEFECT_DOC_AI_READ","attr":"defect_doc_ai_read","type":"bool","category":"Доказательства / 1С","label":"Читать документы/фото брака ИИ (точный флаг). Выкл = только наличие файлов","default":False},
    {"key":"DEFECT_VISION_ENABLED","attr":"defect_vision_enabled","type":"bool","category":"Доказательства / 1С","label":"Разрешить Vision для вложений брака","default":False},
    {"key":"DEFECT_ATTACHMENT_STRATEGY","attr":"defect_attachment_strategy","type":"select","options":["metadata_only","pdf_first","pdf_then_images","full_vision"],"category":"Доказательства / 1С","label":"Стратегия чтения вложений брака","default":"metadata_only"},
    {"key":"MAX_DEFECT_IMAGES_PER_CASE","attr":"max_defect_images_per_case","type":"int","category":"Доказательства / 1С","label":"Максимум изображений брака на кейс","default":2},
    {"key":"DEFECT_READ_PDF_FIRST","attr":"defect_read_pdf_first","type":"bool","category":"Доказательства / 1С","label":"Сначала читать PDF/документы","default":True},
    {"key":"DEFECT_READ_IMAGES_ORDER","attr":"defect_read_images_order","type":"select","options":["first_last_then_inner","first_then_inner","in_attachment_order"],"category":"Доказательства / 1С","label":"Порядок чтения изображений","default":"first_last_then_inner"},
    {"key":"DEFECT_SEND_FLAGS_TO_1C","attr":"defect_send_flags_to_1c","type":"bool","category":"Доказательства / 1С","label":"Передавать defect-флаги в payload 1С","default":True},
    {"key":"AUTO_QUEUE_READY_TO_OUTBOX","attr":"auto_queue_ready_to_outbox","type":"bool","category":"1С / Outbox","label":"Автоматически класть ready в outbox","default":False},
    {"key":"ONE_C_EXPORT_MODE","attr":"one_c_export_mode","type":"select","options":["off","local_receiver","file","http","both"],"category":"1С / Outbox","label":"Режим интеграции 1С (local_receiver — локальный приёмник, без внешней 1С)","default":"file"},
    {"key":"ONE_C_FILE_DIR","attr":"one_c_file_dir","type":"str","category":"1С / Outbox","label":"Папка JSON-файлов для 1С","default":"/app/data/outbox_1c"},
    {"key":"ONE_C_HTTP_URL","attr":"one_c_http_url","type":"str","category":"1С / Outbox","label":"HTTP endpoint 1С","default":""},
    {"key":"ONE_C_HTTP_TOKEN","attr":"one_c_http_token","type":"secret","category":"1С / Outbox","label":"HTTP token / Bearer","default":""},
    {"key":"ONE_C_HTTP_TIMEOUT_SECONDS","attr":"one_c_http_timeout_seconds","type":"int","category":"1С / Outbox","label":"HTTP timeout, сек","default":20},
    {"key":"ONE_C_HTTP_VERIFY_TLS","attr":"one_c_http_verify_tls","type":"bool","category":"1С / Outbox","label":"Проверять TLS сертификат","default":True},
    {"key":"OUTBOX_DELIVER_INTERVAL_SECONDS","attr":"outbox_deliver_interval_seconds","type":"int","category":"1С / Outbox","label":"Интервал автодоставки outbox (сек)","default":300},
    {"key":"AUTO_QUEUE_CONTROL_EVENTS","attr":"auto_queue_control_events","type":"bool","category":"1С / Outbox","label":"Класть в outbox все контрольные события","default":True},
    {"key":"ONE_C_INCLUDE_PRICE","attr":"one_c_include_price","type":"bool","category":"1С / Поля JSON","label":"Передавать цену позиции","default":True},
    {"key":"ONE_C_INCLUDE_COMMENT","attr":"one_c_include_comment","type":"bool","category":"1С / Поля JSON","label":"Передавать комментарий/причину текстом","default":True},
    {"key":"ONE_C_INCLUDE_META","attr":"one_c_include_meta","type":"bool","category":"1С / Поля JSON","label":"Передавать служебное meta (confidence, версия, strong_key)","default":False},
    {"key":"ONE_C_INCLUDE_PROCESSING","attr":"one_c_include_processing","type":"bool","category":"1С / Поля JSON","label":"Передавать служебное processing (mode, ai_overlay)","default":False},
    {"key":"ONE_C_INCLUDE_EVIDENCE_FLAGS","attr":"one_c_include_evidence_flags","type":"bool","category":"1С / Поля JSON","label":"Передавать флаги вложений/доказательств","default":False},
    {"key":"ONE_C_INCLUDE_DEFECT_FLAGS","attr":"one_c_include_defect_flags","type":"bool","category":"1С / Поля JSON","label":"Передавать флаги (документы брака, фото/документы)","default":True},
    {"key":"ONE_C_INCLUDE_STATUS","attr":"one_c_include_status","type":"bool","category":"1С / Поля JSON","label":"Передавать статусы (состояние/контроль/приоритет)","default":True},
    {"key":"ONE_C_INCLUDE_TEXT","attr":"one_c_include_text","type":"bool","category":"1С / Поля JSON","label":"Передавать тело и тему письма","default":True},
    {"key":"ONE_C_INCLUDE_ATTACHMENTS","attr":"one_c_include_attachments","type":"bool","category":"1С / Поля JSON","label":"Передавать описание вложений (имя/тип/размер)","default":True},
    {"key":"ONE_C_INCLUDE_SOURCE","attr":"one_c_include_source","type":"bool","category":"1С / Поля JSON","label":"Передавать источник (raw_email_id, message_id, дата)","default":True},
    {"key":"AUTO_DELIVER_OUTBOX","attr":"auto_deliver_outbox","type":"bool","category":"1С / Outbox","label":"Автодоставка outbox после импорта","default":False},
    {"key":"INCLUDE_CONTEXT_EVENTS_IN_1C","attr":"include_context_events_in_1c","type":"bool","category":"1С / Outbox","label":"Отправлять в 1С диалоги/напоминания/контекст","default":True},
    {"key":"SEND_FOLLOWUPS_TO_1C","attr":"send_followups_to_1c","type":"bool","category":"1С / Outbox","label":"Продолжения диалогов → 1С (иначе только new_return)","default":False},
    {"key":"OUTBOX_RETRY_AFTER_SECONDS","attr":"outbox_retry_after_seconds","type":"int","category":"1С / Надёжность","label":"Повтор доставки через N секунд","default":300},
    {"key":"OUTBOX_MAX_ATTEMPTS","attr":"outbox_max_attempts","type":"int","category":"1С / Надёжность","label":"Максимум попыток доставки","default":8},
    {"key":"OUTBOX_ALERT_AFTER_SECONDS","attr":"outbox_alert_after_seconds","type":"int","category":"1С / Надёжность","label":"Считать неотправленное тревогой через N секунд","default":1800},
    {"key":"PROCESSING_MODE","attr":"processing_mode","type":"select","options":["manual","semiauto","auto","auto_trust"],"category":"Обработка","label":"Режим обработки","default":"manual"},
    {"key":"IMPORT_MODE","attr":"import_mode","type":"select","options":["new","unseen","all","search"],"category":"Почта","label":"Режим импорта","default":"new"},
    {"key":"IMPORT_SEARCH_QUERY","attr":"import_search_query","type":"str","category":"Почта","label":"IMAP поисковый запрос","default":""},
    {"key":"IMPORT_MAX_ATTACHMENT_MB","attr":"import_max_attachment_mb","type":"int","category":"Почта","label":"Макс. размер вложения MB","default":10},
    {"key":"IMAP_TIMEOUT_SECONDS","attr":"imap_timeout_seconds","type":"int","category":"Почта","label":"IMAP таймаут (сек)","default":30},
    {"key":"IMAP_MAX_RAW_EMAIL_MB","attr":"imap_max_raw_email_mb","type":"int","category":"Почта","label":"Макс. размер письма в MB (oversized)","default":25},
    {"key":"IMPORT_DOWNLOAD_ATTACHMENTS","attr":"import_download_attachments","type":"bool","category":"Почта","label":"Загружать вложения","default":True},
    {"key":"IMPORT_SAVE_BODY","attr":"import_save_body","type":"bool","category":"Почта","label":"Сохранять тело письма","default":True},
    {"key":"IMPORT_SKIP_DUPLICATES","attr":"import_skip_duplicates","type":"bool","category":"Почта","label":"Пропускать дубликаты","default":True},
    {"key":"AUTO_IMPORT_ENABLED","attr":"auto_import_enabled","type":"bool","category":"Автоматизация","label":"Автоимпорт включён","default":False},
    {"key":"AUTO_PROCESS_ENABLED","attr":"auto_process_enabled","type":"bool","category":"Автоматизация","label":"Автообработка AI включена","default":False},
    {"key":"REQUIRE_CONFIRMATION_BEFORE_CASE","attr":"require_confirmation_before_case","type":"bool","category":"Обработка","label":"Требовать подтверждения кейса","default":True},
    {"key":"REQUIRE_CONFIRMATION_BEFORE_OUTBOX","attr":"require_confirmation_before_outbox","type":"bool","category":"Обработка","label":"Требовать подтверждения outbox","default":True},
    {"key":"CONFIDENCE_THRESHOLD","attr":"confidence_threshold","type":"float","category":"Обработка","label":"Порог уверенности","default":0.85},
    # Telegram
    {"key":"TG_BOT_TOKEN","attr":"tg_bot_token","type":"secret","category":"Telegram","label":"Bot Token","default":""},
    {"key":"TG_CHAT_IDS","attr":"tg_chat_ids","type":"str","category":"Telegram","label":"Chat IDs (через запятую)","default":""},
    {"key":"TG_WHITELIST_ENABLED","attr":"tg_whitelist_enabled","type":"bool","category":"Telegram","label":"Белый список чатов","default":True},
    {"key":"TG_NOTIFY_ON_CYCLE","attr":"tg_notify_on_cycle","type":"bool","category":"Telegram","label":"Итоги цикла","default":True},
    {"key":"TG_NOTIFY_UNRESOLVED","attr":"tg_notify_unresolved","type":"bool","category":"Telegram","label":"Неразобранные","default":True},
    {"key":"TG_NOTIFY_ERRORS","attr":"tg_notify_errors","type":"bool","category":"Telegram","label":"Ошибки 1С","default":True},
    {"key":"TG_NOTIFY_READY","attr":"tg_notify_ready","type":"bool","category":"Telegram","label":"Каждый готовый кейс","default":False},
    {"key":"TG_UNRESOLVED_MIN","attr":"tg_unresolved_min","type":"int","category":"Telegram","label":"Мин. неразобранных для уведомления","default":1},
    {"key":"TG_REPORT_INTERVAL_MINUTES","attr":"tg_report_interval_minutes","type":"int","category":"Telegram","label":"Таймер часового отчёта, минут","default":60},
    {"key":"TG_REPORT_INCLUDE_REASONS","attr":"tg_report_include_reasons","type":"bool","category":"Telegram","label":"Показывать причины в отчёте","default":True},
    {"key":"TG_DAILY_REPORT_ENABLED","attr":"tg_daily_report_enabled","type":"bool","category":"Telegram","label":"Суточный отчёт","default":True},
    {"key":"TG_DAILY_REPORT_HOUR","attr":"tg_daily_report_hour","type":"int","category":"Telegram","label":"Час суточного отчёта (0-23)","default":9},
    {"key":"TG_DAILY_REPORT_PROBLEMS","attr":"tg_daily_report_problems","type":"bool","category":"Telegram","label":"Блок «проблемные» (где система не справилась)","default":True},
    {"key":"TG_DAILY_REPORT_PROBLEMS_LIMIT","attr":"tg_daily_report_problems_limit","type":"int","category":"Telegram","label":"Сколько проблемных перечислять","default":15},
    # --- Import window ---
    {"key":"IMPORT_WINDOW_ENABLED","attr":"import_window_enabled","type":"bool","category":"Почта","label":"Ограничить импорт датой старта","default":False},
    {"key":"IMPORT_FROM_DATETIME","attr":"import_from_datetime","type":"str","category":"Почта","label":"Импортировать письма с (ISO UTC)","default":""},
    {"key":"SKIP_BEFORE_START","attr":"skip_before_start","type":"bool","category":"Почта","label":"Письма раньше границы помечать skipped","default":True},
    # --- Server / auth / developer (runtime) ---
    {"key":"SERVER_ALLOW_LAN","attr":"server_allow_lan","type":"bool","category":"Сервер","label":"Доступ из локальной сети (LAN)","default":False},
    {"key":"SERVER_REQUIRE_AUTH","attr":"server_require_auth","type":"bool","category":"Сервер","label":"Требовать вход (auth)","default":False},
    {"key":"DEVELOPER_MODE","attr":"developer_mode","type":"bool","category":"Сервер","label":"Developer mode (инженерные вкладки)","default":False},
]

_BY_KEY = {d["key"]: d for d in SETTING_DEFS}
_BY_ATTR = {d["attr"]: d for d in SETTING_DEFS}


def _cast_value(defn: dict[str, Any], value: Any) -> Any:
    typ = defn.get("type")
    if typ == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1","true","yes","y","on","да","вкл"}
    if typ == "int":
        try:
            return int(value)
        except Exception:
            return int(defn.get("default") or 0)
    if typ == "float":
        try:
            return float(value)
        except Exception:
            return float(defn.get("default") or 0.0)
    if typ == "select":
        val = str(value or defn.get("default") or "").strip()
        return val if val in (defn.get("options") or []) else defn.get("default")
    return "" if value is None else str(value)


def apply_runtime_settings() -> dict[str, Any]:
    """Load persisted UI settings from SQLite and mutate the pydantic settings object.

    This keeps .env as a bootstrap fallback, while the operator controls runtime from the panel.
    """
    stored = get_app_settings()
    applied: dict[str, Any] = {}
    for defn in SETTING_DEFS:
        key = defn["key"]
        if key not in stored:
            continue
        value = _cast_value(defn, stored[key])
        setattr(settings, defn["attr"], value)
        applied[key] = value
    return applied



def get_settings_payload(mask_secrets: bool = True) -> dict[str, Any]:
    stored = get_app_settings()
    apply_runtime_settings()
    items = []
    # Build a flat dict keyed by attr name so the frontend can read s.imap_host etc.
    flat: dict[str, Any] = {}
    for defn in SETTING_DEFS:
        value = getattr(settings, defn["attr"], defn.get("default"))
        configured = bool(value) if defn.get("type") == "secret" else None
        item = {k: v for k, v in defn.items() if k != "attr"}
        if defn.get("type") == "secret" and mask_secrets:
            item["value"] = ""
            item["configured"] = configured
            item["placeholder"] = "сохранено" if configured else "не задано"
            # For secrets, if configured mark with special sentinel so frontend knows
            flat[defn["attr"]] = "__configured__" if configured else ""
        else:
            item["value"] = value
            flat[defn["attr"]] = value
        item["source"] = "panel" if defn["key"] in stored else "env/default"
        items.append(item)
    # Also add non-SETTING_DEFS attrs that the frontend reads
    for extra_attr in ("archive_full_days", "archive_meta_days", "default_return_deadline_days",
                       "overdue_return_manual_only", "escalation_unanswered_count",
                       "send_reminders_to_1c", "ui_auto_refresh_seconds"):
        if extra_attr not in flat:
            flat[extra_attr] = getattr(settings, extra_attr, None)
    return {"ok": True, "items": items, "settings": flat}


def update_settings_from_panel(values: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    current = get_app_settings()
    for key, value in (values or {}).items():
        # Accept both uppercase KEY (IMAP_HOST) and lowercase attr (imap_host) from the frontend
        if key in _BY_KEY:
            defn = _BY_KEY[key]
        elif key in _BY_ATTR:
            defn = _BY_ATTR[key]
            key = defn["key"]  # normalize to uppercase for DB storage
        else:
            continue
        if defn.get("type") == "secret" and value in (None, "", "********", "••••••••"):
            # Empty secret means preserve existing value.
            if key in current:
                continue
            # No persisted secret: do not overwrite env/default with blank unless explicitly __CLEAR__.
            continue
        if value == "__CLEAR__":
            clean[key] = ""
        else:
            clean[key] = _cast_value(defn, value)
    if clean:
        set_app_settings(clean)
    applied = apply_runtime_settings()
    return {"ok": True, "updated": sorted(clean.keys()), "applied": sorted(applied.keys())}
