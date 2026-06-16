from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterator

from .config import settings
from .buyer_evidence import build_buyer_evidence
from .evidence_repair import repair_evidence
from .quality_gate import quality_gate, write_quality_artifacts

SCHEMA_VERSION = 14


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value: str | bytes | None, default: Any = None) -> Any:
    if value in (None, "", b""):
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for key in (
        "fields_json", "missing_json", "quality_json", "payload_json", "export_json",
        "references_json", "folder_seen_json", "sample_subjects_json", "field_stats_json",
        "inferred_json", "evidence_json", "response_json", "delivery_response_json", "summary_json", "details_json",
    ):
        if key in d:
            if key in {"references_json", "missing_json", "quality_json", "folder_seen_json", "sample_subjects_json"}:
                default: Any = []
            else:
                default = {}
            d[key.replace("_json", "")] = loads(d.pop(key), default)
    return d


def _ensure_journal_mode(path: Path) -> None:
    """Перевести БД из WAL в TRUNCATE один раз, вне транзакции. Безопасно вызывать многократно.

    WAL требует shared-memory файл `-shm` (mmap), который ненадёжен на bind-mount Docker
    Desktop (gRPC-FUSE) → плавающие `disk I/O error`. TRUNCATE/DELETE такого файла не требует.
    Выход из WAL персистентен (пишется в заголовок БД), сам режим TRUNCATE ставим на каждое
    соединение в connect().
    """
    try:
        c = sqlite3.connect(str(path), timeout=10.0)
        c.isolation_level = None  # autocommit — journal_mode pragma должен идти вне транзакции
        c.execute("PRAGMA journal_mode=TRUNCATE")
        c.close()
    except Exception:
        pass


# Транзиентные ошибки SQLite на сетевых/FUSE-ФС (Docker Desktop): повторяем подключение.
_TRANSIENT_DB_ERRORS = ("disk i/o error", "database is locked", "database is busy")


def _open_connection() -> sqlite3.Connection:
    con = sqlite3.connect(str(settings.database_path), timeout=60.0, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=60000")
    con.execute("PRAGMA journal_mode=TRUNCATE")  # FUSE-safe: без -shm, иначе WAL даёт disk I/O error
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA cache_size=-8000")  # 8 MB page cache
    return con


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    con: sqlite3.Connection | None = None
    last_exc: Exception | None = None
    for attempt in range(5):
        try:
            con = _open_connection()
            break
        except sqlite3.OperationalError as exc:
            last_exc = exc
            if any(s in str(exc).lower() for s in _TRANSIENT_DB_ERRORS):
                try:
                    if con is not None:
                        con.close()
                except Exception:
                    pass
                con = None
                time.sleep(0.2 * (attempt + 1))
                continue
            raise
    if con is None:
        raise last_exc if last_exc is not None else sqlite3.OperationalError("не удалось подключиться к БД")
    try:
        yield con
        con.commit()
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        raise
    finally:
        con.close()


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r["name"]) for r in con.execute(f"PRAGMA table_info({table})")}
    except Exception:
        return set()


def _add_column(con: sqlite3.Connection, table: str, name: str, ddl: str) -> None:
    if name not in _columns(con, table):
        con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def migrate_raw_emails_uidvalidity(con: sqlite3.Connection) -> bool:
    """Идемпотентно перевести raw_emails на identity (mailbox, uid, uidvalidity).

    Снимает inline UNIQUE(mailbox, uid) (нельзя ALTER в SQLite → rebuild) и ставит
    UNIQUE INDEX(mailbox, uid, uidvalidity). Существующие строки получают uidvalidity=''
    (чтобы не дублировались). Безопасно при фоновых читателях; писатели должны быть на паузе
    (на старте приложения автопилот ещё не запущен). Возвращает True, если перестроила.
    """
    row = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='raw_emails'").fetchone()
    if not row or not row[0]:
        return False
    sql = str(row[0])
    needs = ("UNIQUE(mailbox, uid)" in sql) or ("UNIQUE (mailbox, uid)" in sql) or \
            ("UNIQUE(mailbox,uid)" in sql)
    if not needs:
        # уже без inline UNIQUE — просто гарантируем индекс идентичности
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_identity ON raw_emails(mailbox, uid, uidvalidity)")
        return False
    info = con.execute("PRAGMA table_info(raw_emails)").fetchall()
    if "uidvalidity" not in {str(c["name"]) for c in info}:
        con.execute("ALTER TABLE raw_emails ADD COLUMN uidvalidity TEXT")
        info = con.execute("PRAGMA table_info(raw_emails)").fetchall()
    coldefs: list[str] = []
    cols: list[str] = []
    for c in info:
        name = str(c["name"]); typ = str(c["type"] or "TEXT")
        cols.append(name)
        if int(c["pk"] or 0) == 1 and "INT" in typ.upper():
            coldefs.append(f"{name} INTEGER PRIMARY KEY AUTOINCREMENT")
            continue
        d = f"{name} {typ}"
        if int(c["notnull"] or 0):
            d += " NOT NULL"
        if c["dflt_value"] is not None:
            d += f" DEFAULT {c['dflt_value']}"
        coldefs.append(d)
    col_list = ", ".join(cols)
    select_list = ", ".join(("COALESCE(uidvalidity,'')" if cn == "uidvalidity" else cn) for cn in cols)
    con.execute("DROP TABLE IF EXISTS raw_emails_mig")
    con.execute(f"CREATE TABLE raw_emails_mig ({', '.join(coldefs)})")
    con.execute(f"INSERT INTO raw_emails_mig ({col_list}) SELECT {select_list} FROM raw_emails")
    con.execute("DROP TABLE raw_emails")
    con.execute("ALTER TABLE raw_emails_mig RENAME TO raw_emails")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_identity ON raw_emails(mailbox, uid, uidvalidity)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_raw_message_id ON raw_emails(message_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_raw_canonical_key ON raw_emails(canonical_key)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_raw_hash ON raw_emails(raw_hash)")
    return True


def init_db() -> None:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_journal_mode(settings.database_path)
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );


            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS raw_emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mailbox TEXT NOT NULL,
                uid TEXT NOT NULL,
                folder_seen_json TEXT DEFAULT '[]',
                canonical_key TEXT,
                duplicate_of_raw_email_id INTEGER,
                direction TEXT,
                message_id TEXT,
                in_reply_to TEXT,
                references_json TEXT DEFAULT '[]',
                subject TEXT,
                from_addr TEXT,
                to_addr TEXT,
                cc_addr TEXT,
                received_at TEXT,
                body_text TEXT,
                body_html TEXT,
                snippet TEXT,
                raw_hash TEXT,
                raw_path TEXT,
                quote_markers INTEGER DEFAULT 0,
                archived_body INTEGER DEFAULT 0,
                archived_meta INTEGER DEFAULT 0,
                raw_size INTEGER DEFAULT 0,
                status TEXT DEFAULT 'imported',
                imported_at TEXT NOT NULL,
                updated_at TEXT,
                UNIQUE(mailbox, uid)
            );

            CREATE INDEX IF NOT EXISTS idx_raw_message_id ON raw_emails(message_id);
            CREATE INDEX IF NOT EXISTS idx_raw_canonical_key ON raw_emails(canonical_key);
            CREATE INDEX IF NOT EXISTS idx_raw_direction ON raw_emails(direction);
            CREATE INDEX IF NOT EXISTS idx_raw_received_at ON raw_emails(received_at);
            CREATE INDEX IF NOT EXISTS idx_raw_hash ON raw_emails(raw_hash);

            CREATE TABLE IF NOT EXISTS attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                raw_email_id INTEGER NOT NULL REFERENCES raw_emails(id) ON DELETE CASCADE,
                filename TEXT,
                content_type TEXT,
                size_bytes INTEGER
            );

            CREATE TABLE IF NOT EXISTS cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                raw_email_id INTEGER NOT NULL REFERENCES raw_emails(id) ON DELETE CASCADE,
                buyer_code TEXT,
                buyer_name TEXT,
                event_type TEXT NOT NULL,
                claim_kind TEXT,
                status TEXT,
                priority TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0,
                deadline_at TEXT,
                thread_key TEXT,
                strong_key TEXT,
                weak_key TEXT,
                is_followup INTEGER NOT NULL DEFAULT 0,
                ready_for_export INTEGER NOT NULL DEFAULT 0,
                needs_review INTEGER NOT NULL DEFAULT 1,
                state TEXT NOT NULL,
                fields_json TEXT NOT NULL DEFAULT '{}',
                missing_json TEXT NOT NULL DEFAULT '[]',
                quality_json TEXT NOT NULL DEFAULT '[]',
                payload_json TEXT NOT NULL DEFAULT '{}',
                export_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                outbox_validated INTEGER NOT NULL DEFAULT 0,
                needs_ai INTEGER NOT NULL DEFAULT 0,
                has_min_fields INTEGER NOT NULL DEFAULT 0,
                reminder_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_cases_state ON cases(state);
            CREATE INDEX IF NOT EXISTS idx_cases_priority ON cases(priority);
            CREATE INDEX IF NOT EXISTS idx_cases_deadline ON cases(deadline_at);
            CREATE INDEX IF NOT EXISTS idx_cases_raw_email_id ON cases(raw_email_id);
            CREATE INDEX IF NOT EXISTS idx_cases_buyer_code ON cases(buyer_code);
            CREATE INDEX IF NOT EXISTS idx_cases_event_state ON cases(event_type, state);
            CREATE INDEX IF NOT EXISTS idx_cases_strong_key ON cases(strong_key);
            CREATE INDEX IF NOT EXISTS idx_cases_thread_key ON cases(thread_key);

            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                created_at TEXT NOT NULL,
                sent_at TEXT,
                event_type TEXT NOT NULL DEFAULT 'return_ready',
                event_key TEXT,
                channel TEXT NOT NULL DEFAULT 'file',
                business_priority TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                next_attempt_at TEXT,
                last_attempt_at TEXT,
                file_path TEXT,
                delivery_response_json TEXT NOT NULL DEFAULT '{}',
                resolved_at TEXT,
                resolution_note TEXT
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_event_key ON outbox(event_key);
            CREATE INDEX IF NOT EXISTS idx_outbox_status_channel ON outbox(status, channel);
            CREATE INDEX IF NOT EXISTS idx_outbox_case_event ON outbox(case_id, event_type);

            CREATE TABLE IF NOT EXISTS outbox_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                outbox_id INTEGER NOT NULL REFERENCES outbox(id) ON DELETE CASCADE,
                case_id INTEGER,
                event_type TEXT,
                channel TEXT,
                attempt_no INTEGER NOT NULL DEFAULT 1,
                ok INTEGER NOT NULL DEFAULT 0,
                status_code INTEGER,
                error TEXT,
                response_json TEXT NOT NULL DEFAULT '{}',
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_outbox_attempts_outbox ON outbox_attempts(outbox_id);
            CREATE INDEX IF NOT EXISTS idx_outbox_attempts_ok ON outbox_attempts(ok, finished_at);

            CREATE TABLE IF NOT EXISTS process_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stage TEXT NOT NULL,
                level TEXT NOT NULL DEFAULT 'info',
                message TEXT NOT NULL,
                case_id INTEGER,
                raw_email_id INTEGER,
                subject TEXT,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_process_events_created ON process_events(created_at);
            CREATE INDEX IF NOT EXISTS idx_process_events_stage ON process_events(stage, created_at);
            CREATE INDEX IF NOT EXISTS idx_process_events_case ON process_events(case_id, created_at);

            CREATE TABLE IF NOT EXISTS test_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL DEFAULT 'running',
                params_json TEXT NOT NULL DEFAULT '{}',
                before_json TEXT NOT NULL DEFAULT '{}',
                after_json TEXT NOT NULL DEFAULT '{}',
                import_result_json TEXT NOT NULL DEFAULT '{}',
                queue_result_json TEXT NOT NULL DEFAULT '{}',
                delivery_result_json TEXT NOT NULL DEFAULT '{}',
                summary_json TEXT NOT NULL DEFAULT '{}',
                error TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_test_runs_started ON test_runs(started_at);
            CREATE INDEX IF NOT EXISTS idx_test_runs_status ON test_runs(status);


            CREATE TABLE IF NOT EXISTS buyer_identities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                identity_type TEXT NOT NULL,
                identity_value TEXT NOT NULL,
                buyer_code TEXT NOT NULL,
                buyer_name TEXT,
                source TEXT NOT NULL DEFAULT 'human',
                confidence REAL NOT NULL DEFAULT 0.8,
                seen_count INTEGER NOT NULL DEFAULT 1,
                confirmed_count INTEGER NOT NULL DEFAULT 0,
                rejected_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(identity_type, identity_value)
            );

            CREATE INDEX IF NOT EXISTS idx_buyer_identity_value ON buyer_identities(identity_type, identity_value);
            CREATE INDEX IF NOT EXISTS idx_buyer_identity_buyer ON buyer_identities(buyer_code);

            CREATE TABLE IF NOT EXISTS learning_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER REFERENCES cases(id) ON DELETE SET NULL,
                raw_email_id INTEGER REFERENCES raw_emails(id) ON DELETE SET NULL,
                kind TEXT NOT NULL,
                source TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_learning_events_case ON learning_events(case_id);
            CREATE INDEX IF NOT EXISTS idx_learning_events_kind ON learning_events(kind);

            CREATE TABLE IF NOT EXISTS ai_suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
                model TEXT,
                prompt_hash TEXT,
                response_json TEXT NOT NULL DEFAULT '{}',
                accepted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ai_cache (
                prompt_hash TEXT PRIMARY KEY,
                provider TEXT,
                model TEXT,
                response_json TEXT NOT NULL DEFAULT '{}',
                raw_excerpt TEXT,
                prompt_chars INTEGER NOT NULL DEFAULT 0,
                response_chars INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                last_used_at TEXT NOT NULL,
                use_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS ai_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER REFERENCES cases(id) ON DELETE SET NULL,
                provider TEXT,
                model TEXT,
                prompt_hash TEXT,
                prompt_chars INTEGER NOT NULL DEFAULT 0,
                response_chars INTEGER NOT NULL DEFAULT 0,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                cached INTEGER NOT NULL DEFAULT 0,
                ok INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_ai_usage_case ON ai_usage(case_id);
            CREATE INDEX IF NOT EXISTS idx_ai_usage_prompt ON ai_usage(prompt_hash);

            CREATE TABLE IF NOT EXISTS client_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                identity_type TEXT NOT NULL,
                identity_value TEXT NOT NULL,
                buyer_code TEXT NOT NULL,
                buyer_name TEXT,
                status TEXT NOT NULL DEFAULT 'provisional',
                confidence REAL NOT NULL DEFAULT 0.25,
                seen_count INTEGER NOT NULL DEFAULT 0,
                structured_count INTEGER NOT NULL DEFAULT 0,
                ready_like_count INTEGER NOT NULL DEFAULT 0,
                promoted_count INTEGER NOT NULL DEFAULT 0,
                sample_subjects_json TEXT NOT NULL DEFAULT '[]',
                field_stats_json TEXT NOT NULL DEFAULT '{}',
                inferred_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(identity_type, identity_value)
            );

            CREATE INDEX IF NOT EXISTS idx_client_profiles_value ON client_profiles(identity_type, identity_value);
            CREATE INDEX IF NOT EXISTS idx_client_profiles_buyer ON client_profiles(buyer_code);
            CREATE INDEX IF NOT EXISTS idx_client_profiles_status ON client_profiles(status);

            CREATE TABLE IF NOT EXISTS field_pattern_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                buyer_code TEXT NOT NULL,
                field_name TEXT NOT NULL,
                value_sample TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'observed',
                confidence REAL NOT NULL DEFAULT 0.2,
                seen_count INTEGER NOT NULL DEFAULT 1,
                accepted INTEGER NOT NULL DEFAULT 0,
                rejected INTEGER NOT NULL DEFAULT 0,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(buyer_code, field_name, value_sample)
            );

            CREATE INDEX IF NOT EXISTS idx_field_pattern_candidates_buyer ON field_pattern_candidates(buyer_code, field_name);
            """
        )
        # v1.0 -> v1.1 light migrations.
        for name, ddl in [
            ("folder_seen_json", "TEXT DEFAULT '[]'"),
            ("canonical_key", "TEXT"),
            ("duplicate_of_raw_email_id", "INTEGER"),
            ("direction", "TEXT"),
            ("cc_addr", "TEXT"),
            ("updated_at", "TEXT"),
            ("quote_markers", "INTEGER DEFAULT 0"),
        ]:
            _add_column(con, "raw_emails", name, ddl)
        _add_column(con, "cases", "quality_json", "TEXT NOT NULL DEFAULT '[]'")
        con.execute("CREATE INDEX IF NOT EXISTS idx_buyer_identity_value ON buyer_identities(identity_type, identity_value)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_learning_events_case ON learning_events(case_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_cases_raw_email_id ON cases(raw_email_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_cases_event_state ON cases(event_type, state)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_ai_suggestions_case ON ai_suggestions(case_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_raw_canonical_key ON raw_emails(canonical_key)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_raw_direction ON raw_emails(direction)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_client_profiles_value ON client_profiles(identity_type, identity_value)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_client_profiles_status ON client_profiles(status)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_field_pattern_candidates_buyer ON field_pattern_candidates(buyer_code, field_name)")
        # v2.1: добавить file_path для вложений
        _add_column(con, "attachments", "file_path", "TEXT")
        _add_column(con, "ai_usage", "prompt_tokens", "INTEGER NOT NULL DEFAULT 0")
        _add_column(con, "ai_usage", "completion_tokens", "INTEGER NOT NULL DEFAULT 0")
        # B2: режим конвейера (pattern_fallback|full_ai) и тип модели (text|vision) — для
        # раздельного учёта токенов по овалам и аналитики по дням/неделям/месяцам.
        _add_column(con, "ai_usage", "mode", "TEXT")
        _add_column(con, "ai_usage", "kind", "TEXT")
        # v2.1: добавить pattern_regex если ещё нет
        _add_column(con, "field_pattern_candidates", "pattern_regex", "TEXT")
        _add_column(con, "field_pattern_candidates", "context_before", "TEXT")
        _add_column(con, "field_pattern_candidates", "context_after", "TEXT")
        _add_column(con, "field_pattern_candidates", "promoted_at", "TEXT")
        con.execute("CREATE INDEX IF NOT EXISTS idx_ai_usage_case ON ai_usage(case_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_ai_usage_prompt ON ai_usage(prompt_hash)")
        for name, ddl in [
            ("event_type", "TEXT NOT NULL DEFAULT 'return_ready'"),
            ("event_key", "TEXT"),
            ("channel", "TEXT NOT NULL DEFAULT 'file'"),
            ("business_priority", "TEXT"),
            ("attempt_count", "INTEGER NOT NULL DEFAULT 0"),
            ("last_error", "TEXT"),
            ("next_attempt_at", "TEXT"),
            ("file_path", "TEXT"),
            ("delivery_response_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("last_attempt_at", "TEXT"),
            ("resolved_at", "TEXT"),
            ("resolution_note", "TEXT"),
        ]:
            _add_column(con, "outbox", name, ddl)
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_event_key ON outbox(event_key)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_outbox_status_channel ON outbox(status, channel)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_outbox_case_event ON outbox(case_id, event_type)")
        con.execute("""
            CREATE TABLE IF NOT EXISTS outbox_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                outbox_id INTEGER NOT NULL REFERENCES outbox(id) ON DELETE CASCADE,
                case_id INTEGER,
                event_type TEXT,
                channel TEXT,
                attempt_no INTEGER NOT NULL DEFAULT 1,
                ok INTEGER NOT NULL DEFAULT 0,
                status_code INTEGER,
                error TEXT,
                response_json TEXT NOT NULL DEFAULT '{}',
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_outbox_attempts_outbox ON outbox_attempts(outbox_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_outbox_attempts_ok ON outbox_attempts(ok, finished_at)")
        con.executescript("""
            CREATE TABLE IF NOT EXISTS process_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stage TEXT NOT NULL,
                level TEXT NOT NULL DEFAULT 'info',
                message TEXT NOT NULL,
                case_id INTEGER,
                raw_email_id INTEGER,
                subject TEXT,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_process_events_created ON process_events(created_at);
            CREATE INDEX IF NOT EXISTS idx_process_events_stage ON process_events(stage, created_at);
            CREATE INDEX IF NOT EXISTS idx_process_events_case ON process_events(case_id, created_at);
        """)
        con.executescript("""
            CREATE TABLE IF NOT EXISTS test_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL DEFAULT 'running',
                params_json TEXT NOT NULL DEFAULT '{}',
                before_json TEXT NOT NULL DEFAULT '{}',
                after_json TEXT NOT NULL DEFAULT '{}',
                import_result_json TEXT NOT NULL DEFAULT '{}',
                queue_result_json TEXT NOT NULL DEFAULT '{}',
                delivery_result_json TEXT NOT NULL DEFAULT '{}',
                summary_json TEXT NOT NULL DEFAULT '{}',
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_test_runs_started ON test_runs(started_at);
            CREATE INDEX IF NOT EXISTS idx_test_runs_status ON test_runs(status);
        """)
        # v2 migrations: архивация писем + escalation
        for _col, _ddl in [
            ("archived_body", "INTEGER NOT NULL DEFAULT 0"),
            ("archived_meta", "INTEGER NOT NULL DEFAULT 0"),
        ]:
            _add_column(con, "raw_emails", _col, _ddl)
        _add_column(con, "cases", "escalation_flag", "INTEGER NOT NULL DEFAULT 0")
        _add_column(con, "cases", "link_quarantine", "INTEGER NOT NULL DEFAULT 0")
        _add_column(con, "cases", "outbox_validated", "INTEGER NOT NULL DEFAULT 0")
        _add_column(con, "cases", "needs_ai", "INTEGER NOT NULL DEFAULT 0")
        _add_column(con, "cases", "has_min_fields", "INTEGER NOT NULL DEFAULT 0")
        _add_column(con, "cases", "reminder_count", "INTEGER NOT NULL DEFAULT 0")
        # v5: мультипозиция — несколько товарных позиций в одном письме = несколько кейсов.
        _add_column(con, "cases", "item_index", "INTEGER NOT NULL DEFAULT 0")
        con.execute("CREATE INDEX IF NOT EXISTS idx_raw_archived ON raw_emails(archived_body, received_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_cases_escalation ON cases(escalation_flag)")
        # v3: диагностика импорта
        _add_column(con, "raw_emails", "raw_size", "INTEGER DEFAULT 0")
        _add_column(con, "raw_emails", "status", "TEXT DEFAULT 'imported'")
        # v4: кэш расклеенного видимого текста — чтобы reprocess не расклеивал HTML заново
        _add_column(con, "raw_emails", "visible_text", "TEXT")
        # v7: UIDVALIDITY в raw identity (колонка + idempotent rebuild на (mailbox,uid,uidvalidity))
        _add_column(con, "raw_emails", "uidvalidity", "TEXT")
        migrate_raw_emails_uidvalidity(con)
        con.executescript("""
            CREATE TABLE IF NOT EXISTS import_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT UNIQUE,
                status TEXT NOT NULL,
                mode TEXT,
                started_at TEXT,
                finished_at TEXT,
                last_heartbeat_at TEXT,
                current_stage TEXT,
                current_folder TEXT,
                current_display_folder TEXT,
                current_uid TEXT,
                processed_count INTEGER DEFAULT 0,
                imported_count INTEGER DEFAULT 0,
                skipped_count INTEGER DEFAULT 0,
                failed_count INTEGER DEFAULT 0,
                error_count INTEGER DEFAULT 0,
                result_json TEXT
            );
            CREATE TABLE IF NOT EXISTS import_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT,
                account TEXT,
                mailbox TEXT,
                display_folder TEXT,
                uid TEXT,
                stage TEXT,
                error_type TEXT,
                error_message TEXT,
                raw_size INTEGER,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS import_uid_failures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account TEXT,
                mailbox TEXT,
                uid TEXT,
                stage TEXT,
                error_type TEXT,
                error_message TEXT,
                attempts INTEGER DEFAULT 1,
                status TEXT DEFAULT 'failed',
                first_seen_at TEXT,
                last_seen_at TEXT,
                next_retry_at TEXT,
                UNIQUE(account, mailbox, uid, stage)
            );
            CREATE INDEX IF NOT EXISTS idx_import_jobs_status ON import_jobs(status);
            CREATE INDEX IF NOT EXISTS idx_import_errors_job ON import_errors(job_id);
            CREATE INDEX IF NOT EXISTS idx_import_errors_mailbox ON import_errors(mailbox, uid);
            CREATE INDEX IF NOT EXISTS idx_import_uid_failures_mailbox ON import_uid_failures(mailbox, uid);
        """)
        # v6: расширенный карантин/ретрай импорта (после создания таблицы import_uid_failures)
        _add_column(con, "import_uid_failures", "uidvalidity", "TEXT")
        _add_column(con, "import_uid_failures", "message_id", "TEXT")
        _add_column(con, "import_uid_failures", "recoverable", "INTEGER")
        con.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )


