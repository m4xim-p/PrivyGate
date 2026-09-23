# PrivyGate

Учебный MVP privacy-preserving LLM proxy на FastAPI. Gateway использует
расширяемый набор rule-based detectors и optional локальный NER, маскирует PII,
выбирает один из трёх mock LLM через Round Robin, потоково проксирует ответ и
восстанавливает PII перед отправкой клиенту.

## Инженерная документация

Перед разработкой прочитайте [AGENTS.md](AGENTS.md). Канонические документы:

- [требования и матрица покрытия](docs/requirements.md);
- [архитектура](docs/architecture.md);
- [контракт автоматической проверки](docs/evaluation-contract.md);
- [обязательная security policy](SECURITY.md) и
  [подробная модель угроз](docs/security.md);
- [roadmap](docs/roadmap.md);
- [архитектурные решения](docs/adr/).

`POST /process` реализует обязательный контракт AlfaSonar. Существующий
`POST /v1/chat/completions` остаётся продуктовым и
демонстрационным LLM-proxy интерфейсом; оба endpoint должны использовать одно
PII-ядро.

## Архитектура

```text
Client
  -> Gateway :8000
     -> request-scoped PII masking
     -> in-memory Round Robin
     -> backend-1 | backend-2 | backend-3
     <- streaming masked response
     <- boundary-safe streaming demasking
  <- Client
```

Mapping placeholder → исходное значение хранится только в локальных объектах
обработки запроса. В логи Gateway попадают request ID, адрес backend, latency,
число и типы PII, а также безопасные решения вида
`PHONE:0.90:masked`/`PHONE:0.00:below_threshold`. Каждый mock backend
дополнительно пишет `backend_id`, число и
безопасные имена PII placeholders, флаги `raw_email_detected`/
`raw_phone_detected` и настроенный размер выходного чанка. Prompt, исходные PII
и mapping не логируются.

## Запуск через Docker Compose

Требуется Docker с Compose:

```bash
docker compose up --build
```

Gateway доступен на `http://localhost:8000`. Backend-сервисы доступны только во
внутренней сети Compose.

## Локальный запуск

Требуется Python 3.12:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

BACKEND_ID=backend-1 uvicorn mock_llm.main:app --port 8001
BACKEND_ID=backend-2 uvicorn mock_llm.main:app --port 8002
BACKEND_ID=backend-3 uvicorn mock_llm.main:app --port 8003
uvicorn app.main:app --port 8000
```

Последние четыре команды нужно запустить в отдельных терминалах. По умолчанию
Gateway использует порты 8001–8003. Список можно переопределить переменной
`BACKEND_URLS`, разделяя URL запятыми.

## Подключение реальной LLM (ADR-0008)

`/v1/chat/completions` маршрутизирует запросы по `model` через внутренний конфиг
моделей (`config/models.json`, путь через `MODELS_CONFIG_PATH`). Конфиг
перечитывается по TTL (`MODELS_RELOAD_INTERVAL_SECONDS`, по умолчанию 30) без
редеплоя.

```json
{
  "models": [
    { "name": "mock-1", "api_base": "http://localhost:8001", "model": "mock-model" },
    { "name": "mock-2", "api_base": "http://localhost:8002", "model": "mock-model" },
    { "name": "gpt-4o", "api_base": "https://api.openai.com", "model": "gpt-4o" },
    { "name": "alfagen", "api_base": "https://alfagen.alfabank.ru/continue-dev", "model": "alfagen-model" }
  ]
}
```

Mock-модели (`mock-1`, `mock-2`) указывают на mock backend (`BACKEND_URLS`) —
удобно для демо сравнения ответов mock и реальной модели. Реальные модели
требуют `X-Model-API-Key`.

Для реальных моделей с российским корневым сертификатом (например,
`alfagen.alfabank.ru`) укажите путь к CA-сертификату через `CA_CERTS_PATH`
(сертификат Минцифры лежит в `certs/russiantrustedca2024.pem`):

```bash
CA_CERTS_PATH="$PWD/certs/russiantrustedca2024.pem" \
MODELS_CONFIG_PATH="$PWD/config/models.json" \
POLICY_CONFIG_PATH="$PWD/config/policy.json" \
uvicorn app.main:app --port 8000
```

Клиент передаёт в запросе:
- `model` — имя модели (должно совпадать с `name` в конфиге);
- `X-Model-API-Key` — свой API-ключ для модели (прокси использует его как
  `Authorization: Bearer` при запросе к upstream);
- `Authorization`/`X-API-Key` — allowlist-ключ (ADR-0004);
- `X-Consumer-ID` — профиль политики.

```bash
curl -N http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'X-Consumer-ID: crm' \
  -H 'Authorization: Bearer <allowlist_key>' \
  -H 'X-Model-API-Key: <client_model_key>' \
  -d '{
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "Привет"}],
    "stream": true
  }'
