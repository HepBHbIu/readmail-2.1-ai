# Терминальный монитор Readmail — быстрый старт

`scripts/readmailctl.py` — пульт управления и монитор из терминала. По умолчанию всё **read-only**.
**1С и AI не вызываются. Реальная доставка outbox не запускается.**

## Запуск
```bash
cd /path/to/readmail_v2
python3 scripts/readmailctl.py <команда>
```

## Веб-панель
```bash
python3 scripts/readmailctl.py open-url     # печатает локальный + LAN URL
python3 scripts/readmailctl.py server       # запустить сервер (http://localhost:8765)
```

## Статус (один снимок)
```bash
python3 scripts/readmailctl.py status        # текстовый монитор
python3 scripts/readmailctl.py status --json # машинно-читаемо
```
Показывает: сервер, почту (live/snapshot), обработку, workers, outbox (delivery off/paused),
AI (паттерны = 0 токенов), ⚠️ warnings и next actions.

## Живой монитор (tui)
```bash
python3 scripts/readmailctl.py tui --refresh 5   # авто-обновление каждые 5с (Ctrl+C — выход)
python3 scripts/readmailctl.py tui               # интерактивное меню (кроссплатформенно)
```
Меню: обновить статус · пауза/возобновить всех · worker-test (dry-run) · reconcile · backfill (dry-run) ·
поиск · trace · outbox summary/preview · выход. Пишут только pause/resume (флаги).

## Пауза / возобновление воркеров (единственные штатные write-действия)
```bash
python3 scripts/readmailctl.py pause all
python3 scripts/readmailctl.py resume all
python3 scripts/readmailctl.py pause import      # import|stage2|ai|outbox|delivery|telegram
python3 scripts/readmailctl.py resume stage2 --json
```
Пишут только `runtime_flags`. Неизвестный воркер → ошибка с кодом возврата 2.

## Поиск (единый)
```bash
python3 scripts/readmailctl.py search "BR-500X"
python3 scripts/readmailctl.py search "2001" --scope all
python3 scripts/readmailctl.py search "<abc123@mail.ru>"
python3 scripts/readmailctl.py search "автоевро" --scope clients --limit 10 --json
```
Ищет по письмам / кейсам / outbox / клиентам. Показывает detected_type и в какую вкладку открыть объект.

## Trace (цепочка расследования)
```bash
python3 scripts/readmailctl.py trace case 2001
python3 scripts/readmailctl.py trace raw_email 1001
python3 scripts/readmailctl.py trace outbox 3001 --compact
python3 scripts/readmailctl.py trace case 2001 --json
```
Показывает: письмо → кейс(ы) → outbox(ы) → попытки доставки. Только snippet'ы, без полного тела, без секретов.

## Outbox (безопасный просмотр)
```bash
python3 scripts/readmailctl.py outbox summary
python3 scripts/readmailctl.py outbox preview --limit 5
python3 scripts/readmailctl.py outbox preview --ids 1,2 --profile standard
```
**Read-only.** Не вызывает 1С, не меняет status. Делит контрольные/возвратные события, показывает,
что автодоставка выключена. `--profile minimal|standard|debug` показывает, как payload выглядел бы
после профиля (debug → предупреждение, не для боевой доставки).

## Сверка / диагностика / тест воркеров (read-only)
```bash
python3 scripts/readmailctl.py reconcile            # сверка IMAP ↔ БД
python3 scripts/readmailctl.py worker-test --stage all --limit 20
python3 scripts/readmailctl.py diagnostic
python3 scripts/readmailctl.py backfill --dry-run   # без --apply ничего не пишет
```

## Что безопасно
- **Read-only:** status, tui, search, trace, outbox summary/preview, reconcile, worker-test, diagnostic, open-url.
- **Пишут только флаги:** pause, resume.
- **Пишут данные (с явным флагом):** `backfill --apply`, `worker` (autopilot loop).
- **1С/AI не вызываются ни одной из команд монитора. Реальная доставка outbox не запускается.**
