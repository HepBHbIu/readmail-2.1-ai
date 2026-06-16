# Readmail Interactive Shell — быстрый старт

Интерактивная консоль управления Readmail (как у Claude/Codex): одна команда — живой статус и команды через `/`.
**По умолчанию всё read-only. 1С и AI не вызываются. Реальная доставка outbox не запускается.**

## Запуск
```bash
python3 scripts/readmailctl.py shell      # основной
python3 scripts/readmailctl.py sh         # алиас
python3 scripts/readmailctl.py tui --shell
./scripts/readmail                        # короткий wrapper (локально)
```
В Docker:
```bash
docker compose exec app python scripts/readmailctl.py shell
```

При старте печатается шапка:
```
═══ READMAIL CONTROL SHELL ═══
SERVER: RUNNING    WEB: http://127.0.0.1:8765
MAIL: raw=..., missing=..., quarantine=...
PROCESSING: cases=..., ready=..., review=...
OUTBOX/1C: new=..., delivery=OFF
AI: OFF, patterns=0 tokens, today=0
Введите / для списка команд.
readmail>
```

## Команды (вводятся с `/`)
Введите `/` — покажется полный список с группами.

**SYSTEM:** `/status` `/refresh` `/open` `/doctor` `/quit` `/exit`
**MAIL:** `/mail` `/reconcile` `/backfill` `/quarantine`
**PROCESSING:** `/processing` `/workers` `/pause [worker]` `/resume [worker]` `/worker-test`
**SEARCH:** `/search <q>` · `/trace case <id>` · `/trace raw <id>` · `/trace outbox <id>`
**OUTBOX/1C:** `/outbox` · `/outbox preview [N]` · `/outbox preview ids 1,2,3` · `/delivery status`
**AI:** `/ai` `/ai cost` `/ai modes` `/vision`
**SETTINGS (read-only):** `/settings` · `/settings runtime|import|workers|ai|onec|auth|paths|env-safe`
**LOGS/DIAG:** `/logs` `/logs errors` `/reports` `/tests info`

## Примеры
```
readmail> /search BR-500X
readmail> /trace case 2001
readmail> /outbox preview 5
readmail> /settings workers
readmail> /ai
readmail> /doctor
readmail> /pause import
readmail> /quit
```

## Что безопасно
- **Read-only:** status, refresh, mail, reconcile (снимок), quarantine, processing, workers, worker-test,
  search, trace, outbox summary/preview, ai, settings, logs, reports, doctor, open.
- **Пишут только runtime_flags:** `/pause`, `/resume`.

## Что отключено в shell v1 (DANGEROUS)
`/deliver` `/reset` `/cleanup` `/mass-import` `/ai batch` — выводят сообщение, что команда отключена.
Реальная доставка в 1С и AI batch требуют отдельного confirm-flow вне shell.

## Секреты
`/settings` НИКОГДА не печатает пароли, API-ключи, session_secret, password_hash или полный URL 1С
(`/settings onec` показывает только `http_url_present: true/false`). `/settings env-safe` маскирует
чувствительные ключи как `**** (set)`.

## Выход
`/quit`, `/exit`, `Ctrl+C` или `Ctrl+D`.