```

Неизвестная модель → `404`. Per-consumer квота токенов
(`max_tokens_per_request` в policy) → `429` при превышении.

Реальная модель должна быть OpenAI-compatible (`/v1/chat/completions`, SSE).
Gateway парсит SSE, демаскирует `delta.content` и пересобирает SSE, сохраняя
корректность при пересечении placeholder'ами границ событий. Ключ клиента
передаётся в запросе, не хранится на прокси.

## Пример запроса

Опция `-N` отключает buffering вывода curl:

```bash
curl -N http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "mock-model",
    "messages": [{
      "role": "user",
      "content": "Позвони мне на +7 999 123-45-67 или напиши test@example.com"
    }],
    "stream": true
  }'
```

Повторные запросы последовательно попадут в backend-1, backend-2, backend-3 и
снова backend-1. Проверка здоровья: `curl http://localhost:8000/health`.

## Контракт `/process`

Обязательный эндпоинт для автоматической проверки AlfaSonar. Маскирование и
демаскирование коррелируются по `payload_id`. Полная OpenAPI-спецификация —
[`process_api.yaml`](process_api.yaml).

По каждому запросу `/process` логируются выявленные типы ПДН и их количество
(без raw PII): `process_masked payload_id=... pii_count=4 pii_types=EMAIL,PASSPORT,...`.
Это соответствует ТЗ §4.1 (логирование выявленных типов ПДН по каждому запросу).

```bash
# Маскирование (первый запрос с новым payload_id)
curl -X POST http://localhost:8000/process \
  -H 'Content-Type: application/json' \
  -d '{"payload":"Иванов Иван Иванович, паспорт 45 10 123456","payload_id":"demo-1"}'
# -> {"result":"__PII_PERSON_1__, паспорт __PII_PASSPORT_1__"}

# Демаскирование (второй запрос с тем же payload_id и ранее выданной маской)
curl -X POST http://localhost:8000/process \
  -H 'Content-Type: application/json' \
  -d '{"payload":"__PII_PERSON_1__, паспорт __PII_PASSPORT_1__","payload_id":"demo-1"}'
# -> {"result":"Иванов Иван Иванович, паспорт 45 10 123456"}
```

Конфигурация `/process` через переменные окружения: `PROCESS_ACTIVE_TTL_SECONDS`,
`PROCESS_COMPLETED_TTL_SECONDS`, `PROCESS_STORE_MAX_ENTRIES`, `PROCESS_STORE_MAX_BYTES`,
`PROCESS_WAITER_TIMEOUT_SECONDS`, `PROCESS_MAX_PAYLOAD_BYTES`, `PROCESS_MAX_ESTIMATED_TOKENS`,
`PROCESS_MASK_WORKERS`, `PROCESS_DETECTION_PROFILE`. NER-переменные (`NER_ENABLED`,
`NER_MODEL`, `NER_MODEL_REVISION`, `NER_MAX_CONCURRENCY` и др.) описаны в разделе
«Optional local NER».

