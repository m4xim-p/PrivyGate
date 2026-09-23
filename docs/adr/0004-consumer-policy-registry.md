# ADR-0004: Consumer Policy Registry и allowlist

- Статус: Accepted
- Дата: 2026-09-23

## Контекст

ТЗ (раздел 4.4) и критерий 3.4 требуют гибкой настройки по системам-потребителям:
вкл/откл обращения, перечень типов ПДН, наличие демаскирования. Критерий 3.6 требует
ограничения круга систем (allowlist). Сейчас per-consumer настройка отсутствует
(статус `missing` в `docs/requirements.md`): `PIIMaskingEngine` получает фиксированный
набор детекторов, `/v1/chat/completions` не защищён, `PIIMasker` жёстко зашит на
typed placeholders и единый `min_confidence`.

Контракт `/process` (Приложение A/B) не содержит consumer ID или auth headers:
проверяющая система AlfaSonar шлёт запросы без заголовков и использует default
evaluation profile. Обязательная product-auth не применяется к evaluation endpoint.

## Источники решения

- Приложения A/B: [`../source/track-specification.txt`](../source/track-specification.txt).
- Критерии оценивания: [`../source/evaluation-criteria.md`](../source/evaluation-criteria.md).
- Сохранённые ответы организаторов:
  [`../source/organizer-clarifications.txt`](../source/organizer-clarifications.txt).
- `SECURITY.md` (раздел 4 — allowlist), `docs/architecture.md` (целевой
  `PrivacyEngine`/`MaskingPolicy`), `docs/evaluation-contract.md` (default profile
  `alfasonar`).

## Решение

### 1. Consumer Policy Registry (`app/policy.py`)

Новый модуль в application services слое (между API adapters и PII domain). Не знает
о FastAPI/HTTP/ProcessStore.

`ConsumerPolicy` (dataclass) — минимальный набор полей для критерия 3.4:

- `consumer_id: str` — идентификатор системы-потребителя;
- `enabled: bool` — вкл/откл обращения в модуль;
- `enabled_pii_types: frozenset[str]` — перечень типов ПДН для маскирования;
- `allow_demasking: bool` — право демаскирования.

Расширения (не обязательны для базового балла 3.4, добавляются позже):

- `min_confidence: float` — порог срабатывания;
- `masking_mode: Literal["typed_placeholder", "synthetic", "format_preserving"]` —
  вид маски (доп. плюс 3.7);
- `degradation: Literal["fail_closed", "rule_only"]` — явно разрешённая деградация;
- `combination_rules: tuple[...]` — контекстные комбинации (задел для Трека E).

`PolicyRegistry`:

- загрузка из JSON-файла при старте;
- `resolve(consumer_id: str | None) -> ConsumerPolicy` — возвращает default profile
  `alfasonar` для `/process` и для неизвестных/неавторизованных вызовов;
- `is_allowed(consumer_id, api_key) -> bool` — allowlist-проверка для продуктового API.

### 2. Формат конфига — JSON-файл

Конфиг policy хранится в JSON-файле (путь через env `POLICY_CONFIG_PATH`). Файл
перечитывается по mtime или TTL-кэшу, чтобы менять настройки **без редеплоя и без
перезапуска** сервиса. Env-переменные используются только для простых флагов
(например, путь к файлу, вкл/откл allowlist).

### 3. Идентификация потребителя — как в Kong

Разделяем секрет и идентификатор потребителя (паттерн Kong key-auth):

- **API-ключ** (секрет) — аутентификация «кто ты». Передаётся в заголовке
  `Authorization: Bearer <key>` или `X-API-Key`.
- **Consumer ID** (не секрет) — идентификация «какой профиль применить». Передаётся
  в заголовке `X-Consumer-ID`.

`PolicyRegistry.is_allowed` проверяет API-ключ и маппит его на consumer_id; профиль
выбирается по consumer_id. Для `/process` идентификация не применяется — всегда
default profile `alfasonar`.

### 4. Default profile `alfasonar` для `/process`

`/process` приходит без auth headers (AlfaSonar). Он использует default profile
`alfasonar`, который:

- включает все 17 обязательных категорий ПДН;
- разрешает демаскирование (контракт требует пары mask/demask);
- использует typed placeholders (ADR-0003);
- применяет fail-closed при недоступности детектора.

Это профиль маскирования для автопроверки, а не анализатор кода.

### 5. Затрагиваемые компоненты

- `app/policy.py` — новый модуль (ConsumerPolicy + PolicyRegistry).
- `app/main.py` — создать PolicyRegistry в lifespan; allowlist для
  `/v1/chat/completions`; default profile для `/process`.
- `app/pii_engine.py` — принимать profile (enabled types, threshold, mode).
- `app/pii.py` — опциональные параметры `PIIMasker` (enabled types, masking mode)
  с дефолтами, сохраняющими текущее поведение.
- `app/process_service.py` — прокинуть default profile в engine (без изменения
  контракта `/process`).
- `app/errors.py` — добавить `ForbiddenError` (403) для allowlist.
- `docker-compose.yml` — путь к JSON-конфигу policy.

## Последствия

Плюсы:

- per-consumer настройка без правки ядра (критерий 3.4);
- allowlist для продуктового API (критерий 3.6, SECURITY.md);
- `/process` не блокируется auth (контракт AlfaSonar);
- единое PII-ядро сохраняется — нет второго pipeline;
- конфиг меняется без редеплоя.

Минусы:

- `PIIMasker`/`PIIMaskingEngine` усложняются опциональными параметрами;
- нужен механизм перечитывания JSON-конфига;
- риск случайного применения allowlist к `/process` — требуется тест.

## Не делать

- Не применять allowlist к `/process` (сломает AlfaSonar).
- Не менять контракт `/process` (schema, state machine).
- Не создавать второй PII pipeline.
- Не переносить policy-логику в `app/pii.py`.
- Не делать format-preserving/synthetic masking default для `alfasonar` (ADR-0003).