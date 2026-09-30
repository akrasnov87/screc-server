# Техническая документация API сервиса `screc-server`

**Версия:** 1.1.0
**Назначение:** синхронизация записей (протоколов, стенограмм, summary) между клиентами одного пользователя через HTTP API. Хранение — файловая система, без БД.

---

## Содержание

1. [Обзор](#1-обзор)
2. [Быстрый старт](#2-быстрый-старт)
3. [Аутентификация](#3-аутентификация)
4. [Модель данных](#4-модель-данных)
5. [Структура хранилища](#5-структура-хранилища)
6. [Справочник эндпоинтов](#6-справочник-эндпоинтов)
7. [Работа с хэш-суммами](#7-работа-с-хэш-суммами)
8. [Сценарии интеграции](#8-сценарии-интеграции)
9. [Ошибки](#9-ошибки)
10. [Ограничения и лимиты](#10-ограничения-и-лимиты)
11. [Пример клиента на Python](#11-пример-клиента-на-python)
12. [Эксплуатация](#12-эксплуатация)
13. [Версионирование и совместимость](#13-версионирование-и-совместимость)

---

## 1. Обзор

### 1.1. Что делает сервис

- Принимает «лёгкие» артефакты записей (стенограммы, протоколы, summary, вложения) от клиентов.
- Хранит их в виде дерева `records/<project>/<year>/<month>/<folder_name>/`.
- Отдаёт ссылки на большие файлы (видео, аудио) — сами файлы не загружаются.
- Поддерживает delta-синхронизацию через монотонный `revision` и `changelog.jsonl`.
- Даёт полнотекстовый поиск (инвертированный индекс в JSON).
- **Проверяет SHA-256 артефактов** — можно не перезаписывать файл, если он уже сохранён (см. §7).
- **Поддерживает условную загрузку** — клиент может спросить, нужно ли вообще отправлять файл.

### 1.2. Чего сервис **не** делает

- Не хранит видео и аудио.
- Не запускает транскрибацию/суммаризацию.
- Не управляет правами — один API-ключ на весь сервис.
- Не различает пользователей — вся модель под одного владельца.
- Не делает глобальную дедупликацию по контенту (одинаковый файл под разными именами будет сохранён дважды).

### 1.3. Технологии

| Слой | Технология |
|---|---|
| Web-фреймворк | FastAPI 0.115 |
| Сервер | Uvicorn 0.32 |
| Валидация | Pydantic 2.9 |
| Конфигурация | pydantic-settings |
| Хранилище | файловая система |
| Логи | logging + RotatingFileHandler |
| Хэши | SHA-256 |

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
  "time": "2026-09-30T12:05:00Z"
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
| `sha256` | string | SHA-256 содержимого (считается сервером) |
| `deleted_at` | string \| null | Если задано — артефакт помечен soft-deleted |

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
| `entity` | string | `"record"` или `"artifact"` |
| `action` | string | см. таблицу ниже |
| `id` | string | ID записи |
| `path` | string | Относительный путь |
| `payload` | object \| null | Метаданные изменения |
| `old_path` | string \| null | Прежний путь (для `move`) |

**Значения `action`:**

| `entity` | `action` | Что означает |
|---|---|---|
| `record` | `create` | Создана новая запись |
| `record` | `update` | Обновлены поля записи |
| `record` | `delete` | Удалена (soft или hard) |
| `record` | `update_links` | Обновлена ссылка на видео/аудио |
| `artifact` | `artifact_upload` | Загружен новый/обновлённый артефакт |
| `artifact` | `artifact_delete` | Удалён артефакт (физически) |
| `artifact` | `artifact_soft_delete` | Помечен как удалённый |
| `artifact` | `artifact_delete_all` | Удалены все артефакты записи |

### 4.6. `ArtifactCheckItem` / `ArtifactCheckResult`

Схемы для эндпоинта `POST /records/{id}/artifacts/check`.

**`ArtifactCheckItem`:**

| Поле | Тип | Обяз. |
|---|---|---|
| `filename` | string | ✅ |
| `sha256` | string \| null | ❌ |

**`ArtifactCheckResult`:**

| Поле | Тип | Описание |
|---|---|---|
| `filename` | string | Имя файла |
| `sha256` | string | Хэш существующего файла (`""`, если файла нет) |
| `skip` | bool | `true` — файл уже есть с таким хэшем, загрузка не нужна |
| `reason` | string | См. §7 |
| `size` | int | Размер существующего файла |
| `kind` | string | Тип существующего артефакта (`""`, если нет) |

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
      "sha256": "abc123...",
      "deleted_at": null
    }
  ],
  "revision": 101,
  "deleted_at": null
}
```

**Про `artifacts[]`:**

- `sha256` — сервер считает сам, всегда.
- `deleted_at` — `null` для живых артефактов; ISO-таймстемп, если soft-deleted.

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
{"rev":1,"ts":"...","entity":"record","action":"create","id":"770e...","path":"vNext/2026/09/...","payload":{"full":true,"project":"vNext","date":"2026-09-08","skipped_artifacts":[],"uploaded_artifacts":["transcript.md"]}}
{"rev":2,"ts":"...","entity":"record","action":"update","id":"770e...","path":"vNext/2026/09/...","payload":{"changed":["summary_bb","tags"]}}
{"rev":3,"ts":"...","entity":"artifact","action":"artifact_upload","id":"770e...","path":"vNext/2026/09/...","payload":{"filename":"transcript.md","kind":"transcript","size":12345,"sha256":"abc...","reason":"artifact_absent"}}
{"rev":4,"ts":"...","entity":"artifact","action":"artifact_delete","id":"770e...","path":"vNext/2026/09/...","payload":{"filename":"transcript.md","hard":true}}
{"rev":5,"ts":"...","entity":"record","action":"delete","id":"770e...","path":"vNext/2026/09/..."}
```

---

## 6. Справочник эндпоинтов

Базовый префикс: `/api/v1`. Все эндпоинты ниже требуют заголовок `X-API-Key`, если не указано иное.

### 6.1. Health

#### `GET /health` — без авторизации

**Ответ 200:**

```json
{
  "status": "ok",
  "revision": 101,
  "records_count": 42,
  "data_root": "/data",
  "fts_enabled": true,
  "time": "2026-09-30T12:05:00+00:00"
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

Записи с `_deleted.json` в этот список не попадают.

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
| `sha256` | string[] | ❌ | Параллельный массив SHA-256 хэшей для `files`. |

**Правила:**
- Длина `kinds` и `sha256`, если заданы, должна совпадать с длиной `files`.
- Если `kinds` не задан — все файлы получат `kind="attachment"`.
- Если `sha256[i]` пустой — для этого файла проверка не применяется, файл перезаписывается принудительно.
- Если `sha256[i]` совпадает с уже сохранённым артефактом с таким же именем — файл **не перезаписывается** (см. §7).
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
    {"kind": "transcript", "filename": "transcript_запись.md", "size": 45678, "sha256": "abc123..."}
  ],
  "skipped_artifacts": [],
  "uploaded_artifacts": [
    {"kind": "transcript", "filename": "transcript_запись.md", "size": 45678, "sha256": "abc123..."}
  ]
}
```

- `action` = `"create"` при первой публикации, `"update"` при повторной (по тому же пути).
- `skipped_artifacts` — артефакты, которые **не перезаписывались** (хэш совпал).
- `uploaded_artifacts` — артефакты, которые были **сохранены** в этом запросе.

**Ошибки:**
- `400` — некорректный payload, `kinds` или `sha256` не совпадает с `files`.
- `413` — payload или файл превышает лимит.

**Идемпотентность:** повторная публикация по тому же `(project, year, month, folder_name)` обновляет запись, а не создаёт дубликат. Если все файлы пропущены по хэшу — `action` = `"update"`, `uploaded_artifacts` = `[]`.

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

#### `DELETE /records/{record_id}` — удаление записи

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

**Идемпотентность:** повторный soft-delete возвращает `reason: "already_deleted"` и не создаёт новую ревизию.

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
      "sha256": "abc123...",
      "deleted_at": null
    }
  ]
}
```

Soft-deleted артефакты тоже присутствуют, но с полем `deleted_at`.

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

#### `HEAD /records/{record_id}/artifacts/{filename}` — проверить перед загрузкой

**Назначение:** клиент спрашивает, нужно ли загружать файл, **не отправляя содержимое**.

**Заголовки запроса:**

| Заголовок | Обяз. | Описание |
|---|---|---|
| `X-Content-SHA256` | ❌ | SHA-256 клиента (hex, нижний регистр). |

**Заголовки ответа:**

| Заголовок | Описание |
|---|---|
| `X-Artifact-Skip` | `"true"` — файл уже есть с таким хэшем, можно не грузить. `"false"` — грузить. |
| `X-Artifact-SHA256` | Хэш существующего файла или `""`. |
| `X-Artifact-Size` | Размер существующего файла или `0`. |
| `X-Artifact-Kind` | Тип существующего файла или `""`. |

**Коды:**

| HTTP | Когда | Что делать клиенту |
|---|---|---|
| `200 OK` + `X-Artifact-Skip: true` | Файл есть, хэш совпал | Не грузить |
| `200 OK` + `X-Artifact-Skip: false` | Файл есть, хэш не совпал (или не передан) | Грузить (перезапишет) |
| `404 Not Found` | Файла нет | Грузить |
| `404 Not Found` (запись не найдена) | Записи с таким id нет | Сначала создать запись |

**Пример:**

```bash
curl -I -X HEAD \
  "http://server/api/v1/records/770e.../artifacts/transcript.md" \
  -H "X-API-Key: $KEY" \
  -H "X-Content-SHA256: abc123..."
```

Ответ:

```
HTTP/1.1 200 OK
X-Artifact-Skip: true
X-Artifact-SHA256: abc123...
X-Artifact-Size: 22
X-Artifact-Kind: transcript
```

---

#### `POST /records/{record_id}/artifacts/check` — пакетная проверка

**Назначение:** за один запрос узнать, какие из N файлов нужно загрузить.

**Content-Type:** `application/json`

**Тело:**

```json
{
  "artifacts": [
    {"filename": "transcript.md", "sha256": "abc123..."},
    {"filename": "protocol.docx", "sha256": "def456..."},
    {"filename": "attachment.pdf", "sha256": "789abc..."}
  ]
}
```

**Ответ 200:**

```json
{
  "id": "770e8400-...",
  "results": [
    {"filename": "transcript.md",  "sha256": "abc123...", "skip": true,  "reason": "sha256_match",     "size": 22,    "kind": "transcript"},
    {"filename": "protocol.docx",  "sha256": "def456...", "skip": true,  "reason": "sha256_match",     "size": 4567,  "kind": "protocol"},
    {"filename": "attachment.pdf", "sha256": "789abc...", "skip": false, "reason": "artifact_absent",  "size": 0,     "kind": ""}
  ]
}
```

Клиент отправляет только те файлы, где `skip: false`.

**Ошибки:** `404` — запись не найдена.

---

#### `POST /records/{record_id}/artifacts` — загрузить артефакт

**Content-Type:** `multipart/form-data`

| Поле | Тип | Обяз. | Описание |
|---|---|---|---|
| `file` | file | ✅ | Файл |
| `kind` | string | ❌ | По умолчанию `"attachment"` |
| `sha256` | string | ❌ | SHA-256 хэш файла (hex, нижний регистр) |

**Ответ 200, если файл записан:**

```json
{
  "id": "770e8400-...",
  "filename": "новая_схема.docx",
  "size": 12345,
  "sha256": "abc123...",
  "skipped": false,
  "reason": "artifact_absent"
}
```

**Ответ 200, если файл пропущен (хэш совпал):**

```json
{
  "id": "770e8400-...",
  "filename": "новая_схема.docx",
  "size": 12345,
  "sha256": "abc123...",
  "skipped": true,
  "reason": "sha256_match"
}
```

---

#### `DELETE /records/{record_id}/artifacts/{filename}` — удалить артефакт

**Query-параметры:**

| Имя | Тип | По умолчанию | Описание |
|---|---|---|---|
| `hard` | bool | `true` | `true` — удалить файл с диска. `false` — soft-delete (файл остаётся, но помечается). |

**Ответ 200 (успешно удалён):**

```json
{
  "id": "770e8400-...",
  "filename": "transcript.md",
  "deleted": true,
  "hard": true,
  "revision": 105,
  "reason": "deleted"
}
```

**Ответ 200 (файла не было — идемпотентно):**

```json
{
  "id": "770e8400-...",
  "filename": "transcript.md",
  "deleted": false,
  "hard": true,
  "revision": 105,
  "reason": "already_absent"
}
```

**Возможные `reason`:**

| Значение | Описание |
|---|---|
| `deleted` | Удалено |
| `already_absent` | Файла не было |
| `already_deleted` | Уже был soft-deleted |

**Важно:** удаление пишется в `changelog` и инкрементирует `revision`. Клиент B, синхронизирующийся через `/sync/changes`, увидит событие `entity: "artifact"` и сможет удалить файл у себя.

---

#### `DELETE /records/{record_id}/artifacts` — удалить все артефакты записи

**Query-параметры:**

| Имя | Тип | По умолчанию | Описание |
|---|---|---|---|
| `hard` | bool | `true` | `true` — удалить физически. `false` — soft-delete. |
| `keep_kinds` | string[] | `[]` | Список kind, которые **не** удалять (например, `transcript`, `summary`). |

**Примеры:**

```bash
# Удалить всё
DELETE /records/{id}/artifacts

# Только пометить
DELETE /records/{id}/artifacts?hard=false

# Удалить всё, кроме стенограммы и summary
DELETE /records/{id}/artifacts?keep_kinds=transcript&keep_kinds=summary
```

**Ответ 200:**

```json
{
  "id": "770e8400-...",
  "deleted_count": 4,
  "deleted_filenames": [
    "protocol.docx",
    "deepseek_prompt.md",
    "attachment.pdf",
    "manual_protocol.md"
  ],
  "hard": true,
  "revision": 107
}
```

Если нечего удалять:

```json
{
  "id": "770e8400-...",
  "deleted_count": 0,
  "deleted_filenames": [],
  "hard": true,
  "revision": 107
}
```

**Важно:** пишется в `changelog` как `entity: "artifact"`, `action: "artifact_delete_all"`.

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
  "server_revision": 107,
  "has_more": false,
  "changes": [
    {
      "rev": 101,
      "ts": "2026-09-30T12:05:00+00:00",
      "entity": "record",
      "action": "create",
      "id": "770e8400-...",
      "path": "vNext/2026/09/2026-09-08 формирование...",
      "payload": {
        "full": true,
        "project": "vNext",
        "date": "2026-09-08",
        "skipped_artifacts": [],
        "uploaded_artifacts": ["transcript.md"]
      }
    },
    {
      "rev": 105,
      "ts": "2026-09-30T12:10:00+00:00",
      "entity": "artifact",
      "action": "artifact_delete",
      "id": "770e8400-...",
      "path": "vNext/2026/09/2026-09-08 формирование...",
      "payload": {"filename": "protocol.docx", "hard": true}
    }
  ]
}
```

**Алгоритм клиента:**
1. Хранить `last_synced_revision` локально.
2. Запрашивать `changes?since_revision=<last_synced_revision>`.
3. Применять изменения:
   - `entity: "record"` → применить запись целиком (скачать `GET /records/{id}`).
   - `entity: "artifact"` → перезагрузить запись и привести локальные файлы в соответствие (удалить то, чего нет на сервере; скачать недостающие).
4. Обновить `last_synced_revision = server_revision`.
5. Если `has_more == true` — повторить с новым `since_revision`.

---

#### `GET /sync/snapshot` — полный дамп

**Ответ 200:**

```json
{
  "server_revision": 107,
  "total": 42,
  "records": [
    { ...RecordFull... }
  ]
}
```

Используется при **первой** синхронизации (когда `last_synced_revision = 0` и `changes` не подходит).

---

## 7. Работа с хэш-суммами

### 7.1. Зачем

Позволяет:

- Не перезаписывать файл, если его содержимое уже сохранено.
- Не отправлять файлы, которые уже есть на сервере (через `HEAD` или `check`).
- Делать публикации идемпотентными при сетевых сбоях.

### 7.2. Правила проверки

| Ситуация | Что делает сервер |
|---|---|
| Хэш не передан | Перезаписывает файл принудительно |
| Хэш передан, файла нет | Загружает файл |
| Хэш передан, файл есть, хэши совпадают | **Не перезаписывает**, возвращает `skipped: true` |
| Хэш передан, файл есть, хэши не совпадают | Перезаписывает файл |

### 7.3. Формат хэша

SHA-256 в hex-нижнем регистре, 64 символа:

```
a3f1c0e2b4d8...9e7a
```

Регистр и пробелы нормализуются сервером. Пустая строка = «хэш не передан».

### 7.4. Значения `reason`

| Значение | Когда |
|---|---|
| `no_sha256_provided` | Клиент не передал хэш → файл записан принудительно |
| `record_new` | Запись создаётся впервые → файл записан |
| `artifact_absent` | Файла с таким именем не было → записан |
| `sha256_match` | Хэш совпал → файл **не перезаписан** |
| `sha256_mismatch` | Хэш передан, но не совпал → файл перезаписан |

### 7.5. Серверный хэш — источник истины

Даже если клиент передал «правильный» хэш, сервер **всегда** пересчитывает SHA-256 из фактического содержимого и сохраняет **свой** в `_meta.json`.

Клиентский хэш используется **только** для решения «пропустить/записать». Это защищает от подмены: если клиент пришлёт чужой хэш, сервер запишет правильный, и при следующей публикации хэши разойдутся — файл будет перезаписан.

### 7.6. Три способа работы

#### Способ 1: обычная публикация (загружаем всегда)

```bash
curl -X POST http://server/api/v1/records \
  -H "X-API-Key: $KEY" \
  -F 'payload={...}' \
  -F 'files=@transcript.md' \
  -F 'kinds=transcript'
```

Файл уходит по сети, сервер сам решает, писать или нет.

#### Способ 2: публикация с хэшами

```bash
SHA=$(sha256sum /tmp/transcript.md | awk '{print $1}')

curl -X POST http://server/api/v1/records \
  -H "X-API-Key: $KEY" \
  -F 'payload={...}' \
  -F 'files=@transcript.md' \
  -F 'kinds=transcript' \
  -F "sha256=$SHA"
```

Файл всё равно уходит, но если такой же уже есть на сервере — не будет перезаписан.

#### Способ 3: условная загрузка (не отправляем файл)

```bash
# 1. Спросить сервер
curl -I -X HEAD \
  "http://server/api/v1/records/$ID/artifacts/transcript.md" \
  -H "X-API-Key: $KEY" \
  -H "X-Content-SHA256: $SHA"

# Если X-Artifact-Skip: true → файл не отправляем
# Если 404 или X-Artifact-Skip: false → отправляем
```

Или в батче:

```bash
curl -X POST "http://server/api/v1/records/$ID/artifacts/check" \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{"artifacts": [{"filename": "transcript.md", "sha256": "'$SHA'"}]}'
```

**Экономия:** сам файл не уходит по сети.

### 7.7. Сравнение

| Способ | Трафик | Точность |
|---|---|---|
| Обычная публикация | Полный размер файла | Файл перезаписывается всегда |
| Публикация с хэшем | Полный размер файла | Файл не перезаписывается, если хэш совпал |
| Условная загрузка (`HEAD`/`check`) | ~200 байт на проверку | Файл вообще не отправляется, если хэш совпал |

---

## 8. Сценарии интеграции

### 8.1. Публикация одной записи (Python + requests)

```python
import hashlib
import json
import requests

BASE = "http://localhost:8000"
KEY = "<ваш API-ключ>"
HEADERS = {"X-API-Key": KEY}


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


payload = {
    "project": "vNext",
    "year": "2026",
    "month": "09",
    "folder_name": "2026-09-30 тест",
    "name": "тест",
    "date": "2026-09-30",
    "tags": ["тест"],
    "summary_bb": "**Краткое описание**",
}

files_data = [
    ("transcript", "/tmp/transcript.md"),
    ("protocol", "/tmp/protocol.docx"),
]

files = []
kinds = []
hashes = []
for kind, path in files_data:
    fname = path.split("/")[-1]
    files.append(("files", (fname, open(path, "rb"))))
    kinds.append(("kinds", (None, kind)))
    hashes.append(("sha256", (None, sha256_of(path))))

r = requests.post(
    f"{BASE}/api/v1/records",
    headers=HEADERS,
    data=[
        ("payload", (None, json.dumps(payload, ensure_ascii=False))),
        *kinds,
        *hashes,
    ],
    files=files,
    timeout=60,
)
r.raise_for_status()
result = r.json()
print("Создано:", result["action"])
print("Загружено:", [a["filename"] for a in result["uploaded_artifacts"]])
print("Пропущено:", [a["filename"] for a in result["skipped_artifacts"]])
```

**Важно:** `kinds` и `sha256` — списки, но каждая пара `(None, value)` идёт как отдельный элемент multipart. В `requests` порядок сохраняется.

### 8.2. Условная загрузка (не отправляем, если есть)

```python
def upload_if_needed(base, key, record_id, kind, path):
    fname = path.split("/")[-1]
    sha = sha256_of(path)

    # 1. Спросить сервер
    r = requests.head(
        f"{base}/api/v1/records/{record_id}/artifacts/{fname}",
        headers={"X-API-Key": key, "X-Content-SHA256": sha},
        timeout=15,
    )

    if r.status_code == 200 and r.headers.get("X-Artifact-Skip") == "true":
        return {"skipped": True, "reason": "sha256_match"}

    # 2. Отправить файл
    with open(path, "rb") as f:
        r = requests.post(
            f"{base}/api/v1/records/{record_id}/artifacts",
            headers={"X-API-Key": key},
            files={"file": (fname, f)},
            data={"kind": kind, "sha256": sha},
            timeout=300,
        )
    r.raise_for_status()
    return r.json()
```

### 8.3. Пакетная условная загрузка

```python
def upload_batch(base, key, record_id, artifacts):
    """
    artifacts: [(kind, path), ...]
    """
    items = []
    for kind, path in artifacts:
        items.append({
            "kind": kind,
            "path": path,
            "filename": path.split("/")[-1],
            "sha256": sha256_of(path),
        })

    # Один запрос на проверку
    r = requests.post(
        f"{base}/api/v1/records/{record_id}/artifacts/check",
        headers={"X-API-Key": key},
        json={"artifacts": [
            {"filename": it["filename"], "sha256": it["sha256"]}
            for it in items
        ]},
        timeout=15,
    )
    r.raise_for_status()
    check = {x["filename"]: x for x in r.json()["results"]}

    results = {}
    for it in items:
        info = check[it["filename"]]
        if info["skip"]:
            results[it["filename"]] = {"skipped": True, "reason": info["reason"]}
            continue

        with open(it["path"], "rb") as f:
            r = requests.post(
                f"{base}/api/v1/records/{record_id}/artifacts",
                headers={"X-API-Key": key},
                files={"file": (it["filename"], f)},
                data={"kind": it["kind"], "sha256": it["sha256"]},
                timeout=300,
            )
        r.raise_for_status()
        results[it["filename"]] = r.json()

    return results
```

### 8.4. Первая синхронизация на пустом клиенте

```python
state = {"last_synced_revision": 0}

snap = requests.get(
    f"{BASE}/api/v1/sync/snapshot", headers=HEADERS
).json()

for rec in snap["records"]:
    # 1. Создать папку rec["path"] в локальном хранилище
    # 2. Скачать все артефакты
    for art in rec["artifacts"]:
        if art.get("deleted_at"):
            continue
        url = f"{BASE}/api/v1/records/{rec['id']}/artifacts/{quote(art['filename'])}"
        content = requests.get(url, headers=HEADERS).content
        # сохранить по локальному пути
    # 3. Сохранить _meta.json и _links.json локально

state["last_synced_revision"] = snap["server_revision"]
```

### 8.5. Инкрементальная синхронизация (с учётом удалений артефактов)

```python
while True:
    r = requests.get(
        f"{BASE}/api/v1/sync/changes",
        headers=HEADERS,
        params={"since_revision": state["last_synced_revision"], "limit": 200},
    ).json()

    for ch in r["changes"]:
        if ch["entity"] == "record":
            if ch["action"] == "delete":
                # удалить локальную папку
                pass
            else:
                # скачать запись через GET /records/{id}
                rec = requests.get(
                    f"{BASE}/api/v1/records/{ch['id']}", headers=HEADERS
                ).json()
                apply_record_locally(rec)
        elif ch["entity"] == "artifact":
            # перезагрузить запись и привести локальные файлы
            # в соответствие: удалить то, чего нет на сервере,
            # скачать недостающие
            rec = requests.get(
                f"{BASE}/api/v1/records/{ch['id']}", headers=HEADERS
            ).json()
            reconcile_artifacts_locally(rec)

    state["last_synced_revision"] = r["server_revision"]
    if not r["has_more"]:
        break

# сохранить state["last_synced_revision"] на диск
```

### 8.6. Обновление summary без перезагрузки файлов

```python
requests.patch(
    f"{BASE}/api/v1/records/{record_id}",
    headers={**HEADERS, "Content-Type": "application/json"},
    json={"summary_bb": "Новое описание", "tags": ["важное"]},
)
```

Файлы-артефакты не трогаются. `revision` инкрементируется, изменение попадает в `changelog`.

### 8.7. Удалить один артефакт

```python
requests.delete(
    f"{BASE}/api/v1/records/{record_id}/artifacts/protocol.docx",
    headers=HEADERS,
)
```

### 8.8. Удалить все артефакты, кроме стенограммы

```python
requests.delete(
    f"{BASE}/api/v1/records/{record_id}/artifacts",
    headers=HEADERS,
    params={"keep_kinds": ["transcript"], "hard": True},
)
```

---

## 9. Ошибки

### 9.1. Формат тела ошибки

```json
{
  "detail": "Human-readable описание"
}
```

### 9.2. Коды

| HTTP | Значение | Когда |
|---|---|---|
| 200 | OK | Успех |
| 400 | Bad Request | Некорректный payload, `kinds`/`sha256` не совпадает, path traversal |
| 401 | Unauthorized | Нет или неверный `X-API-Key` |
| 404 | Not Found | Запись/проект/файл не найдены |
| 413 | Payload Too Large | Файл или payload превышает лимит |
| 422 | Unprocessable Entity | Ошибки валидации Pydantic |
| 500 | Internal Server Error | Ошибка сервера (смотрите `logs/screc.error.log`) |

### 9.3. Примеры

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

**Несовпадение длин `sha256` и `files`:**

```json
{"detail": "sha256 length (1) != files length (2)"}
```

---

## 10. Ограничения и лимиты

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
| Макс. артефактов на запись | — | 500 (в `check`) |
| Индексируемый размер артефакта | — | 2 МБ |
| Макс. результатов поиска | — | 500 |

### 10.1. Санитизация имён

Все сегменты пути (`project`, `year`, `month`, `folder_name`) проходят через `_safe_segment`:
- NFC-нормализация Unicode;
- запрещённые символы `<>:"/\|?*` и управляющие → `_`;
- схлопывание пробелов;
- удаление ведущих/замыкающих точек и пробелов;
- обрезка до 200 байт UTF-8 (без разрыва символов).

Имена артефактов — только базовое имя (`Path(filename).name`). Путь проверяется через `assert_inside`.

### 10.2. Формат хэша

- SHA-256, hex-нижний регистр.
- 64 символа.
- Регистр и пробелы нормализуются.
- Пустая строка = «не передан».

---

## 11. Пример клиента на Python

```python
"""Клиент для screc-server с поддержкой проверки хэшей."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote

import httpx


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


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
        artifacts: Optional[Iterable[tuple[str, Path]]] = None,
        *,
        send_sha256: bool = True,
    ) -> dict:
        """
        Публикует запись.

        artifacts: [(kind, path), ...]
        send_sha256: если True — сервер использует хэши для пропуска.
        """
        files = []
        kinds = []
        hashes = []

        for kind, path in artifacts or []:
            files.append(
                ("files", (path.name, path.read_bytes()))
            )
            kinds.append(("kinds", (None, kind)))
            if send_sha256:
                hashes.append(
                    ("sha256", (None, sha256_of(path)))
                )

        r = self.client.post(
            "/api/v1/records",
            data=[
                ("payload", (None, json.dumps(payload, ensure_ascii=False))),
                *kinds,
                *hashes,
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
        self,
        record_id: str,
        kind: str,
        path: Path,
        *,
        send_sha256: bool = True,
    ) -> dict:
        data = {"kind": kind}
        if send_sha256:
            data["sha256"] = sha256_of(path)

        r = self.client.post(
            f"/api/v1/records/{record_id}/artifacts",
            data=data,
            files={"file": (path.name, path.read_bytes())},
        )
        r.raise_for_status()
        return r.json()

    def check_artifact(
        self,
        record_id: str,
        filename: str,
        sha256: str,
    ) -> dict:
        """
        Спрашивает сервер, нужно ли загружать файл.

        Возвращает {"skip": bool, "reason": str, ...}.
        """
        r = self.client.head(
            f"/api/v1/records/{record_id}/artifacts/{quote(filename)}",
            headers={"X-Content-SHA256": sha256},
        )
        return {
            "skip": r.headers.get("X-Artifact-Skip") == "true",
            "exists": r.status_code == 200,
            "sha256": r.headers.get("X-Artifact-SHA256", ""),
            "size": int(r.headers.get("X-Artifact-Size", 0)),
            "kind": r.headers.get("X-Artifact-Kind", ""),
            "status_code": r.status_code,
        }

    def check_artifacts_batch(
        self,
        record_id: str,
        items: list[dict],
    ) -> dict:
        """
        items: [{"filename": str, "sha256": str}, ...]
        """
        r = self.client.post(
            f"/api/v1/records/{record_id}/artifacts/check",
            json={"artifacts": items},
        )
        r.raise_for_status()
        return r.json()

    def upload_if_needed(
        self,
        record_id: str,
        kind: str,
        path: Path,
    ) -> dict:
        """
        Загружает файл, если его ещё нет на сервере.

        Сначала HEAD, потом при необходимости POST.
        """
        sha = sha256_of(path)
        info = self.check_artifact(record_id, path.name, sha)

        if info["skip"]:
            return {
                "skipped": True,
                "reason": "sha256_match",
                "size": info["size"],
                "sha256": info["sha256"],
            }

        return self.upload_artifact(
            record_id, kind, path, send_sha256=True,
        )

    def delete_artifact(
        self,
        record_id: str,
        filename: str,
        hard: bool = True,
    ) -> dict:
        r = self.client.delete(
            f"/api/v1/records/{record_id}/artifacts/{quote(filename)}",
            params={"hard": hard},
        )
        r.raise_for_status()
        return r.json()

    def delete_all_artifacts(
        self,
        record_id: str,
        hard: bool = True,
        keep_kinds: Optional[list[str]] = None,
    ) -> dict:
        params: list[tuple[str, str]] = [("hard", str(hard).lower())]
        for k in (keep_kinds or []):
            params.append(("keep_kinds", k))
        r = self.client.delete(
            f"/api/v1/records/{record_id}/artifacts",
            params=params,
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

    # Публикация с хэшами (файлы всё равно отправляются)
    result = c.publish(
        payload={
            "project": "vNext",
            "year": "2026",
            "month": "09",
            "folder_name": "2026-09-30 тест",
            "name": "тест",
            "date": "2026-09-30",
        },
        artifacts=[
            ("transcript", Path("/tmp/transcript.md")),
        ],
        send_sha256=True,
    )
    print("Пропущено:", [a["filename"] for a in result["skipped_artifacts"]])
    print("Загружено:", [a["filename"] for a in result["uploaded_artifacts"]])

    # Условная загрузка (файл не отправляется, если есть)
    info = c.upload_if_needed(
        record_id=result["id"],
        kind="attachment",
        path=Path("/tmp/attachment.pdf"),
    )
    print(info)
```

---

## 12. Эксплуатация

### 12.1. Переменные окружения

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

### 12.2. Логи

| Файл | Содержимое |
|---|---|
| `/logs/screc.log` | Все события уровня DEBUG и выше |
| `/logs/screc.error.log` | Только WARNING и выше |
| `/logs/access/access.log` | HTTP-запросы |

Ротация: 10 МБ × 5 файлов (для access — ×10).

**Формат основной строки:**

```
2026-09-30 12:05:00.756 [INFO    ] screc.app.main: сообщение
```

**Формат access:**

```
2026-09-30 12:05:00.756 [ACCESS] 172.18.0.1 POST /api/v1/records → 200 (45.2 ms) [a1b2c3d4]
```

**Записи про хэши:**

```
screc.app.records_service: Артефакт пропущен (sha256 совпал): record=..., kind=transcript, name=transcript.md, sha=abc123...
screc.app.records_service: Артефакт сохранён: record=..., kind=transcript, name=transcript.md, size=12345, reason=artifact_absent, sha=def456...
screc.app.api.artifacts: HEAD артефакта: record=..., name=transcript.md, exists=True, skip=True, reason=sha256_match
```

### 12.3. Проверка состояния

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

# Артефакты с их хэшами
jq '.artifacts[] | {filename, sha256, size}' \
  "data/records/vNext/2026/09/2026-09-30 тест/_meta.json"
```

### 12.4. Бэкап

```bash
tar czf screc-backup-$(date +%F).tar.gz data/
```

При остановленном контейнере — достаточно скопировать `data/`.

### 12.5. Восстановление

```bash
tar xzf screc-backup-YYYY-MM-DD.tar.gz
docker compose restart
```

Если FTS-индекс рассинхронизировался (редкий случай) — удалите `data/fts.json` и перезапустите сервис. Индекс пересоберётся при следующей публикации/обновлении.

---

## 13. Версионирование и совместимость

### 13.1. Текущая версия

`1.1.0` — добавлена проверка хэшей и условная загрузка.
`1.0.0` — базовая версия API.

### 13.2. Правила совместимости

- **Мажорная версия** — несовместимые изменения (переименование полей, изменение семантики action, смена схемы хранения).
- **Минорная** — новые поля (клиенты их игнорируют), новые эндпоинты.
- **Патч** — исправления.

### 13.3. Что нового в 1.1.0

| Изменение | Совместимость |
|---|---|
| Параметр `sha256` в `POST /records` | Обратно совместимо (опциональный) |
| Параметр `sha256` в `POST /records/{id}/artifacts` | Обратно совместимо (опциональный) |
| Поля `skipped_artifacts` / `uploaded_artifacts` в ответе | Обратно совместимо (клиенты игнорируют) |
| `HEAD /records/{id}/artifacts/{filename}` | Новый эндпоинт |
| `POST /records/{id}/artifacts/check` | Новый эндпоинт |
| `DELETE /records/{id}/artifacts` | Новый эндпоинт |
| `?hard=` в `DELETE /records/{id}/artifacts/{filename}` | Обратно совместимо (по умолчанию `true`) |
| `entity: "artifact"` в `changelog` | Обратно совместимо (старые клиенты игнорируют) |
| Поле `deleted_at` в `artifacts[]` | Обратно совместимо (по умолчанию `null`) |
| Удаление артефакта теперь пишется в changelog | Обратно совместимо, но улучшает multi-client |

### 13.4. Что делать клиенту при обновлении сервера

- Читать `record.revision` — он всегда растёт.
- Не полагаться на порядок полей в JSON.
- Игнорировать незнакомые поля (FastAPI/Pydantic это позволяет).
- Использовать `since_revision` для дельты, а `snapshot` — только при `last_synced_revision == 0`.
- Обрабатывать `entity: "artifact"` в `changelog` (если пользуетесь синхронизацией).

### 13.5. Обратная совместимость хранилища

- Новые поля в `_meta.json` добавляются, старые не удаляются.
- Поле `deleted_at` в `artifacts[]` — новое; старые записи читаются как `null`.
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
| DELETE | `/api/v1/records/{id}` | ✅ | Удалить запись |
| GET | `/api/v1/records/{id}/video-url` | ✅ | Ссылка на видео |
| PUT | `/api/v1/records/{id}/video-url` | ✅ | Обновить ссылку |
| GET | `/api/v1/records/{id}/artifacts` | ✅ | Список артефактов |
| GET | `/api/v1/records/{id}/artifacts/{filename}` | ✅ | Скачать |
| HEAD | `/api/v1/records/{id}/artifacts/{filename}` | ✅ | Проверить перед загрузкой |
| POST | `/api/v1/records/{id}/artifacts/check` | ✅ | Пакетная проверка |
| POST | `/api/v1/records/{id}/artifacts` | ✅ | Загрузить |
| DELETE | `/api/v1/records/{id}/artifacts/{filename}` | ✅ | Удалить артефакт |
| DELETE | `/api/v1/records/{id}/artifacts` | ✅ | Удалить все артефакты |
| GET | `/api/v1/records/{id}/transcript` | ✅ | Стенограмма |
| GET | `/api/v1/records/{id}/summary` | ✅ | Summary |
| GET | `/api/v1/records/_/search` | ✅ | Поиск |
| GET | `/api/v1/sync/changes` | ✅ | Дельта изменений |
| GET | `/api/v1/sync/snapshot` | ✅ | Полный снапшот |

---

## Приложение B. Чек-лист интеграции

### Базовый

- [ ] Получить `SCREC_API_KEY` от администратора сервера.
- [ ] Проверить `GET /health` — сервис жив.
- [ ] Сделать пробный `GET /api/v1/tree` с ключом — 200.
- [ ] Реализовать `POST /api/v1/records` — публикация одной записи.
- [ ] Проверить скачивание артефакта через `GET /api/v1/records/{id}/artifacts/{filename}`.
- [ ] Реализовать первичную синхронизацию через `GET /api/v1/sync/snapshot`.
- [ ] Сохранять `server_revision` в локальный файл.
- [ ] Реализовать периодический `GET /api/v1/sync/changes`.
- [ ] Обрабатывать `action` ∈ `{create, update, delete, update_links}`.
- [ ] Обрабатывать `video_url` — не пытаться скачать видео, использовать ссылку.
- [ ] Логировать HTTP-ошибки (особенно 401, 413, 500).
- [ ] Настроить таймауты: connect 15 с, read ≥ 60 с (для загрузки больших файлов).
- [ ] Обработать `has_more == true` в `/sync/changes`.

### С хэшами

- [ ] Реализовать подсчёт SHA-256 для файлов на клиенте.
- [ ] Передавать `sha256` в `POST /records` (для идемпотентности).
- [ ] Передавать `sha256` в `POST /records/{id}/artifacts` (для идемпотентности).
- [ ] Использовать `HEAD /records/{id}/artifacts/{filename}` для условной загрузки.
- [ ] Или использовать `POST /records/{id}/artifacts/check` для пакетной проверки.
- [ ] Обрабатывать поле `skipped` в ответе.
- [ ] Обрабатывать поле `reason` (см. §7.4).

### С удалением и синхронизацией

- [ ] Обрабатывать `entity: "artifact"` в `/sync/changes`.
- [ ] При `artifact_delete`/`artifact_soft_delete`/`artifact_delete_all` перезагружать запись и синхронизировать локальные файлы.
- [ ] Использовать `DELETE /records/{id}/artifacts/{filename}` для удаления одного.
- [ ] Использовать `DELETE /records/{id}/artifacts` для массового удаления.
- [ ] Использовать `?keep_kinds=transcript` для сохранения стенограммы.