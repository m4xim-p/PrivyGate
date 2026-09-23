# Архитектура PrivyGate

## Схема для жюри

Цепочка «Система-потребитель → Модуль (идентификация → маскирование → LLM →
демаскирование) → Потребитель»:

```text
┌─────────────────────┐
│ Система-потребитель │  (CRM, колл-центр, email-агент, ...)
└──────────┬──────────┘
           │  POST /v1/chat/completions   (X-Consumer-ID, API-ключ)
           ▼
┌──────────────────────────────────────────────────────────────┐
│                     GATEWAY  (:8000)                          │
│                                                              │
│  ┌──────────────┐   ┌─────────────────────────────────────┐  │
│  │ PolicyRegistry│   │        PII-ядро (единое)            │  │
│  │ - allowlist   │   │  детекторы → overlap resolution     │  │
│  │ - consumer    │──▶│  → confidence → маскирование        │  │
│  │   profile     │   │  (typed/synthetic/format_preserving)│  │
│  └──────────────┘   └─────────────────────────────────────┘  │
│                                                              │
│  Детекторы PII-ядра:                                          │
│  ┌────────────────────────────┐  ┌─────────────────────────┐ │
│  │ Внутренние rule-based      │  │ Локальный NER           │ │
│  │ правила (regex, checksum,  │  │ (Hugging Face,          │ │
│  │ контекст, 17 категорий)    │  │ LLAIMlegal/ru-legal-ner,│ │
│  │ app/pii.py                 │  │ offline, app/ner.py)    │ │
│  └────────────────────────────┘  └─────────────────────────┘ │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐    │
│  │  UpstreamClient + ModelRegistry (маршрутизация)      │    │
│  └──────────────────────────┬───────────────────────────┘    │
└─────────────────────────────┼────────────────────────────────┘
                              │  замаскированный запрос
                              ▼
              ┌───────────────────────────────┐
              │  LLM (mock-1/2/3 или реальная) │
              └───────────────────────────────┘
                              │  streaming замаскированный ответ
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                     GATEWAY  (:8000)                          │
│  ┌──────────────────────────────────────────────────────┐    │
│  │  StreamingDemasker (boundary-safe, только если        │    │
│  │  consumer имеет allow_demasking)                      │    │
│  └──────────────────────────────────────────────────────┘    │
└──────────────────────────────┬────────────────────────────────┘
                               │  демаскированный ответ
                               ▼
┌─────────────────────┐
│     Потребитель      │
└─────────────────────┘
```

## Решение

PrivyGate предоставляет два API-адаптера над единым PII-ядром:

- `POST /process` — обязательный stateful контракт AlfaSonar для пар
  masking/demasking;
- `POST /v1/chat/completions` — прозрачный OpenAI-like proxy с вызовом LLM и
  streaming demasking.

`/process` реализован через `ProcessService` + `ProcessStore` над единым PII-ядром.
`PolicyRegistry` выбирает профиль по consumer identity (для `/process` — default
`alfasonar`).

```text
                          +--------------------+
POST /process ---------->| ProcessService     |----> ProcessStore
                          +---------+----------+
                                    |
                                    v
                          +--------------------+
                          | PIIMaskingEngine   |
                          | - detector profile |
                          | - PIIMasker        |
                          +---------+----------+
                                    ^
                                    |
POST /v1/chat/completions----------+
          |                         |
          |            +------------+------------+
          |            | PolicyRegistry          |
          |            | - allowlist (API key)   |
          |            | - resolve consumer      |
          |            |   profile               |
          |            +-------------------------+
          +----> ChatProxyService ----> upstream LLM
                         |
                         +----> streaming demasking

GET /metrics ----------> ProcessMetrics + EventLoopDelaySampler
                         (store size, counters, event loop delay)
```

`ProcessService`, `ProcessStore`, `PIIMaskingEngine` и `PolicyRegistry` реализованы.
`PolicyRegistry` (ADR-0004) реализует целевое направление `PrivacyEngine` с
`DetectorRegistry`/`MaskingPolicy` для per-consumer политик: выбирает профиль по
consumer identity, применяет allowlist к продуктовому API и default profile
`alfasonar` к `/process`.