При превышении лимита `/process` возвращает `413` с пояснением в стиле DeepSeek:
`payload too large: maximum context length is N tokens, but you requested M tokens`.
Byte и token лимиты проверяются отдельно (не «100k = 400 КБ»).

## Метрики (`GET /metrics`)

Gateway отдаёт runtime-метрики в формате Prometheus text по `GET /metrics`
(реализация — `app/metrics.py`). Endpoint лёгкий: только чтение счётчиков и
размеров store, без дорогих вычислений в hot path. Не логирует raw PII.

```bash
curl http://localhost:8000/metrics
```

Доступные метрики:

| Метрика | Тип | Описание |
|---|---|---|
| `privygate_uptime_seconds` | gauge | Время работы процесса. |
| `privygate_process_mask_total` | counter | Успешные маскирования `/process`. |
| `privygate_process_demask_total` | counter | Успешные демаскирования `/process`. |
| `privygate_process_retry_total` | counter | Повторные запросы (retry masking/demasking). |
| `privygate_process_rate_limited_total` | counter | Ответы `429` (admission limit / waiter timeout). |
| `privygate_process_conflict_total` | counter | Конфликты `409` (payload_id с другим payload). |
| `privygate_process_error_total` | counter | Внутренние ошибки `5xx`. |
| `privygate_process_new_id_total` | counter | Новые уникальные `payload_id`. |
| `privygate_store_evictions_total` | counter | Число evicted (истёкших) сессий. |
| `privygate_store_sessions` | gauge | Число сессий в store (ACTIVE + COMPLETED). |
| `privygate_store_pending` | gauge | Число pending-запросов (ожидают маскирования). |
| `privygate_store_tombstones` | gauge | Число tombstone-записей (истёкшие payload_id). |
| `privygate_store_bytes` | gauge | Приблизительный размер store в байтах. |
| `privygate_event_loop_delay_ms` | gauge | Задержка event loop (мс), замеряется фоновой задачей. |

Счётчики инкрементируются в `ProcessService` (лёгкие атомарные инкременты, не в
hot path). Размер store читается из in-memory store на запрос. Event loop delay
сэмплируется отдельной фоновой задачей (`EventLoopDelaySampler`), не в request
path.

Store использует min-heap индекс истечения (ADR-0006): ленивая проверка TTL при
обращении к `payload_id`, eviction перед проверкой capacity, фоновая очистка
истёкших записей небольшими порциями (`StoreEvictionTask`). Это устраняет O(n)
скан всех сессий на каждый запрос (исходный bottleneck, 78% активного CPU).

Tombstone TTL = 60s (небольшой запас после COMPLETED TTL 120s), настраивается
через `PROCESS_TOMBSTONE_TTL_SECONDS`. `tombstone_max_entries` по умолчанию
100 000 (`PROCESS_TOMBSTONE_MAX_ENTRIES`). Действующие tombstones **никогда не
вытесняются рано** (это сломало бы защиту `410`); при достижении лимита новые
`payload_id` отклоняются с `429` + `Retry-After`, пока tombstones не истекут по
TTL.

### Политики потребителей (ADR-0004)

Per-consumer настройка маскирования через `PolicyRegistry`. Конфиг — JSON-файл,
путь задаётся `POLICY_CONFIG_PATH`; файл перечитывается каждые
`POLICY_RELOAD_INTERVAL_SECONDS` (по умолчанию 30) без редеплоя.

**Шаблон конфига:** [`config/policy.example.json`](config/policy.example.json).
Скопируйте его в `config/policy.json` (не коммитится — содержит API-ключи) и
заполните реальные ключи. В docker-compose `config/` монтируется в `/config`, а
`POLICY_CONFIG_PATH` по умолчанию указывает на `/config/policy.json`.

#### Поля `ConsumerPolicy`

