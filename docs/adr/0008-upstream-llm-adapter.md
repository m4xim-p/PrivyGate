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

### 1. `UpstreamClient` (app/upstream.py)

Новый upstream-адаптер для OpenAI-compatible endpoint:

- `stream_chat(payload)` — POST на `{base_url}/v1/chat/completions`, стриминг.
- Поддерживает два формата по `content-type`:
  - `text/plain` (mock) — демаскирует raw текст;
  - `text/event-stream` (реальная модель) — парсит SSE, демаскирует
    `choices[0].delta.content`, пересобирает SSE.
- Auth-заголовок `Authorization: Bearer` из env (`UPSTREAM_API_KEY`).
- Override модели через `UPSTREAM_MODEL`.

### 2. Гибрид: mock + реальная модель

`BACKEND_URLS` — mock backend (text/plain). `UPSTREAM_URL` — опциональная
реальная модель (SSE). Когда `UPSTREAM_URL` задан, она добавляется в ротацию
`RoundRobinRouter`, чтобы демо могло сравнить ответы mock и реальной модели.

### 3. SSE-демаскирование

`StreamingDemasker` буферизует placeholder'ы, пересекающие границы чанков. Для
SSE: парсится каждое событие, `delta.content` передаётся в `StreamingDemasker`,
демаскированный результат вставляется обратно в SSE-событие. Если demasker
буферизует (placeholder разбит между событиями), событие пропускается (пустой
delta), полный placeholder приходит позже.

### 4. Fail-closed

При недоступности upstream — `502` с безопасным сообщением, без раскрытия
текста (SECURITY.md §5).

### 5. Затрагиваемые компоненты

- `app/upstream.py` — новый `UpstreamClient`.
- `app/main.py` — инициализация `UpstreamClient` в lifespan, env
  `UPSTREAM_URL`/`UPSTREAM_API_KEY`/`UPSTREAM_MODEL`.
- `app/proxy.py` — остаётся (StreamingDemasker, UpstreamStream), но транспорт
  вынесен в `UpstreamClient`.
- `docker-compose.yml` — env для реальной модели.
- `docs/` — README, architecture, requirements.

## Последствия

Плюсы:

- реальная модель для демо без изменения PII-ядра;
- mock остаётся fallback для локальных тестов;
- SSE-демаскирование сохраняет корректность (placeholder не пересекает границы);
- единое PII-ядро — нет второго pipeline.

Минусы:

- SSE-парсинг добавляет сложность;
- зависимость от внешнего API (сеть, лимиты);
- реальная модель может отвечать дольше mock.

## Условия принятия

1. **Тесты**: text/plain демаскирование, SSE-демаскирование, fail-closed,
   отсутствие PII в логах, round-trip.
2. **check.sh** зелёный.

## Не делать

- Не менять PII-ядро и контракт `/process`.
- Не логировать API-ключ и текст запроса/ответа.
- Не создавать второй PII pipeline.
- Не требовать реальную модель для автопроверки (она не обязательна).