## Существующие компоненты

- `app/pii.py` — detector protocol, rule detectors, overlap resolution, masker,
  streaming demasker и `CustomTermDetector` (per-consumer термины, ADR-0007);
- `app/pii_engine.py` — `PIIMaskingEngine`: immutable detector profile, создаёт
  `PIIMasker` на запрос, маскирует в worker thread, возвращает только masked и
  безопасную диагностику (без mapping);
- `app/process_store.py` — `ProcessStore`: bounded in-memory TTL store с
  ACTIVE/COMPLETED lifecycle, pending-координацией и tombstone;
- `app/process_service.py` — `ProcessService`: оркестратор `/process` (admission,
  валидация, state machine), без regex/tokenizer/masking;
- `app/errors.py` — domain errors для `/process` (409/413/422/429/410);
- `app/metrics.py` — `ProcessMetrics` (лёгкие счётчики mask/demask/retry/429),
  `EventLoopDelaySampler` (фоновая задача замера задержки event loop),
  `render_prometheus` (Prometheus text для `GET /metrics`);
- `app/ner.py` — optional PERSON detector и Transformers adapter;
- `app/main.py` — FastAPI lifespan и OpenAI-like Gateway endpoint;
- `app/proxy.py` — асинхронный upstream streaming (StreamingDemasker);
- `app/upstream.py` — `UpstreamClient`: OpenAI-compatible upstream (SSE/text),
  auth, таймауты (ADR-0008);
- `app/model_registry.py` — `ModelRegistry`: конфиг моделей (api_base, model),
  маршрутизация по `model` (ADR-0008);
- `app/routing.py` — in-memory Round Robin;
- `mock_llm/main.py` — три конфигурируемых mock backend процесса.

### Таблица компонентов

| Компонент | Файл | Роль |
|---|---|---|
| PII-ядро | `app/pii.py`, `app/pii_engine.py` | Детекторы, overlap resolution, маскирование, streaming demasking |
| PolicyRegistry | `app/policy.py` | Per-consumer профили, allowlist, masking mode, custom terms |
| ProcessService/Store | `app/process_service.py`, `app/process_store.py` | Контракт `/process`, state machine, TTL store |
| UpstreamClient | `app/upstream.py` | OpenAI-compatible upstream (SSE/text), auth, таймауты |
| ModelRegistry | `app/model_registry.py` | Маршрутизация по `model`, квоты токенов |
| Metrics | `app/metrics.py` | `/metrics` (Prometheus text), event loop delay |
| NER (optional) | `app/ner.py` | Локальный PERSON-детектор (Transformers, offline) |
| Mock LLM | `mock_llm/main.py` | Три конфигурируемых mock backend |

## Разрешённое направление зависимостей

```text
API adapters
    -> application services
        -> PII domain interfaces
            -> detector/model adapters
        -> state/upstream interfaces
            -> in-memory/HTTP implementations
```

Запрещено:

- endpoint напрямую компилирует regex или вызывает tokenizer;
- detector знает о FastAPI, HTTP или ProcessStore;
- `/process` вызывает `/v1/chat/completions` по HTTP;
- proxy вызывает `/process` по HTTP внутри того же приложения;
- два endpoint имеют разные overlap/masking реализации.

## Общий PII pipeline

```text
text
 -> configured detectors
 -> PIIMatch candidates with original offsets
 -> confidence policy
 -> overlap resolution
 -> masking policy (typed placeholders — default AlfaSonar)
 -> masked text + safe diagnostics (count, types)
```

Mapping существует только временно внутри `PIIMasker.mask()` и не возвращается
наружу: `PIIMaskingEngine` возвращает masked текст и безопасную диагностику без
mapping. Detector отвечает только за кандидатов. Решение «маскировать или нет» и
вид маски принадлежат policy/application layer. Это позволяет менять weights и
context words без переписывания endpoints.

