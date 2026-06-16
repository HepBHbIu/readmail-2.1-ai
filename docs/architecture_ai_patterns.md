# Readmail — архитектура: конвейер, паттерны, ИИ, валидация, 1С

Полный разбор: что приходит на вход, как письмо проходит стадии, где **паттерны** и **промты**, как
работает **валидация** (почему «понятого» письма может не хватить для 1С), как устроены **ИИ-запросы**
(текст и vision через один OpenAI-совместимый клиент со сменой модели), **обработка брака** и
**сортировка диалогов**, и кто за что отвечает по файлам.

---

## 0. Вход и выход

**Вход — письмо** (одно или цепочка): тема, тело (часто HTML-таблица, расклеивается в `visible_text`
с разделителем `" | "`), **вложения** (фото `.jpg`, сканы/`.pdf`, акты `.xls/.xlsx`), внешние **ссылки**
(страница возврата, облако с фото). Вложения качаются на диск `data/attachments/<raw_email_id>/`, их
**текст** (PDF/Excel) подмешивается к телу для паттернов и ИИ; **фото/сканы** идут в vision.

**Выход — документ возврата `readmail-1c-v2`** (см. §7): клиент, документ №/дата, позиции
(артикул/бренд/имя/кол-во/цена/факт-пересорт), причина, флаги документов брака, опц. тело/вложения/статус.

---

## 1. Конвейер (стадии)

```
① ИМПОРТ      imap_importer.py + email_parser.py
     IMAP → raw_emails (+ вложения на диск). Дедуп по (mailbox,uid) и message_id.
② ПАТТЕРНЫ    classifier.py — 0 токенов
     regex(FIELD_REGEX) → фразовые шаблоны → YAML клиента → таблицы «| » → split бренд/артикул
     + detect_kind (тип претензии) + detect_event_type (возврат / диалог / отчёт …)
③ ВАЛИДАЦИЯ   quality_gate.py + *_evidence.py
     каждое поле должно быть ПОДТВЕРЖДЕНО контекстом (evidence). Не прошло → кейс в Сверку, не в 1С.
④ ИИ          ai_client.py  (что паттерны не добрали / режим «полный ИИ»)
     текст → модель → JSON. Результат ИИ НЕ доверяется напрямую — снова проходит ③ (validator).
⑤ БРАК vision  main.py api_check_defect_docs (авто из ④ при claim_kind=defect/nonconforming)
     по каждому файлу: PDF-текст → если пусто, vision; ищет 3 документа брака.
⑥ СОРТИРОВКА   _auto_link_followup + inbox_sorter + final_case_sorter
     связывание родитель↔продолжения, маршрут письма (возврат / диалог / отчёт / скрытое).
⑦ ЭКСПОРТ 1С   db.py build_case_event_payload → apply_one_c_payload_profile (v2) → outbox → доставка.
```

Два режима прогона (фоновые воркеры в `main.py`, тег режима ставит `set_ai_usage_context`):
- **pattern** — импорт → паттерны → ИИ только на хвост (что не разобралось) → 1С;
- **full_ai** — импорт → ИИ по каждому письму (компактный промт) → Сверка → 1С.

---

## 1-бис. Паттерн-конвейер vs Автопилот ИИ (структура · промт · выход · % · ₽)

Два способа получить тот же результат (поля для 1С). Отличаются тем, **кто извлекает** и **сколько стоит**.

| | **Паттерн-конвейер** (pattern + ИИ-хвост) | **Автопилот ИИ** (full_ai) |
|---|---|---|
| Структура | импорт → **паттерны (0 ток.)** → валидация → ИИ только на то, что не разобралось → 1С | импорт → **ИИ на КАЖДОЕ письмо** → валидация → Сверка → 1С |
| Кто извлекает | детерминированные regex/YAML; ИИ — добивка хвоста | модель на всё подряд (паттерны не нужны) |
| Промт | **полный** `SYSTEM_PROMPT` (спека + примеры + правила клиента) — точный, дорогой вход | **компактный** промт (схема + текст письма) — вход −~54 % |
| На выходе | те же поля; 85–90 % писем — без единого токена, с evidence-аудитом | те же поля; покрытие 100 %, но каждое письмо платное |
| Плюсы | дёшево, детерминированно, проверяемо (нет галлюцинаций на массиве) | не нужны пер-клиентские паттерны; ровно работает на новых форматах |
| Минусы | надо вести `config/buyers/*.yml`; новый формат сперва падает в хвост | платим за все письма; ответ зависит от модели |

