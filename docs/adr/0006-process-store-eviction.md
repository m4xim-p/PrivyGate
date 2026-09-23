# ADR-0006: Оптимизация ProcessStore eviction через индекс истечения

- Статус: Accepted
- Дата: 2026-09-23

## Контекст

Трек C (производительность) и критерий 3.5 требуют latency ≤0.5s при 1000 RPS.
CPU-профиль (py-spy, 5-мин прогон, `docs/benchmarks/load-test-baseline.md`)
показал, что bottleneck — **`ProcessStore._evict_expired_locked` (78.77%
активного CPU)**, а не маскирование/детекторы (6.69%). Worker threads
маскирования в основном idle (70% сэмплов).

Причина: `_evict_expired_locked` вызывается при каждом
`get_or_create_pending`/`get`/`is_tombstoned` под `asyncio.Lock` и сканирует
**все** сессии (O(n) на запрос). При 1000 RPS store накапливает тысячи сессий
(ACTIVE TTL 900s), и суммарная стоимость становится O(n²), блокируя event loop.

ADR-0005 (кэширование детекторов) решал не тот bottleneck: детекторы — лишь
6.69% активного CPU. Он переведён в `Superseded`; кэш детекторов остаётся
вторичной оптимизацией после лечения основного bottleneck.

## Источники решения

- Приложения A/B: [`../source/track-specification.txt`](../source/track-specification.txt).
- Критерии оценивания: [`../source/evaluation-criteria.md`](../source/evaluation-criteria.md).
- Сохранённые ответы организаторов:
  [`../source/organizer-clarifications.txt`](../source/organizer-clarifications.txt).
- `SECURITY.md` (bounded store, fail-closed), `docs/architecture.md`
  (single-process baseline, общее PII-ядро), `docs/evaluation-contract.md`
  (state machine, idempotency, 409/410/429).
- ADR-0002 (in-memory store, single-process), ADR-0003 (typed placeholders),
  ADR-0004 (policy registry), ADR-0005 (кэш детекторов, superseded).
- Benchmark: [`../benchmarks/load-test-baseline.md`](../benchmarks/load-test-baseline.md).

## Решение

### 1. Индекс истечения по времени (min-heap)

Заменить полный скан `_evict_expired_locked` на min-heap по `expires_at`:

- Каждая сессия хранит `expires_at` (время истечения) и `version` (generation).
- Heap хранит кортежи `(expires_at, version, payload_id)`.
- При создании ACTIVE сессии в heap добавляется событие с `expires_at =
  created_at + active_ttl`.
- При ACTIVE → COMPLETED обновляется `expires_at = completed_at +
  completed_ttl` и увеличивается `version`. Старое событие в heap остаётся, но
  игнорируется по несовпадению `version` (lazy deletion).

### 2. Ленивая проверка TTL при обращении к payload_id

При обращении к конкретному `payload_id` атомарно проверяется его `expires_at`:

- Если `session.expires_at <= now` — сессия истекла: переводится в tombstone,
  удаляется из `_sessions`, `current_bytes` уменьшается, возвращается `410 Gone`.
- Это заменяет полный скан на O(1) проверку конкретной сессии.

### 3. Eviction перед проверкой capacity

Перед проверкой `max_entries`/`max_bytes` из heap выталкиваются уже истёкшие
записи (пока `heap[0].expires_at <= now`), удаляя соответствующие сессии и
освобождая capacity. Это O(k log n), где k — число истёкших, а не O(n).

### 4. Фоновая очистка (дополнительно)

Отдельная asyncio-задача (по аналогии с `EventLoopDelaySampler`) периодически
выталкивает истёкшие записи из heap **небольшими ограниченными порциями**
(например, до N записей за тик), чтобы не блокировать event loop. Запускается в
lifespan `main.py`, останавливается при shutdown.

### 5. Сохранение контракта

Контракт `ProcessStore` (get_or_create_pending, publish_active, complete, get,
release_pending, is_tombstoned, expire_all) **не меняется**. Сохраняются:

- точные entry/byte bounds (`max_entries`, `max_bytes`, `current_bytes`);
- pending futures и retry-семантика (повтор исходного payload → та же маска,
  повтор masked → original);
- ACTIVE/COMPLETED lifecycle и разные TTL;
- tombstone и `410 Gone` для истёкших ID;
- `last_accessed_at` обновляется при `get`, но **не продлевает** TTL
  (retry не продлевает срок жизни — тест `test_retry_does_not_extend_ttl_forever`).

### 6. Пересчёт ёмкости store