### Два источника детекторов

Детекторы делятся на два источника:

- **Внутренние rule-based правила** (`app/pii.py`) — regex, checksum (Luhn,
  контрольные цифры ИНН/СНИЛС), контекстные слова и границы. Покрывают все
  17 обязательных категорий ПДН. Работают всегда, без внешних зависимостей.
- **Локальный NER** (`app/ner.py`, optional) — Transformers-модель
  `LLAIMlegal/ru-legal-ner` с Hugging Face. Используется для PERSON detection.
  Модель скачивается при сборке образа в `/models/ner` и в runtime грузится с
  `local_files_only=True` (offline, без сети). Включается через `NER_ENABLED=true`.

Оба источника возвращают `PIIMatch` с offsets исходного текста и проходят один
общий overlap resolution и маскирование — единый pipeline, без дублирования
логики.

## Поток `/process`

```text
request(payload, payload_id)
 -> admission control
 -> atomic ProcessStore lookup/create (pending)
 -> new payload: mask outside store lock, publish ACTIVE
 -> pending + same payload: wait shared Future (asyncio.shield)
 -> pending + different payload: 409
 -> original payload: retry masking (same masked)
 -> masked payload: return original (demasking)
 -> conflicting payload: 409
 -> response(result)
```

ProcessSession хранит original, masked, state, created_at, last_accessed_at и
безопасную диагностику (pii_count, pii_types). Mapping в session не хранится:
он существует только временно внутри `PIIMasker.mask()` и удаляется после
формирования masked. Demasking `/process` выполняется возвратом сохранённого
original, поскольку checker присылает точную строку masked. Raw session data не
логируется. Подробная state machine описана в `evaluation-contract.md`.

Checker передаёт на обратном шаге ровно ранее выданный `masked` текст. Robust
demasking изменённого LLM-ответа остаётся задачей proxy/demo, но не требуется для
автоматической проверки `/process`.

## Поток LLM proxy

```text
chat request
 -> request-scoped mask
 -> select upstream backend
 -> stream masked response
 -> request-scoped streaming demask
 -> client
```

Mapping proxy живёт только в течение запроса. Mapping `/process` существует только
временно внутри `PIIMasker.mask()` и не хранится в session: demasking выполняется
возвратом сохранённого original. Жизненные циклы mapping proxy и `/process` не
смешиваются.

## Конфигурация потребителей

`PolicyRegistry` (ADR-0004) выбирает профиль по доверенному consumer identity.
Профиль определяет enabled PII types, thresholds, context weights, masking mode,
combination rules и право demasking. Для автоматической проверки используется
default профиль `alfasonar`, потому что контракт не содержит consumer ID или auth
headers. Обязательная product-auth не применяется к evaluation endpoint.

Идентификация потребителя разделяет секрет и идентификатор (паттерн Kong key-auth):
API-ключ (`Authorization: Bearer` / `X-API-Key`) аутентифицирует, `X-Consumer-ID`
выбирает профиль. Конфиг policy хранится в JSON-файле и перечитывается без редеплоя.

## Масштабирование

Первый `/process` может использовать один Uvicorn process и in-memory TTL store.
Несколько worker процессов недопустимы без shared state или гарантированной sticky
routing: masking и demasking могут попасть в разные процессы. Переход к Redis или
другому shared store выполняется только после benchmark и ADR.

Уточнённая нагрузка использует ступенчатый разгон до 1000 RPS с удержанием достигнутого
уровня и не более 200 одновременных соединений; в сохранённом ответе организаторов
также указан средний профиль около 330 RPS. Это усиливает выбор single-process baseline,
но его достаточность должна подтверждаться полным пятиминутным benchmark; shared store
не добавляется до измерений.

## Расширение

Новый тип PII добавляется через:

1. реализацию `PIIDetector`;
2. регистрацию detector в registry/profile;
3. positive/negative/overlap/round-trip tests;
4. обновление матрицы требований;
5. quality evaluation до статуса `validated`.