| Поле | Тип | По умолчанию | Описание |
|---|---|---|---|
| `consumer_id` | string | — | Идентификатор системы-потребителя (заголовок `X-Consumer-ID`). |
| `enabled` | bool | `true` | Вкл/откл обращения в модуль. |
| `enabled_pii_types` | string[] | все 17 типов | Перечень типов ПДН для маскирования. |
| `excluded_pii_types` | string[] | `[]` | Типы ПДН, которые **не** маскировать (вычитаются из `enabled_pii_types`). |
| `allow_demasking` | bool | `true` | Право демаскирования ответа LLM. |
| `require_card_for_pin` | bool | `true` | Комбинационное правило: при `true` PIN маскируется только рядом с номером карты; при `false` — всегда. |
| `min_confidence` | number | `0.80` | Порог уверенности детектора. |
| `masking_mode` | string | `typed_placeholder` | Вид маски (`typed_placeholder` / `synthetic` / `format_preserving`). |
| `degradation` | string | `fail_closed` | Поведение при недоступности детектора (`fail_closed` / `rule_only`). |
| `custom_terms` | string[] | `[]` | Список терминов для маскирования только для этой системы (ADR-0007). |
| `api_keys` | string[] | `[]` | Allowlist ключей (заголовок `Authorization: Bearer` или `X-API-Key`). |

#### Типы ПДН (`enabled_pii_types` / `excluded_pii_types`)

`PERSON`, `DATE_OF_BIRTH`, `BIRTH_PLACE`, `PASSPORT`, `CITIZENSHIP`,
`PASSPORT_AUTHORITY`, `PASSPORT_UNIT_CODE`, `PASSPORT_ISSUE_DATE`,
`DRIVING_LICENSE`, `ADDRESS`, `EMAIL`, `PHONE`, `INN`, `CARD`, `CVV`, `PIN`,
`CARD_HOLDER`.

#### Примеры сценариев

**1. Маскировать только паспорт** (`enabled_pii_types`):

```json
{
  "consumer_id": "passport-only",
  "enabled": true,
  "enabled_pii_types": ["PASSPORT"],
  "allow_demasking": true,
  "api_keys": ["CHANGE_ME_passport_api_key"]
}
```

**2. Маскировать всё, кроме EMAIL** (`excluded_pii_types` — оператор видит почту
клиента, остальное скрыто):

```json
{
  "consumer_id": "email-agent",
  "enabled": true,
  "excluded_pii_types": ["EMAIL"],
  "allow_demasking": true,
  "api_keys": ["CHANGE_ME_email_agent_api_key"]
}
```

**3. Маскировать всё, кроме PHONE** (колл-центр видит номер, остальное скрыто):

```json
{
  "consumer_id": "call-center",
  "enabled": true,
  "excluded_pii_types": ["PHONE"],
  "allow_demasking": true,
  "api_keys": ["CHANGE_ME_call_center_api_key"]
}
```

`/process` (AlfaSonar) всегда использует default profile `alfasonar` без auth.
Allowlist применяется только к продуктовому `/v1/chat/completions`.

### Типы маскирования (`masking_mode`)

Каждая система-потребитель может выбрать вид маскирования через поле
`masking_mode`. Доступны три режима:

| Режим | Описание | Пример для `Иван Иванов, email ivan@example.com` |
|---|---|---|
| `typed_placeholder` (по умолчанию) | Замена на типизированный placeholder | `__PII_PERSON_1__, email __PII_EMAIL_1__` |
| `synthetic` | Замена на фиксированные синтетические данные | `Иванов Иван Иванович_1, email user@example.com_1` |
| `format_preserving` | Сохранение длины и разделителей, символы → `*` | `**** ******, email ****************` |

Пример конфига с синтетическим маскированием:

```json
{
  "consumer_id": "synthetic-agent",
  "enabled": true,
  "masking_mode": "synthetic",
  "allow_demasking": true,
  "api_keys": ["CHANGE_ME_synthetic_api_key"]
}
```

`typed_placeholder` — безопасный default для AlfaSonar (ADR-0003). `synthetic` и
`format_preserving` — дополнительные возможности для отдельных consumer profiles
(критерий 3.7). Во всех режимах mapping хранит original, поэтому demasking
работает одинаково.