### Примерные цифры (на 1000 писем, цены deepseek-chat 9 ₽/млн вход · 3.72 ₽/млн выход)
Замеры этой сессии: полный промт ≈ **4250 вх / 350 вых** на письмо (≈ 0.040 ₽); компактный ≈ **820 вх /
230 вых** (≈ 0.008 ₽). Паттерны закрывают ~85 % бесплатно (из заметок по покрытию).

| Режим | Через ИИ | Токены ИИ | **Стоимость / 1000 писем** |
|---|---|---|---|
| Паттерн-конвейер | ~15 % (≈150 писем × полный промт) | ≈ 0.7 млн | **≈ 6 ₽** |
| Автопилот ИИ | 100 % (1000 × компактный) | ≈ 1.05 млн | **≈ 8–9 ₽** |

Вывод: по деньгам близко, но паттерны дают 85 % **бесплатно и детерминированно** (нужны для аудита и
стабильности), а полный ИИ удобнее на разнородном/новом потоке. Обычно: паттерн-конвейер как основной,
ИИ — хвост.

### Брак (vision) — считается отдельно
Документы брака гонит **vision-модель** (qwen-vl, та же цена). Прогон одного брак-письма с ~14 фото ≈
**16k вх / 1.4k вых ≈ 0.15 ₽**. Брак — меньшинство писем, но в пересчёте на письмо это самая дорогая
операция; поэтому vision включается только для `claim_kind=defect/nonconforming` и только по нужным файлам.

---

## 2. Паттерны (бесплатно, без токенов)

`app/classifier.py`:
- `FIELD_REGEX` — поля по меткам (артикул/накладная/УПД/кол-во…).
- `DEFAULT_KIND_PATTERNS` + `detect_kind()` — тип претензии (брак/пересорт/недовоз…).
- `extract_fields()` — конвейер: `FIELD_REGEX` → `_apply_phrase_templates()` (общие фразы, в т.ч. пересорт
  «Заказывал X Привезли Y» → `part_number`+`received_part_number`) → `_apply_yaml_templates()`
  (пер-клиентские из `config/buyers/*.yml`) → `_parse_pipe_table[_rows]()` (таблицы, мультипозиция) →
  `_maybe_split_part_brand()` (ETZ1107MRKrauf → ETZ1107MR + Krauf).
- `classify_email()` — общий вход (visible_text = тема + тело + текст вложений).
- `classify_defect_documents()` / `DEFECT_DOC_TYPES` — статическое определение 3 документов брака по
  именам/тексту.

**Пер-клиентские профили — `config/buyers/<code>.yml`** (17 файлов: avtoto, trinity_parts, ixora,
avtoformula, parterra, profit_liga, shate_m, autorus, autoeuro, berg, favorit, motexc … + `_default.yml`):
`aliases` (домен/тема), `statuses`, `fields.regex`, `templates`, `item_templates`, индивидуальный
`ai_prompt`. Кнопка «Обучить» (learning_engine) генерит regex из правок оператора и дописывает в YAML.

---

## 3. Валидация (Evidence Gate) — почему «понял» ≠ «можно в 1С»

Извлечь значение мало — оно должно быть **подтверждено контекстом**, иначе в 1С уйдёт мусор. Этим
занимается `app/quality_gate.py` (+ модули доказательств). Это и есть **validator**: и паттерны, и ИИ
проходят его одинаково; именно validator решает `ready_for_export`.

Модули доказательств по полям (каждый возвращает evidence-статус):
- **`part_number_evidence.py`** — артикул: подтверждён явной меткой / компактной строкой `Арт. HP4465, шт.1`
  / колонкой таблицы / близостью к `product_name`. Телефон/дата/короткое число без метки → `weak_found`.
- **`document_number_evidence.py`** — №документа: УПД/накладная/счёт-фактура/реализация подтверждают;
  претензия/заявка/обращение/заказ — **блокируются** (это не номер реализации).
- **`quantity_evidence.py`** — количество: метка кол-ва / `шт.` / колонка / компактная строка с тем же
  артикулом. Цены/даты/телефоны количество не подтверждают.
- **`claim_kind_evidence.py`** (+ `config/claim_kind_rules.yaml`) — причина: явная причина/подпись/колонка;
  общие `отказ/возврат` → `weak_generic_refusal`; противоречие → `conflict_reason_detected`.
- **`buyer_evidence.py`** — покупатель по домену/правилу; конфликт уверенного отправителя с YAML-профилем →
  `dangerous_profile_conflict` (блок).

