# API Reference

> **Назначение:** Полная спецификация REST API эндпоинтов бэкенда. Форматы запросов, ответов, коды ошибок.
>
> **Базовый URL:** `/api/v1`
>
> **Связанные документы:**
> - [`fastapi_api_architecture.md`](fastapi_api_architecture.md) — архитектура API
> - [`error_handling.md`](error_handling.md) — коды ошибок
> - [`data_flow.md`](data_flow.md) — сквозной поток данных

---

## 1. POST `/api/v1/jobs` — Создать задачу

Создаёт новую задачу в статусе `awaiting_upload`.

### Request

```
POST /api/v1/jobs
Content-Type: application/json
```

Тело запроса (опционально):

```json
{
    "mode": "heuristic"
}
```

| Поле | Тип | По умолчанию | Описание |
|---|---|---|---|
| `mode` | `string` | `"heuristic"` | Режим обработки: `"heuristic"` или `"ml"` |

### Response — `201 Created`

```json
{
    "id": 42,
    "mode": "heuristic",
    "status": "awaiting_upload",
    "session_token": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "created_at": "2026-06-22T12:00:00"
}
```

### Errors

| Код | HTTP | Условие |
|---|---|---|
| `INTERNAL_ERROR` | 500 | Ошибка БД |

---

## 2. GET `/api/v1/jobs/{job_id}` — Получить статус задачи

### Request

```
GET /api/v1/jobs/42
```

### Response — `200 OK`

```json
{
    "id": 42,
    "mode": "ml",
    "status": "processing",
    "stage": "processing_cards",
    "total": 1000,
    "processed": 456,
    "failed": 2,
    "bom_uploaded": true,
    "archive_uploaded": true,
    "created_at": "2026-06-22T12:00:00",
    "updated_at": "2026-06-22T12:05:30"
}
```

### Поля статуса

| Статус | Описание |
|---|---|
| `awaiting_upload` | Ожидание загрузки файлов |
| `processing` | Идёт обработка (смотри `stage`) |
| `done` | Успешно завершено |
| `error` | Завершено с ошибками |
| `cancelled` | Отменено пользователем |

### Поля стадии (когда `status = processing`)

| Стадия | Режим | Описание |
|---|---|---|
| `unpacking` | ML | Распаковка архива |
| `analyzing_mapping` | ML | Определение структуры через ML |
| `processing_cards` | ML + Heuristic | Обработка карт |
| `aggregating` | ML | Финальная сверка |
| `packaging` | ML | Упаковка результатов |
| `extracting_cards` | Heuristic | Распаковка архива во временную директорию |
| `generating_report` | Heuristic | Генерация отчёта |

### Errors

| Код | HTTP | Условие |
|---|---|---|
| `JOB_NOT_FOUND` | 404 | Задача с указанным ID не существует |

---

## 3. POST `/api/v1/jobs/{job_id}/parse-bom` — Разобрать BOM

Парсит загруженный BOM-файл и возвращает список доступных конфигураций. Опциональный шаг перед `/start`.

### Request

```
POST /api/v1/jobs/42/parse-bom
Content-Type: application/json
```

Тело запроса не требуется.

### Response — `200 OK`

```json
{
    "configs": ["V31", "V32", "V33"]
}
```

### Errors

| Код | HTTP | Условие |
|---|---|---|
| `JOB_NOT_FOUND` | 404 | Задача не найдена |
| `INVALID_JOB_STATE` | 409 | Статус не `awaiting_upload` |
| `FILE_UPLOAD_ERROR` | 422 | BOM не загружен |

---

## 4. POST `/api/v1/jobs/{job_id}/start` — Запустить обработку

### Request

```
POST /api/v1/jobs/42/start
Content-Type: application/json
```

Тело запроса (опционально, для ML-режима):

```json
{
    "selected_configs": ["V31", "V32"]
}
```

| Поле | Тип | Описание |
|---|---|---|
| `selected_configs` | `string[]` | Список выбранных конфигураций BOM (опционально) |

### Response — `202 Accepted`

```json
{
    "id": 42,
    "status": "processing",
    "stage": "unpacking"
}
```

Для heuristic-режима `stage` будет `extracting_cards`.

### Errors

| Код | HTTP | Условие |
|---|---|---|
| `JOB_NOT_FOUND` | 404 | Задача не найдена |
| `INVALID_JOB_STATE` | 409 | Статус не `awaiting_upload` |
| `FILE_UPLOAD_ERROR` | 422 | BOM или архив не загружены |

---

## 5. PUT `/api/v1/jobs/{job_id}/files/{role}/chunks/{n}` — Загрузить чанк

Загружает один чанк файла. Поддерживает идемпотентность — повторная отправка того же чанка возвращает `200 OK`.

### Path Parameters

| Параметр | Тип | Описание |
|---|---|---|
| `job_id` | int | ID задачи |
| `role` | string | `bom` или `archive` |
| `n` | int | Индекс чанка (0-based) |

### Headers

| Header | Обязательный | Описание |
|---|---|---|
| `Content-Type` | Да | `application/octet-stream` |
| `X-Total-Chunks` | Да | Общее количество чанков |

### Request