`max_entries = 25000` недостаточно для устойчивых 1000 HTTP RPS при COMPLETED
TTL 120s. Оценка: при 1000 RPS и паре mask/demask (~500 пар/с) за 120s
накапливается ~60 000 COMPLETED сессий. Значение `max_entries` увеличивается
(например, до 200 000) с учётом `max_bytes` (512 MiB) и bounded store
(SECURITY.md). Точное значение фиксируется в конфигурации и проверяется
benchmark.

### 7. Tombstone TTL и bounded tombstone store

Исходный `tombstone_ttl = 3600s` (1 час) избыточен: tombstone должен быть
**краткоживущим** (ADR-0002) и покрывать только парный вызов и retries после
ошибки/429 (evaluation-contract). При 1000 RPS за 1 час накопится ~3.6M
tombstones, что гарантированно превышает лимит 50k и приводит к принудительному
вытеснению живых tombstones → нарушение защиты `410` (повторный `payload_id`
принимается как новый).

Решение:

- `tombstone_ttl = 60s` — небольшой запас "висяка" после COMPLETED TTL (120s).
  COMPLETED уже покрывает retry после потерянного demasking response; tombstone
  60s — дополнительный буфер, чтобы не принять повторный `payload_id` как новый.
- `tombstone_max_entries = 100 000` (по умолчанию, через env
  `PROCESS_TOMBSTONE_MAX_ENTRIES`). Tombstone крошечный (~40 байт), поэтому
  лимит можно сделать большим без риска OOM.
- **Действующие tombstones никогда не вытесняются рано.** `_trim_tombstones_locked`
  удаляет только истёкшие по TTL. При достижении лимита новые `payload_id`
  отклоняются с `429` + `Retry-After` (в `get_or_create_pending`), пока tombstones
  не истекут по TTL. Это сохраняет защиту `410` для всех действующих ID.
- `tombstone_ttl` и `tombstone_max_entries` прокидываются из env
  (`PROCESS_TOMBSTONE_TTL_SECONDS`, `PROCESS_TOMBSTONE_MAX_ENTRIES`), а не
  скрыты дефолтами.

**Расчёт безопасного лимита:** `tombstone_max_entries ≥ payload_id/с ×
tombstone_ttl`. Для 2100 RPS: 1050 × 60 = 63 000 < 100 000 (запас 1.6x). Для
1000 RPS: 500 × 60 = 30 000 — ещё меньше. При превышении лимита новые ID
получают `429`, а не теряют защиту `410`.

### 8. Затрагиваемые компоненты

- `app/process_store.py` — min-heap индекс, ленивая проверка TTL, eviction
  перед capacity, фоновая очистка, tombstone TTL 180s, callback ранних
  вытеснений.
- `app/main.py` — запуск/остановка фоновой eviction-задачи, env для tombstone.
- `app/metrics.py` — счётчики `privygate_store_evictions_total`,
  `privygate_store_tombstones_evicted_early_total`.
- `app/pii_engine.py` — кэш детекторов (вторично, из ADR-0005, после основного
  фикса).
- `docker-compose.yml` / env — `PROCESS_STORE_MAX_ENTRIES`,
  `PROCESS_TOMBSTONE_TTL_SECONDS`, `PROCESS_TOMBSTONE_MAX_ENTRIES`.

## Последствия

Плюсы:

- убирает O(n) скан из hot path: eviction становится O(k log n) по истёкшим;
- ленивая проверка TTL конкретной сессии — O(1);
- фоновая очистка не блокирует event loop;
- контракт `/process` и state machine не меняются;
- единое PII-ядро сохраняется — нет второго pipeline.

Минусы:

- min-heap добавляет сложность и требует тщательных тестов TTL для
  ACTIVE/COMPLETED (разные TTL, version/generation);
- lazy deletion оставляет устаревшие события в heap до их выталкивания
  (память, но bounded);
- увеличение `max_entries` повышает верхнюю границу памяти (контролируется
  `max_bytes`).

## Условия принятия

1. **Тесты store**: ACTIVE/COMPLETED TTL, retry не продлевает TTL, tombstone →
   410, entry/byte bounds, pending futures, version/generation при
   ACTIVE→COMPLETED, фоновая очистка порциями.
2. **Воспроизводимый benchmark**: повторный замер той же командой
   (`scripts/k6/run_benchmark.sh`), результат в
   `docs/benchmarks/load-test-baseline.md`. Цель: подтвердить рост RPS и
   снижение p95/p99, без регрессии golden dataset
   (`tests/test_golden_dataset.py`, ratchet).

## Не делать

- Не менять контракт `/process` (schema, state machine, 409/410/429).
- Не создавать второй PII pipeline.
- Не добавлять `--workers`/Redis без shared store (ADR-0002).
- Не продлевать TTL при retry (`get` обновляет только `last_accessed_at`).
- Не делать полный скан сессий на каждый запрос (исходный bottleneck).