# ТЗ на разработку Бэкенда (BOM Verification System)

> **Связанные документы:**
> - [`fastapi_api_architecture.md`](fastapi_api_architecture.md) — архитектура FastAPI бэкенда (слои, компоненты, диаграммы)
> - [`api_reference.md`](api_reference.md) — полная спецификация эндпоинтов с примерами запросов/ответов
> - [`error_handling.md`](error_handling.md) — коды ошибок, иерархия исключений, примеры
> - [`data_flow.md`](data_flow.md) — сквозной поток данных через все компоненты системы
> - [`container_architecture.md`](container_architecture.md) — C4 Level 2 (контейнеры)
> - [`celery_worker_arc.md`](celery_worker_arc.md) — C4 Level 3 (воркеры)

---

## 1. Архитектурный стек и конфигурация

* **Базовый язык:** Python 3.12
* **Фреймворк:** FastAPI + Uvicorn
* **Очередь задач:** Celery (Брокер = Redis, Результаты = Redis)
* **База данных:** SQLite в режиме WAL (библиотека `aiosqlite` для асинхронного взаимодействия, `sqlite3` для синхронного в Celery-воркерах)
* **Анализ таблиц:** `openpyxl` (read_only для снапшотов, обычный режим для парсинга)
* **Интеграция с ML:** Синхронный HTTP-клиент `httpx.Client` (с Circuit Breaker)

Все параметры приложения настраиваются через класс `BaseSettings` (`pydantic-settings`) в файле `app/core/config.py`, считывающий переменные из `.env`:

* `REDIS_URL` (по умолчанию `redis://redis:6379/0`)
* `ML_SERVICE_URL` (адрес сервиса машинного перевода)
* `STORAGE_PATH` (корневой каталог для сохранения файлов, по умолчанию `/data`)
* `CHUNK_SIZE_BYTES` (размер одного чанка, строго `20971520` байт / 20 МБ)

---

## 2. Жизненный цикл задачи (State Machine)

Управление конвейером осуществляется через изменение полей `status` и `stage` в таблице `jobs`.

**Важное правило:** Любая критическая ошибка на любом этапе или наличие хотя бы одного поврежденного файла (`failed > 0`) переводит статус задачи в `error`.

### 2.1. ML-режим (`mode="ml"`)

```
[awaiting_upload]
       │
   (Фронтенд загрузил BOM и Архив, вызвал /start)
       ▼
[processing / unpacking] ──> [processing / analyzing_mapping] ──> [processing / processing_cards]
                                                                              │
                                      ┌──────────────────────────────────────┴────────────────────────────────┐
                                      │                                                                       │
                          (Успешно обработано 100% карт)                                        (Есть хотя бы одна ошибка / битый файл)
                                      ▼                                                                           ▼
[processing / aggregating] ──> [processing / packaging] ──> [done]                                              [error]
```

### 2.2. Эвристический режим (`mode="heuristic"` — по умолчанию)

```
[awaiting_upload]
       │
   (Фронтенд загрузил Архив, вызвал /start)
       ▼
[processing / extracting_cards] ──> [processing / processing_cards] ──> [processing / generating_report]
                                                                                  │
                                            ┌────────────────────────────────────┴────────────────────┐
                                            │                                                         │
                                        (успех)                                                  (ошибка)
                                            ▼                                                         ▼
                                        [done]                                                   [error]
```

### Описание стадий (ML-режим):
1. **`unpacking`**: Чтение оглавления ZIP-архива, создание записей о картах в БД.
2. **`analyzing_mapping`**: Извлечение JSON-снапшотов (300 строк, макс 200 колонок) из BOM и примеров карт, отправка в ML-сервис для определения полей сопоставления. Результат сохраняется в `jobs.mapping_config`.
3. **`processing_cards`**: Параллельная обработка каждой карты: парсинг → перевод через ML (`translate_batch`) → инкремент прогресса в БД.
4. **`aggregating`**: Сбор всех материалов карт (`card_materials/*.json`), сверка с BOM через `comparator_service`, генерация `diff.xlsx` через `report_service`.
5. **`packaging`**: Сборка финального архива `translated_cards.zip` через `package` task.

### Описание стадий (эвристический режим):
1. **`extracting_cards`**: Распаковка ZIP-архива во временную директорию.
2. **`processing_cards`**: Параллельная обработка карт через `burlak_parser` (splitter → card_parser → comparator → report_generator) с использованием `ProcessPoolExecutor`.
3. **`generating_report`**: Финальная сборка `diff.xlsx` и `translated_cards.zip`.

---

## 3. Критически важные правила реализации (Памятка разработчику)