### Custom terms маскирование (`custom_terms`, ADR-0007)

Система-потребитель может указать список терминов/слов, которые должны
маскироваться **только для этой системы**. Термины матчатся как точные слова
(word-boundary, case-insensitive) и маскируются в выбранном `masking_mode`.

```json
{
  "consumer_id": "custom-terms-agent",
  "enabled": true,
  "custom_terms": ["проект-альфа", "секрет"],
  "allow_demasking": true,
  "api_keys": ["CHANGE_ME_custom_terms_api_key"]
}
```

Термины маскируются как категория `CUSTOM_TERM` (например,
`__PII_CUSTOM_TERM_1__`). Они **не входят** в 17 обязательных категорий и
активны только для consumer, у которого заданы. `/process` (default `alfasonar`)
не маскирует custom terms — фича работает только для product API
(`/v1/chat/completions`), где policy резолвится по `X-Consumer-ID`.

### Dev-only просмотр полного запроса к mock backend

Для ручной проверки masking на синтетических данных можно явно включить полный
лог тела запроса, полученного mock backend:

```bash
MOCK_LOG_REQUEST_BODY=true NER_ENABLED=true docker compose up --build
```

В backend-логах появится строка `DEV_ONLY ... request_body=...`. Этот режим
может записать незамаскированные PII, если detector их пропустил, поэтому он
выключен по умолчанию и не должен использоваться с реальными данными или в
production. Для просмотра только backend-логов:

```bash
docker compose logs -f backend-1 backend-2 backend-3
```

## Тесты

```bash
pip install -e '.[dev]'
./scripts/check.sh
```

Gate последовательно запускает Ruff (lint, conventions и complexity), mypy,
Bandit, pip-audit и полный pytest. Та же команда блокирует merge/deploy в CI.

## Async load test

Скрипт открывает указанное количество одновременных streaming-запросов и
полностью читает каждый ответ. Значение `--concurrency` одновременно является
числом конкурентных и общим числом запросов:

```bash
python scripts/load_test.py --concurrency 10
python scripts/load_test.py --concurrency 100
```

Для каждого запроса измеряются время до первого непустого чанка и полная
latency. Итоговый отчёт содержит throughput, average, p50/p95 и распределение
ошибок. В запросах используются только синтетические email и телефоны.

## Нагрузочное тестирование `/process` (k6)