def _canonical_key(email_data: dict[str, Any]) -> str:
    message_id = (email_data.get("message_id") or "").strip().lower()
    if message_id:
        return f"message_id:{message_id}"
    raw_hash = (email_data.get("raw_hash") or "").strip().lower()
    if raw_hash:
        return f"raw_hash:{raw_hash}"
    return f"mailbox_uid:{email_data.get('mailbox')}:{email_data.get('uid')}"


def _merge_folders(existing_json: str | None, mailbox: str) -> str:
    folders = loads(existing_json, []) or []
    if mailbox and mailbox not in folders:
        folders.append(mailbox)
    return dumps(folders)


def _raw_identity_uid(email_data: dict[str, Any]) -> str:
    uid = str(email_data.get("uid") or "").strip()
    if uid:
        return uid
    raw_hash = str(email_data.get("raw_hash") or "").strip().lower()
    if raw_hash:
        return f"raw:{raw_hash}"
    seed = dumps({
        "message_id": email_data.get("message_id"),
        "received_at": email_data.get("received_at"),
        "from_addr": email_data.get("from_addr"),
        "subject": email_data.get("subject"),
        "body_text": email_data.get("body_text"),
        "body_html": email_data.get("body_html"),
    })
    return f"generated:{hashlib.sha256(seed.encode('utf-8')).hexdigest()}"


def upsert_email(con: sqlite3.Connection, email_data: dict[str, Any]) -> tuple[int, bool]:
    now = utcnow()
    mailbox = email_data.get("mailbox") or "INBOX"
    uid = _raw_identity_uid(email_data)
    canonical_key = _canonical_key(email_data)
    raw_hash = str(email_data.get("raw_hash") or "").strip().lower()
    # UIDVALIDITY входит в raw identity: тот же (mailbox, uid) при ДРУГОМ uidvalidity = новый
    # серверный идентификатор → новая raw-строка (сервер переиспользовал UID). Деградирует мягко,
    # если колонки нет (старая схема в тестах).
    has_uv = "uidvalidity" in _columns(con, "raw_emails")
    uidvalidity = str(email_data.get("uidvalidity") or "") if has_uv else ""

    if has_uv:
        rows = con.execute(
            "SELECT id, folder_seen_json, raw_hash, COALESCE(uidvalidity,'') uv "
            "FROM raw_emails WHERE mailbox=? AND uid=? ORDER BY id",
            (mailbox, uid),
        ).fetchall()
        # Совпадение идентичности: тот же uidvalidity, либо одна из сторон неизвестна ('').
        match = next((r for r in rows if r["uv"] == uidvalidity or r["uv"] == "" or uidvalidity == ""), None)
        if match:
            con.execute(
                "UPDATE raw_emails SET folder_seen_json=?, updated_at=? WHERE id=?",
                (_merge_folders(match["folder_seen_json"], mailbox), now, int(match["id"])),
            )
            # Дозаполнить uidvalidity у legacy-строки, когда он стал известен.
            if match["uv"] == "" and uidvalidity:
                con.execute("UPDATE raw_emails SET uidvalidity=? WHERE id=?", (uidvalidity, int(match["id"])))
            return int(match["id"]), False
        # rows есть, но все с ДРУГИМ непустым uidvalidity → проваливаемся в INSERT (новая identity).
    else:
        existing = con.execute(
            "SELECT id, folder_seen_json, raw_hash FROM raw_emails WHERE mailbox=? AND uid=?",
            (mailbox, uid),
        ).fetchone()
        if existing:
            con.execute(
                "UPDATE raw_emails SET folder_seen_json=?, updated_at=? WHERE id=?",
                (_merge_folders(existing["folder_seen_json"], mailbox), now, int(existing["id"])),
            )
            return int(existing["id"]), False

    canonical_existing = con.execute(
        """
        SELECT id, folder_seen_json, raw_hash
        FROM raw_emails
        WHERE canonical_key=?
        ORDER BY id
        LIMIT 1
        """,
        (canonical_key,),
    ).fetchone()
    existing_hash = str(canonical_existing["raw_hash"] or "").strip().lower() if canonical_existing else ""
    if canonical_existing and raw_hash and existing_hash and raw_hash == existing_hash:
        # Byte-identical copy in another folder: keep one raw row and record the occurrence.
        con.execute(
            "UPDATE raw_emails SET folder_seen_json=?, updated_at=? WHERE id=?",
            (_merge_folders(canonical_existing["folder_seen_json"], mailbox), now, int(canonical_existing["id"])),
        )
        record_process_event(
            con,
            stage="dedup",
            message="Exact duplicate reused",
            raw_email_id=int(canonical_existing["id"]),
            subject=email_data.get("subject"),
            details={
                "decision": "exact_duplicate_reused",
                "mailbox": mailbox,
                "uid": uid,
                "canonical_key": canonical_key,
                "raw_hash": raw_hash,
            },
        )
        return int(canonical_existing["id"]), False

    duplicate_of_raw_email_id = int(canonical_existing["id"]) if canonical_existing else None
    base_cols = (
        "mailbox, uid, folder_seen_json, canonical_key, duplicate_of_raw_email_id, "
        "status, message_id, in_reply_to, references_json, subject, "
        "from_addr, to_addr, cc_addr, received_at, body_text, body_html, visible_text, snippet, "
        "raw_hash, raw_path, quote_markers, imported_at, updated_at"
    )
    base_vals: list[Any] = [
        mailbox, uid, dumps([mailbox]), canonical_key, duplicate_of_raw_email_id,
        "duplicate" if duplicate_of_raw_email_id else "imported",
        email_data.get("message_id"), email_data.get("in_reply_to"),
        dumps(email_data.get("references") or []), email_data.get("subject"),
        email_data.get("from_addr"), email_data.get("to_addr"), email_data.get("cc_addr"),
        email_data.get("received_at"), email_data.get("body_text"), email_data.get("body_html"),
        email_data.get("visible_text"), email_data.get("snippet"),
        email_data.get("raw_hash"), email_data.get("raw_path"),
        int(email_data.get("quote_markers") or 0), now, now,
    ]
    if has_uv:
        base_cols += ", uidvalidity"
        base_vals.append(uidvalidity)
    placeholders = ", ".join(["?"] * len(base_vals))
    cur = con.execute(f"INSERT INTO raw_emails({base_cols}) VALUES ({placeholders})", base_vals)
    raw_email_id = int(cur.lastrowid)
    if duplicate_of_raw_email_id:
        record_process_event(
            con,
            stage="dedup",
            message="Semantic duplicate linked, not dropped",
            raw_email_id=raw_email_id,
            subject=email_data.get("subject"),
            details={
                "decision": "semantic_duplicate_linked",
                "duplicate_of_raw_email_id": duplicate_of_raw_email_id,
                "mailbox": mailbox,
                "uid": uid,
                "canonical_key": canonical_key,
                "raw_hash": raw_hash,
                "original_raw_hash": existing_hash,
            },
        )
    for att in email_data.get("attachments") or []:
        # Сохраняем байты вложения на диск, если есть
        file_path = _save_attachment_file(raw_email_id, att)
        cur2 = con.execute(
            "INSERT INTO attachments(raw_email_id, filename, content_type, size_bytes, file_path) VALUES (?, ?, ?, ?, ?)",
            (raw_email_id, att.get("filename"), att.get("content_type"), att.get("size_bytes"), file_path),
        )
        att["_db_id"] = int(cur2.lastrowid)
    return raw_email_id, True