1. **Потоковая работа с ZIP (`services/archive_service.py`):** Запрещено физически распаковывать архив весом до 1.3 ГБ на жесткий диск сервера. Таска `unpack.py` должна считать оглавление архива через `zipfile.ZipFile.infolist()`. Воркеры `process_card.py` должны читать бинарные данные конкретного файла напрямую из архива через `zf.read(card_path)` (в память, для последующей обработки через `openpyxl`).
2. **Идемпотентность загрузки (`api/v1/files.py`):** Если из-за сбоя сети фронтенд повторно отправляет чанк `n`, бэкенд проверяет его наличие на диске через `core/storage.py`. Если размер файла совпадает — возвращаем `200 OK`. Если не совпадает — отдаем `422 CHUNK_CORRUPTED`.
3. **Атомарная синхронизация в SQLite вместо Celery Chord:** Отказаться от использования тяжелых Celery-аккордов (`chord`). Метод `sync_repository.increment_progress(job_id)` должен атомарно (в рамках транзакции `BEGIN IMMEDIATE` в WAL-режиме) увеличивать счетчик обработанных карт. Воркер в конце выполнения таски `process_card` проверяет: если `processed + failed == total`, он самостоятельно отправляет задачу агрегации результатов `aggregate.delay()`.
4. **Стриминг ответов (`api/v1/results.py`):** Файлы `diff.xlsx` и `translated_cards.zip` должны отдаваться клиенту через `FastAPI.responses.StreamingResponse`. Запрещено читать файлы целиком в оперативную память бэкенд-процесса.
5. **Работа с ML и XLSX:**
    * Для отправки данных в ML-сервис используется извлечение JSON-снапшота через `snapshot_service.extract_snapshot_from_bytes()` (300 строк, макс 200 колонок, в памяти через `io.BytesIO` + `openpyxl` read_only).
    * **Генерация итоговых карт:** Сохраняются **оригинальные XLSX-байты** (прочитанные из архива через `zf.read()`), а не стиль-презервинг через `openpyxl`. Это необходимо для сохранения встроенных изображений в картах. Материалы сохраняются отдельно в JSON.
6. **Два режима обработки:** Система поддерживает два режима, выбираемых при создании задачи (`POST /jobs`):
    * `mode="heuristic"` (по умолчанию) — монолитная обработка через `burlak_parser`, не требует BOM и ML-сервиса.
    * `mode="ml"` — цепочка Celery-задач с обращением к внешнему ML-сервису, требует BOM.
7. **SSE-стриминг прогресса:** Прогресс обработки публикуется в Redis Pub/Sub после каждого `increment_progress`. Фронтенд может подписаться через `GET /jobs/{id}/stream?token=<session_token>` для получения real-time обновлений.
8. **Redis cache-aside:** Статус задачи кешируется в Redis с TTL 5 секунд для быстрых GET-запросов без обращения к SQLite.
9. **Circuit Breaker для ML:** При 5 последовательных ошибках вызова ML-сервиса `StructureAdapter` переходит в режим ожидания на 60 секунд, возвращая `ManualResponseNeededError`.

---

## 4. Контракты API (Спецификация эндпоинтов)

Все ответы в случае ошибок должны возвращаться в едином формате:

```json
{ "error": { "code": "КОД_ОШИБКИ", "message": "Человекочитаемый текст", "detail": null } }
```

### 1. `POST /api/v1/jobs` — Создать задачу
* **Вход:** `{ "mode": "heuristic" | "ml" }` (опционально, по умолчанию `heuristic`)
* **Выход (201 Created):** Инициализирует задачу в состоянии `awaiting_upload`. Возвращает `id`, `mode`, `status`, `session_token`.

### 2. `PUT /api/v1/jobs/{job_id}/files/{role}/chunks/{n}` — Загрузить чанк
* **Path:** `role` (`bom` или `archive`), `n` (индекс чанка, 0-based).
* **Headers:** `Content-Type: application/octet-stream`, `X-Total-Chunks`.

### 3. `POST /api/v1/jobs/{job_id}/files/{role}/complete` — Подтвердить загрузку
* **Логика:** Вызывает `storage.assemble_chunks()`, склеивает файл, удаляет чанки.

### 4. `POST /api/v1/jobs/{job_id}/parse-bom` — Разобрать BOM (опционально)
* **Логика:** Парсит загруженный BOM-файл через `bom_parser_service`, возвращает список доступных конфигураций (например, `["V31", "V32"]`).
* **Выход (200 OK):** `{ "configs": ["V31", "V32"] }`

### 5. `POST /api/v1/jobs/{job_id}/start` — Запустить обработку
* **Вход:** `{ "selected_configs": ["V31", "V32"] }` (опционально, для ML-режима)
* **Логика:** Проверяет готовность файлов. Переводит статус в `processing`. Диспатчит задачу в зависимости от режима:
  * `mode="heuristic"` → `process_heuristic.delay(job_id)`
  * `mode="ml"` → `unpack.delay(job_id)`
* **Выход (202 Accepted):** `{ "status": "processing", "stage": "unpacking" | "extracting_cards" }`

### 6. `GET /api/v1/jobs/{job_id}` — Получить статус задачи
* **Выход (200 OK):** Возвращает состояние прогресса (`status`, `stage`, `processed`, `failed`, `total`). Использует Redis cache-aside (TTL 5 сек).

### 7. `GET /api/v1/jobs/{job_id}/stream` — SSE-стрим прогресса
* **Query:** `token=<session_token>`
* **Выход:** Server-Sent Events с событиями `progress` и `complete`.
* **Логика:** Подписывается на Redis Pub/Sub канал `job:{id}:progress`.

