# Архитектура PrivyGate

## Решение

PrivyGate предоставляет два API-адаптера над единым PII-ядром:

- `POST /process` — обязательный stateful контракт AlfaSonar для пар
  masking/demasking;
- `POST /v1/chat/completions` — прозрачный OpenAI-like proxy с вызовом LLM и
  streaming demasking.

`/process` пока является целевой, но ещё не реализованной частью архитектуры.

```text
                         +--------------------+
POST /process ---------->| ProcessService     |----> ProcessStore
                         +---------+----------+
                                   |
                                   v
                         +--------------------+
                         | PrivacyEngine      |
                         | - DetectorRegistry |
                         | - OverlapResolver  |
                         | - MaskingPolicy    |
                         | - Demasker         |
                         +---------+----------+
                                   ^
                                   |
POST /v1/chat/completions----------+
          |
          +----> ChatProxyService ----> upstream LLM
                         |
                         +----> streaming demasking
```

Названия `ProcessService`, `PrivacyEngine`, `MaskingPolicy` и `ProcessStore`
описывают целевые границы. Они не означают, что одноимённые модули уже существуют.

## Существующие компоненты

- `app/pii.py` — detector protocol, rule detectors, overlap resolution, masker и
  streaming demasker;
- `app/ner.py` — optional PERSON detector и Transformers adapter;
- `app/main.py` — FastAPI lifespan и OpenAI-like Gateway endpoint;
- `app/proxy.py` — асинхронный upstream streaming;
- `app/routing.py` — in-memory Round Robin;
- `mock_llm/main.py` — три конфигурируемых mock backend процесса.

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
 -> MaskingResult(text, mapping, safe diagnostics)
```

Detector отвечает только за кандидатов. Решение «маскировать или нет» и вид маски
принадлежат policy/application layer. Это позволяет менять weights и context words
без переписывания endpoints.

## Поток `/process`

```text
request(payload, payload_id)
 -> admission control
 -> atomic ProcessStore lookup/create
 -> new/original payload: mask and store session
 -> masked payload: return original
 -> conflicting payload: 409
 -> response(result)
```

ProcessSession должен как минимум хранить original, masked, mapping, created_at и
last_accessed_at. Raw session data не логируется. Подробная state machine описана в
`evaluation-contract.md`.

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

Mapping proxy живёт только в течение запроса. Mapping `/process` живёт до второго
вызова или TTL — смешивать эти жизненные циклы нельзя.

## Конфигурация потребителей

Целевой policy registry выбирает профиль по доверенному consumer identity. Профиль
определяет enabled PII types, thresholds, context weights, masking mode,
combination rules и право demasking. Для автоматической проверки используется
default профиль `alfasonar`, потому что контракт не содержит consumer ID или auth
headers. Обязательная product-auth не применяется к evaluation endpoint.

## Масштабирование

Первый `/process` может использовать один Uvicorn process и in-memory TTL store.
Несколько worker процессов недопустимы без shared state или гарантированной sticky
routing: masking и demasking могут попасть в разные процессы. Переход к Redis или
другому shared store выполняется только после benchmark и ADR.

Уточнённая нагрузка имеет ramp-up, среднюю около 330 RPS, пики до 1000 RPS и не
более 200 одновременных соединений. Это усиливает выбор single-process baseline;
shared store не добавляется до измерений.

## Расширение

Новый тип PII добавляется через:

1. реализацию `PIIDetector`;
2. регистрацию detector в registry/profile;
3. positive/negative/overlap/round-trip tests;
4. обновление матрицы требований;
5. quality evaluation до статуса `validated`.