`evidence_repair.py` — мягкий ремонт: напр. ищет дату документа по контексту «№… от ДД.ММ» рядом с номером.
Не прошёл гейт → кейс уходит в **Сверку** (оператор), а не в 1С. Read-only аудит этого слоя —
[`evidence_audit.md`](evidence_audit.md).

---

## 4. ИИ: промты и формат запроса

Всё в `app/ai_client.py`. Запрос — **OpenAI-совместимый** `POST {base_url}/chat/completions`,
`Authorization: Bearer <ключ>`, тело собирает `_payload_for_kind()`:
```json
{"model":"…","messages":[{"role":"system","content":"…"},{"role":"user","content":"…"}],
 "temperature":0.0,"max_tokens":<AI_MAX_OUTPUT_TOKENS>,"stream":false,
 "response_format":{"type":"json_object"}}   // если AI_RESPONSE_FORMAT=json_object
```
`_request_chat()` пишет каждый вызов в живой лог (`_ai_log_push`: запрос/ответ/токены/мс) и в `ai_usage`
(`record_ai_usage`, тег режим+тип). `run_ai_suggestion()` = кэш → `_chat_payload` → `_request_chat` →
`_extract_json`. Применение к кейсу — `_apply_ai_to_case_id` (main.py) **через validator** (§3).

### Промт извлечения — `SYSTEM_PROMPT` (полный режим/хвост)
Содержит правила полей и **примеры «письмо → JSON»**, например:
```
[«вернуть товар надлежащего качества по причине отказа конечного покупателя
  RF5161S ROCK FORCE Набор ключей комбинированных в количестве 1 по документу №79324 от 22.04.26»]
→ {"event_type":"new_return","claim_kind":"quality_refusal","fields":{
     "document_number":"79324","document_date":"22.04.2026","part_number":"RF5161S",
     "brand":"ROCK FORCE","product_name":"Набор ключей комбинированных","quantity":"1",
     "comment":"отказ конечного покупателя"}}

[«Причина – пересорт Покупали: GY1733G Амортизатор подвески | CTR по счёт-фактуре № 81068 от 13.05.2026»]
→ {"claim_kind":"wrong_item","fields":{"document_number":"81068","part_number":"GY1733G",
     "brand":"CTR","product_name":"Амортизатор подвески","comment":"пересорт"}}
```
Правила, которые промт навязывает: не брать заголовки таблиц/слова-метки как значения; brand НЕ из
e-mail/домена и не из марки авто; № заявки ≠ № документа; дата документа ≠ дата письма; цитаты ниже
(`>`, «От:») — только контекст.