### 8. `POST /api/v1/jobs/{job_id}/cancel` — Отменить задачу
* **Логика:** Проверяет, что задача в статусе `processing`. Ревокит Celery-таску по `celery_task_id`. Переводит статус в `cancelled`.
* **Выход (200 OK):** `{ "status": "cancelled" }`

### 9. `GET /api/v1/jobs/{job_id}/results/diff` — Скачать таблицу расхождений
* **Выход (200 OK):** Бинарный поток `diff.xlsx`.
* **Ошибки:** `RESULTS_NOT_READY` (409 — если статус задачи не `done` и не `error`).

### 10. `GET /api/v1/jobs/{job_id}/results/cards` — Скачать переведённые карты
* **Выход (200 OK):** Бинарный поток `translated_cards.zip`.

### 11. `GET /api/v1/health` — Проверка состояния системы

---

## 5. Точная файловая структура бэкенда

```
backend/
├── app/
│   ├── api/                           # HTTP слой: роутинг и валидация
│   │   └── v1/
│   │       ├── router.py
│   │       ├── jobs.py                # POST /jobs, GET /jobs/{id}, POST /start, POST /cancel, SSE /stream
│   │       ├── files.py               # PUT /chunks, POST /complete
│   │       ├── results.py             # GET /results/diff, GET /results/cards
│   │       └── health.py
│   │
│   ├── worker/                        # Оркестрация фонового конвейера Celery
│   │   ├── celery_app.py              # Конфигурация Celery + очереди (unpack, mapping, cards, aggregate, heuristic, cleanup)
│   │   └── tasks/
│   │       ├── unpack.py              # (ML) Чтение ZIP, создание записей карт
│   │       ├── analyze_mapping.py     # (ML) Извлечение снапшотов, вызов ML для mapping_config
│   │       ├── process_card.py        # (ML) Парсинг, перевод, инкремент SQLite
│   │       ├── aggregate.py           # (ML) Сборка diff.xlsx
│   │       ├── package.py             # (ML) Сборка translated_cards.zip
│   │       ├── process_heuristic.py   # (Heuristic) Монолитная обработка через burlak_parser
│   │       └── cleanup.py             # Периодическая очистка старых задач (Beat)
│   │
│   ├── services/                      # Бизнес-логика
│   │   ├── archive_service.py         # Потоковая работа с zipfile
│   │   ├── snapshot_service.py        # XLSX → JSON снапшот (300 строк, 200 колонок)
│   │   ├── structure_adapter.py       # Синхронный HTTP-клиент для ML с Circuit Breaker
│   │   ├── card_parser_service.py     # ML-управляемый парсинг карт
│   │   ├── card_processing_service.py # Полный пайплайн обработки одной карты
│   │   ├── bom_parser_service.py      # Парсинг BOM-файлов
│   │   ├── comparator_service.py      # Алгоритмы сопоставления BOM vs карты
│   │   ├── report_service.py          # Генерация diff.xlsx
│   │   ├── heuristic_analyzer.py      # Эвристический анализатор структуры
│   │   ├── splitter.py                # Разделение multi-card листов
│   │   ├── normalizer.py              # Нормализация наименований
│   │   ├── fuzzy_matcher.py           # Нечёткое сопоставление
│   │   ├── validator.py               # Валидация XLSX-файлов
│   │   ├── xls_converter.py           # Конвертация .xls → .xlsx
│   │   ├── cache_service.py           # Redis cache-aside для статусов
│   │   ├── notification_service.py    # Уведомления через Telegram
│   │   ├── file_service.py            # Файловые операции
│   │   ├── result_service.py          # Сервис результатов
│   │   └── job_creation_service.py    # Создание задач
│   │
│   ├── db/                            # Слой персистентности (SQLite)
│   │   ├── database.py
│   │   ├── models.py                  # Таблицы jobs (вкл. mapping_config, mode) + cards
│   │   ├── async_repository.py        # Асинхронный репозиторий для FastAPI
│   │   └── sync_repository.py         # Синхронный репозиторий для Celery-воркеров
│   │
│   ├── schemas/                       # Валидация Pydantic
│   │   ├── job.py
│   │   ├── cards.py
│   │   ├── file.py
│   │   └── health.py
│   │
│   ├── core/                          # Системное ядро
│   │   ├── config.py
│   │   ├── exceptions.py
│   │   ├── redis.py                   # Redis-клиент + Pub/Sub
│   │   ├── storage.py
│   │   └── ml_stub.py                 # Заглушка ML-сервиса для тестов
│   │
│   └── main.py
│
├── tests/                             # Тесты
│   ├── backend/                       # Unit-тесты бэкенда
│   ├── parser/                        # Тесты парсера
│   ├── services/                      # Тесты сервисов
│   ├── worker/                        # Тесты Celery-задач
│   ├── integration/                   # Интеграционные тесты
│   └── e2e/                           # End-to-end тесты
│
├── pyproject.toml
├── uv.lock
├── .python-version
├── Dockerfile
└── .env.example