```
PUT /api/v1/jobs/42/files/bom/chunks/0
Content-Type: application/octet-stream
X-Total-Chunks: 5

<binary data — 20 MB>
```

### Response — `200 OK`

```json
{
    "received": 20971520,
    "chunk_index": 0,
    "total_chunks": 5
}
```

### Errors

| Код | HTTP | Условие |
|---|---|---|
| `JOB_NOT_FOUND` | 404 | Задача не найдена |
| `FILE_UPLOAD_ERROR` | 422 | Неверный `role` (не `bom`/`archive`) |
| `CHUNK_CORRUPTED` | 422 | Размер чанка не совпадает при повторной отправке |
| `STORAGE_ERROR` | 500 | Ошибка записи на диск |

---

## 6. POST `/api/v1/jobs/{job_id}/files/{role}/complete` — Подтвердить загрузку

Склеивает все чанки в итоговый файл и обновляет статус загрузки.

### Request

```
POST /api/v1/jobs/42/files/bom/complete
Content-Type: application/json
```

Тело запроса не требуется.

### Response — `200 OK`

```json
{
    "role": "bom",
    "file_size": 104857600,
    "file_path": "/data/42/bom.xlsx"
}
```

### Errors

| Код | HTTP | Условие |
|---|---|---|
| `JOB_NOT_FOUND` | 404 | Задача не найдена |
| `FILE_UPLOAD_ERROR` | 422 | Не все чанки загружены |
| `STORAGE_ERROR` | 500 | Ошибка сборки файла |

---

## 7. GET `/api/v1/jobs/{job_id}/stream` — SSE-стрим прогресса

Подписка на real-time прогресс обработки через Server-Sent Events.

### Request

```
GET /api/v1/jobs/42/stream?token=a1b2c3d4-e5f6-7890-abcd-ef1234567890
```

### Query Parameters

| Параметр | Тип | Обязательный | Описание |
|---|---|---|---|
| `token` | string | Да | `session_token` из ответа создания задачи |

### Response — `200 OK`

```
Content-Type: text/event-stream

event: progress
data: {"processed":5,"failed":0,"total":100,"stage":"processing_cards"}

event: progress
data: {"processed":10,"failed":1,"total":100}

event: complete
data: {"status":"done","stage":"done"}
```

### Errors

| Код | HTTP | Условие |
|---|---|---|
| `JOB_NOT_FOUND` | 404 | Задача не найдена |
| `INVALID_JOB_STATE` | 401 | Неверный token |

---

## 8. POST `/api/v1/jobs/{job_id}/cancel` — Отменить задачу

Отменяет выполняющуюся задачу.

### Request

```
POST /api/v1/jobs/42/cancel
Content-Type: application/json
```

Тело запроса не требуется.

### Response — `200 OK`

```json
{
    "status": "cancelled"
}
```

### Errors

| Код | HTTP | Условие |
|---|---|---|
| `JOB_NOT_FOUND` | 404 | Задача не найдена |
| `INVALID_JOB_STATE` | 409 | Статус не `processing` |

---

## 9. GET `/api/v1/jobs/{job_id}/results/diff` — Скачать таблицу расхождений

### Request

```
GET /api/v1/jobs/42/results/diff
```

### Response — `200 OK`

```
Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet
Content-Disposition: attachment; filename="diff.xlsx"

<binary stream — XLSX file>
```

### Errors

| Код | HTTP | Условие |
|---|---|---|
| `JOB_NOT_FOUND` | 404 | Задача не найдена |
| `RESULTS_NOT_READY` | 409 | Статус не `done` и не `error` |
| `STORAGE_ERROR` | 500 | Файл не найден на диске |

---

## 10. GET `/api/v1/jobs/{job_id}/results/cards` — Скачать переведённые карты

### Request

```
GET /api/v1/jobs/42/results/cards
```

### Response — `200 OK`

```
Content-Type: application/zip
Content-Disposition: attachment; filename="translated_cards.zip"

<binary stream — ZIP file>
```

### Errors

| Код | HTTP | Условие |
|---|---|---|
| `JOB_NOT_FOUND` | 404 | Задача не найдена |
| `RESULTS_NOT_READY` | 409 | Статус не `done` и не `error` |
| `STORAGE_ERROR` | 500 | Файл не найден на диске |

---

## 11. GET `/api/v1/health` — Проверка состояния системы

### Request

```
GET /api/v1/health
```

### Response — `200 OK`

```json
{
    "status": "healthy",
    "checks": {
        "database": "ok",
        "redis": "ok",
        "storage": "ok"
    }
}
```

### Response — `503 Service Unavailable`

```json
{
    "status": "unhealthy",
    "checks": {
        "database": "ok",
        "redis": "failed: connection refused",
        "storage": "ok"
    }
}
```

---

## 12. Единый формат ошибки

Все ошибки возвращаются в едином формате:

```json
{
    "error": {
        "code": "JOB_NOT_FOUND",
        "message": "Job 42 not found",
        "detail": null
    }
}
```

Поле `detail` может содержать дополнительные данные (например, список невалидных полей).

Полный список кодов ошибок — в [`error_handling.md`](error_handling.md).