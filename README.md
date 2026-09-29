# screc-server

Сервис синхронизации записей Screen Recorder & Transcriber.

Принимает «лёгкие» артефакты записей (стенограммы, протоколы,
summary, вложения), хранит их в виде дерева
`records/<project>/<year>/<month>/<folder_name>/`, отдаёт ссылки
на большие файлы (видео, аудио) и поддерживает delta-синхронизацию
между клиентами.

**Особенности:**

- Хранение — только файловая система. Без БД.
- Один пользователь, один API-ключ.
- Полнотекстовый поиск (инвертированный индекс в JSON).
- Дельта-синхронизация через `revision` + `changelog.jsonl`.
- Логи с ротацией.
- Готов к запуску в Docker за минуту.

---

## Содержание

1. [Требования](#1-требования)
2. [Быстрый старт](#2-быстрый-старт)
3. [Конфигурация](#3-конфигурация)
4. [Сборка образа](#4-сборка-образа)
5. [Запуск](#5-запуск)
6. [Проверка работоспособности](#6-проверка-работоспособности)
7. [Локальная отладка без Docker](#7-локальная-отладка-без-docker)
8. [Структура хранилища](#8-структура-хранилища)
9. [Логи](#9-логи)
10. [Обновление](#10-обновление)
11. [Бэкап и восстановление](#11-бэкап-и-восстановление)
12. [Устранение неполадок](#12-устранение-неполадок)
13. [API](#13-api)
14. [Лицензия](#14-лицензия)

---

## 1. Требования

### Для запуска в Docker

| Компонент | Минимум | Рекомендуется |
|---|---|---|
| Docker Engine | 20.10+ | 24.0+ |
| Docker Compose | v2 (плагин `docker compose`) | v2.20+ |
| RAM | 256 МБ | 512 МБ |
| Диск | 1 ГБ + место под записи | — |

### Для запуска без Docker

| Компонент | Версия |
|---|---|
| Python | 3.11+ |
| pip | 22+ |
| ОС | Linux, macOS |

---

## 2. Быстрый старт

Три команды — и сервис работает.

```bash
# 1. Клонировать и перейти в каталог
git clone <repo-url> screc-server
cd screc-server

# 2. Создать .env и задать ключ
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
# скопировать вывод и вставить в .env → SCREC_API_KEY=<значение>

# 3. Собрать и запустить
docker compose up -d --build
```

Проверка:

```bash
curl -s http://localhost:8000/health | jq
```

Ожидаемый ответ:

```json
{
  "status": "ok",
  "service": "screc-sync",
  "time": "2026-09-29T12:05:00Z"
}
```

Готово. Swagger доступен на <http://localhost:8000/docs>.

---

## 3. Конфигурация

Всё через переменные окружения в файле `.env` (см. `.env.example`).

### 3.1. Обязательные

| Переменная | Описание | Пример |
|---|---|---|
| `SCREC_API_KEY` | Ключ авторизации. Передаётся в заголовке `X-API-Key`. | `Xq8vN2pL9mK4jH7gF5dS3aZ...` |

> ⚠️ Если оставить значение `change-me-please-to-a-long-random-string` из примера, сервис запустится, но ключ будет предсказуемым. Обязательно замените.

### 3.2. Хранилище

| Переменная | По умолчанию | Описание |
|---|---|---|
| `SCREC_DATA_ROOT` | `/data` | Корень данных. Внутри появится `records/`. |
| `SCREC_LOG_ROOT` | `/logs` | Корень логов. |

В `docker-compose.yml` эти пути монтируются из `./data` и `./logs` на хосте.

### 3.3. Сервер

| Переменная | По умолчанию | Описание |
|---|---|---|
| `SCREC_HOST` | `0.0.0.0` | Адрес bind. |
| `SCREC_PORT` | `8000` | Порт. |
| `SCREC_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |
| `SCREC_ACCESS_LOG` | `true` | Писать access-лог. |
| `SCREC_OPENAPI_ENABLED` | `true` | Отдавать `/docs`, `/redoc`, `/openapi.json`. |

### 3.4. Лимиты

| Переменная | По умолчанию | Описание |
|---|---|---|
| `SCREC_MAX_ARTIFACT_MB` | `50` | Макс. размер одного файла-артефакта. |
| `SCREC_MAX_PAYLOAD_KB` | `512` | Макс. размер JSON-payload. |
| `SCREC_SYNC_PAGE_SIZE` | `200` | Размер страницы `/sync/changes`. |

### 3.5. Поиск

| Переменная | По умолчанию | Описание |
|---|---|---|
| `SCREC_FTS_ENABLED` | `true` | Включать полнотекстовый индекс. |

### 3.6. Полный пример `.env`

```env
# ==== Авторизация ====
SCREC_API_KEY=Zq8vN2pL9mK4jH7gF5dS3aX6cB1nM0qW8eR2tY4uI9oP

# ==== Хранилище ====
SCREC_DATA_ROOT=/data
SCREC_LOG_ROOT=/logs

# ==== Сервер ====
SCREC_HOST=0.0.0.0
SCREC_PORT=8000
SCREC_LOG_LEVEL=INFO
SCREC_ACCESS_LOG=true
SCREC_OPENAPI_ENABLED=true

# ==== Лимиты ====
SCREC_MAX_ARTIFACT_MB=50
SCREC_MAX_PAYLOAD_KB=512
SCREC_SYNC_PAGE_SIZE=200

# ==== Поиск ====
SCREC_FTS_ENABLED=true
```

### 3.7. Смена ключа

```bash
# 1. Сгенерировать новый ключ
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# 2. Вписать в .env → SCREC_API_KEY

# 3. Пересоздать контейнер (без пересборки)
docker compose up -d --force-recreate

# 4. Проверить
docker compose exec -T screc-server printenv SCREC_API_KEY
```

---

## 4. Сборка образа

### 4.1. Через Docker Compose (рекомендуется)

```bash
docker compose build
```

С флагом `--no-cache`, если нужно гарантированно пересобрать:

```bash
docker compose build --no-cache
```

### 4.2. Вручную через `docker build`

```bash
docker build -t screc-server:latest .
```

### 4.3. Параметры сборки

Если `id -u` на хосте не 1000, нужно переопределить UID/GID внутри образа, чтобы bind-mount `./data` и `./logs` были доступны на запись:

```bash
docker build \
  --build-arg UID=$(id -u) \
  --build-arg GID=$(id -g) \
  -t screc-server:latest .
```

Или задать в `.env`:

```env
HOST_UID=1000
HOST_GID=1000
```

и в `docker-compose.yml`:

```yaml
build:
  context: .
  args:
    UID: "${HOST_UID:-1000}"
    GID: "${HOST_GID:-1000}"
```

---

## 5. Запуск

### 5.1. В фоне

```bash
docker compose up -d
```

### 5.2. С логами в терминал

```bash
docker compose up
```

### 5.3. Проверить статус

```bash
docker compose ps
```

Ожидаемо: `screc-server` в состоянии `Up (healthy)`.

### 5.4. Остановить

```bash
docker compose stop
```

### 5.5. Остановить и удалить контейнер

```bash
docker compose down
```

Данные в `./data` и `./logs` сохраняются.

### 5.6. Полная очистка (с данными)

```bash
docker compose down
rm -rf data/ logs/
```

> ⚠️ Удалит все записи и логи. Необратимо.

---

## 6. Проверка работоспособности

### 6.1. Health

```bash
curl -s http://localhost:8000/health | jq
```

### 6.2. Авторизация

```bash
API_KEY=$(docker compose exec -T screc-server printenv SCREC_API_KEY)

# без ключа → 401
curl -s -o /dev/null -w "%{http_code}\n" \
  http://localhost:8000/api/v1/tree
# 401

# с неправильным → 401
curl -s -o /dev/null -w "%{http_code}\n" \
  -H "X-API-Key: wrong" \
  http://localhost:8000/api/v1/tree
# 401

# с правильным → 200
curl -s -H "X-API-Key: $API_KEY" \
  http://localhost:8000/api/v1/tree | jq
# []
```

### 6.3. Публикация тестовой записи

```bash
API_KEY=$(docker compose exec -T screc-server printenv SCREC_API_KEY)

echo "# Стенограмма" > /tmp/transcript.md
echo "Протокол совещания" > /tmp/protocol.txt

curl -s -X POST http://localhost:8000/api/v1/records \
  -H "X-API-Key: $API_KEY" \
  -F 'payload={
    "project": "vNext",
    "year": "2026",
    "month": "09",
    "folder_name": "2026-09-29 тестовая запись",
    "name": "тестовая запись",
    "date": "2026-09-29",
    "tags": ["тест"]
  }' \
  -F 'kinds=transcript' -F 'files=@/tmp/transcript.md' \
  -F 'kinds=protocol'   -F 'files=@/tmp/protocol.txt' | jq
```

Ожидаемый ответ:

```json
{
  "id": "770e8400-...",
  "revision": 1,
  "action": "create",
  "path": "vNext/2026/09/2026-09-29 тестовая запись",
  "artifacts": [
    {"kind": "transcript", "filename": "transcript.md", "size": 15, "sha256": "..."},
    {"kind": "protocol",   "filename": "protocol.txt", "size": 19, "sha256": "..."}
  ]
}
```

### 6.4. Проверка дерева

```bash
curl -s -H "X-API-Key: $API_KEY" \
  http://localhost:8000/api/v1/tree | jq
```

### 6.5. Проверка файлов на диске

```bash
find data/records -type f
cat data/revision.txt
tail -1 data/changelog.jsonl | jq
```

### 6.6. Проверка логов

```bash
tail -f logs/screc.log
tail -f logs/access/access.log
```

### 6.7. Swagger

Откройте <http://localhost:8000/docs>. Все эндпоинты с примерами. Для авторизации нажмите **Authorize** и введите ключ.

---

## 7. Локальная отладка без Docker

### 7.1. Автоматический скрипт

```bash
chmod +x run_local.sh
./run_local.sh             # обычный запуск
./run_local.sh --reload    # авто-перезагрузка при изменении кода
```

Скрипт создаст `.venv`, установит зависимости, подхватит `.env`, запустит uvicorn.

### 7.2. Вручную

```bash
# 1. Виртуальное окружение
python3 -m venv .venv
source .venv/bin/activate

# 2. Зависимости
pip install -r requirements.txt

# 3. Переменные окружения
set -a; source .env; set +a

# 4. Запуск с авто-перезагрузкой
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### 7.3. Docker Compose с hot-reload

Создайте `docker-compose.override.yml` (не коммитить):

```yaml
services:
  screc-server:
    command: >
      uvicorn app.main:app
      --host 0.0.0.0
      --port 8000
      --reload
      --reload-dir /app/app
    volumes:
      - ./app:/app/app:ro
      - ./data:/data
      - ./logs:/logs
```

Затем:

```bash
docker compose up
```

Изменения в `app/` будут применяться автоматически.

---

## 8. Структура хранилища

### 8.1. Дерево

```
data/
├── ._lock                        # flock-файл
├── revision.txt                  # монотонный счётчик
├── changelog.jsonl               # append-only лог изменений
├── fts.json                      # полнотекстовый индекс
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

### 8.2. Служебные файлы

Все начинаются с `_`:

| Файл | Назначение |
|---|---|
| `_meta.json` | Метаданные записи (поля `RecordFull`, кроме `video`). |
| `_links.json` | Ссылки на видео/аудио. |
| `_deleted.json` | Маркер soft-delete. Присутствие = запись удалена. |

### 8.3. Что **не** попадает в хранилище

- Видео (`video.mp4`, `video.webm`, …) — только ссылка.
- Аудио (`video.mp3`, `video.wav`, …) — только ссылка.
- Очередь задач (`queue.json`) — это клиентское.

---

## 9. Логи

### 9.1. Файлы

| Файл | Уровень | Ротация |
|---|---|---|
| `logs/screc.log` | DEBUG+ | 10 МБ × 5 |
| `logs/screc.error.log` | WARNING+ | 10 МБ × 5 |
| `logs/access/access.log` | INFO | 10 МБ × 10 |

### 9.2. Формат

**Основной:**

```
2026-09-29 12:05:00.756 [INFO    ] screc.app.main: Screen Recorder Sync Server запущен
2026-09-29 12:05:00.780 [INFO    ] screc.app.api.records: Запись create: id=770e..., revision=1, файлов=2
```

**Access:**

```
2026-09-29 12:05:00.756 [ACCESS] 172.18.0.1 POST /api/v1/records → 200 (45.2 ms) [a1b2c3d4]
2026-09-29 12:05:05.789 [ACCESS] 172.18.0.1 GET /api/v1/tree → 200 (2.1 ms) [e5f6g7h8]
```

### 9.3. Просмотр

```bash
# Из хоста
tail -f logs/screc.log
tail -f logs/screc.error.log
tail -f logs/access/access.log

# Из контейнера
docker compose logs -f
docker compose exec screc-server tail -f /logs/screc.log

# Только ошибки
docker compose exec screc-server tail -f /logs/screc.error.log
```

### 9.4. Уровень логирования

Меняется переменной `SCREC_LOG_LEVEL`. Для детальной отладки:

```env
SCREC_LOG_LEVEL=DEBUG
```

Затем:

```bash
docker compose up -d --force-recreate
```

### 9.5. Очистка

```bash
# Остановить, удалить, запустить
docker compose stop
rm -f logs/screc.log* logs/screc.error.log* logs/access/access.log*
docker compose start
```

---

## 10. Обновление

### 10.1. Обновление кода

```bash
cd screc-server
git pull
docker compose down
docker compose build --no-cache
docker compose up -d
```

Данные в `./data` сохраняются.

### 10.2. Откат

```bash
git log --oneline
git checkout <previous-commit>
docker compose build --no-cache
docker compose up -d
```

Если схема хранения поменялась — восстановите `data/` из бэкапа (см. ниже).

---

## 11. Бэкап и восстановление

### 11.1. Что бэкапить

Только `data/`. Логи можно бэкапить опционально.

```bash
tar czf screc-backup-$(date +%F_%H-%M).tar.gz data/
```

### 11.2. Автоматизация (cron)

```cron
# Ежедневно в 3:00
0 3 * * * cd /path/to/screc-server && tar czf /backup/screc-$(date +\%F).tar.gz data/ && find /backup -name 'screc-*.tar.gz' -mtime +30 -delete
```

### 11.3. Восстановление

```bash
docker compose down
mv data data.old
tar xzf screc-backup-YYYY-MM-DD.tar.gz
docker compose up -d
```

### 11.4. Проверка бэкапа

```bash
tar tzf screc-backup-YYYY-MM-DD.tar.gz | head
```

Должны быть видны `data/revision.txt`, `data/changelog.jsonl`, `data/records/...`.

---

## 12. Устранение неполадок

### 12.1. `PermissionError: [Errno 13] Permission denied: '/logs/access'`

Контейнер работает под непривилегированным пользователем, но папки на хосте принадлежат вам. Решения:

**А. Выровнять права (быстро):**

```bash
sudo chown -R $(id -u):$(id -g) data logs
docker compose up -d --force-recreate
```

**Б. Собрать образ с вашими UID/GID:**

```bash
docker compose build --no-cache \
  --build-arg UID=$(id -u) \
  --build-arg GID=$(id -g)
docker compose up -d
```

**В. Для локальной отладки — разрешить всем:**

```bash
chmod -R 777 data logs
docker compose up -d --force-recreate
```

Подробнее — см. раздел [Сборка образа](#4-сборка-образа).

### 12.2. `Application startup failed`

Смотрите:

```bash
docker compose logs screc-server
tail -50 logs/screc.error.log
```

Частые причины:
- Неверный `SCREC_API_KEY` (пустой).
- Недоступен `SCREC_DATA_ROOT`.
- Синтаксическая ошибка в `.env`.

### 12.3. `401 Unauthorized` при правильном ключе

```bash
# Убедиться, что контейнер видит ключ
docker compose exec -T screc-server printenv SCREC_API_KEY

# Сравнить с .env
grep SCREC_API_KEY .env
```

Если значения различаются — пересоздать контейнер:

```bash
docker compose up -d --force-recreate
```

### 12.4. `413 Payload Too Large`

Увеличить лимиты в `.env`:

```env
SCREC_MAX_ARTIFACT_MB=200
SCREC_MAX_PAYLOAD_KB=2048
```

Перезапустить:

```bash
docker compose up -d --force-recreate
```

### 12.5. Файлы на диске есть, но `/tree` их не видит

Проверить права и наличие `_meta.json`:

```bash
ls -la "data/records/vNext/2026/09/"
find data/records -name '_meta.json' | head
```

Если у папки нет `_meta.json` — она не считается записью. Это может быть папка, созданная вручную.

### 12.6. FTS не находит ничего

1. Проверить, что `SCREC_FTS_ENABLED=true`.
2. Проверить, что запись проиндексирована:
   ```bash
   cat data/fts.json | jq '.records | keys'
   ```
3. Пересобрать индекс — удалить `data/fts.json` и переопубликовать записи (или написать скрипт переиндексации).

### 12.7. Контейнер в состоянии `Restarting` (по кругу)

```bash
docker compose logs --tail=100 screc-server
```

Смотрите первое сообщение об ошибке — там будет причина.

### 12.8. Порт 8000 занят

Изменить в `docker-compose.yml`:

```yaml
ports:
  - "8080:8000"
```

Сервис будет доступен на `http://localhost:8080`.

---

## 13. API

Полная документация API — в файле [`API.md`](./API.md) (или в Swagger на `/docs`).

### 13.1. Краткая шпаргалка

```bash
API_KEY=<ваш ключ>
BASE=http://localhost:8000

# Health
curl -s $BASE/health | jq

# Список проектов
curl -s -H "X-API-Key: $API_KEY" $BASE/api/v1/tree | jq

# Одна запись
curl -s -H "X-API-Key: $API_KEY" $BASE/api/v1/records/<id> | jq

# Публикация
curl -X POST $BASE/api/v1/records \
  -H "X-API-Key: $API_KEY" \
  -F 'payload={...}' \
  -F 'kinds=transcript' -F 'files=@transcript.md'

# Скачать артефакт
curl -OJ -H "X-API-Key: $API_KEY" \
  "$BASE/api/v1/records/<id>/artifacts/transcript.md"

# Синхронизация
curl -s -H "X-API-Key: $API_KEY" \
  "$BASE/api/v1/sync/changes?since_revision=0" | jq

# Поиск
curl -s -H "X-API-Key: $API_KEY" \
  "$BASE/api/v1/records/_/search?q=реестр" | jq
```

### 13.2. OpenAPI

Машинночитаемая схема:

```
http://localhost:8000/openapi.json
```

Импортируется в Postman, Insomnia, Swagger Editor, `openapi-generator`.

---

## 14. Лицензия

Внутренний проект Screen Recorder & Transcriber.

---

## Приложение: структура репозитория

```
screc-server/
├── app/
│   ├── __init__.py
│   ├── main.py                 # точка входа FastAPI
│   ├── config.py               # pydantic-settings
│   ├── auth.py                 # проверка X-API-Key
│   ├── logger.py               # логирование + access-лог
│   ├── paths.py                # безопасная работа с путями
│   ├── atomic.py               # атомарная запись
│   ├── locks.py                # flock
│   ├── revision.py             # revision + changelog
│   ├── fts.py                  # полнотекстовый индекс
│   ├── models.py               # Pydantic-схемы
│   ├── records_service.py      # бизнес-логика
│   └── api/
│       ├── __init__.py
│       ├── health.py
│       ├── tree.py
│       ├── records.py
│       ├── artifacts.py
│       └── sync.py
├── Dockerfile
├── docker-compose.yml
├── docker-compose.override.yml.example
├── .dockerignore
├── .env.example
├── .gitignore
├── requirements.txt
├── run_local.sh
├── README.md                   # этот файл
├── API.md                      # подробная документация API
├── data/                       # монтируется в /data
└── logs/                       # монтируется в /logs
```

---

## Приложение: полезные команды

```bash
# Пересобрать и перезапустить
docker compose down && docker compose build --no-cache && docker compose up -d

# Посмотреть логи в реальном времени
docker compose logs -f

# Зайти в контейнер
docker compose exec screc-server sh

# Проверить переменные окружения
docker compose exec -T screc-server printenv | grep SCREC

# Посмотреть права внутри контейнера
docker compose exec screc-server ls -lan /data /logs

# Проверить, что процесс жив
docker compose ps

# Перезапустить без пересборки
docker compose restart

# Остановить всё и удалить контейнер
docker compose down

# Остановить, удалить контейнер и данные
docker compose down -v && rm -rf data logs
```