Воспроизводимый benchmark контракта `/process` по профилю AlfaSonar
(разгон до 1000 RPS, до 200 соединений, keep-alive, masking/demasking поровну,
retries, 429). Требуется [k6](https://k6.io) (`brew install k6`) и запущенный
gateway с `/metrics`.

```bash
# Полный профиль (ramp 60s + hold 60s, peak 1000 RPS, 200 VU)
scripts/k6/run_benchmark.sh --container privygate-bench

# Уменьшенный профиль для быстрой проверки
scripts/k6/run_benchmark.sh --ramp 30 --hold 30 --peak 1000 --vus 200 --container privygate-bench
```

`run_benchmark.sh` запускает параллельно:

- **k6** (`scripts/k6/process_load.js`) — HTTP-метрики: latency (mask/demask
  отдельно), RPS, retries, 429, новые ID, round-trip checks;
- **Python-сборщик** (`scripts/k6/metrics_collector.py`) — real-time серверные
  метрики: store size, event loop delay (через `/metrics`), CPU/RSS процесса
  (через `--container` для docker или `--pid` для host-процесса).

Опции `run_benchmark.sh`: `--ramp`, `--hold`, `--peak`, `--vus`, `--duration`
(длительность сборщика), `--container`/`--pid` (источник CPU/RSS).

### CPU-профилирование (py-spy)

Для снятия CPU-профиля с разбивкой по функциям (ProcessStore / детекторы /
HTTP) во время нагрузки используется `py-spy` внутри контейнера:

```bash
# Установить py-spy в контейнер (один раз)
docker exec privygate-bench pip install py-spy

# Снять CPU-профиль во время 5-минутного прогона (ramp 60s + hold 240s)
scripts/k6/cpu_profile.sh --container privygate-bench --peak 1000 --ramp 60 --hold 240 --vus 200
```

`cpu_profile.sh` запускает `py-spy record` внутри контейнера параллельно с k6,
затем анализирует профиль через `scripts/k6/analyze_profile.py`, агрегируя
сэмплы по категориям: `ProcessStore`, `Detectors`, `HTTP`, `Idle` (idle worker
threads). Результат — разбивка активного CPU по категориям и топ функций.

Результаты фиксируются в [`docs/benchmarks/load-test-baseline.md`](docs/benchmarks/load-test-baseline.md)
и служат точкой сравнения для подтверждения/опровержения улучшений или регрессий
после изменений (правило AGENTS.md: производительные оптимизации принимаются
только вместе с воспроизводимым benchmark).

## Optional local NER

По умолчанию NER выключен, и Gateway использует только rule-based detectors. Для
локального PERSON detection установите optional dependencies и включите модель:

```bash
pip install -e '.[ner]'
NER_ENABLED=true uvicorn app.main:app --port 8000
```

Модель задаётся через `NER_MODEL`; начальное значение —
`LLAIMlegal/ru-legal-ner`. Checkpoint закреплён через `NER_MODEL_REVISION`
(по умолчанию `924a4b1912ec6e55a4be959cab215ad8ff32a750`); при смене модели нужно
явно указать соответствующий полный commit SHA. Fast tokenizer сохраняет offsets исходной строки,
LOC и ORG игнорируются. Модель создаётся один раз в FastAPI lifespan, а inference
выполняется в worker thread. Длинные тексты обрабатываются перекрывающимися
tokenizer windows. В лог попадают только latency, status и количества, без текста.

Запуск с Docker Compose:

```bash
NER_ENABLED=true docker compose up --build
```

NER-модель разбивает широкие PERSON-span'ы по союзам («и», «а»), чтобы не
пропускать имена в смешанных предложениях (например, «Иванов Иван и Полищук
Максим» → два отдельных имени).

### Кеширование NER-модели в docker-образе

При `NER_ENABLED=true` модель скачивается **при сборке образа** в `/models/ner`
(через `snapshot_download`). В runtime модель загружается из `/models/ner` с
`local_files_only=True` — сетевая загрузка запрещена (`NER_OFFLINE=true` по
умолчанию). Если модель отсутствует или повреждена, запуск при `NER_ENABLED=true`
завершается ошибкой.

```bash
# Сборка образа с предзагруженной моделью
docker build --build-arg INSTALL_NER=true -t privygate .

# Запуск (модель грузится из /models/ner, без сети)
NER_ENABLED=true docker compose up --build
```

`NER_MODEL_PATH` (по умолчанию `/models/ner`) задаёт каталог локальной модели.
`NER_OFFLINE` (по умолчанию `true`) запрещает скачивание из Hugging Face Hub в
runtime.

## Ограничения MVP

- Rule-based detectors поддерживают ограниченный набор форматов. СНИЛС и ИНН
  проверяются по контрольным цифрам, карты — по Luhn. Для паспортов принимаются
  явно разделённые серии и номера; веса PHONE-контекста пока задаются в коде
  через `PhoneDetectorConfig`.
- In-memory Round Robin действует в пределах одного процесса Gateway; несколько
  worker-процессов имеют независимые счётчики.
- Mapping находится в памяти до завершения потока; внешнего защищённого хранилища нет.
- Mock streaming — обычный `text/plain`, а не OpenAI SSE-протокол.
- Нет Redis, Kubernetes, Kafka, настоящей LLM, auth, rate limiting,
  Prometheus, retries и circuit breaker.
- При обрыве backend-потока уже отправленный HTTP-ответ нельзя заменить на 5xx.