def _save_attachment_file(raw_email_id: int, att: dict) -> str | None:
    """Сохраняет байты вложения в data/attachments/{email_id}/{filename}."""
    raw_bytes = att.get("_bytes")
    filename = att.get("filename") or "attachment"
    if not raw_bytes:
        return None
    try:
        import re as _re
        safe_name = _re.sub(r"[^\w\-. ]", "_", filename)[:100]
        att_dir = Path(settings.database_path).parent / "attachments" / str(raw_email_id)
        att_dir.mkdir(parents=True, exist_ok=True)
        path = att_dir / safe_name
        if not path.exists():
            path.write_bytes(raw_bytes)
        return str(path)
    except Exception:
        return None


def _original_source_for_quality(con: sqlite3.Connection, raw_email_id: int) -> tuple[str, dict[str, Any]]:
    row = con.execute(
        """
        SELECT subject, visible_text, body_text, body_html, snippet,
               from_addr, to_addr, cc_addr, mailbox, in_reply_to, references_json
        FROM raw_emails
        WHERE id=?
        """,
        (int(raw_email_id),),
    ).fetchone()
    if not row:
        return "", {}
    attachment_names = [
        str(r["filename"] or "")
        for r in con.execute(
            "SELECT filename FROM attachments WHERE raw_email_id=? ORDER BY id",
            (int(raw_email_id),),
        ).fetchall()
        if r["filename"]
    ]
    parts = [
        row["subject"], row["visible_text"], row["body_text"], row["body_html"], row["snippet"],
        row["from_addr"], row["to_addr"], row["cc_addr"], row["mailbox"], *attachment_names,
    ]
    metadata = {
        "subject": row["subject"],
        "in_reply_to": row["in_reply_to"],
        "references": loads(row["references_json"], []),
        "attachment_names": attachment_names,
        "raw_email": {
            "subject": row["subject"],
            "visible_text": row["visible_text"],
            "body_text": row["body_text"],
            "body_html": row["body_html"],
            "snippet": row["snippet"],
            "from_addr": row["from_addr"],
            "to_addr": row["to_addr"],
            "cc_addr": row["cc_addr"],
            "mailbox": row["mailbox"],
        },
    }
    return "\n".join(str(x or "") for x in parts if x), metadata


def _apply_quality_gate(
    con: sqlite3.Connection,
    raw_email_id: int,
    case_data: dict[str, Any],
    *,
    item_index: int = 0,
    case_id: int | None = None,
) -> dict[str, Any]:
    original_text, source_metadata = _original_source_for_quality(con, raw_email_id)
    metadata = {
        "raw_email_id": raw_email_id,
        "case_id": case_id,
        "item_index": item_index,
        **source_metadata,
    }
    metadata["buyer_evidence"] = build_buyer_evidence(
        case_data.get("buyer_code"),
        source_metadata.get("raw_email") or {},
        case_data,
    )
    quality = quality_gate(original_text, case_data, metadata)
    initial_gate = dict(quality.get("evidence_gate") or {})
    repair_result: dict[str, Any] = {
        "changed": False,
        "case_data": case_data,
        "repairs": [],
        "warnings": [],
        "candidates": {},
    }
    if not initial_gate.get("passed", True):
        repair_result = repair_evidence(
            case_data,
            source_metadata.get("raw_email") or {},
            initial_gate,
        )
        if repair_result.get("changed"):
            case_data = dict(repair_result.get("case_data") or case_data)
            repaired_export = dict(case_data.get("export") or {})
            repaired_document = dict(repaired_export.get("document") or {})
            repaired_date = (case_data.get("fields") or {}).get("document_date")
            if repaired_date:
                repaired_document["date"] = repaired_date
                repaired_export["document"] = repaired_document
                case_data["export"] = repaired_export
            quality = quality_gate(original_text, case_data, metadata)
    evidence_gate = dict(quality.get("evidence_gate") or {})
    if not initial_gate.get("passed", True):
        evidence_gate["initial_gate"] = {
            "passed": bool(initial_gate.get("passed")),
            "blocking_errors": initial_gate.get("blocking_errors") or [],
            "blocking_warnings": initial_gate.get("blocking_warnings") or [],
            "field_statuses": initial_gate.get("field_statuses") or {},
        }
        evidence_gate["repair_attempted"] = True
        evidence_gate["repaired"] = bool(repair_result.get("changed"))
        evidence_gate["repairs"] = repair_result.get("repairs") or []
        evidence_gate["repair_warnings"] = repair_result.get("warnings") or []
        evidence_gate["repair_candidates"] = repair_result.get("candidates") or {}
        if repair_result.get("repairs"):
            date_repair = next(
                (item for item in repair_result["repairs"] if item.get("field") == "document_date"),
                None,
            )
            if date_repair:
                field_audit = dict(evidence_gate.get("field_audit") or {})
                date_audit = dict(field_audit.get("document_date") or {})
                date_audit.update(
                    {
                        "source": date_repair.get("source"),
                        "status": date_repair.get("status"),
                        "evidence_snippet": date_repair.get("evidence_snippet"),
                        "repair_method": date_repair.get("repair_method"),
                        "repaired": True,
                    }
                )
                field_audit["document_date"] = date_audit
                evidence_gate["field_audit"] = field_audit
    quality["evidence_gate"] = evidence_gate
    case_data = dict(case_data or {})
    payload = dict(case_data.get("payload") or {})
    export = dict(case_data.get("export") or {})
    payload["_quality"] = quality
    payload["evidence_gate"] = evidence_gate
    export["_quality"] = quality
    export["evidence_gate"] = evidence_gate
    case_data["evidence_gate"] = evidence_gate
    case_data["payload"] = payload
    case_data["export"] = export
    if quality.get("case_status") != "accepted" or not evidence_gate.get("passed", True):
        if case_data.get("state") == "ready_to_1c" or case_data.get("ready_for_export"):
            case_data["state"] = "needs_review"
            case_data["ready_for_export"] = False
            case_data["needs_review"] = True
    try:
        write_quality_artifacts(raw_email_id, case_data, quality)
    except Exception:
        pass
    return case_data


