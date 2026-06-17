from __future__ import annotations

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Readmail v2"
    app_version: str = "2.0.0"

    database_path: Path = Path("/app/data/readmail.sqlite3")
    buyer_config_dir: Path = Path("/app/config/buyers")

    # --- IMAP ---
    imap_host: str = "imap.yandex.com"
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    imap_folders: str = "INBOX"
    imap_exclude_folders_regex: str = r"(?i)(trash|deleted|spam|junk|draft|чернов|спам|корзин|удален|удалён)"
    imap_search: str = "ALL"
    # Период загрузки по дате: «от» (SINCE) и «до» (BEFORE), каждый по своей галочке.
    imap_date_from_enabled: bool = False
    imap_date_from: str = ""
    imap_date_to_enabled: bool = False
    imap_date_to: str = ""
    imap_batch_size: int = 20
    imap_timeout_seconds: int = 30
    imap_limit: int = 500
    imap_total_limit: int = 5000
    imap_max_raw_email_mb: int = 25
    # Никогда не помечаем письма прочитанными на сервере — только внутренний учёт по message_id
    imap_readonly: bool = True
    store_raw_emails: bool = False
    raw_email_dir: Path = Path("/app/data/raw_emails")
    # --- Import modes (runtime) ---
    processing_mode: str = "manual"
    import_mode: str = "new"
    import_search_query: str = ""
    import_max_attachment_mb: int = 10
    import_download_attachments: bool = True
    import_save_body: bool = True
    import_skip_duplicates: bool = True
    auto_import_enabled: bool = False
    auto_process_enabled: bool = False
    require_confirmation_before_case: bool = True
    require_confirmation_before_outbox: bool = True
    confidence_threshold: float = 0.85
    enable_inbox_sorter: bool = False

    # --- Компания (для определения направления письма) ---
    company_domains: str = ""
    company_emails: str = ""
    internal_forward_markers: str = "fw:,fwd:,пересл:,пересланное сообщение,forwarded message"
    configured_folders_are_customer: bool = True

    # --- Архивация ---
    # Сколько дней хранить полные данные письма (тело + вложения)
    archive_full_days: int = 90
    # После archive_full_days — хранить только JSON-метаданные ещё столько дней
    archive_meta_days: int = 365
    # Автоматически запускать чистку при старте
    archive_auto_cleanup: bool = True

    # --- Минимальный набор полей для 1С (не блокирует экспорт, только warning) ---
    # Минимум: document_number ИЛИ part_number
    export_min_fields_required: bool = True  # false = отправлять всегда без проверки

    # --- Ручное подтверждение ---
    # Таймаут на отмену после двойного подтверждения (секунды)
    confirm_undo_seconds: int = 60

    # --- Сроки возврата по умолчанию (дни от даты УПД) ---
    default_return_deadline_days: int = 60
    # Если превышен срок — только ручное согласование
    overdue_return_manual_only: bool = True

    # --- Флаг эскалации ---
    # Сколько писем подряд без ответа от нас = эскалация
    escalation_unanswered_count: int = 2

    # --- AI общие ---
    enable_ai: bool = True
    # AI-only сборка (v2.1): паттерны полностью отключены, извлечение полей делает только ИИ.
    ai_only: bool = True
    # Провайдер: routerai | gigachat | yandexgpt | openai_compatible
    ai_provider: str = "routerai"
    ai_api_key: str = ""
    ai_model: str = "deepseek/deepseek-v3"
    ai_timeout_seconds: int = 90  # routerai бывает медленным — не рвём рабочие вызовы
    ai_max_chars: int = 6000
    ai_max_output_tokens: int = 2048
    ai_cache_enabled: bool = True
    ai_response_format: str = "json_object"
    ai_context_mode: str = "full_visible"
    ai_test_prompt: str = '{"ok": true, "purpose": "readmail_test"}'
    ai_base_url: str = "http://host.docker.internal:8080/v1"
    ai_endpoint_mode: str = "openai_chat"  # v2.1: routerai/OpenAI-совм. — без пробинга мёртвых путей
    ai_endpoint_path: str = ""
    ai_conserve_tokens: bool = True

    # Vision-модель для изображений и PDF
    ai_vision_enabled: bool = False
    ai_vision_provider: str = "routerai"
    ai_vision_model: str = "qwen/qwen2.5-vl-7b-instruct"
    ai_vision_api_key: str = ""

    # --- RouterAI ---
    routerai_api_key: str = ""
    routerai_base_url: str = "https://routerai.ru/api/v1"
    routerai_default_model: str = "deepseek/deepseek-v3"

    # --- GigaChat ---
    gigachat_enabled: bool = False
    gigachat_auth_key: str = ""
    gigachat_scope: str = "GIGACHAT_API_PERS"
    gigachat_oauth_url: str = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    gigachat_base_url: str = "https://gigachat.devices.sberbank.ru/api/v1"

    # --- Яндекс GPT ---
    yandexgpt_api_key: str = ""
    yandexgpt_folder_id: str = ""
    yandexgpt_base_url: str = "https://llm.api.cloud.yandex.net"

    # --- Счётчик токенов ---
    token_counter_enabled: bool = True
    token_alert_threshold: int = 100000  # предупреждение при превышении за день
    ai_price_rules_json: str = ""

    # --- Парсинг внешних ссылок ---
    # Домены которым доверяем (парсим автоматически)
    trusted_link_domains: str = "avtoto.ru,storage.yandexcloud.net,claim-transfer.parterra.ru,pr-lg.ru,auto-sputnik.ru"
    # Новые неизвестные ссылки — в карантин
    link_quarantine_enabled: bool = True
    # Авто-заход по ссылкам-доказательствам в ядре обработки (условно: свежие + данные за ссылкой).
    auto_fetch_links: bool = True
    # Свежесть: дёргаем сайт только для писем не старше N дней (старые ссылки мертвы).
    link_fetch_fresh_days: int = 2
    # Прокси для захода по ссылкам (per-request, НЕ маршрут всей машины). Пусто = напрямую.
    link_fetch_proxy: str = ""
    # Прокси применяется ТОЛЬКО к этим доменам (avtoto банит дата-центр-IP). Остальное — напрямую.
    link_proxy_domains: str = "avtoto.ru"

    # --- AI автоматика ---
    auto_ai_unknown_buyer: bool = False
    auto_ai_first_unknown_customer: bool = True
    auto_apply_ai_on_first_unknown_customer: bool = False  # ТЗ разд.9: AI решает, не автоприменяем вслепую
    ai_first_unknown_requires_claim_words: bool = True
    auto_apply_ai_validated: bool = False  # ТЗ разд.9: на время отладки — false
    auto_learn_buyer_domains: bool = True
    auto_learn_unknown_buyers: bool = True
    auto_promote_unknown_buyer_after: int = 3
    auto_promote_min_structured: int = 2
    auto_promote_confidence: float = 0.72

    # --- Обучение ---
    learning_min_confirmations: int = 2

    # --- SLA ---
    default_deadline_days: int = 5
    sla_shortage_days: int = 3
    sla_wrong_item_days: int = 3
    sla_incomplete_set_days: int = 3
    sla_nonconforming_days: int = 5
    sla_defect_days: int = 5
    sla_overdelivery_days: int = 3
    sla_quality_refusal_days: int = 3
    sla_correction_request_days: int = 2
    sla_marking_request_days: int = 2
    sla_followup_hours: int = 4
    sla_supplier_decision_hours: int = 4
    sla_warning_hours: int = 24
    sla_critical_hours: int = 0
    sla_overdue_escalate_hours: int = 24

    # --- 1С экспорт ---
    one_c_export_mode: str = "file"  # off | file | http | both
    one_c_file_dir: Path = Path("/app/data/outbox_1c")
    one_c_http_url: str = ""
    one_c_http_token: str = ""
    one_c_http_timeout_seconds: int = 20
    one_c_http_verify_tls: bool = True
    auto_queue_control_events: bool = True
    auto_deliver_outbox: bool = False
    include_context_events_in_1c: bool = True
    # Отправлять продолжения диалогов (followup/reminder) в 1С?
    # False = только материнские new_return, True = + обновления событий
    send_followups_to_1c: bool = False
    # Отправлять напоминалки в 1С?
    send_reminders_to_1c: bool = False
    # --- 1С payload policy (что класть в пакет; по умолчанию чисто) ---
    send_subject_to_1c: bool = True       # тема письма — короткая, безопасна
    send_body_to_1c: bool = False         # полный текст письма НЕ отправлять по умолчанию
    send_links_to_1c: bool = True         # сводка внешних ссылок (без скачивания)
    send_defect_flags_to_1c: bool = True  # флаги документов/фото брака

    # --- Evidence ---
    strict_evidence_validation: bool = False
    return_link_counts_as_evidence: bool = True
    require_photo_proof: bool = False
    require_defect_documents: bool = False
    # Читать документы/фото брака ИИ-зрением (точный флаг) или только по наличию файлов (дёшево).
    defect_doc_ai_read: bool = False
    # Отдельный предохранитель vision для брака. Сам по себе ничего не вызывает:
    # он только разрешает выбранную стратегию обработчику вложений.
    defect_vision_enabled: bool = False
    defect_attachment_strategy: str = "metadata_only"
    max_defect_images_per_case: int = 2
    defect_read_pdf_first: bool = True
    defect_read_images_order: str = "first_last_then_inner"
    defect_send_flags_to_1c: bool = True
    auto_queue_ready_to_outbox: bool = True
    auto_queue_sla_events: bool = True

    # --- 1С JSON: какие секции/поля передавать (отсечь служебный мусор) ---
    one_c_include_meta: bool = False          # confidence/classifier_version/strong_key/deadline — служебное
    one_c_include_processing: bool = False     # source/mode/ai_overlay/manual_gate — служебное
    one_c_include_price: bool = True
    one_c_include_comment: bool = True
    one_c_include_evidence_flags: bool = False  # has_attachments и т.п.
    # Компактный блок defect-флагов в standard payload (has_photos/has_defect_documents/missing…).
    one_c_include_defect_flags: bool = True
    # v2-блоки (несём всё, выключаем ненужное): статусы / тело+тема / вложения / источник.
    one_c_include_status: bool = True
    one_c_include_text: bool = True
    one_c_include_attachments: bool = True
    one_c_include_source: bool = True
    # Профиль payload для 1С: minimal | standard | debug.
    # minimal/standard — для боевой 1С (без trace/field_audit/internal). debug — полный (для аудита).
    one_c_payload_profile: str = "standard"

    # --- Outbox ---
    outbox_deliver_interval_seconds: int = 300
    outbox_retry_after_seconds: int = 300
    outbox_max_attempts: int = 8
    outbox_alert_after_seconds: int = 1800

    # --- Sanity checks ---
    document_number_min_len: int = 5
    document_number_max_len: int = 14
    part_number_min_len: int = 3
    part_number_max_len: int = 50

    # --- UI ---
    scan_interval_seconds: int = 30
    ui_auto_refresh_seconds: int = 15
    ui_show_stats_per_hour: bool = True

    # --- Tunnel / audit ---
    tunnel_public_ai_url: str = ""
    remote_audit_model: str = ""

    # --- Telegram ---
    tg_bot_token: str = ""
    tg_chat_ids: str = ""          # через запятую: 123456789,987654321
    tg_whitelist_enabled: bool = True
    tg_notify_on_cycle: bool = True         # сводка после цикла
    tg_notify_unresolved: bool = True       # неразобранные
    tg_notify_errors: bool = True           # ошибки доставки
    tg_notify_ready: bool = False           # каждое готовое в 1С
    tg_unresolved_min: int = 1             # минимум неразобранных для уведомления
    tg_report_interval_minutes: int = 60   # период сводного отчёта (часовой)
    tg_report_include_reasons: bool = True  # включать разбивку по причинам
    tg_daily_report_enabled: bool = True   # суточный отчёт
    tg_daily_report_hour: int = 9          # час суток (0-23) для суточного отчёта
    tg_daily_report_problems: bool = True  # блок «проблемные» (где система не справилась)
    tg_daily_report_problems_limit: int = 15  # сколько проблемных кейсов перечислять

    # --- Import window (не качать архив раньше даты старта) ---
    import_window_enabled: bool = False
    import_from_datetime: str = ""        # ISO UTC, напр. 2026-06-01T00:00:00+00:00
    skip_before_start: bool = True        # письма раньше границы помечать skipped, не качать

    # --- Server core (LAN web panel) ---
    server_host: str = ""                 # пусто → авто по allow_lan
    server_port: int = 8765
    server_public_base_url: str = ""
    server_allow_lan: bool = False        # безопасный дефолт: только localhost
    server_require_auth: bool = False     # включить перед выставлением в LAN
    server_session_secret: str = ""       # только из env, не в git

    # --- Auth (минимальная модель) ---
    admin_username: str = ""
    admin_password_hash: str = ""         # никогда не plain text

    # --- Developer mode / UI visibility ---
    developer_mode: bool = False

    # --- Worker concurrency / limits ---
    static_workers: int = 2
    import_workers: int = 1
    stage2_workers: int = 2
    ai_text_workers: int = 1
    ai_vision_workers: int = 1
    outbox_workers: int = 1
    ai_max_requests_per_minute: int = 20
    ai_max_cost_per_day: float = 500.0
    ai_max_cost_per_month: float = 5000.0
    vision_max_images_per_case: int = 5
    max_parallel_cases: int = 4
    worker_lock_ttl_seconds: int = 300

    # --- AI pricing (для cost ledger; пусто → unknown_cost=true, система не падает) ---
    ai_pricing_provider: str = "routerai"
    ai_text_input_per_1k: float = 0.0
    ai_text_output_per_1k: float = 0.0
    ai_vision_per_image: float = 0.0
    ai_call_base_price: float = 0.0

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def folders(self) -> list[str]:
        return [p.strip() for p in self.imap_folders.split(",") if p.strip()]

    @property
    def discover_all_folders(self) -> bool:
        return any(f.upper() in {"*", "ALL", "ALL_FOLDERS"} for f in self.folders)

    @property
    def company_domain_list(self) -> list[str]:
        return [p.strip().lower().lstrip("@") for p in self.company_domains.split(",") if p.strip()]

    @property
    def company_email_list(self) -> list[str]:
        return [p.strip().lower() for p in self.company_emails.split(",") if p.strip()]

    @property
    def internal_forward_marker_list(self) -> list[str]:
        return [p.strip().lower() for p in self.internal_forward_markers.split(",") if p.strip()]

    @property
    def trusted_link_domain_list(self) -> list[str]:
        return [p.strip().lower() for p in self.trusted_link_domains.split(",") if p.strip()]


settings = Settings()
