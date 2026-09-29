# Техническая документация API сервиса `screc-server`

Версия: **1.0.0**
Назначение: синхронизация записей (протоколов, стенограмм, summary) между клиентами одного пользователя через HTTP API. Хранение — файловая система, без БД.

---

## Содержание

1. [Обзор](#1-обзор)
2. [Быстрый старт](#2-быстрый-старт)
3. [Аутентификация](#3-аутентификация)
4. [Модель данных](#4-модель-данных)
5. [Структура хранилища](#5-структура-хранилища)
6. [Справочник эндпоинтов](#6-справочник-эндпоинтов)
7. [Сценарии интеграции](#7-сценарии-интеграции)
8. [Ошибки](#8-ошибки)
9. [Ограничения и лимиты](#9-ограничения-и-лимиты)
10. [Пример клиента на Python](#10-пример-клиента-на-python)
11. [Эксплуатация](#11-эксплуатация)
12. [Версионирование и совместимость](#12-версионирование-и-совместимость)

---

## 1. Обзор

### 1.1. Что делает сервис

- Принимает «лёгкие» артефакты записей (стенограммы, протоколы, summary, вложения) от клиентов.
- Хранит их в виде дерева `records/<project>/<year>/<month>/<folder_name>/`.
- Отдаёт ссылки на большие файлы (видео, аудио) — сами файлы не загружаются.
- Поддерживает delta-синхронизацию через монотонный `revision` и `changelog.jsonl`.
- Даёт полнотекстовый поиск (инвертированный индекс в JSON).

### 1.2. Чего сервис **не** делает

- Не хранит видео и аудио.
- Не запускает транскрибацию/суммаризацию.
- Не управляет правами — один API-ключ на весь сервис.
- Не различает пользователей — вся модель под одного владельца.

### 1.3. Технологии

| Слой | Технология |
|---|---|
| Web-фреймворк | FastAPI 0.115 |
| Сервер | Uvicorn 0.32 |
| Валидация | Pydantic 2.9 |
| Конфигурация | pydantic-settings |
| Хранилище | файловая система |
| Логи | logging + RotatingFileHandler |

---

## 2. Быстрый старт

### 2.1. Запуск через Docker Compose

```bash
git clone <repo>
cd screc-server
cp .env.example .env
# отредактировать .env — задать SCREC_API_KEY
docker compose up -d --build
```

### 2.2. Проверка

```bash
curl -s http://localhost:8000/health | jq
```

Ответ:

```json
{
  "status": "ok",
  "service": "screc-sync",
  "time": "2026-09-29T12:05:00Z"
}
```

### 2.3. Первый запрос с ключом

```bash
API_KEY="<значение SCREC_API_KEY из .env>"

curl -s -H "X-API-Key: $API_KEY" \
     http://localhost:8000/api/v1/tree | jq
```

Ожидаемо: `[]` (пустой список проектов).

---

## 3. Аутентификация

### 3.1. Схема

Единственный API-ключ. Передаётся в заголовке:

```
X-API-Key: <ключ>
```

Ключ задаётся через переменную окружения `SCREC_API_KEY` на сервере. Сравнение — constant-time (`hmac.compare_digest`).

### 3.2. Что защищено

| Эндпоинт | Требует ключ |
|---|---|
| `GET /health` | ❌ |
| `GET /docs`, `/openapi.json`, `/redoc` | ❌ (если включено) |
| Все `/api/v1/**` | ✅ |

### 3.3. Ответы при ошибке

| Ситуация | HTTP | Тело |
|---|---|---|
| Заголовок отсутствует | 401 | `{"detail": "Missing X-API-Key header"}` |
| Ключ неверный | 401 | `{"detail": "Invalid API key"}` |
| Ключ не задан на сервере | 500 | `{"detail": "Server misconfigured: API key not set"}` |

### 3.4. Рекомендации

- Ключ должен быть длинным: ≥ 32 символа. Генерация:
  ```bash
  python3 -c "import secrets; print(secrets.token_urlsafe(32))"
  ```
- Передавать только по HTTPS (в проде — за nginx/Traefik с TLS).
- Не логировать значение ключа (сервер этого и не делает).

---

## 4. Модель данных

### 4.1. `RecordPayload` — запрос на создание/обновление

Передаётся как **JSON-строка** в поле `payload` multipart-формы.

| Поле | Тип | Обяз. | Описание |
|---|---|---|---|
| `id` | string \| null | ❌ | UUID записи. Если не задан — сервер сгенерирует. |
| `project` | string | ✅ | Имя проекта (папка верхнего уровня). 1–200 символов. |
| `year` | string | ✅ | Год, 4 символа, например `"2026"`. |
| `month` | string | ✅ | Месяц, 1–2 символа, например `"09"` или `"9"`. |
| `folder_name` | string | ✅ | Имя папки записи. 1–250 символов. |
| `name` | string | ❌ | Читаемое название записи. |
| `description` | string | ❌ | Описание. |
| `comment` | string | ❌ | Комментарий пользователя. |
| `source` | string | ❌ | `"record"` / `"upload"` / `"import"`. По умолчанию `"record"`. |
| `is_scrum` | bool | ❌ | Признак скрам-митинга. |
| `date` | string | ❌ | Дата записи, `YYYY-MM-DD`. |
| `time` | string | ❌ | Время, `HH-MM-SS` или `HH:MM:SS`. |
| `tags` | string[] | ❌ | Список тегов. |
| `summary_bb` | string | ❌ | Краткое описание в Markdown. |
| `prompt` | string | ❌ | Пользовательский промпт. |
| `prompt_name` | string | ❌ | Имя промпта из библиотеки. |
| `prompt_edited` | bool | ❌ | Был ли промпт отредактирован вручную. |
| `name_template` | string | ❌ | Шаблон имени. |
| `name_abbr` | string | ❌ | Сокращение. |
| `generate_summary` | bool | ❌ | Флаг генерации summary. |
| `generate_deepseek_prompt` | bool | ❌ | Флаг генерации DeepSeek-промпта. |
| `include_name_in_prompt` | bool | ❌ | Включать имя в промпт. |
| `include_project_in_prompt` | bool | ❌ | Включать проект в промпт. |
| `include_comment_in_prompt` | bool | ❌ | Включать комментарий. |
| `include_tags_in_prompt` | bool | ❌ | Включать теги. |
| `video_url` | string | ❌ | Ссылка на видео (`file://`, `http(s)://`). |
| `video_size` | int | ❌ | Размер видео в байтах. |
| `video_mime` | string | ❌ | MIME-тип видео. |
| `video_duration` | float | ❌ | Длительность в секундах. |

### 4.2. `RecordFull` — полное представление записи

Возвращается `GET /api/v1/records/{id}`.

Содержит все поля `RecordPayload` плюс:

| Поле | Тип | Описание |
|---|---|---|
| `revision` | int | Номер ревизии. Растёт при каждом изменении. |
| `path` | string | Относительный путь внутри `records/`. |
| `created_at` | string | ISO-8601 UTC. |
| `updated_at` | string | ISO-8601 UTC. |
| `artifacts` | `ArtifactInfo[]` | Список артефактов. |
| `video` | object \| null | `{url, size, mime, duration, updated_at}`. |

### 4.3. `ArtifactInfo`

| Поле | Тип | Описание |
|---|---|---|
| `kind` | string | `transcript` / `summary` / `protocol` / `manual_protocol` / `deepseek_prompt` / `attachment` |
| `filename` | string | Оригинальное имя файла |
| `size` | int | Размер в байтах |
| `sha256` | string | SHA-256 содержимого |

### 4.4. `TreeRecord` — элемент списка в дереве

| Поле | Тип |
|---|---|
| `id` | string |
| `folder_name` | string |
| `name` | string |
| `date` | string |
| `tags` | string[] |
| `has_summary` | bool |
| `artifacts_count` | int |

### 4.5. `SyncChange` — элемент changelog

| Поле | Тип | Описание |
|---|---|---|
| `rev` | int | Номер ревизии |
| `ts` | string | ISO-8601 UTC |
| `entity` | string | Всегда `"record"` (пока) |
| `action` | string | `create` / `update` / `delete` / `update_links` |
| `id` | string | ID записи |
| `path` | string | Относительный путь |
| `payload` | object \| null | Метаданные изменения |
| `old_path` | string \| null | Прежний путь (для `move`) |

---

## 5. Структура хранилища

### 5.1. Дерево каталогов

```
/data/
├── ._lock                        # flock-файл
├── revision.txt                  # монотонный счётчик
├── changelog.jsonl               # append-only лог
├── fts.json                      # инвертированный индекс
└── records/
    ├── vNext/
    │   └── 2026/
    │       └── 09/
    │           └── 2026-09-08 формирование реестра замечаний после ПСИ/
    │               ├── _meta.json
    │               ├── _links.json
    │               ├── transcript_запись.md
    │               └── ПРОТОКОЛ СОВЕЩАНИЯ.docx
    └── ЛенЭнерго/
        └── 2026/...
```

### 5.2. Служебные файлы

Все служебные файлы начинаются с `_`:

- **`_meta.json`** — метаданные записи (все поля `RecordFull`, кроме `video`).
- **`_links.json`** — ссылки на большие файлы (видео/аудио).
- **`_deleted.json`** — маркер soft-delete. Присутствие файла = запись удалена.

### 5.3. Формат `_meta.json`

```json
{
  "id": "770e8400-e29b-41d4-a716-446655440111",
  "project": "vNext",
  "year": "2026",
  "month": "09",
  "folder_name": "2026-09-08 формирование реестра замечаний после ПСИ",
  "name": "формирование реестра замечаний после ПСИ",
  "description": "",
  "comment": "",
  "source": "record",
  "is_scrum": false,
  "date": "2026-09-08",
  "time": "08-31-14",
  "created_at": "2026-09-08T08:35:00+00:00",
  "updated_at": "2026-09-08T08:35:00+00:00",
  "tags": ["важное", "ПСИ"],
  "summary_bb": "**Краткое описание**\n...",
  "prompt": "...",
  "prompt_name": "Протокол совещания",
  "prompt_edited": true,
  "name_template": "",
  "name_abbr": "",
  "generate_summary": true,
  "generate_deepseek_prompt": true,
  "include_name_in_prompt": true,
  "include_project_in_prompt": true,
  "include_comment_in_prompt": true,
  "include_tags_in_prompt": true,
  "artifacts": [
    {
      "kind": "transcript",
      "filename": "transcript_запись.md",
      "size": 45678,
      "sha256": "abc123..."
    }
  ],
  "revision": 101,
  "deleted_at": null
}
```

### 5.4. Формат `_links.json`

```json
{
  "video": {
    "url": "file:///home/user/recs/video.webm",
    "size": 123456789,
    "mime": "video/webm",
    "duration": 3600.5,
    "updated_at": "2026-09-08T08:35:00+00:00"
  },
  "updated_at": "2026-09-08T08:35:00+00:00"
}
```

Если видео нет — `"video": null`.

### 5.5. Формат `changelog.jsonl`

Одна строка = одно событие:

```jsonl
{"rev":1,"ts":"2026-09-08T08:35:00+00:00","entity":"record","action":"create","id":"770e...","path":"vNext/2026/09/2026-09-08 формирование...","payload":{"full":true,"project":"vNext","date":"2026-09-08"}}
{"rev":2,"ts":"2026-09-08T09:00:00+00:00","entity":"record","action":"update","id":"770e...","path":"vNext/2026/09/2026-09-08 формирование...","payload":{"changed":["summary_bb","tags"]}}
{"rev":3,"ts":"2026-09-08T10:00:00+00:00","entity":"record","action":"delete","id":"770e...","path":"vNext/2026/09/2026-09-08 формирование..."}
```

---

## 6. Справочник эндпоинтов

Базовый префикс: `/api/v1`. Все эндпоинты ниже требуют заголовок `X-API-Key`, если не указано иное.

### 6.1. Health

#### `GET /health` — без авторизации

Проверка работоспособности.

**Ответ 200:**

```json
{
  "status": "ok",
  "revision": 101,
  "records_count": 42,
  "data_root": "/data",
  "fts_enabled": true,
  "time": "2026-09-29T12:05:00+00:00"
}
```

---

### 6.2. Дерево

#### `GET /tree` — список проектов

**Ответ 200:**

```json
[
  {
    "name": "vNext",
    "years": ["2025", "2026"],
    "records_count": 15
  },
  {
    "name": "ЛенЭнерго",
    "years": ["2026"],
    "records_count": 3
  }
]
```

#### `GET /tree/{project}` — годы проекта

**Ответ 200:** `["2025", "2026"]`

**Ошибки:** `404` — проект не найден.

#### `GET /tree/{project}/{year}` — месяцы

**Ответ 200:** `["01", "02", ..., "09"]`

#### `GET /tree/{project}/{year}/{month}` — записи месяца

**Ответ 200:**

```json
[
  {
    "id": "770e8400-...",
    "folder_name": "2026-09-08 формирование реестра замечаний после ПСИ",
    "name": "формирование реестра замечаний после ПСИ",
    "date": "2026-09-08",
    "tags": ["важное", "ПСИ"],
    "has_summary": true,
    "artifacts_count": 2
  }
]
```

---

### 6.3. Записи

#### `POST /records` — создать или обновить

**Content-Type:** `multipart/form-data`

**Поля формы:**

| Поле | Тип | Обяз. | Описание |
|---|---|---|---|
| `payload` | string (JSON) | ✅ | JSON-строка с `RecordPayload`. |
| `files` | file[] | ❌ | Артефакты. |
| `kinds` | string[] | ❌ | Параллельный массив типов для `files`. |

**Правила:**
- Если `kinds` задан, его длина должна совпадать с длиной `files`.
- Если `kinds` не задан — все файлы получат `kind="attachment"`.
- Максимальный размер `payload`: `SCREC_MAX_PAYLOAD_KB` (по умолчанию 512 КБ).
- Максимальный размер каждого файла: `SCREC_MAX_ARTIFACT_MB` (по умолчанию 50 МБ).

**Ответ 200:**

```json
{
  "id": "770e8400-...",
  "revision": 101,
  "action": "create",
  "path": "vNext/2026/09/2026-09-08 формирование реестра замечаний после ПСИ",
  "artifacts": [
    {
      "kind": "transcript",
      "filename": "transcript_запись.md",
      "size": 45678,
      "sha256": "abc123..."
    }
  ]
}
```

- `action` = `"create"` при первой публикации, `"update"` при повторной (по тому же пути).

**Ошибки:**
- `400` — некорректный payload, `kinds` не совпадает с `files`.
- `413` — payload или файл превышает лимит.

**Идемпотентность:** повторная публикация по тому же `(project, year, month, folder_name)` обновляет запись, а не создаёт дубликат.

---

#### `GET /records/{record_id}` — полная запись

**Ответ 200:** объект `RecordFull`.

**Ошибки:** `404` — запись не найдена.

---

#### `PATCH /records/{record_id}` — частичное обновление

**Content-Type:** `application/json`

**Тело:** объект с любыми полями из списка (все опциональные):

`summary_bb`, `comment`, `name`, `description`, `tags`, `is_scrum`, `generate_summary`, `generate_deepseek_prompt`, `include_name_in_prompt`, `include_project_in_prompt`, `include_comment_in_prompt`, `include_tags_in_prompt`, `prompt`, `prompt_name`, `prompt_edited`, `video_url`, `video_size`, `video_mime`.

**Ответ 200:**

```json
{
  "id": "770e8400-...",
  "revision": 102,
  "changed": ["summary_bb", "tags"]
}
```

Если ничего не изменилось — `"changed": []` и та же ревизия.

**Ошибки:** `404`.

---

#### `DELETE /records/{record_id}` — удаление

**Query-параметры:**

| Имя | Тип | По умолчанию | Описание |
|---|---|---|---|
| `hard` | bool | `false` | Если `true` — удалить папку физически. Если `false` — soft-delete (маркер `_deleted.json`). |

**Ответ 200:**

```json
{
  "id": "770e8400-...",
  "revision": 103,
  "hard": false
}
```

Soft-delete: запись исключается из `/tree`, но остаётся на диске. Событие `delete` пишется в `changelog`.

---

#### `GET /records/{record_id}/video-url` — ссылка на видео

**Ответ 200:**

```json
{
  "url": "file:///home/user/recs/video.webm",
  "size": 123456789,
  "mime": "video/webm",
  "duration": 3600.5,
  "available": true
}
```

Если ссылки нет — `{"url": "", "available": false}`.

---

#### `PUT /records/{record_id}/video-url` — обновить ссылку

**Content-Type:** `application/json`

**Тело:**

```json
{
  "url": "file:///home/user/recs/video.webm",
  "size": 123456789,
  "mime": "video/webm",
  "duration": 3600.5
}
```

Все поля опциональные. Пустые не перезаписываются.

**Ответ 200:** `{"id": "...", "revision": 104, "video": {...}}`.

---

### 6.4. Артефакты

#### `GET /records/{record_id}/artifacts` — список

**Ответ 200:**

```json
{
  "id": "770e8400-...",
  "items": [
    {
      "kind": "transcript",
      "filename": "transcript_запись.md",
      "size": 45678,
      "sha256": "abc123..."
    }
  ]
}
```

---

#### `GET /records/{record_id}/artifacts/{filename}` — скачать

**Query-параметры:**

| Имя | Тип | По умолчанию | Описание |
|---|---|---|---|
| `inline` | bool | `true` | `Content-Disposition: inline` vs `attachment`. |

**Ответ 200:** бинарный контент с `Content-Disposition: inline; filename="..."`.

**Ошибки:** `404` — запись или файл не найден; `400` — недопустимое имя файла.

Имя файла URL-кодируется. Кириллица допустима:

```
GET /api/v1/records/770e.../artifacts/ПРОТОКОЛ%20СОВЕЩАНИЯ%20от%201%20сентября%202026.docx
```

---

#### `POST /records/{record_id}/artifacts` — загрузить артефакт

**Content-Type:** `multipart/form-data`

| Поле | Тип | Обяз. |
|---|---|---|
| `file` | file | ✅ |
| `kind` | string | ❌ (по умолчанию `"attachment"`) |

**Ответ 200:**

```json
{
  "id": "770e8400-...",
  "filename": "новая_схема.docx",
  "size": 12345
}
```

Если файл с таким именем уже есть — он **заменяется**. Запись в `_meta.json` обновляется.

---

#### `DELETE /records/{record_id}/artifacts/{filename}` — удалить

**Ответ 200:** `{"id": "...", "filename": "...", "deleted": true}`.

Файл удаляется с диска и из `_meta.json`.

---

#### `GET /records/{record_id}/transcript` — стенограмма

Возвращает содержимое артефакта с `kind="transcript"` как `text/plain; charset=utf-8`.

**Ошибки:** `404` — стенограммы нет.

---

#### `GET /records/{record_id}/summary` — summary

Возвращает поле `summary_bb` из `_meta.json` как `text/markdown; charset=utf-8`.

**Ошибки:** `404` — summary нет.

---

### 6.5. Поиск

#### `GET /records/_/search` — полнотекстовый поиск

**Query-параметры:**

| Имя | Тип | Обяз. | Описание |
|---|---|---|---|
| `q` | string | ✅ | Поисковый запрос. |
| `project` | string | ❌ | Ограничить проектом. |
| `limit` | int | ❌ | Максимум результатов (1–500, по умолчанию 50). |

**Ответ 200:**

```json
{
  "query": "реестр замечаний",
  "total": 3,
  "items": [
    {
      "id": "770e8400-...",
      "path": "vNext/2026/09/2026-09-08 формирование реестра замечаний после ПСИ",
      "project": "vNext",
      "date": "2026-09-08",
      "name": "формирование реестра замечаний после ПСИ",
      "score": 3
    }
  ]
}
```

`score` — число совпавших токенов запроса. Результаты отсортированы по `score` по убыванию.

**Токенизация:** регистронезависимая, поддерживает кириллицу и латиницу. Схлопывает 3+ повторов (`аааа` → `аа`).

**Что индексируется:** `summary_bb`, `comment`, `prompt`, `name`, а также содержимое артефактов с `kind` ∈ `{transcript, protocol, manual_protocol}` (если файл < 2 МБ).

---

### 6.6. Синхронизация

#### `GET /sync/changes` — дельта изменений

**Query-параметры:**

| Имя | Тип | Обяз. | Описание |
|---|---|---|---|
| `since_revision` | int | ❌ | Вернуть изменения с `rev > since_revision`. По умолчанию 0. |
| `limit` | int | ❌ | Максимум изменений (1–1000, по умолчанию `SCREC_SYNC_PAGE_SIZE` = 200). |

**Ответ 200:**

```json
{
  "server_revision": 103,
  "has_more": false,
  "changes": [
    {
      "rev": 101,
      "ts": "2026-09-29T12:05:00+00:00",
      "entity": "record",
      "action": "create",
      "id": "770e8400-...",
      "path": "vNext/2026/09/2026-09-08 формирование...",
      "payload": {"full": true, "project": "vNext", "date": "2026-09-08"}
    }
  ]
}
```

**Алгоритм клиента:**
1. Хранить `last_synced_revision` локально.
2. Запрашивать `changes?since_revision=<last_synced_revision>`.
3. Применять изменения, обновить `last_synced_revision = server_revision`.
4. Если `has_more == true` — повторить с новым `since_revision`.

---

#### `GET /sync/snapshot` — полный дамп

**Ответ 200:**

```json
{
  "server_revision": 103,
  "total": 42,
  "records": [
    { ...RecordFull... }
  ]
}
```

Используется при **первой** синхронизации (когда `last_synced_revision = 0` и `changes` не подходит).

---

## 7. Сценарии интеграции

### 7.1. Публикация одной записи (Python + requests)

```python
import json
import requests

BASE = "http://localhost:8000"
KEY = "<ваш API-ключ>"
HEADERS = {"X-API-Key": KEY}

payload = {
    "project": "vNext",
    "year": "2026",
    "month": "09",
    "folder_name": "2026-09-08 формирование реестра замечаний после ПСИ",
    "name": "формирование реестра замечаний после ПСИ",
    "date": "2026-09-08",
    "time": "08-31-14",
    "tags": ["важное", "ПСИ"],
    "summary_bb": "**Краткое описание**\n\nОбсудили реестр.",
    "video_url": "file:///home/user/recs/video.webm",
    "video_size": 123456789,
    "video_mime": "video/webm",
}

files = [
    ("files", ("transcript.md", open("/tmp/transcript.md", "rb"), "text/markdown")),
    ("kinds", (None, "transcript")),
    ("files", ("protocol.docx", open("/tmp/protocol.docx", "rb"),
               "application/vnd.openxmlformats-officedocument.wordprocessingml.document")),
    ("kinds", (None, "protocol")),
]

r = requests.post(
    f"{BASE}/api/v1/records",
    headers=HEADERS,
    data={"payload": json.dumps(payload, ensure_ascii=False)},
    files=files,
    timeout=60,
)
r.raise_for_status()
print(r.json())
```

**Важно:** `kinds` — тоже список, но каждая пара `(None, value)` идёт как отдельный элемент multipart. В `requests` порядок сохраняется.

### 7.2. Первая синхронизация на пустом клиенте

```python
state = {"last_synced_revision": 0}

snap = requests.get(
    f"{BASE}/api/v1/sync/snapshot", headers=HEADERS
).json()

for rec in snap["records"]:
    # 1. Создать папку rec["path"] в локальном хранилище
    # 2. Скачать все артефакты
    for art in rec["artifacts"]:
        url = f"{BASE}/api/v1/records/{rec['id']}/artifacts/{quote(art['filename'])}"
        content = requests.get(url, headers=HEADERS).content
        # сохранить по локальному пути
    # 3. Сохранить _meta.json и _links.json локально

state["last_synced_revision"] = snap["server_revision"]
```

### 7.3. Инкрементальная синхронизация

```python
while True:
    r = requests.get(
        f"{BASE}/api/v1/sync/changes",
        headers=HEADERS,
        params={"since_revision": state["last_synced_revision"], "limit": 200},
    ).json()

    for ch in r["changes"]:
        if ch["action"] == "delete":
            # удалить локальную папку
            pass
        else:
            # скачать запись через GET /records/{id}
            rec = requests.get(
                f"{BASE}/api/v1/records/{ch['id']}", headers=HEADERS
            ).json()
            # применить локально
            pass

    state["last_synced_revision"] = r["server_revision"]

    if not r["has_more"]:
        break

# сохранить state["last_synced_revision"] на диск
```

### 7.4. Клиент без видео (только ссылки)

При получении `RecordFull`:
- `video.url` — использовать как есть (открывать в браузере или скачивать).
- Если `video.url` начинается с `file://`, а файла нет — запись помечается как «видео недоступно локально».

### 7.5. Обновление summary без перезагрузки файлов

```python
requests.patch(
    f"{BASE}/api/v1/records/{record_id}",
    headers={**HEADERS, "Content-Type": "application/json"},
    json={"summary_bb": "Новое описание", "tags": ["важное"]},
)
```

Файлы-артефакты не трогаются. `revision` инкрементируется, изменение попадает в `changelog`.

---

## 8. Ошибки

### 8.1. Формат тела ошибки

```json
{
  "detail": "Human-readable описание"
}
```

### 8.2. Коды

| HTTP | Значение | Когда |
|---|---|---|
| 200 | OK | Успех |
| 400 | Bad Request | Некорректный payload, `kinds` не совпадает, path traversal |
| 401 | Unauthorized | Нет или неверный `X-API-Key` |
| 404 | Not Found | Запись/проект/файл не найдены |
| 413 | Payload Too Large | Файл или payload превышает лимит |
| 422 | Unprocessable Entity | Ошибки валидации Pydantic |
| 500 | Internal Server Error | Ошибка сервера (смотрите `logs/screc.error.log`) |

### 8.3. Примеры

**Неверный ключ:**

```json
{"detail": "Invalid API key"}
```

**Слишком большой файл:**

```json
{"detail": "Artifact 'big.docx' too large: 52428801 > 52428800"}
```

**Некорректный payload:**

```json
{"detail": "Invalid payload: 1 validation error for RecordPayload\nproject\n  Field required"}
```

---

## 9. Ограничения и лимиты

| Параметр | Переменная | По умолчанию |
|---|---|---|
| Макс. размер payload | `SCREC_MAX_PAYLOAD_KB` | 512 КБ |
| Макс. размер артефакта | `SCREC_MAX_ARTIFACT_MB` | 50 МБ |
| Лимит выдачи `/sync/changes` | `SCREC_SYNC_PAGE_SIZE` | 200 |
| Абсолютный лимит `/sync/changes` | — | 1000 |
| Макс. длина `project` | — | 200 |
| Макс. длина `folder_name` | — | 250 |
| Макс. длина `year` | — | 4 |
| Макс. длина `month` | — | 2 |
| Индексируемый размер артефакта | — | 2 МБ |
| Макс. результатов поиска | — | 500 |

### 9.1. Санитизация имён

Все сегменты пути (`project`, `year`, `month`, `folder_name`) проходят через `_safe_segment`:
- NFC-нормализация Unicode;
- запрещённые символы `<>:"/\|?*` и управляющие → `_`;
- схлопывание пробелов;
- удаление ведущих/замыкающих точек и пробелов;
- обрезка до 200 байт UTF-8 (без разрыва символов).

Имена артефактов — только базовое имя (`Path(filename).name`). Путь проверяется через `assert_inside`.

---

## 10. Пример клиента на Python

```python
"""Минимальный клиент для screc-server."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx


class ScrecClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={"X-API-Key": api_key},
            timeout=timeout,
        )

    # --- Health ---
    def health(self) -> dict:
        return self.client.get("/health").json()

    # --- Дерево ---
    def projects(self) -> list:
        return self.client.get("/api/v1/tree").json()

    def years(self, project: str) -> list[str]:
        return self.client.get(f"/api/v1/tree/{quote(project)}").json()

    def months(self, project: str, year: str) -> list[str]:
        return self.client.get(
            f"/api/v1/tree/{quote(project)}/{year}"
        ).json()

    def records_of_month(
        self, project: str, year: str, month: str
    ) -> list:
        return self.client.get(
            f"/api/v1/tree/{quote(project)}/{year}/{month}"
        ).json()

    # --- Записи ---
    def publish(
        self,
        payload: dict,
        artifacts: list[tuple[str, Path]] | None = None,
    ) -> dict:
        files = []
        kinds = []
        for kind, path in artifacts or []:
            files.append(("files", (path.name, path.read_bytes())))
            kinds.append(("kinds", (None, kind)))

        r = self.client.post(
            "/api/v1/records",
            data=[
                ("payload", (None, json.dumps(payload, ensure_ascii=False))),
                *kinds,
            ],
            files=files,
        )
        r.raise_for_status()
        return r.json()

    def get_record(self, record_id: str) -> dict:
        r = self.client.get(f"/api/v1/records/{record_id}")
        r.raise_for_status()
        return r.json()

    def patch_record(self, record_id: str, patch: dict) -> dict:
        r = self.client.patch(
            f"/api/v1/records/{record_id}", json=patch
        )
        r.raise_for_status()
        return r.json()

    def delete_record(self, record_id: str, hard: bool = False) -> dict:
        r = self.client.delete(
            f"/api/v1/records/{record_id}", params={"hard": hard}
        )
        r.raise_for_status()
        return r.json()

    # --- Артефакты ---
    def download_artifact(
        self, record_id: str, filename: str, target: Path
    ) -> Path:
        url = (
            f"/api/v1/records/{record_id}/artifacts/"
            f"{quote(filename)}"
        )
        with self.client.stream("GET", url) as r:
            r.raise_for_status()
            with open(target, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        return target

    def upload_artifact(
        self, record_id: str, kind: str, path: Path
    ) -> dict:
        r = self.client.post(
            f"/api/v1/records/{record_id}/artifacts",
            data={"kind": kind},
            files={"file": (path.name, path.read_bytes())},
        )
        r.raise_for_status()
        return r.json()

    # --- Sync ---
    def changes(self, since_revision: int, limit: int = 200) -> dict:
        r = self.client.get(
            "/api/v1/sync/changes",
            params={"since_revision": since_revision, "limit": limit},
        )
        r.raise_for_status()
        return r.json()

    def snapshot(self) -> dict:
        r = self.client.get("/api/v1/sync/snapshot")
        r.raise_for_status()
        return r.json()

    # --- Поиск ---
    def search(self, q: str, project: str = "", limit: int = 50) -> dict:
        r = self.client.get(
            "/api/v1/records/_/search",
            params={"q": q, "project": project, "limit": limit},
        )
        r.raise_for_status()
        return r.json()

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "ScrecClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
```

**Использование:**

```python
with ScrecClient("http://localhost:8000", "my-key") as c:
    print(c.health())
    print(c.projects())

    result = c.publish(
        payload={
            "project": "vNext",
            "year": "2026",
            "month": "09",
            "folder_name": "2026-09-29 тест",
            "name": "тест",
            "date": "2026-09-29",
        },
        artifacts=[
            ("transcript", Path("/tmp/transcript.md")),
        ],
    )
    print(result)

    hits = c.search("тест")
    print(hits)
```

---

## 11. Эксплуатация

### 11.1. Переменные окружения

| Переменная | Назначение | Пример |
|---|---|---|
| `SCREC_API_KEY` | Ключ авторизации | `secret-token` |
| `SCREC_DATA_ROOT` | Корень данных | `/data` |
| `SCREC_LOG_ROOT` | Корень логов | `/logs` |
| `SCREC_HOST` | Адрес bind | `0.0.0.0` |
| `SCREC_PORT` | Порт | `8000` |
| `SCREC_LOG_LEVEL` | Уровень логов | `INFO` |
| `SCREC_ACCESS_LOG` | Включать access-log | `true` |
| `SCREC_MAX_ARTIFACT_MB` | Лимит на файл | `50` |
| `SCREC_MAX_PAYLOAD_KB` | Лимит на payload | `512` |
| `SCREC_SYNC_PAGE_SIZE` | Размер страницы sync | `200` |
| `SCREC_FTS_ENABLED` | Включать FTS | `true` |
| `SCREC_OPENAPI_ENABLED` | Отдавать `/docs` | `true` |

### 11.2. Логи

| Файл | Содержимое |
|---|---|
| `/logs/screc.log` | Все события уровня DEBUG и выше |
| `/logs/screc.error.log` | Только WARNING и выше |
| `/logs/access/access.log` | HTTP-запросы |

Ротация: 10 МБ × 5 файлов (для access — ×10).

**Формат основной строки:**

```
2026-09-29 12:05:00.756 [INFO    ] screc.app.main: сообщение
```

**Формат access:**

```
2026-09-29 12:05:00.756 [ACCESS] 172.18.0.1 POST /api/v1/records → 200 (45.2 ms) [a1b2c3d4]
```

### 11.3. Проверка состояния

```bash
# Health
curl -s http://localhost:8000/health | jq

# Количество записей на диске
find data/records -name _meta.json | wc -l

# Текущая ревизия
cat data/revision.txt

# Последние 10 изменений
tail -10 data/changelog.jsonl | jq

# Размер индекса
ls -lh data/fts.json
```

### 11.4. Бэкап

```bash
tar czf screc-backup-$(date +%F).tar.gz data/
```

При остановленном контейнере — достаточно скопировать `data/`.

### 11.5. Восстановление

```bash
tar xzf screc-backup-YYYY-MM-DD.tar.gz
docker compose restart
```

Если FTS-индекс рассинхронизировался (редкий случай) — удалите `data/fts.json` и перезапустите сервис. Индекс пересоберётся при следующей публикации/обновлении.

---

## 12. Версионирование и совместимость

### 12.1. Текущая версия

`1.0.0` — API стабилен, breaking changes не планируются без мажорного бампа.

### 12.2. Правила совместимости

- **Мажорная версия** — несовместимые изменения (переименование полей, изменение семантики action, смена схемы хранения).
- **Минорная** — новые поля (клиенты их игнорируют), новые эндпоинты.
- **Патч** — исправления.

### 12.3. Что делать клиенту при обновлении сервера

- Читать `record.revision` — он всегда растёт.
- Не полагаться на порядок полей в JSON.
- Игнорировать незнакомые поля (FastAPI/Pydantic это позволяет).
- Использовать `since_revision` для дельты, а `snapshot` — только при `last_synced_revision == 0`.

### 12.4. Обратная совместимость хранилища

- Новые поля в `_meta.json` добавляются, старые не удаляются.
- `_links.json` может содержать новые ключи (`audio`) — старые клиенты их игнорируют.
- Формат `changelog.jsonl` append-only; старые события не переписываются.
- При изменении формата `_meta.json` сервис мигрирует на чтении.

---

## Приложение A. Полный список эндпоинтов

| Метод | Путь | Авторизация | Назначение |
|---|---|---|---|
| GET | `/health` | ❌ | Проверка живости |
| GET | `/docs` | ❌ | Swagger UI |
| GET | `/redoc` | ❌ | ReDoc |
| GET | `/openapi.json` | ❌ | OpenAPI-схема |
| GET | `/api/v1/health` | ✅ | Дублирует `/health` |
| GET | `/api/v1/tree` | ✅ | Список проектов |
| GET | `/api/v1/tree/{project}` | ✅ | Годы проекта |
| GET | `/api/v1/tree/{project}/{year}` | ✅ | Месяцы |
| GET | `/api/v1/tree/{project}/{year}/{month}` | ✅ | Записи месяца |
| POST | `/api/v1/records` | ✅ | Создать/обновить |
| GET | `/api/v1/records/{id}` | ✅ | Получить |
| PATCH | `/api/v1/records/{id}` | ✅ | Обновить поля |
| DELETE | `/api/v1/records/{id}` | ✅ | Удалить |
| GET | `/api/v1/records/{id}/video-url` | ✅ | Ссылка на видео |
| PUT | `/api/v1/records/{id}/video-url` | ✅ | Обновить ссылку |
| GET | `/api/v1/records/{id}/artifacts` | ✅ | Список артефактов |
| GET | `/api/v1/records/{id}/artifacts/{filename}` | ✅ | Скачать |
| POST | `/api/v1/records/{id}/artifacts` | ✅ | Загрузить |
| DELETE | `/api/v1/records/{id}/artifacts/{filename}` | ✅ | Удалить |
| GET | `/api/v1/records/{id}/transcript` | ✅ | Стенограмма |
| GET | `/api/v1/records/{id}/summary` | ✅ | Summary |
| GET | `/api/v1/records/_/search` | ✅ | Поиск |
| GET | `/api/v1/sync/changes` | ✅ | Дельта изменений |
| GET | `/api/v1/sync/snapshot` | ✅ | Полный снапшот |

---

## Приложение B. Чек-лист интеграции

- [ ] Получить `SCREC_API_KEY` от администратора сервера.
- [ ] Проверить `GET /health` — сервис жив.
- [ ] Сделать пробный `GET /api/v1/tree` с ключом — 200.
- [ ] Реализовать `POST /api/v1/records` — публикация одной записи.
- [ ] Проверить скачивание артефакта через `GET /api/v1/records/{id}/artifacts/{filename}`.
- [ ] Реализовать первичную синхронизацию через `GET /api/v1/sync/snapshot`.
- [ ] Сохранять `server_revision` в локальный файл.
- [ ] Реализовать периодический `GET /api/v1/sync/changes`.
- [ ] Обрабатывать `action` ∈ `{create, update, delete}`.
- [ ] Обрабатывать `video_url` — не пытаться скачать видео, использовать ссылку.
- [ ] Логировать HTTP-ошибки (особенно 401, 413, 500).
- [ ] Настроить таймауты: connect 15 с, read ≥ 60 с (для загрузки больших файлов).
- [ ] Обработать `has_more == true` в `/sync/changes`.

---

**Конец документа.**

Если нужна документация в формате OpenAPI 3.1 (машинночитаемая) — она автоматически отдаётся сервисом по адресу `/openapi.json`. Можно скачать и импортировать в Postman/Insomnia/Swagger Editor.