def save_case(con: sqlite3.Connection, raw_email_id: int, case_data: dict[str, Any], item_index: int = 0) -> int:
    now = utcnow()
    direction = (case_data.get("payload") or {}).get("direction")
    if direction:
        con.execute("UPDATE raw_emails SET direction=?, updated_at=? WHERE id=?", (direction, now, raw_email_id))

    existing = con.execute("SELECT id FROM cases WHERE raw_email_id=? AND item_index=?", (raw_email_id, int(item_index))).fetchone()
    case_data = _apply_quality_gate(
        con,
        raw_email_id,
        case_data,
        item_index=int(item_index),
        case_id=int(existing["id"]) if existing else None,
    )
    values = (
        case_data.get("buyer_code"),
        case_data.get("buyer_name"),
        case_data.get("event_type") or "unknown",
        case_data.get("claim_kind"),
        case_data.get("status"),
        case_data.get("priority") or "normal",
        float(case_data.get("confidence") or 0),
        case_data.get("deadline_at"),
        case_data.get("thread_key"),
        case_data.get("strong_key"),
        case_data.get("weak_key"),
        1 if case_data.get("is_followup") else 0,
        1 if case_data.get("ready_for_export") else 0,
        1 if case_data.get("needs_review", True) else 0,
        case_data.get("state") or "needs_review",
        dumps(case_data.get("fields") or {}),
        dumps(case_data.get("missing") or []),
        dumps(case_data.get("quality") or []),
        dumps(case_data.get("payload") or {}),
        dumps(case_data.get("export") or {}),
        1 if case_data.get("needs_ai") else 0,
        1 if case_data.get("has_min_fields") else 0,
    )
    if existing:
        con.execute(
            """
            UPDATE cases SET
                buyer_code=?, buyer_name=?, event_type=?, claim_kind=?, status=?, priority=?, confidence=?,
                deadline_at=?, thread_key=?, strong_key=?, weak_key=?, is_followup=?, ready_for_export=?,
                needs_review=?, state=?, fields_json=?, missing_json=?, quality_json=?, payload_json=?, export_json=?,
                needs_ai=?, has_min_fields=?, outbox_validated=0, updated_at=?
            WHERE id=?
            """,
            values + (now, int(existing["id"])),
        )
        return int(existing["id"])
    cur = con.execute(
        """
        INSERT INTO cases(
            raw_email_id, buyer_code, buyer_name, event_type, claim_kind, status, priority, confidence,
            deadline_at, thread_key, strong_key, weak_key, is_followup, ready_for_export,
            needs_review, state, fields_json, missing_json, quality_json, payload_json, export_json,
            needs_ai, has_min_fields, item_index, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (raw_email_id,) + values + (int(item_index), now, now),
    )
    return int(cur.lastrowid)



def _email_from_addr(value: str | None) -> tuple[str, str]:
    from email.utils import parseaddr
    _, addr = parseaddr(value or "")
    addr = addr.lower().strip()
    domain = addr.rsplit("@", 1)[-1] if "@" in addr else ""
    return addr, domain


def load_buyer_identities(con: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        SELECT identity_type, identity_value, buyer_code, buyer_name, source, confidence, seen_count, confirmed_count
        FROM buyer_identities
        WHERE rejected_count=0
        ORDER BY confirmed_count DESC, confidence DESC, seen_count DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def record_learning_event(
    con: sqlite3.Connection,
    *,
    kind: str,
    source: str,
    payload: dict[str, Any] | None = None,
    case_id: int | None = None,
    raw_email_id: int | None = None,
    confidence: float = 0.0,
) -> int:
    cur = con.execute(
        """
        INSERT INTO learning_events(case_id, raw_email_id, kind, source, confidence, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (case_id, raw_email_id, kind, source, float(confidence or 0), dumps(payload or {}), utcnow()),
    )
    return int(cur.lastrowid)


def upsert_buyer_identity(
    con: sqlite3.Connection,
    *,
    identity_type: str,
    identity_value: str,
    buyer_code: str,
    buyer_name: str | None,
    source: str = "human",
    confidence: float = 0.9,
    confirmed: bool = False,
) -> None:
    identity_type = identity_type.strip().lower()
    identity_value = identity_value.strip().lower()
    buyer_code = buyer_code.strip()
    if not identity_type or not identity_value or not buyer_code:
        return
    now = utcnow()
    existing = con.execute(
        "SELECT id FROM buyer_identities WHERE identity_type=? AND identity_value=?",
        (identity_type, identity_value),
    ).fetchone()
    if existing:
        con.execute(
            """
            UPDATE buyer_identities
               SET buyer_code=?, buyer_name=?, source=?, confidence=MAX(confidence, ?),
                   seen_count=seen_count+1,
                   confirmed_count=confirmed_count+?,
                   updated_at=?
             WHERE id=?
            """,
            (buyer_code, buyer_name, source, float(confidence), 1 if confirmed else 0, now, int(existing["id"])),
        )
    else:
        con.execute(
            """
            INSERT INTO buyer_identities(
                identity_type, identity_value, buyer_code, buyer_name, source, confidence,
                seen_count, confirmed_count, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (identity_type, identity_value, buyer_code, buyer_name, source, float(confidence), 1 if confirmed else 0, now, now),
        )


def learn_buyer_from_case(con: sqlite3.Connection, case_id: int, source: str = "human") -> dict[str, Any]:
    row = con.execute(
        """
        SELECT c.id, c.raw_email_id, c.buyer_code, c.buyer_name, e.from_addr
        FROM cases c JOIN raw_emails e ON e.id=c.raw_email_id
        WHERE c.id=?
        """,
        (case_id,),
    ).fetchone()
    if not row:
        return {"ok": False, "error": "case_not_found"}
    buyer_code = row["buyer_code"]
    if not buyer_code:
        return {"ok": False, "error": "buyer_code_empty"}
    email_addr, domain = _email_from_addr(row["from_addr"])
    learned: list[dict[str, str]] = []
    if email_addr:
        upsert_buyer_identity(
            con,
            identity_type="email",
            identity_value=email_addr,
            buyer_code=buyer_code,
            buyer_name=row["buyer_name"],
            source=source,
            confidence=0.98,
            confirmed=True,
        )
        learned.append({"type": "email", "value": email_addr})
    if domain:
        upsert_buyer_identity(
            con,
            identity_type="domain",
            identity_value=domain,
            buyer_code=buyer_code,
            buyer_name=row["buyer_name"],
            source=source,
            confidence=0.90,
            confirmed=True,
        )
        learned.append({"type": "domain", "value": domain})
    record_learning_event(
        con,
        kind="buyer_identity_confirmed",
        source=source,
        case_id=case_id,
        raw_email_id=int(row["raw_email_id"]),
        confidence=1.0,
        payload={"buyer_code": buyer_code, "buyer_name": row["buyer_name"], "learned": learned},
    )
    return {"ok": True, "learned": learned}




def get_ai_cache(con: sqlite3.Connection, prompt_hash: str) -> dict[str, Any] | None:
    row = con.execute("SELECT * FROM ai_cache WHERE prompt_hash=?", (prompt_hash,)).fetchone()
    return row_to_dict(row) if row else None


def put_ai_cache(
    con: sqlite3.Connection,
    *,
    prompt_hash: str,
    provider: str | None,
    model: str | None,
    response: dict[str, Any],
    raw_excerpt: str | None = None,
    prompt_chars: int = 0,
    response_chars: int = 0,
) -> None:
    now = utcnow()
    con.execute(
        """
        INSERT INTO ai_cache(prompt_hash, provider, model, response_json, raw_excerpt, prompt_chars, response_chars, created_at, last_used_at, use_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(prompt_hash) DO UPDATE SET
            provider=excluded.provider, model=excluded.model, response_json=excluded.response_json, raw_excerpt=excluded.raw_excerpt,
            prompt_chars=excluded.prompt_chars, response_chars=excluded.response_chars, last_used_at=excluded.last_used_at, use_count=ai_cache.use_count+1
        """,
        (prompt_hash, provider, model, dumps(response), raw_excerpt, int(prompt_chars or 0), int(response_chars or 0), now, now),
    )


import threading as _threading
_ai_usage_ctx = _threading.local()

def set_ai_usage_context(mode: str | None = None, kind: str | None = None) -> None:
    """Контекст текущего AI-режима (поток) — record_ai_usage берёт его как дефолт
    для тегов mode (pattern|full_ai) и kind (text|vision), чтобы не тянуть их через слои."""
    _ai_usage_ctx.mode = mode
    _ai_usage_ctx.kind = kind


def record_ai_usage(
    con: sqlite3.Connection,
    *,
    case_id: int | None = None,
    provider: str | None = None,
    model: str | None = None,
    prompt_hash: str | None = None,
    prompt_chars: int = 0,
    response_chars: int = 0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached: bool = False,
    ok: bool = False,
    error: str | None = None,
    mode: str | None = None,
    kind: str | None = None,
) -> int:
    if mode is None:
        mode = getattr(_ai_usage_ctx, "mode", None)
    if kind is None:
        kind = getattr(_ai_usage_ctx, "kind", None)
    cur = con.execute(
        """
        INSERT INTO ai_usage(case_id, provider, model, prompt_hash, prompt_chars, response_chars, prompt_tokens, completion_tokens, cached, ok, error, created_at, mode, kind)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (case_id, provider, model, prompt_hash, int(prompt_chars or 0), int(response_chars or 0),
         int(prompt_tokens or 0), int(completion_tokens or 0), 1 if cached else 0, 1 if ok else 0, error, utcnow(),
         mode, kind),
    )
    return int(cur.lastrowid)

def load_client_profiles(con: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        SELECT identity_type, identity_value, buyer_code, buyer_name, status, confidence,
               seen_count, structured_count, ready_like_count, promoted_count,
               sample_subjects_json, field_stats_json, inferred_json, updated_at
        FROM client_profiles
        ORDER BY status='trusted' DESC, confidence DESC, seen_count DESC, identity_value
        """
    ).fetchall()
    return [row_to_dict(r) or {} for r in rows]


def upsert_client_profile(
    con: sqlite3.Connection,
    *,
    identity_type: str,
    identity_value: str,
    buyer_code: str,
    buyer_name: str | None,
    subject: str | None = None,
    fields: dict[str, Any] | None = None,
    structured: bool = False,
    ready_like: bool = False,
    inferred: dict[str, Any] | None = None,
) -> dict[str, Any]:
    identity_type = identity_type.strip().lower()
    identity_value = identity_value.strip().lower()
    buyer_code = buyer_code.strip()
    if not identity_type or not identity_value or not buyer_code:
        return {"ok": False, "error": "empty_identity"}
    now = utcnow()
    row = con.execute(
        "SELECT * FROM client_profiles WHERE identity_type=? AND identity_value=?",
        (identity_type, identity_value),
    ).fetchone()
    fields = fields or {}
    if row:
        current = row_to_dict(row) or {}
        samples = current.get("sample_subjects") or []
        clean_subject = (subject or "").strip()
        if clean_subject and clean_subject not in samples:
            samples = (samples + [clean_subject])[-8:]
        stats = current.get("field_stats") or {}
        for k, v in fields.items():
            if v in (None, "", [], {}):
                continue
            item = stats.setdefault(str(k), {"seen": 0, "samples": []})
            item["seen"] = int(item.get("seen") or 0) + 1
            sv = str(v)[:80]
            if sv not in item.setdefault("samples", []):
                item["samples"] = (item.get("samples") or [])[-4:] + [sv]
        inferred_merged = current.get("inferred") or {}
        inferred_merged.update(inferred or {})
        seen = int(current.get("seen_count") or 0) + 1
        structured_count = int(current.get("structured_count") or 0) + (1 if structured else 0)
        ready_like_count = int(current.get("ready_like_count") or 0) + (1 if ready_like else 0)
        confidence = min(0.70, 0.18 + seen * 0.07 + structured_count * 0.08 + ready_like_count * 0.05)
        con.execute(
            """
            UPDATE client_profiles
               SET buyer_code=?, buyer_name=?, confidence=MAX(confidence, ?), seen_count=?,
                   structured_count=?, ready_like_count=?, sample_subjects_json=?, field_stats_json=?,
                   inferred_json=?, updated_at=?
             WHERE id=?
            """,
            (
                buyer_code,
                buyer_name,
                confidence,
                seen,
                structured_count,
                ready_like_count,
                dumps(samples),
                dumps(stats),
                dumps(inferred_merged),
                now,
                int(current["id"]),
            ),
        )
        return {"ok": True, "id": int(current["id"]), "seen_count": seen, "structured_count": structured_count, "ready_like_count": ready_like_count, "confidence": confidence}
    samples = [subject.strip()] if subject and subject.strip() else []
    stats: dict[str, Any] = {}
    for k, v in fields.items():
        if v in (None, "", [], {}):
            continue
        stats[str(k)] = {"seen": 1, "samples": [str(v)[:80]]}
    confidence = min(0.55, 0.18 + (0.10 if structured else 0) + (0.07 if ready_like else 0))
    cur = con.execute(
        """
        INSERT INTO client_profiles(
            identity_type, identity_value, buyer_code, buyer_name, status, confidence,
            seen_count, structured_count, ready_like_count, sample_subjects_json, field_stats_json,
            inferred_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'provisional', ?, 1, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            identity_type,
            identity_value,
            buyer_code,
            buyer_name,
            confidence,
            1 if structured else 0,
            1 if ready_like else 0,
            dumps(samples),
            dumps(stats),
            dumps(inferred or {}),
            now,
            now,
        ),
    )
    return {"ok": True, "id": int(cur.lastrowid), "seen_count": 1, "structured_count": 1 if structured else 0, "ready_like_count": 1 if ready_like else 0, "confidence": confidence}


def maybe_promote_client_profile(
    con: sqlite3.Connection,
    *,
    identity_type: str,
    identity_value: str,
    min_seen: int,
    min_structured: int,
    promote_confidence: float,
) -> dict[str, Any]:
    row = con.execute(
        "SELECT * FROM client_profiles WHERE identity_type=? AND identity_value=?",
        (identity_type.strip().lower(), identity_value.strip().lower()),
    ).fetchone()
    if not row:
        return {"promoted": False, "reason": "profile_not_found"}
    data = row_to_dict(row) or {}
    if data.get("status") == "trusted":
        return {"promoted": False, "reason": "already_trusted", "profile": data}
    seen = int(data.get("seen_count") or 0)
    structured = int(data.get("structured_count") or 0)
    if seen < min_seen or structured < min_structured:
        return {"promoted": False, "reason": "not_enough_evidence", "seen_count": seen, "structured_count": structured}
    buyer_code = data.get("buyer_code")
    buyer_name = data.get("buyer_name") or buyer_code
    upsert_buyer_identity(
        con,
        identity_type=identity_type,
        identity_value=identity_value,
        buyer_code=buyer_code,
        buyer_name=buyer_name,
        source="auto_promoted_profile",
        confidence=promote_confidence,
        confirmed=False,
    )
    con.execute(
        "UPDATE client_profiles SET status='trusted', confidence=MAX(confidence, ?), promoted_count=promoted_count+1, updated_at=? WHERE id=?",
        (float(promote_confidence), utcnow(), int(data["id"])),
    )
    record_learning_event(
        con,
        kind="client_profile_auto_promoted",
        source="autonomous_learning",
        confidence=promote_confidence,
        payload={"identity_type": identity_type, "identity_value": identity_value, "buyer_code": buyer_code, "seen_count": seen, "structured_count": structured},
    )
    return {"promoted": True, "buyer_code": buyer_code, "buyer_name": buyer_name, "seen_count": seen, "structured_count": structured}


def upsert_field_pattern_candidate(
    con: sqlite3.Connection,
    *,
    buyer_code: str,
    field_name: str,
    value_sample: str,
    source: str = "observed",
    confidence: float = 0.2,
    evidence: dict[str, Any] | None = None,
    pattern_regex: str | None = None,
    context_before: str | None = None,
    context_after: str | None = None,
) -> None:
    buyer_code = (buyer_code or "").strip()
    field_name = (field_name or "").strip()
    value_sample = str(value_sample or "").strip()[:120]
    if not buyer_code or not field_name or not value_sample:
        return
    now = utcnow()
    existing = con.execute(
        "SELECT id, pattern_regex FROM field_pattern_candidates WHERE buyer_code=? AND field_name=? AND value_sample=?",
        (buyer_code, field_name, value_sample),
    ).fetchone()
    if existing:
        # Обновляем: увеличиваем confidence и seen_count; обновляем regex если он стал лучше
        new_regex = pattern_regex or existing["pattern_regex"]
        con.execute(
            """
            UPDATE field_pattern_candidates
               SET confidence=MIN(0.92, confidence + ?), seen_count=seen_count+1,
                   source=CASE WHEN ? IN ('ai_applied','ai_training','operator_correction') THEN ? ELSE source END,
                   pattern_regex=COALESCE(?, pattern_regex),
                   context_before=COALESCE(?, context_before),
                   context_after=COALESCE(?, context_after),
                   evidence_json=?, updated_at=?
             WHERE id=?
            """,
            (float(confidence) * 0.15, source, source, new_regex, context_before, context_after,
             dumps(evidence or {}), now, int(existing["id"])),
        )
    else:
        con.execute(
            """
            INSERT INTO field_pattern_candidates(
                buyer_code, field_name, value_sample, source, confidence,
                pattern_regex, context_before, context_after,
                evidence_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (buyer_code, field_name, value_sample, source, float(confidence),
             pattern_regex, context_before, context_after,
             dumps(evidence or {}), now, now),
        )


def _outbox_channels() -> list[str]:
    mode = str(getattr(settings, "one_c_export_mode", "file") or "file").strip().lower()
    # local_receiver — безопасный режим: НИКАКОГО внешнего file/http-канала автодоставки.
    # Пакеты попадают в локальный приёмник 1С только ручной командой (local-send), а не воркером.
    if mode in {"off", "none", "disabled", "0", "local_receiver", "local"}:
        return []
    if mode == "both":
        return ["file", "http"]
    if mode in {"file", "http"}:
        return [mode]
    return ["file"]


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _dynamic_control(data: dict[str, Any]) -> dict[str, Any]:
    payload = data.get("payload") or {}
    control = dict(payload.get("control") or {})
    state = str(data.get("state") or "")
    event_type = str(data.get("event_type") or "unknown")
    missing = data.get("missing") or []
    quality = data.get("quality") or []
    if not control:
        if int(data.get("ready_for_export") or 0) and state == "ready_to_1c":
            control = {"status": "ready_to_1c", "action": "send_to_1c", "owner": "system"}
        elif state == "needs_link":
            control = {"status": "needs_link", "action": "link_to_existing_case", "owner": "operator_or_1c"}
        elif event_type == "followup_reminder":
            control = {"status": "customer_reminder", "action": "raise_priority_existing_case", "owner": "operator_or_1c"}
        elif state == "needs_review":
            control = {"status": "needs_review", "action": "review_or_run_ai", "owner": "system_then_operator"}
        else:
            control = {"status": state or event_type, "action": "record_control_event", "owner": "system"}
    deadline = _parse_iso_datetime(data.get("deadline_at") or control.get("deadline_at"))
    now_dt = datetime.now(timezone.utc).replace(microsecond=0)
    if deadline is not None:
        delta_h = (deadline - now_dt).total_seconds() / 3600
        control["deadline_at"] = deadline.replace(microsecond=0).isoformat()
        control["hours_left"] = round(delta_h, 2)
        control["overdue"] = delta_h < 0
        control["overdue_hours"] = round(abs(delta_h), 2) if delta_h < 0 else None
        warning_h = float(getattr(settings, "sla_warning_hours", 24) or 24)
        control["due_soon"] = 0 <= delta_h <= warning_h
        if delta_h < 0 and not str(control.get("status") or "").startswith("overdue_") and str(control.get("status")) not in {"ready_to_1c", "ignored_internal"}:
            control["status"] = "overdue_" + str(control.get("status") or "unknown")
            control["action"] = "escalate_" + str(control.get("action") or "record_control_event")
    control.setdefault("missing", missing)
    control.setdefault("quality_issue_codes", [q.get("code") for q in quality if isinstance(q, dict)])
    return control


def _case_event_type(data: dict[str, Any], related: list[dict[str, Any]]) -> str:
    state = str(data.get("state") or "")
    event_type = str(data.get("event_type") or "unknown")
    quality = data.get("quality") or []
    missing = set(data.get("missing") or [])
    control = _dynamic_control(data)
    control_status = str(control.get("status") or "")
    if bool(control.get("overdue")) and state not in {"ready_to_1c", "ignored_internal", "closed", "exported"}:
        return "sla_overdue"
    if bool(control.get("due_soon")) and state not in {"ready_to_1c", "ignored_internal", "closed", "exported"}:
        return "sla_due_soon"
    if control_status in {"waiting_customer_documents", "overdue_waiting_customer_documents"}:
        return "waiting_customer_documents"
    if int(data.get("ready_for_export") or 0) and state == "ready_to_1c":
        if related:
            return "duplicate_or_repeat_ready"
        return "return_ready"
    if event_type == "followup_reminder":
        return "followup_reminder"
    if event_type == "followup_dialog":
        return "followup_dialog"
    if event_type == "supplier_decision":
        return "supplier_decision"
    if state == "needs_link":
        return "needs_link"
    if related and event_type == "new_return":
        return "duplicate_or_repeat"
    if any(q.get("code") in {"missing_photo_evidence", "missing_service_document"} for q in quality) or {"photo_evidence", "service_document"} & missing:
        return "blocked_missing_evidence"
    if state == "needs_review":
        return "needs_review"
    if state in {"ignored_internal", "context_sent", "linked_event"}:
        return "status_update"
    return "status_update"


def _control_action(event_type: str, data: dict[str, Any]) -> str:
    if event_type == "sla_overdue":
        return "escalate_overdue_case"
    if event_type == "sla_due_soon":
        return "prioritize_before_deadline"
    if event_type == "waiting_customer_documents":
        return "request_missing_documents_or_photos"
    if event_type == "return_ready":
        return "create_or_update_return"
    if event_type == "duplicate_or_repeat_ready":
        return "prioritize_existing_or_merge_return"
    if event_type in {"followup_reminder", "duplicate_or_repeat"}:
        return "prioritize_existing_case"
    if event_type in {"followup_dialog", "supplier_decision", "status_update"}:
        return "append_to_case_history"
    if event_type == "needs_link":
        return "request_link_to_existing_case"
    if event_type in {"needs_review", "blocked_missing_evidence"}:
        return "request_operator_review"
    return "record_control_event"


def _event_hash(payload: dict[str, Any]) -> str:
    compact = dumps(payload)
    return hashlib.sha1(compact.encode("utf-8")).hexdigest()[:20]


def _load_case_for_outbox(con: sqlite3.Connection, case_id: int) -> dict[str, Any] | None:
    row = con.execute(
        """
        SELECT c.*, e.mailbox, e.uid, e.message_id, e.in_reply_to, e.references_json, e.subject, e.from_addr,
               e.to_addr, e.cc_addr, e.direction, e.folder_seen_json, e.received_at, e.snippet
        FROM cases c JOIN raw_emails e ON e.id = c.raw_email_id
        WHERE c.id=?
        """,
        (case_id,),
    ).fetchone()
    return row_to_dict(row) if row else None


def _related_cases_for_event(con: sqlite3.Connection, data: dict[str, Any]) -> list[dict[str, Any]]:
    strong_key = data.get("strong_key")
    if not strong_key:
        return []
    rows = con.execute(
        """
        SELECT id, state, event_type, priority, deadline_at, ready_for_export, updated_at
        FROM cases
        WHERE strong_key=? AND id<>?
        ORDER BY id
        LIMIT 20
        """,
        (strong_key, int(data.get("id") or 0)),
    ).fetchall()
    return [dict(r) for r in rows]


def _clean_from_export(export: dict[str, Any], dotted_path: str) -> Any:
    """Достаёт значение из export-структуры по точечному пути (doc.number, claim.comment)."""
    parts = dotted_path.split(".")
    val: Any = export
    for part in parts:
        if not isinstance(val, dict):
            return None
        val = val.get(part)
    if val is None:
        return None
    if isinstance(val, (int, float, str, bool)):
        return val
    return None


def _item_field(export: dict[str, Any], field_name: str) -> Any:
    """Достаёт поле из первого items[] в export."""
    items = export.get("items") or []
    if not items:
        return None
    return items[0].get(field_name) or None


_PRE_DELIVERY_REASON = "Отказ клиента до поставки"


def mark_pre_delivery_refusal_payload(payload: dict[str, Any], *, reason: str | None = None) -> dict[str, Any]:
    """Пометить payload как отказ клиента ДО поставки: документ реализации неприменим.

    Чистый helper (без БД) — выставляет document_required=false и обнуляет документные поля.
    """
    ret = payload.setdefault("return", {})
    ret["document_number"] = None
    ret["document_date"] = None
    ret["document_type"] = None
    payload["event_type"] = "pre_delivery_refusal"
    payload["pre_delivery_refusal"] = True
    payload["document_required"] = False
    payload["document_number"] = None
    payload["document_date"] = None
    payload["reason"] = reason or _PRE_DELIVERY_REASON
    payload["operator_attention"] = True
    return payload


_DEFECT_KINDS = {"defect", "nonconforming"}
_PDF_HINT = (".pdf",)
_XLS_HINT = (".xls", ".xlsx")


def build_defect_flags(payload: dict[str, Any]) -> dict[str, Any]:
    """Компактные defect/evidence-флаги для 1С (по quality.evidence + claim_kind).

    Без vision: только по наличию файлов/ссылок и расширениям имён. Не падает на пустых данных.
    """
    quality = payload.get("quality") or {}
    ev = quality.get("evidence") or {}
    ret = payload.get("return") or {}
    claim_kind = ret.get("claim_kind") or payload.get("claim_kind")
    docs = (ev.get("documents") or []) + (ev.get("service_documents") or [])

    def _has_ext(items: list, exts: tuple) -> bool:
        for it in items or []:
            name = str((it or {}).get("filename") or "").lower()
            ctype = str((it or {}).get("content_type") or "").lower()
            if any(name.endswith(e) for e in exts) or ("pdf" in ctype and ".pdf" in exts):
                return True
        return False

    has_photos = bool(ev.get("has_photo"))
    has_pdf = _has_ext(docs, _PDF_HINT) or _has_ext(ev.get("photos") or [], _PDF_HINT)
    has_excel = _has_ext(docs, _XLS_HINT)
    has_links = bool(ev.get("has_return_link")) or int(ev.get("links_count") or 0) > 0
    has_service_doc = bool(ev.get("has_service_document"))
    has_defect_documents = bool(has_pdf or has_service_doc)
    attachments_count = int(ev.get("attachments_count") or 0)
    is_defect = claim_kind in _DEFECT_KINDS

    missing = bool(is_defect and attachments_count == 0 and not has_links)
    incomplete = bool(is_defect and (has_photos or attachments_count > 0) and not has_defect_documents)
    complete = bool(is_defect and has_defect_documents)
    possible_photo_document = bool(has_photos and not has_defect_documents)
    operator_attention = bool(payload.get("operator_attention") or missing or incomplete)
    vision_allowed = bool(
        getattr(settings, "defect_doc_ai_read", False)
        and getattr(settings, "defect_vision_enabled", False)
        and getattr(settings, "ai_vision_enabled", False)
    )
    if vision_allowed:
        documents_status = "complete" if complete else ("incomplete" if incomplete else "missing")
    elif attachments_count:
        documents_status = "metadata_only"
    else:
        documents_status = "unknown_not_read"
    if is_defect and not vision_allowed:
        operator_attention = True

    return {
        "claim_kind": claim_kind,
        "has_attachments": attachments_count > 0,
        "has_images": has_photos,
        "has_photos": has_photos,
        "has_pdf_documents": has_pdf,
        "has_excel_documents": has_excel,
        "has_external_links": has_links,
        "has_defect_documents": has_defect_documents,
        "defect_documents_missing": missing,
        "defect_documents_incomplete": incomplete,
        "defect_documents_complete": complete,
        "possible_photo_document": possible_photo_document,
        "operator_attention": operator_attention,
        "attachments_count": attachments_count,
        "defect_documents_status": documents_status,
        "attachment_strategy": getattr(settings, "defect_attachment_strategy", "metadata_only"),
        "read_pdf_first": bool(getattr(settings, "defect_read_pdf_first", True)),
        "images_order": getattr(settings, "defect_read_images_order", "first_last_then_inner"),
        "max_images": max(0, int(getattr(settings, "max_defect_images_per_case", 2) or 0)),
        "vision_enabled": vision_allowed,
    }


def apply_one_c_payload_profile(payload: dict[str, Any], profile: str | None = None) -> dict[str, Any]:
    """Чистая схема 1С v2: бизнес-основа + блоки по тумблерам (несём всё, выключаем ненужное).

    minimal/standard — чистый v2 (основа: клиент/документ/позиции; блоки comment/flags/status/text/
      attachments/source включаются настройками ONE_C_INCLUDE_*).
    debug            — полный payload как есть (QA/evidence/gate — для аудита, НЕ для боевой 1С).
    """
    profile = str(profile or getattr(settings, "one_c_payload_profile", "standard") or "standard").strip().lower()
    if profile not in {"minimal", "standard", "debug"}:
        profile = "standard"
    if profile == "debug":
        return payload

    def on(attr: str, default: bool = True) -> bool:
        return bool(getattr(settings, attr, default))

    ev = payload.get("event") or {}
    ret = payload.get("return") or {}
    src = payload.get("source_email") or {}
    export = payload.get("export_ready_payload") or payload.get("export_data") or {}

    # Позиции: приоритет export (мультипозиция + received_part_number у пересорта), иначе одиночная из return.
    items = [dict(it) for it in (export.get("items") or [])]
    if not items and (ret.get("part_number") or ret.get("product_name")):
        single = {k: ret.get(k) for k in ("part_number", "brand", "product_name", "quantity", "price")
                  if ret.get(k) is not None}
        if ret.get("received_part_number"):
            single["received_part_number"] = ret.get("received_part_number")
        items = [single] if single else []
    if not on("one_c_include_price"):
        items = [{k: v for k, v in it.items() if k != "price"} for it in items]

    out: dict[str, Any] = {
        "schema": "readmail-1c-v2",
        "payload_profile": profile,
        "generated_at": payload.get("generated_at"),
        "event": ev.get("type"),
        "case_id": (payload.get("case") or {}).get("id"),
        "buyer": payload.get("buyer") or {},
        "claim": {
            "kind": ret.get("claim_kind"),
            "kind_label": ret.get("claim_kind_label")
            or _claim_kind_label_ru(ret.get("claim_kind"), source_event_type=ev.get("source_event_type") or ev.get("type")),
            "number": ret.get("claim_number") or ret.get("client_request_number") or ret.get("return_number"),
        },
        "document": {"number": ret.get("document_number"), "date": ret.get("document_date"),
                     "type": ret.get("document_type")},
        "items": items,
    }
    # comment / причина текстом
    if on("one_c_include_comment"):
        out["comment"] = ret.get("comment")
    # флаги (документы брака + наличие фото/документов)
    if on("one_c_include_defect_flags"):
        flags: dict[str, Any] = {}
        dd = export.get("defect_documents") or payload.get("defect_documents")
        if dd:
            flags["defect_documents"] = dd
        evd = (payload.get("quality") or {}).get("evidence") or {}
        if evd:
            flags["has_photo"] = bool(evd.get("has_photo"))
            flags["has_document"] = bool(evd.get("has_document"))
            flags["attachments_count"] = evd.get("attachments_count")
        if flags:
            out["flags"] = flags
    # статусы (состояние/контроль/приоритет)
    if on("one_c_include_status"):
        out["status"] = {"state": ev.get("state"), "control_status": ev.get("control_status"),
                         "priority": ev.get("priority"), "ready_for_export": ev.get("ready_for_export"),
                         "needs_review": ev.get("needs_review")}
    # тело и тема письма
    if on("one_c_include_text"):
        out["text"] = {"subject": src.get("subject"), "body": src.get("body") or src.get("snippet")}
    # описание вложений
    if on("one_c_include_attachments"):
        out["attachments"] = payload.get("attachments") or []
    # источник (для трассировки)
    if on("one_c_include_source"):
        out["source"] = {"raw_email_id": src.get("raw_email_id"), "message_id": src.get("message_id"),
                         "received_at": src.get("received_at")}
    # pre_delivery-поля — важны для 1С, несём всегда.
    for key in ("pre_delivery_refusal", "document_required", "reason", "operator_attention"):
        if key in payload:
            out[key] = payload[key]
    return out


def _claim_kind_label_ru(kind: str | None, *, source_event_type: str | None = None) -> str:
    """Русская причина возврата для 1С — чтобы пустые поля (напр. номер документа
    у отказа ДО поставки) были обоснованы человекочитаемой причиной, а не выглядели багом."""
    if source_event_type == "pre_delivery_refusal":
        return "Отказ до поставки"
    try:
        from .classifier import CLAIM_KIND_LABELS
        return CLAIM_KIND_LABELS.get(kind or "", "") or ("Возврат" if kind else "")
    except Exception:
        return ""


def build_case_event_payload(con: sqlite3.Connection, case_id: int, *, explicit_event_type: str | None = None, profile: str | None = None) -> dict[str, Any] | None:
    data = _load_case_for_outbox(con, case_id)
    if not data:
        return None
    related = _related_cases_for_event(con, data)
    event_type = explicit_event_type or _case_event_type(data, related)
    if not getattr(settings, "include_context_events_in_1c", True) and event_type == "status_update":
        return None
    fields = data.get("fields") or {}
    export = data.get("export") or {}
    stored_quality = (data.get("payload") or {}).get("_quality") or export.get("_quality") or {}
    evidence_gate = (
        (data.get("payload") or {}).get("evidence_gate")
        or export.get("evidence_gate")
        or stored_quality.get("evidence_gate")
        or {}
    )
    if event_type in {"return_ready", "duplicate_or_repeat_ready"}:
        if stored_quality and stored_quality.get("case_status") != "accepted":
            return None
        if not evidence_gate or not evidence_gate.get("passed", False):
            return None
    if (
        data.get("state") == "linked_event"
        or str(data.get("event_type") or "").startswith("followup_")
    ) and evidence_gate.get("weak_followup_link"):
        return None
    control = _dynamic_control(data)
    payload = {
        "schema_version": "readmail-new-1c-event-v1.10",
        "generated_at": utcnow(),
        "event": {
            "type": event_type,
            "source_event_type": data.get("event_type"),
            "state": data.get("state"),
            "action": _control_action(event_type, data),
            "priority": data.get("priority"),
            "deadline_at": data.get("deadline_at"),
            "confidence": data.get("confidence"),
            "control_status": control.get("status"),
            "control_action": control.get("action"),
            "control_owner": control.get("owner"),
            "hours_left": control.get("hours_left"),
            "overdue": bool(control.get("overdue")),
            "due_soon": bool(control.get("due_soon")),
            "ready_for_export": bool(data.get("ready_for_export")),
            "needs_review": bool(data.get("needs_review")),
        },
        "case": {
            "id": data.get("id"),
            "strong_key": data.get("strong_key"),
            "thread_key": data.get("thread_key"),
            "weak_key": data.get("weak_key"),
            "related_case_ids": [r.get("id") for r in related],
            "related_cases": related,
        },
        "buyer": {"code": data.get("buyer_code"), "name": data.get("buyer_name")},
        "return": {
            "claim_kind": data.get("claim_kind"),
            "claim_kind_label": _claim_kind_label_ru(data.get("claim_kind"), source_event_type=data.get("event_type")),
            "status": data.get("status"),
            # Чистые данные из export (build_export_json), с fallback на сырые fields
            "document_number": _clean_from_export(export, "document.number") or fields.get("document_number"),
            "document_date": _clean_from_export(export, "document.date") or fields.get("document_date"),
            "document_type": _clean_from_export(export, "document.type") or fields.get("document_type", "УПД"),
            "claim_number": _clean_from_export(export, "claim.claim_number") or fields.get("claim_number"),
            "client_request_number": _clean_from_export(export, "claim.client_request_number") or fields.get("client_request_number"),
            "return_number": _clean_from_export(export, "claim.return_number") or fields.get("return_number"),
            "comment": _clean_from_export(export, "claim.comment") or fields.get("comment"),
            # items[0] — первый (и обычно единственный) товар
            "part_number": _item_field(export, "part_number") or fields.get("part_number"),
            "brand": _item_field(export, "brand") or fields.get("brand"),
            "product_name": _item_field(export, "product_name") or fields.get("product_name"),
            "quantity": _item_field(export, "quantity") or fields.get("quantity"),
            "price": _item_field(export, "price") or fields.get("price"),
        },
        "export_data": export if export and export != {} else None,
        "source_email": {
            "raw_email_id": data.get("raw_email_id"),
            "mailbox": data.get("mailbox"),
            "folders_seen": data.get("folder_seen") or [data.get("mailbox")],
            "uid": data.get("uid"),
            "message_id": data.get("message_id"),
            "in_reply_to": data.get("in_reply_to"),
            "references": data.get("references") or [],
            "subject": data.get("subject"),
            "from": data.get("from_addr"),
            "to": data.get("to_addr"),
            "cc": data.get("cc_addr"),
            "received_at": data.get("received_at"),
            "direction": data.get("direction") or (data.get("payload") or {}).get("direction"),
            "snippet": data.get("snippet"),
        },
        "control": control,
        "quality": {
            "missing": data.get("missing") or [],
            "issues": data.get("quality") or [],
            "gate": stored_quality or None,
            "evidence_gate": evidence_gate or None,
            "evidence": (data.get("payload") or {}).get("evidence") or export.get("evidence") or {},
            "evidence_requirements": (data.get("payload") or {}).get("evidence_requirements") or export.get("evidence_requirements") or {},
        },
        "export_ready_payload": export if event_type in {"return_ready", "duplicate_or_repeat_ready"} else None,
    }
    # Данные вложений + тело письма — для v2-блоков text/attachments (включаются тумблерами).
    try:
        rid = data.get("raw_email_id")
        if rid:
            atts = con.execute(
                "SELECT filename, content_type, COALESCE(size_bytes,0) sz FROM attachments "
                "WHERE raw_email_id=? ORDER BY id", (rid,)).fetchall()
            payload["attachments"] = [{"filename": a["filename"], "type": a["content_type"], "size": a["sz"]}
                                      for a in atts]
            brow = con.execute("SELECT body_text, visible_text FROM raw_emails WHERE id=?", (rid,)).fetchone()
            if brow:
                payload["source_email"]["body"] = (brow["body_text"] or brow["visible_text"] or data.get("snippet") or "")[:4000]
    except Exception:
        pass

    # ── Отказ клиента ДО поставки: документа реализации нет → явно сообщаем 1С. ──
    pre_delivery_refusal = bool((data.get("payload") or {}).get("pre_delivery_refusal"))
    if pre_delivery_refusal:
        mark_pre_delivery_refusal_payload(payload)

    payload["event"]["fingerprint"] = _event_hash({
        "event_type": event_type,
        "case_id": payload["case"]["id"],
        "state": payload["event"]["state"],
        "priority": payload["event"]["priority"],
        "deadline_at": payload["event"]["deadline_at"],
        "control_status": payload["event"].get("control_status"),
        "control_overdue": payload["event"].get("overdue"),
        "strong_key": payload["case"]["strong_key"],
        "fields": payload["return"],
        "missing": payload["quality"]["missing"],
        "quality": payload["quality"]["issues"],
        "related": payload["case"]["related_case_ids"],
        "pre_delivery_refusal": pre_delivery_refusal,
    })
    # Полный payload хранится в outbox (для аудита/трассировки). Профиль применяется при доставке
    # в 1С (deliver_outbox_events) или явно через аргумент profile (preview/аудит-скрипт).
    if profile is not None:
        return apply_one_c_payload_profile(payload, profile)
    return payload


def queue_case_event(
    con: sqlite3.Connection,
    case_id: int,
    *,
    event_type: str | None = None,
    channels: list[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    case_data = _load_case_for_outbox(con, case_id)
    if case_data:
        stored_quality = (case_data.get("payload") or {}).get("_quality") or (case_data.get("export") or {}).get("_quality") or {}
        evidence_gate = (
            (case_data.get("payload") or {}).get("evidence_gate")
            or (case_data.get("export") or {}).get("evidence_gate")
            or stored_quality.get("evidence_gate")
            or {}
        )
        ready_event = (
            event_type in {"return_ready", "duplicate_or_repeat_ready"}
            or (
                case_data.get("state") == "ready_to_1c"
                and bool(case_data.get("ready_for_export"))
            )
        )
        if ready_event and (not evidence_gate or not evidence_gate.get("passed", False)):
            con.execute(
                """
                UPDATE cases
                SET state='needs_review', ready_for_export=0, needs_review=1, updated_at=?
                WHERE id=?
                """,
                (utcnow(), int(case_id)),
            )
            return {
                "ok": True,
                "queued": 0,
                "skipped": 1,
                "reason": "evidence_gate_failed",
                "case_id": case_id,
                "evidence_gate": evidence_gate,
            }
        if (
            case_data.get("state") == "linked_event"
            or str(case_data.get("event_type") or "").startswith("followup_")
        ) and evidence_gate.get("weak_followup_link"):
            return {
                "ok": True,
                "queued": 0,
                "skipped": 1,
                "reason": "weak_followup_link",
                "case_id": case_id,
                "evidence_gate": evidence_gate,
            }
    payload = build_case_event_payload(con, case_id, explicit_event_type=event_type)
    if payload is None:
        return {"ok": True, "queued": 0, "skipped": 1, "reason": "context_event_disabled_or_case_missing", "case_id": case_id}
    channels = channels if channels is not None else _outbox_channels()
    if not channels:
        return {"ok": True, "queued": 0, "skipped": 1, "reason": "outbox_disabled", "case_id": case_id}
    queued = 0
    skipped = 0
    items: list[dict[str, Any]] = []
    event_type_final = payload["event"]["type"]
    fingerprint = payload["event"].get("fingerprint") or _event_hash(payload)
    for channel in channels:
        channel = str(channel or "file").lower()
        event_key = f"v1.10:{channel}:case:{case_id}:{event_type_final}:{fingerprint}"
        existing = con.execute("SELECT id, status FROM outbox WHERE event_key=?", (event_key,)).fetchone()
        if existing and not force:
            skipped += 1
            items.append({"channel": channel, "queued": False, "already_exists": True, "outbox_id": int(existing["id"]), "status": existing["status"]})
            continue
        if existing and force:
            con.execute("UPDATE outbox SET status='new', attempt_count=0, last_error=NULL, sent_at=NULL, payload_json=?, created_at=? WHERE id=?", (dumps(payload), utcnow(), int(existing["id"])))
            queued += 1
            items.append({"channel": channel, "queued": True, "outbox_id": int(existing["id"]), "forced": True})
            continue
        cur = con.execute(
            """
            INSERT INTO outbox(case_id, payload_json, status, created_at, event_type, event_key, channel, business_priority)
            VALUES (?, ?, 'new', ?, ?, ?, ?, ?)
            """,
            (case_id, dumps(payload), utcnow(), event_type_final, event_key, channel, payload["event"].get("priority")),
        )
        queued += 1
        items.append({"channel": channel, "queued": True, "outbox_id": int(cur.lastrowid)})
    return {"ok": True, "case_id": case_id, "event_type": event_type_final, "queued": queued, "skipped": skipped, "items": items}


def queue_case_export(con: sqlite3.Connection, case_id: int) -> dict[str, Any]:
    data = _load_case_for_outbox(con, case_id)
    if not data:
        return {"ok": False, "error": "case_not_found", "case_id": case_id}
    if not int(data.get("ready_for_export") or 0) or data.get("state") != "ready_to_1c":
        # Still queue a control event instead of dropping the message.
        result = queue_case_event(con, case_id)
        return {**result, "not_ready_but_control_event_queued": True}
    return queue_case_event(con, case_id, event_type="return_ready")


def queue_ready_cases(con: sqlite3.Connection, limit: int = 500) -> dict[str, Any]:
    rows = con.execute(
        """
        SELECT id FROM cases
        WHERE ready_for_export=1 AND state='ready_to_1c'
        ORDER BY id
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    queued = 0
    skipped = 0
    items = []
    for row in rows:
        result = queue_case_export(con, int(row["id"]))
        queued += int(result.get("queued") or 0)
        skipped += int(result.get("skipped") or 0)
        items.append(result)
    return {"ok": True, "checked": len(rows), "queued": queued, "skipped": skipped, "items": items[:50]}


def queue_control_events(con: sqlite3.Connection, limit: int = 1000) -> dict[str, Any]:
    """Поставить в очередь 1С ТОЛЬКО кейсы, которые прошли через паттерны/AI и готовы.

    Только state='ready_to_1c' AND ready_for_export=1 попадают в outbox.
    Всё остальное (needs_review, unknown, needs_link) — остаётся в своих вкладках.
    """
    rows = con.execute(
        """
        SELECT id FROM cases
        WHERE state = 'ready_to_1c'
          AND ready_for_export = 1
        ORDER BY updated_at DESC, id DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    queued = 0
    skipped = 0
    items = []
    for row in rows:
        result = queue_case_event(con, int(row["id"]))
        queued += int(result.get("queued") or 0)
        skipped += int(result.get("skipped") or 0)
        items.append(result)
    return {"ok": True, "checked": len(rows), "queued": queued, "skipped": skipped, "items": items[:50]}


def _safe_filename(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in text)
    return cleaned.strip("._")[:120] or "event"


def _next_retry_at(attempt_count: int) -> str:
    base = int(getattr(settings, "outbox_retry_after_seconds", 300) or 300)
    # Gentle exponential backoff, capped at 6 hours.
    seconds = min(base * (2 ** max(0, int(attempt_count) - 1)), 6 * 3600)
    return (datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=seconds)).isoformat()


def _record_outbox_attempt(
    con: sqlite3.Connection,
    *,
    outbox_id: int,
    case_id: int | None,
    event_type: str | None,
    channel: str | None,
    attempt_no: int,
    ok: bool,
    started_at: str,
    status_code: int | None = None,
    error: str | None = None,
    response: dict[str, Any] | None = None,
) -> None:
    con.execute(
        """
        INSERT INTO outbox_attempts(outbox_id, case_id, event_type, channel, attempt_no, ok, status_code, error, response_json, started_at, finished_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (outbox_id, case_id, event_type, channel, int(attempt_no or 1), 1 if ok else 0, status_code, (error or None), dumps(response or {}), started_at, utcnow()),
    )


def outbox_dashboard(con: sqlite3.Connection, *, limit: int = 100) -> dict[str, Any]:
    """Operator/1C control journal: what was sent, what failed, what is due for retry, and why."""
    now = utcnow()
    status_rows = [dict(r) for r in con.execute("SELECT status, COUNT(*) count FROM outbox GROUP BY status ORDER BY count DESC").fetchall()]
    event_rows = [dict(r) for r in con.execute("SELECT event_type, COUNT(*) count FROM outbox GROUP BY event_type ORDER BY count DESC").fetchall()]
    channel_rows = [dict(r) for r in con.execute("SELECT channel, status, COUNT(*) count FROM outbox GROUP BY channel, status ORDER BY channel, status").fetchall()]
    retry_due = con.execute(
        "SELECT COUNT(*) c FROM outbox WHERE status='error' AND (next_attempt_at IS NULL OR next_attempt_at<=?)",
        (now,),
    ).fetchone()["c"]
    stuck_new = con.execute(
        "SELECT COUNT(*) c FROM outbox WHERE status='new'",
    ).fetchone()["c"]
    failed = [
        row_to_dict(r)
        for r in con.execute(
            """
            SELECT o.id, o.case_id, o.status, o.event_type, o.channel, o.business_priority,
                   o.attempt_count, o.last_attempt_at, o.next_attempt_at, o.last_error,
                   o.created_at, o.sent_at, c.state, c.deadline_at, c.buyer_code, c.buyer_name,
                   e.subject, e.from_addr, e.received_at
            FROM outbox o
            LEFT JOIN cases c ON c.id=o.case_id
            LEFT JOIN raw_emails e ON e.id=c.raw_email_id
            WHERE o.status IN ('new','error')
            ORDER BY
                CASE o.status WHEN 'error' THEN 0 ELSE 1 END,
                CASE o.business_priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
                COALESCE(o.next_attempt_at, o.created_at) ASC,
                o.id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    ]
    recent_attempts = [
        row_to_dict(r)
        for r in con.execute(
            """
            SELECT a.id, a.outbox_id, a.case_id, a.event_type, a.channel, a.attempt_no, a.ok,
                   a.status_code, a.error, a.response_json, a.started_at, a.finished_at
            FROM outbox_attempts a
            ORDER BY a.id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    ]
    return {
        "ok": True,
        "schema": "readmail-new-outbox-dashboard-v1",
        "generated_at": now,
        "counts": {
            "by_status": status_rows,
            "by_event_type": event_rows,
            "by_channel_status": channel_rows,
            "retry_due": retry_due,
            "stuck_new": stuck_new,
        },
        "attention": failed,
        "recent_attempts": recent_attempts,
    }


def reset_outbox_errors(con: sqlite3.Connection, *, limit: int = 500) -> dict[str, Any]:
    rows = con.execute(
        """
        SELECT id FROM outbox
        WHERE status='error'
        ORDER BY
            CASE business_priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
            COALESCE(next_attempt_at, created_at) ASC,
            id ASC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    ids = [int(r["id"]) for r in rows]
    if ids:
        placeholders = ",".join("?" for _ in ids)
        con.execute(
            f"UPDATE outbox SET status='new', next_attempt_at=NULL, last_error=NULL, resolved_at=NULL, resolution_note=NULL WHERE id IN ({placeholders})",
            ids,
        )
    return {"ok": True, "reset": len(ids), "ids": ids[:100]}


def reconcile_outbox_events(con: sqlite3.Connection, *, limit: int = 1000) -> dict[str, Any]:
    """Make sure every meaningful case has a queued control event, then retry due delivery errors if enabled by operator."""
    queued = queue_control_events(con, limit=limit)
    dashboard = outbox_dashboard(con, limit=50)
    return {"ok": True, "queued": queued, "dashboard": dashboard}


def deliver_outbox_events(con: sqlite3.Connection, *, limit: int = 100, channel: str | None = None) -> dict[str, Any]:
    """Deliver queued outbox events to file and/or HTTP. Designed to be safe to retry and audited per attempt."""
    now = utcnow()
    filters = ["status IN ('new','error')", "(next_attempt_at IS NULL OR next_attempt_at<=?)"]
    params: list[Any] = [now]
    if channel:
        filters.append("channel=?")
        params.append(channel)
    max_attempts = int(getattr(settings, "outbox_max_attempts", 8) or 8)
    rows = con.execute(
        f"""
        SELECT id, case_id, payload_json, event_type, channel, attempt_count
        FROM outbox
        WHERE {' AND '.join(filters)}
          AND attempt_count < ?
        ORDER BY
          CASE business_priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
          COALESCE(next_attempt_at, created_at) ASC,
          id ASC
        LIMIT ?
        """,
        (*params, max_attempts, int(limit)),
    ).fetchall()
    delivered = 0
    failed = 0
    items: list[dict[str, Any]] = []
    for row in rows:
        outbox_id = int(row["id"])
        attempt_no = int(row["attempt_count"] or 0) + 1
        # В outbox хранится полный payload (аудит); в 1С уходит профилированный (minimal/standard).
        payload = apply_one_c_payload_profile(loads(row["payload_json"], {}) or {})
        ch = str(row["channel"] or "file")
        started_at = utcnow()
        try:
            if ch == "file":
                root = Path(getattr(settings, "one_c_file_dir", "/app/data/outbox_1c"))
                root.mkdir(parents=True, exist_ok=True)
                name = _safe_filename(f"{outbox_id}_{row['event_type']}_case_{row['case_id']}.json")
                path = root / name
                path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                response = {"ok": True, "file_path": str(path)}
                con.execute(
                    "UPDATE outbox SET status='sent', sent_at=?, last_attempt_at=?, attempt_count=attempt_count+1, file_path=?, next_attempt_at=NULL, last_error=NULL, delivery_response_json=? WHERE id=?",
                    (utcnow(), started_at, str(path), dumps(response), outbox_id),
                )
                _record_outbox_attempt(con, outbox_id=outbox_id, case_id=int(row["case_id"]), event_type=row["event_type"], channel=ch, attempt_no=attempt_no, ok=True, started_at=started_at, response=response)
                delivered += 1
                items.append({"id": outbox_id, "channel": ch, "ok": True, "file_path": str(path)})
            elif ch == "http":
                url = str(getattr(settings, "one_c_http_url", "") or "").strip()
                if not url:
                    raise RuntimeError("ONE_C_HTTP_URL is empty")
                import httpx
                headers = {"Content-Type": "application/json"}
                token = str(getattr(settings, "one_c_http_token", "") or "").strip()
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                timeout = float(getattr(settings, "one_c_http_timeout_seconds", 20) or 20)
                verify = bool(getattr(settings, "one_c_http_verify_tls", True))
                with httpx.Client(timeout=timeout, verify=verify) as client:
                    resp = client.post(url, headers=headers, json=payload)
                if resp.status_code >= 400:
                    raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")
                response = {"ok": True, "status_code": resp.status_code, "text": resp.text[:1000]}
                con.execute(
                    "UPDATE outbox SET status='sent', sent_at=?, last_attempt_at=?, attempt_count=attempt_count+1, next_attempt_at=NULL, last_error=NULL, delivery_response_json=? WHERE id=?",
                    (utcnow(), started_at, dumps(response), outbox_id),
                )
                _record_outbox_attempt(con, outbox_id=outbox_id, case_id=int(row["case_id"]), event_type=row["event_type"], channel=ch, attempt_no=attempt_no, ok=True, started_at=started_at, status_code=resp.status_code, response=response)
                delivered += 1
                items.append({"id": outbox_id, "channel": ch, "ok": True, "status_code": resp.status_code})
            else:
                raise RuntimeError(f"Unsupported outbox channel: {ch}")
        except Exception as exc:
            failed += 1
            err = str(exc)[:1000]
            next_at = _next_retry_at(attempt_no)
            con.execute(
                "UPDATE outbox SET status='error', last_attempt_at=?, attempt_count=attempt_count+1, next_attempt_at=?, last_error=?, delivery_response_json=? WHERE id=?",
                (started_at, next_at, err, dumps({"ok": False, "error": err, "next_attempt_at": next_at}), outbox_id),
            )
            _record_outbox_attempt(con, outbox_id=outbox_id, case_id=int(row["case_id"]), event_type=row["event_type"], channel=ch, attempt_no=attempt_no, ok=False, started_at=started_at, error=err, response={"ok": False, "error": err, "next_attempt_at": next_at})
            items.append({"id": outbox_id, "channel": ch, "ok": False, "error": err, "next_attempt_at": next_at})
    dashboard = outbox_dashboard(con, limit=50)
    return {"ok": True, "checked": len(rows), "delivered": delivered, "failed": failed, "items": items[:100], "dashboard": dashboard}




def search_cases(con: sqlite3.Connection, query: str, *, by: str = "auto", limit: int = 50) -> dict[str, Any]:
    """Унифицированный поиск/трассировка кейса по любому идентификатору.

    Поддерживает: case_id, raw_email_id, outbox_id, document_number, part_number, message_id, export_id.
    by='auto' — числовой запрос ищется по id-полям, текстовый — по document/part/message/export.
    Возвращает кейсы со связанными outbox_id и исходным письмом, чтобы оператор нашёл timeline.
    """
    q = str(query or "").strip()
    result: dict[str, Any] = {"ok": True, "query": q, "by": by, "items": []}
    if not q:
        return {**result, "ok": False, "error": "empty_query"}

    case_ids: dict[int, list[str]] = {}

    def add(cid: Any, matched_by: str) -> None:
        try:
            cid = int(cid)
        except (TypeError, ValueError):
            return
        case_ids.setdefault(cid, [])
        if matched_by not in case_ids[cid]:
            case_ids[cid].append(matched_by)

    is_num = q.isdigit()
    fields = [by] if by and by != "auto" else (
        ["case_id", "raw_email_id", "outbox_id"] if is_num
        else ["message_id", "document_number", "part_number", "export_id"]
    )
    like = f"%{q}%"
    for field in fields:
        try:
            if field == "case_id" and is_num:
                for r in con.execute("SELECT id FROM cases WHERE id=?", (int(q),)):
                    add(r["id"], "case_id")
            elif field == "raw_email_id" and is_num:
                for r in con.execute("SELECT id FROM cases WHERE raw_email_id=?", (int(q),)):
                    add(r["id"], "raw_email_id")
            elif field == "outbox_id" and is_num:
                for r in con.execute("SELECT case_id FROM outbox WHERE id=?", (int(q),)):
                    add(r["case_id"], "outbox_id")
            elif field == "message_id":
                for r in con.execute(
                    "SELECT c.id FROM cases c JOIN raw_emails e ON e.id=c.raw_email_id "
                    "WHERE e.message_id LIKE ? LIMIT ?", (like, int(limit))):
                    add(r["id"], "message_id")
            elif field == "document_number":
                for r in con.execute(
                    "SELECT id FROM cases WHERE fields_json LIKE ? LIMIT ?",
                    (f'%"document_number"%{q}%', int(limit))):
                    add(r["id"], "document_number")
            elif field == "part_number":
                for r in con.execute(
                    "SELECT id FROM cases WHERE fields_json LIKE ? LIMIT ?",
                    (f'%"part_number"%{q}%', int(limit))):
                    add(r["id"], "part_number")
            elif field == "export_id":
                for r in con.execute(
                    "SELECT id FROM cases WHERE export_json LIKE ? LIMIT ?", (like, int(limit))):
                    add(r["id"], "export_id")
        except sqlite3.OperationalError:
            continue

    items: list[dict[str, Any]] = []
    for cid in list(case_ids)[:limit]:
        row = con.execute(
            """
            SELECT c.id, c.raw_email_id, c.buyer_code, c.event_type, c.claim_kind, c.state,
                   c.fields_json, e.message_id, e.subject, e.mailbox, e.uid
            FROM cases c LEFT JOIN raw_emails e ON e.id=c.raw_email_id
            WHERE c.id=?
            """,
            (cid,),
        ).fetchone()
        if not row:
            continue
        flds = loads(row["fields_json"], {}) or {}
        outbox_ids = [int(o["id"]) for o in con.execute(
            "SELECT id FROM outbox WHERE case_id=? ORDER BY id", (cid,)).fetchall()]
        items.append({
            "case_id": row["id"],
            "raw_email_id": row["raw_email_id"],
            "buyer_code": row["buyer_code"],
            "event_type": row["event_type"],
            "claim_kind": row["claim_kind"],
            "state": row["state"],
            "document_number": flds.get("document_number"),
            "part_number": flds.get("part_number"),
            "message_id": row["message_id"],
            "subject": row["subject"],
            "mailbox": row["mailbox"],
            "uid": row["uid"],
            "outbox_ids": outbox_ids,
            "matched_by": case_ids[cid],
        })
    result["items"] = items
    result["total"] = len(items)
    return result


def control_dashboard(con: sqlite3.Connection, *, limit: int = 100) -> dict[str, Any]:
    """Business-control screen: today, overdue, waiting, ready, and not delivered."""
    rows = [
        row_to_dict(r)
        for r in con.execute(
            """
            SELECT c.*, e.subject, e.from_addr, e.received_at, e.snippet, e.direction
            FROM cases c JOIN raw_emails e ON e.id=c.raw_email_id
            WHERE c.state NOT IN ('closed','exported','ignored_internal')
            ORDER BY
                CASE c.priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
                COALESCE(c.deadline_at, e.received_at) ASC, c.id DESC
            LIMIT ?
            """,
            (int(limit) * 5,),
        ).fetchall()
    ]
    buckets = {
        "overdue": [],
        "due_soon": [],
        "waiting_customer": [],
        "needs_operator": [],
        "ready": [],
        "followups": [],
        "under_control": [],
    }
    for item in rows:
        control = _dynamic_control(item)
        slim = {
            "id": item.get("id"),
            "buyer_code": item.get("buyer_code"),
            "buyer_name": item.get("buyer_name"),
            "event_type": item.get("event_type"),
            "claim_kind": item.get("claim_kind"),
            "state": item.get("state"),
            "priority": item.get("priority"),
            "deadline_at": item.get("deadline_at"),
            "subject": item.get("subject"),
            "from_addr": item.get("from_addr"),
            "received_at": item.get("received_at"),
            "snippet": item.get("snippet"),
            "missing": item.get("missing") or [],
            "control": control,
        }
        status = str(control.get("status") or "")
        if control.get("overdue"):
            buckets["overdue"].append(slim)
        elif control.get("due_soon"):
            buckets["due_soon"].append(slim)
        if status.endswith("waiting_customer_documents") or control.get("owner") == "customer":
            buckets["waiting_customer"].append(slim)
        elif status in {"needs_review", "needs_business_key", "needs_buyer_identification", "needs_link"} or status.startswith("overdue_needs"):
            buckets["needs_operator"].append(slim)
        elif status == "ready_to_1c":
            buckets["ready"].append(slim)
        elif item.get("event_type") in {"followup_reminder", "followup_dialog", "supplier_decision"}:
            buckets["followups"].append(slim)
        else:
            buckets["under_control"].append(slim)
    for key in buckets:
        buckets[key] = buckets[key][:int(limit)]
    not_delivered = [
        row_to_dict(r)
        for r in con.execute(
            """
            SELECT o.id, o.case_id, o.event_type, o.status, o.channel, o.business_priority,
                   o.attempt_count, o.last_error, o.next_attempt_at, o.created_at, o.last_attempt_at,
                   c.deadline_at, c.state, e.subject, e.from_addr
            FROM outbox o
            LEFT JOIN cases c ON c.id=o.case_id
            LEFT JOIN raw_emails e ON e.id=c.raw_email_id
            WHERE o.status IN ('new','error')
            ORDER BY CASE o.status WHEN 'error' THEN 0 ELSE 1 END,
                     CASE o.business_priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
                     COALESCE(o.next_attempt_at, o.created_at) ASC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    ]
    return {
        "ok": True,
        "schema": "readmail-new-control-dashboard-v1.10",
        "generated_at": utcnow(),
        "counts": {k: len(v) for k, v in buckets.items()} | {"not_delivered": len(not_delivered)},
        "buckets": buckets,
        "not_delivered": not_delivered,
    }


def record_process_event(
    con: sqlite3.Connection,
    *,
    stage: str,
    message: str,
    level: str = "info",
    case_id: int | None = None,
    raw_email_id: int | None = None,
    subject: str | None = None,
    details: dict[str, Any] | None = None,
) -> int:
    """Append an operator-visible timeline event.

    This is intentionally small and append-only: it is for live observability, not business truth.
    """
    cur = con.execute(
        """
        INSERT INTO process_events(stage, level, message, case_id, raw_email_id, subject, details_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            stage,
            level,
            message,
            case_id,
            raw_email_id,
            subject,
            dumps(details or {}),
            utcnow(),
        ),
    )
    return int(cur.lastrowid)


def list_process_events(con: sqlite3.Connection, *, limit: int = 200, since_id: int = 0, stage: str | None = None) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 200), 1000))
    params: list[Any] = [int(since_id or 0)]
    where = "WHERE id > ?"
    if stage and stage != "all":
        where += " AND stage = ?"
        params.append(stage)
    params.append(limit)
    rows = con.execute(
        f"""
        SELECT id, stage, level, message, case_id, raw_email_id, subject, details_json, created_at
        FROM process_events
        {where}
        ORDER BY id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [row_to_dict(r) for r in rows]


def process_event_dashboard(con: sqlite3.Connection, *, limit: int = 200) -> dict[str, Any]:
    rows = list_process_events(con, limit=limit)
    counts = [dict(r) for r in con.execute(
        "SELECT stage, level, COUNT(*) count FROM process_events GROUP BY stage, level ORDER BY count DESC"
    ).fetchall()]
    latest = con.execute("SELECT MAX(id) id FROM process_events").fetchone()["id"] or 0
    return {"latest_id": latest, "counts": counts, "items": rows}


def clear_process_events(con: sqlite3.Connection, *, keep_last: int = 0) -> dict[str, Any]:
    keep_last = max(0, int(keep_last or 0))
    if keep_last <= 0:
        cur = con.execute("DELETE FROM process_events")
    else:
        cur = con.execute("DELETE FROM process_events WHERE id NOT IN (SELECT id FROM process_events ORDER BY id DESC LIMIT ?)", (keep_last,))
    return {"ok": True, "deleted": cur.rowcount if cur.rowcount is not None else 0, "kept_last": keep_last}

def compact_db() -> dict[str, Any]:
    with connect() as con:
        before = Path(settings.database_path).stat().st_size if Path(settings.database_path).exists() else 0
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        con.execute("VACUUM")
        con.execute("PRAGMA optimize")
        after = Path(settings.database_path).stat().st_size if Path(settings.database_path).exists() else 0
    return {"before_bytes": before, "after_bytes": after}



def get_app_settings(con: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Return persisted UI settings. Values are JSON-decoded."""
    def _read(c: sqlite3.Connection) -> dict[str, Any]:
        try:
            rows = c.execute("SELECT key, value_json FROM app_settings").fetchall()
        except sqlite3.OperationalError:
            return {}
        return {str(r["key"]): loads(r["value_json"], None) for r in rows}
    if con is not None:
        return _read(con)
    with connect() as c:
        return _read(c)


def set_app_settings(values: dict[str, Any]) -> dict[str, Any]:
    """Persist UI settings in SQLite so operators do not edit .env files."""
    now = utcnow()
    with connect() as con:
        for key, value in values.items():
            con.execute(
                """
                INSERT INTO app_settings(key, value_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at
                """,
                (str(key), dumps(value), now),
            )
    return {"ok": True, "updated": sorted(values.keys())}


def _clear_dir_contents(path: Path) -> dict[str, Any]:
    removed_files = 0
    removed_dirs = 0
    removed_bytes = 0
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        return {"path": str(path), "removed_files": 0, "removed_dirs": 0, "removed_bytes": 0}
    for child in path.iterdir():
        try:
            if child.is_file() or child.is_symlink():
                try:
                    removed_bytes += child.stat().st_size
                except Exception:
                    pass
                child.unlink()
                removed_files += 1
            elif child.is_dir():
                for nested in child.rglob("*"):
                    try:
                        if nested.is_file() or nested.is_symlink():
                            removed_bytes += nested.stat().st_size
                    except Exception:
                        pass
                shutil.rmtree(child)
                removed_dirs += 1
        except Exception:
            continue
    path.mkdir(parents=True, exist_ok=True)
    return {"path": str(path), "removed_files": removed_files, "removed_dirs": removed_dirs, "removed_bytes": removed_bytes}


def _clear_processing_artifacts() -> dict[str, Any]:
    data_dir = Path(settings.database_path).parent
    cleared_dirs = {
        "attachments": _clear_dir_contents(data_dir / "attachments"),
        "raw_emails": _clear_dir_contents(data_dir / "raw_emails"),
        "outbox_1c": _clear_dir_contents(data_dir / "outbox_1c"),
    }
    cleared_files: dict[str, int] = {}
    for name in (
        "quality_checks.jsonl",
        "quality_errors.jsonl",
        "review_queue.jsonl",
        "quality_report.json",
    ):
        path = data_dir / name
        try:
            size = path.stat().st_size if path.exists() else 0
            if name.endswith(".json"):
                path.write_text("{}", encoding="utf-8")
            elif path.exists():
                path.unlink()
            cleared_files[name] = size
        except Exception:
            cleared_files[name] = -1
    return {"dirs": cleared_dirs, "files": cleared_files}


def _clear_work_artifacts() -> dict[str, Any]:
    data_dir = Path(settings.database_path).parent
    cleared_dirs = {
        "outbox_1c": _clear_dir_contents(data_dir / "outbox_1c"),
    }
    cleared_files: dict[str, int] = {}
    for name in (
        "quality_checks.jsonl",
        "quality_errors.jsonl",
        "review_queue.jsonl",
        "quality_report.json",
    ):
        path = data_dir / name
        try:
            size = path.stat().st_size if path.exists() else 0
            if name.endswith(".json"):
                path.write_text("{}", encoding="utf-8")
            elif path.exists():
                path.unlink()
            cleared_files[name] = size
        except Exception:
            cleared_files[name] = -1
    return {"dirs": cleared_dirs, "files": cleared_files}


def reset_processed_work_data(con: sqlite3.Connection, *, keep_process_events: bool = False) -> dict[str, Any]:
    """Clear processing results while keeping imported emails and attachment files.

    This lets the operator re-run patterns/AI over already imported mail without
    downloading the same mailbox again.
    """
    tables = [
        "outbox_attempts",
        "outbox",
        "ai_suggestions",
        "ai_usage",
        "ai_cache",
        "test_runs",
        "lost_and_found",
        "cases",
    ]
    if not keep_process_events:
        tables.append("process_events")
    deleted: dict[str, int] = {}
    for table in tables:
        try:
            cur = con.execute(f"DELETE FROM {table}")
            deleted[table] = cur.rowcount if cur.rowcount is not None else 0
        except Exception:
            deleted[table] = -1
    artifacts = _clear_work_artifacts()
    before = Path(settings.database_path).stat().st_size if Path(settings.database_path).exists() else 0
    try:
        con.commit()
    except Exception:
        pass
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass
    try:
        con.execute("VACUUM")
    except Exception:
        pass
    try:
        con.execute("PRAGMA optimize")
    except Exception:
        pass
    after = Path(settings.database_path).stat().st_size if Path(settings.database_path).exists() else 0
    return {
        "ok": True,
        "schema": "readmail-reset-work-v1",
        "deleted": deleted,
        "artifacts": artifacts,
        "db_compact": {"before_bytes": before, "after_bytes": after},
        "kept": {
            "raw_emails": True,
            "attachments": True,
            "attachment_files": True,
            "settings": True,
            "learning": True,
            "import_history": True,
            "process_events": keep_process_events,
        },
        "at": utcnow(),
    }


def reset_processing_data(con: sqlite3.Connection, *, keep_settings: bool = True, keep_learning: bool = True, keep_process_events: bool = False) -> dict[str, Any]:
    """Clear imported mail/cases/outbox after the operator has exported a report.

    Safe defaults keep app_settings and learned buyer identities/profiles so the next run
    starts clean but does not lose configured mail/AI/1C settings.
    """
    tables = [
        "outbox_attempts",
        "outbox",
        "ai_suggestions",
        "ai_usage",
        "ai_cache",
        "test_runs",
        "import_uid_failures",
        "import_errors",
        "import_jobs",
        "lost_and_found",
        "attachments",
        "cases",
        "raw_emails",
    ]
    if not keep_process_events:
        tables.append("process_events")
    if not keep_learning:
        tables.extend(["field_pattern_candidates", "learning_events", "client_profiles", "buyer_identities"])
    deleted: dict[str, int] = {}
    for table in tables:
        try:
            cur = con.execute(f"DELETE FROM {table}")
            deleted[table] = cur.rowcount if cur.rowcount is not None else 0
        except Exception as exc:
            deleted[table] = -1
    if not keep_settings:
        try:
            cur = con.execute("DELETE FROM app_settings")
            deleted["app_settings"] = cur.rowcount if cur.rowcount is not None else 0
        except Exception:
            deleted["app_settings"] = -1
    artifacts = _clear_processing_artifacts()
    before = Path(settings.database_path).stat().st_size if Path(settings.database_path).exists() else 0
    try:
        con.commit()
    except Exception:
        pass
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass
    try:
        con.execute("VACUUM")
    except Exception:
        pass
    try:
        con.execute("PRAGMA optimize")
    except Exception:
        pass
    after = Path(settings.database_path).stat().st_size if Path(settings.database_path).exists() else 0
    return {
        "ok": True,
        "schema": "readmail-new-reset-v1.17",
        "deleted": deleted,
        "artifacts": artifacts,
        "db_compact": {"before_bytes": before, "after_bytes": after},
        "kept": {
            "settings": keep_settings,
            "learning": keep_learning,
            "process_events": keep_process_events,
        },
        "at": utcnow(),
    }


# ─── Import Job helpers ────────────────────────────────────────────────


def create_import_job(con: sqlite3.Connection, job_id: str, mode: str = "raw") -> int:
    """Create a new import_job row and return its id."""
    now = utcnow()
    cur = con.execute(
        "INSERT INTO import_jobs(job_id, status, mode, started_at, last_heartbeat_at) VALUES (?, 'running', ?, ?, ?)",
        (job_id, mode, now, now),
    )
    return int(cur.lastrowid)


def update_import_job_heartbeat(
    con: sqlite3.Connection,
    job_id: str,
    *,
    stage: str | None = None,
    folder: str | None = None,
    display_folder: str | None = None,
    uid: str | None = None,
    processed: int | None = None,
    imported: int | None = None,
    skipped: int | None = None,
    failed: int | None = None,
    errors: int | None = None,
) -> None:
    """Update live status of a running import job."""
    now = utcnow()
    sets = ["last_heartbeat_at=?"]
    params: list[Any] = [now]
    if stage is not None:
        sets.append("current_stage=?")
        params.append(stage)
    if folder is not None:
        sets.append("current_folder=?")
        params.append(folder)
    if display_folder is not None:
        sets.append("current_display_folder=?")
        params.append(display_folder)
    if uid is not None:
        sets.append("current_uid=?")
        params.append(uid)
    if processed is not None:
        sets.append("processed_count=?")
        params.append(processed)
    if imported is not None:
        sets.append("imported_count=?")
        params.append(imported)
    if skipped is not None:
        sets.append("skipped_count=?")
        params.append(skipped)
    if failed is not None:
        sets.append("failed_count=?")
        params.append(failed)
    if errors is not None:
        sets.append("error_count=?")
        params.append(errors)
    params.append(job_id)
    con.execute(f"UPDATE import_jobs SET {', '.join(sets)} WHERE job_id=?", params)


def finish_import_job(
    con: sqlite3.Connection,
    job_id: str,
    status: str = "completed",
    result: dict[str, Any] | None = None,
) -> None:
    """Mark import job as finished with final result."""
    now = utcnow()
    con.execute(
        "UPDATE import_jobs SET status=?, finished_at=?, last_heartbeat_at=?, result_json=? WHERE job_id=?",
        (status, now, now, dumps(result or {}), job_id),
    )


def record_import_error(
    con: sqlite3.Connection,
    job_id: str,
    *,
    mailbox: str,
    display_folder: str | None = None,
    uid: str,
    stage: str,
    error_type: str,
    error_message: str,
    raw_size: int = 0,
) -> int:
    """Log a single import error for diagnostics."""
    cur = con.execute(
        "INSERT INTO import_errors(job_id, mailbox, display_folder, uid, stage, error_type, error_message, raw_size, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, mailbox, display_folder or mailbox, uid, stage, error_type, str(error_message)[:2000], raw_size, utcnow()),
    )
    return int(cur.lastrowid)


def record_uid_failure(
    con: sqlite3.Connection,
    *,
    account: str = "",
    mailbox: str,
    uid: str,
    stage: str,
    error_type: str,
    error_message: str,
    max_attempts: int = 3,
    uidvalidity: str | None = None,
    message_id: str | None = None,
    recoverable: bool | None = None,
    next_retry_at: str | None = None,
) -> dict[str, Any]:
    """Track repeated UID failures. Returns quarantined=True if max_attempts exceeded.

    Optional uidvalidity/message_id/recoverable/next_retry_at enrich the quarantine record so a
    targeted backfill can later reason about recoverability without re-querying the server. They are
    only written when provided (COALESCE keeps existing values on conflict).
    """
    now = utcnow()
    recoverable_int = None if recoverable is None else (1 if recoverable else 0)
    try:
        con.execute(
            """
            INSERT INTO import_uid_failures(
                account, mailbox, uid, stage, error_type, error_message, attempts, status,
                first_seen_at, last_seen_at, next_retry_at, uidvalidity, message_id, recoverable)
            VALUES (?, ?, ?, ?, ?, ?, 1, 'failed', ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account, mailbox, uid, stage) DO UPDATE SET
                attempts=attempts+1, error_type=excluded.error_type, error_message=excluded.error_message,
                last_seen_at=excluded.last_seen_at, status='failed',
                next_retry_at=COALESCE(excluded.next_retry_at, import_uid_failures.next_retry_at),
                uidvalidity=COALESCE(excluded.uidvalidity, import_uid_failures.uidvalidity),
                message_id=COALESCE(excluded.message_id, import_uid_failures.message_id),
                recoverable=COALESCE(excluded.recoverable, import_uid_failures.recoverable)
            """,
            (account, mailbox, uid, stage, error_type, str(error_message)[:2000], now, now,
             next_retry_at, uidvalidity, message_id, recoverable_int),
        )
    except Exception:
        pass
    row = con.execute(
        "SELECT attempts FROM import_uid_failures WHERE account=? AND mailbox=? AND uid=? AND stage=?",
        (account, mailbox, uid, stage),
    ).fetchone()
    attempts = int(row["attempts"]) if row else 1
    quarantined = attempts >= max_attempts
    if quarantined:
        con.execute(
            "UPDATE import_uid_failures SET status='quarantined' WHERE account=? AND mailbox=? AND uid=? AND stage=?",
            (account, mailbox, uid, stage),
        )
    return {"attempts": attempts, "quarantined": quarantined}


def record_uid_skipped(
    con: sqlite3.Connection, *, account: str = "", mailbox: str, uid: str,
    uidvalidity: str | None = None, message_id: str | None = None,
    reason: str = "before_import_window",
) -> None:
    """Пометить UID как skipped_before_start (импорт-окно). reconcile видит это как
    skipped_before_start, НЕ missing_local. Тело письма НЕ качается."""
    now = utcnow()
    try:
        con.execute(
            """
            INSERT INTO import_uid_failures(
                account, mailbox, uid, stage, error_type, error_message, attempts, status,
                first_seen_at, last_seen_at, uidvalidity, message_id, recoverable)
            VALUES (?, ?, ?, 'before_start', 'skipped', ?, 0, 'skipped', ?, ?, ?, ?, 0)
            ON CONFLICT(account, mailbox, uid, stage) DO UPDATE SET
                status='skipped', last_seen_at=excluded.last_seen_at,
                uidvalidity=COALESCE(excluded.uidvalidity, import_uid_failures.uidvalidity),
                message_id=COALESCE(excluded.message_id, import_uid_failures.message_id)
            """,
            (account, mailbox, uid, reason, now, now, uidvalidity, message_id),
        )
    except Exception:
        pass


def get_import_job_status(con: sqlite3.Connection, job_id: str | None = None) -> dict[str, Any] | None:
    """Return latest import job status. If job_id is None, return the most recent running job."""
    if job_id:
        row = con.execute("SELECT * FROM import_jobs WHERE job_id=?", (job_id,)).fetchone()
    else:
        row = con.execute("SELECT * FROM import_jobs WHERE status='running' ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    data = dict(row)
    # Compute possible_hang
    if data.get("status") == "running" and data.get("last_heartbeat_at"):
        from datetime import datetime, timezone
        try:
            last = datetime.fromisoformat(str(data["last_heartbeat_at"]).replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            age_seconds = (now - last).total_seconds()
            data["possible_hang"] = age_seconds > 120
            if age_seconds > 600:
                con.execute(
                    "UPDATE import_jobs SET status='abandoned', finished_at=? WHERE job_id=? AND status='running'",
                    (utcnow(), data["job_id"]),
                )
                data["status"] = "abandoned"
                data["possible_hang"] = True
                data["abandoned"] = True
        except Exception:
            data["possible_hang"] = False
    else:
        data["possible_hang"] = False
    return data


def get_import_errors(
    con: sqlite3.Connection,
    job_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return import errors, optionally filtered by job_id."""
    if job_id:
        rows = con.execute(
            "SELECT * FROM import_errors WHERE job_id=? ORDER BY id DESC LIMIT ?",
            (job_id, limit),
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT * FROM import_errors ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_quarantined_uids(con: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    """Return UIDs that have been quarantined after repeated failures."""
    rows = con.execute(
        "SELECT * FROM import_uid_failures WHERE status='quarantined' ORDER BY last_seen_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def retry_quarantined_uid(con: sqlite3.Connection, mailbox: str, uid: str, stage: str) -> None:
    """Reset a quarantined UID so it can be retried."""
    con.execute(
        "UPDATE import_uid_failures SET status='retry_pending', next_retry_at=? WHERE mailbox=? AND uid=? AND stage=?",
        (utcnow(), mailbox, uid, stage),
    )
