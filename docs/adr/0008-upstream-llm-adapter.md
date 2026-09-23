# ADR-0008: Upstream LLM adapter (реальная модель + mock fallback)

- Статус: Accepted
- Дата: 2026-09-23

## Контекст

`/v1/chat/completions` — продуктовый OpenAI-like proxy. Сейчас upstream — три
mock-сервиса (`mock_llm/main.py`), возвращающие `text/plain`. Задача — подключить
реальную LLM (OpenAI-compatible, SSE) для демо, сохранив mock как fallback.

Организаторы подтвердили (organizer-clarifications, вопрос 7): реальное
проксирование в LLM **не обязательно** для баллов, но улучшает демо (критерий
3.8) и «внедряемость» (критерии финалистов). AlfaGen как внешняя LLM допустим.

## Источники решения

- `docs/architecture.md` (разрешённые зависимости, единое PII-ядро).
- `organizer-clarifications.txt` (вопрос 7 — реальная LLM не обязательна).
- `SECURITY.md` (§5 fail-closed, §6 зависимости, §7 секреты через env).
- ADR-0004 (allowlist для product API).

## Решение

### 1. `ModelRegistry` (app/model_registry.py)

Внутренний конфиг моделей (`config/models.json`), обновляется без редеплоя
(перечитывается по TTL, как PolicyRegistry):

```json
{
  "models": [
    { "name": "gpt-4o", "api_base": "https://api.openai.com/v1", "model": "gpt-4o" }
  ]
}
```

`resolve(model_name)` → конфигурацию модели (api_base, model) или `None`.

### 2. Маршрутизация по `model`

Клиент передаёт `model` (имя из конфига). Прокси по `model` находит
конфигурацию и маршрутизирует на `api_base`. Неизвестная модель → `404`.

### 3. `X-Model-API-Key` — ключ клиента

Клиент передаёт свой API-ключ для модели в заголовке `X-Model-API-Key`.
Прокси использует его как `Authorization: Bearer` при запросе к upstream.
Это отдельный заголовок от allowlist (`Authorization`/`X-API-Key`), поэтому
не конфликтует с ADR-0004.

### 4. Квоты токенов per-client

`ConsumerPolicy.max_tokens_per_request` — лимит токенов на запрос. Если
`body.max_tokens` превышает квоту → `429`.

### 5. `UpstreamClient` (app/upstream.py)

- `stream_chat(payload, api_key)` — POST на `{base_url}/v1/chat/completions`.
- Поддерживает два формата по `content-type`:
  - `text/plain` (mock) — демаскирует raw текст;
  - `text/event-stream` (реальная модель) — парсит SSE, демаскирует
    `choices[0].delta.content`, пересобирает SSE.
- `api_key` (ключ клиента) передаётся на каждый запрос.

### 6. SSE-демаскирование

`StreamingDemasker` буферизует placeholder'ы, пересекающие границы чанков. Для
SSE: парсится каждое событие, `delta.content` передаётся в `StreamingDemasker`,
демаскированный результат вставляется обратно в SSE-событие. Если demasker
буферизует (placeholder разбит между событиями), событие пропускается (пустой
delta), полный placeholder приходит позже.

### 7. Fail-closed

При недоступности upstream — `502` с безопасным сообщением, без раскрытия
текста (SECURITY.md §5).

### 8. Затрагиваемые компоненты

- `app/model_registry.py` — новый `ModelRegistry`.
- `app/upstream.py` — `UpstreamClient` (api_key на запрос).
- `app/main.py` — маршрутизация по `model`, `X-Model-API-Key`, квоты.
- `app/policy.py` — `max_tokens_per_request`.
- `config/models.example.json` — пример конфига моделей.
- `docs/` — README, architecture, requirements.

## Последствия

Плюсы:

- маршрутизация по `model` (как LiteLLM/One API);
- ключ клиента передаётся в запросе, не хранится на прокси;
- квоты токенов per-client;
- mock остаётся fallback для локальных тестов;
- SSE-демаскирование сохраняет корректность;
- единое PII-ядро — нет второго pipeline.

Минусы:

- SSE-парсинг добавляет сложность;
- зависимость от внешнего API (сеть, лимиты);
- реальная модель может отвечать дольше mock.

## Условия принятия

1. **Тесты**: text/plain демаскирование, SSE-демаскирование, маршрутизация по
   `model`, `X-Model-API-Key`, квоты токенов, неизвестная модель → 404,
   fail-closed, отсутствие PII в логах.
2. **check.sh** зелёный.

## Не делать

- Не менять PII-ядро и контракт `/process`.
- Не логировать API-ключ и текст запроса/ответа.
- Не создавать второй PII pipeline.
- Не требовать реальную модель для автопроверки (она не обязательна).
- Не ломать существующие заголовки allowlist (ADR-0004) и политики (ADR-0007).