К `SYSTEM_PROMPT` добавляется индивидуальный `ai_prompt` клиента (секция «# ОСОБЕННОСТИ ЭТОГО КЛИЕНТА»).

### Компактный промт — режим «полный ИИ» (экономия входа ~−54 %)
Срезана спека, оставлены короткая инструкция + схема `return_json` + текст письма:
```
system: «Классифицируй письмо и извлеки поля по автозапчастям. Верни ТОЛЬКО JSON. Нет поля → null.
         Не бери заголовки таблиц как значения. Прайс/остатки → supplier_report; корректировка →
         correction_request; … Если есть [ТЕКСТ ВЛОЖЕНИЙ/АКТА] — возьми имя/бренд из строки акта рядом
         с артикулом.»
user:   {"email":{subject,from,text}, "guess":{event_type,claim_kind,buyer_code}, "return_json":{…схема…}}
```

### Дефолтный vision-промт извлечения (`run_vision_extraction`, ai_client.py:1352)
Когда фото/скан читают НЕ как документ брака, а на поля (этикетка/акт), и `prompt_text` не передан:
```
«На изображении этикетка или документ автозапчасти. Извлеки JSON без markdown:
 {part_number, brand, product_name, quantity, document_number, document_date, claim_kind, comment}.
 Не придумывай. Если поля нет — null.»  (+ контекст из письма, если есть)
```

### ⑤ Обучение паттернов — `TRAINING_SYSTEM_PROMPT` (ai_client.py:1461)
**Когда:** оператор поправил поля в Сверке и нажал «Обучить». **Вход:** текст письма + что паттерны
извлекли неверно (`before`) + правильные значения оператора (`after`). **Задача модели:** найти в письме,
где лежат правильные значения, и сгенерить **Python-regex** под формат именно этого клиента (захват в одну
группу, по контексту вокруг значения, не цеплять заголовки таблиц). **Выход:** JSON с `patterns` →
дописываются в `config/buyers/<client>.yml` (`learning_engine.promote_learned_patterns`). Токены тратятся
один раз на обучение, дальше клиент разбирается паттернами бесплатно.

### ⑥ Тест связи — `ai_test_prompt` (config.py:89) + system «Ответь только JSON без markdown»
**Когда:** кнопка «Тест связи» / `test_ai_connection`. **Вход:** крошечный `{"ok":true,"purpose":
"readmail_test"}`. **Выход:** любой валидный JSON. Цель — проверить, что эндпоинт/модель/ключ живы (не
извлечение). Дёшево, для диагностики.

### Итого промтов в системе
| # | Промт | Где | Когда / для чего |
|---|---|---|---|
| ① | `SYSTEM_PROMPT` | ai_client.py:26 | основное текстовое извлечение (паттерн-хвост), с примерами письмо→JSON |
| ② | компактный (`csys`) | ai_client.py:204 | режим «полный ИИ» — то же, но дешевле (−54% входа) |
| ③ | дефолтный vision-извлечения | ai_client.py:1352 | чтение полей с фото/этикетки/акта, когда промт не задан |
| ④ | `DEFECT_DOC_PROMPT` | main.py:7526 | vision: тип документа брака (наряд/акт/фото) |
| ⑤ | `TRAINING_SYSTEM_PROMPT` | ai_client.py:1461 | генерация regex из правок оператора (обучение) |
| ⑥ | тест связи | ai_client.py:965 + config.py:89 | проверка модели/ключа |

**Плюс индивидуальные `ai_prompt`** в каждом `config/buyers/*.yml` — не отдельный вызов, а добавка к ①/②
(секция «# ОСОБЕННОСТИ ЭТОГО КЛИЕНТА»), по числу клиентов.
- **`DEFECT_DOC_PROMPT`** (`app/main.py`) — vision-классификация документа брака:
  ```
  «Определи ТИП документа. JSON: {doc_type: install_order|removal_order|service_act|part_photo|other,
   confidence, reason}. install_order = наряд на установку; removal_order = наряд на снятие;
   service_act = акт/заключение сервиса/дефектовка; part_photo = фото детали.»
  ```

---

## 5. Vision (фото/сканы) — тот же RouterAI, смена модели

Vision идёт через **тот же OpenAI-совместимый клиент** (RouterAI), но `run_vision_extraction()` на время
вызова **подменяет модель/провайдера** на vision-настройки (`ai_vision_provider`/`ai_vision_model`,
напр. `qwen/qwen3-vl-32b-instruct`) и кладёт в сообщение `image_url` (data:base64) + текст-промт; токены
пишутся как `kind=vision`. То есть один прогон брак-кейса в логе виден так: **1 текст (deepseek-chat)**,
затем **N vision (qwen-vl)** — модель переключается в зависимости от типа обработки. Включается
`AI_VISION_ENABLED`.

---

## 6. Обработка брака и сортировка диалогов

**Брак** — `api_check_defect_docs` (main.py), **авто-вызывается** из `_apply_ai_to_case_id` при
`claim_kind ∈ {defect, nonconforming}`: делит вложения на документы/фото, по каждому сначала пробует
текст PDF (`_classify_doc_text`), если пусто → vision (`DEFECT_DOC_PROMPT`); цель — собрать 3 типа
(наряд установки, наряд снятия, акт сервиса), стоп как только собраны. Итог → `payload.defect_doc_flag`
(state complete/partial/absent) → флаги в 1С. (Скан-PDF без текстового слоя пока не растрируется — хвост.)

**Сортировка / диалоги:**
- `detect_event_type()` (classifier.py) — что это за письмо: `new_return` (первое с данными) vs
  `followup_reminder` (напоминание/«вы рассмотрели?») vs `followup_dialog` (ответ без новых данных) vs
  `supplier_decision`/`correction_request`/`marking_request`/`supplier_report`/`ready_to_ship`/`info_only`.
- `_auto_link_followup()` (main.py) — связывает продолжения с родителем по strong-key / треду /
  № документа / **№ возврата-заявки**; наследует поля от родителя, если у followup своих нет. Так «5
  напоминаний» цепляются к исходному возврату, а не плодят пустые кейсы.
- `inbox_sorter.py` — первичная маршрутизация входящих (возврат / отчёт / диалог).
- `final_case_sorter.py` — раскладка кейсов по экспортным bucket-ам (auto_safe / preview / review …).
- Витрины «где каждое письмо»: `visual_accounting.py`, `folder_accounting.py`, `canonical_pipeline.py`,
  `processed_hidden.py`.

---

## 7. Экспорт в 1С (схема v2 + тумблеры)

- `build_export_json` (classifier.py) — `document/claim/items` (+ `received_part_number`, `defect_documents`).
- `build_case_event_payload` (db.py) — полный событийный payload (хранится в outbox для аудита).
- `apply_one_c_payload_profile` (db.py) — режет до **чистой v2** при доставке/preview: основа
  (buyer/claim/document/items) + блоки по тумблерам `ONE_C_INCLUDE_*` (price/comment/flags/status/text/
  attachments/source). Профиль **debug** — полный payload (evidence/gate) для аудита.
- Доставка `ONE_C_EXPORT_MODE`: `file` (JSON) · `http` · `local_receiver` · `both` · `off`.

---

## 8. Кто за что отвечает (карта файлов)

### Ядро
| Файл | Ответственность |
|---|---|
| `app/main.py` | FastAPI: все эндпоинты, оркестрация стадий, автопилоты, батчи, авто-триггер брака, `_apply_ai_to_case_id`, `_auto_link_followup`, `DEFECT_DOC_PROMPT` |
| `app/db.py` | SQLite (схема, `connect`, запросы), payload/профиль 1С, `queue_case_event`, доставка каналов, `record_ai_usage` |
| `app/config.py` / `app/runtime_settings.py` | дефолтные настройки / рантайм-настройки (БД) + список ключей для UI + `apply_runtime_settings` |

### Почта
| `app/imap_importer.py` | импорт IMAP, дедуп, server_counts, backfill, reconcile |
| `app/email_parser.py` | разбор MIME, `select_visible_text`, текст из PDF/XLS/XLSX, вложения |
| `app/inbox_sorter.py` | первичная маршрутизация входящих |
| `app/import_window.py` · `app/archiver.py` | окно импорта по расписанию · архивация тел/мета |

### Извлечение и валидация
| `app/classifier.py` | паттерны/regex, YAML-шаблоны, `extract_fields`, `detect_kind`, `detect_event_type`, `classify_email`, `build_export_json`, мультипозиция, документы брака |
| `app/classification_taxonomy.py` | таксономия типов/состояний (метки, группы) |
| `app/quality_gate.py` | гейт качества/evidence (validator), `ready_for_export` |
| `app/part_number_evidence.py` · `document_number_evidence.py` · `quantity_evidence.py` · `claim_kind_evidence.py` · `buyer_evidence.py` | доказательства по полям |
| `app/evidence_repair.py` · `app/evidence_panel.py` | ремонт полей по контексту · данные панели доказательств |

### ИИ
| `app/ai_client.py` | промты (`SYSTEM_PROMPT`, компактный, `TRAINING_SYSTEM_PROMPT`), запрос к модели, **vision со сменой модели**, обучение паттернов, test/probe/models |
| `app/ai_trace.py` · `app/ai_cost_ledger.py` · `app/ai_smoke.py` | трасса вызовов · стоимость · контролируемый smoke (CLI) |
| `app/learning_engine.py` · `app/learning_ledger.py` | генерация/промоция выученных паттернов в YAML · журнал решений оператора |

### 1С, отчёты, сервис
| `app/local_1c.py` | локальный приёмник 1С (тест) |
| `app/dashboard.py` | `build_overview` (статус/сводка) |
| `app/visual_accounting.py` · `bucket_accounting.py` · `folder_accounting.py` · `canonical_pipeline.py` · `processed_hidden.py` · `final_case_sorter.py` | «где каждое письмо» / маршруты / финальная раскладка |
| `app/search.py` | единый поиск + trace (письмо→кейс→outbox→попытки) |
| `app/server_core.py` · `runtime_control.py` · `job_locks.py` · `auth.py` · `telegram.py` · `worker_test.py` · `_accounting_cache.py` | bind/LAN · пауза воркеров · блокировки · авторизация · алерты · read-only прогон стадий · кэш бухгалтерии |
| `app/pattern_compliance.py` · `decision_compare.py` · `passed_safety_audit.py` · `demo_data.py` | служебные/диагностические (вызываются из CLI/аудит-скриптов, не из веб-рантайма) |

### Терминал / скрипты
| `scripts/readmail_panel.py` (+ `readmail`) | визуальная TUI-панель (Textual) |
| `scripts/readmailctl.py` | CLI/монитор (status/tui/search/trace/outbox) |
| `scripts/audit_* · build_* · *_dry_run.py` | разовые read-only аудиты (см. [`evidence_audit.md`](evidence_audit.md)) |

---

> Док поддерживается вручную. При заметных изменениях логики (новый промт, новая стадия, смена схемы 1С)
> — обновляйте соответствующий раздел здесь и в [`../README.md`](../README.md).
