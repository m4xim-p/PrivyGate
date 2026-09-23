# Benchmark: baseline `/process` load test (Трек C)

Воспроизводимый benchmark-артефакт для Трека C (производительность). Фиксирует
baseline до внедрения оптимизаций ADR-0005 и служит точкой сравнения для
подтверждения/опровержения улучшений или регрессий после изменений.

## Как воспроизвести

### 1. Собрать и запустить gateway

```bash
# Собрать образ (NER выключен — /process не использует NER по умолчанию)
docker build --build-arg INSTALL_NER=false -t privygate-gateway:bench .

# Запустить gateway отдельно (endpoint /process не зависит от LLM backends)
docker run -d --name privygate-bench \
  -p 8000:8000 \
  -e NER_ENABLED=false \
  -e PROCESS_MASK_WORKERS=64 \
  -v "$PWD/config:/config:ro" \
  privygate-gateway:bench \
  uvicorn app.main:app --host 0.0.0.0 --port 8000

# Дождаться готовности
until curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/health | grep -q 200; do sleep 1; done
```

### 2. Запустить benchmark (k6 + metrics collector)

```bash
# Полный профиль (ramp 60s + hold 60s, peak 1000 RPS, 200 VU)
scripts/k6/run_benchmark.sh --container privygate-bench

# Уменьшенный профиль для быстрой проверки
scripts/k6/run_benchmark.sh --ramp 30 --hold 30 --peak 1000 --vus 200 --container privygate-bench
```

`run_benchmark.sh` запускает k6 (HTTP-метрики: latency, RPS, retries, 429,
mask/demask отдельно) и Python-сборщик (серверные метрики: store size, event
loop delay, CPU/RSS) параллельно, выводя real-time отчёт.

### 3. Зафиксировать результат

Результат записать в этот файл (раздел «Результаты») с указанием версии кода
(commit), конфигурации и hardware. Сравнивать с предыдущим baseline по RPS и
latency (p50/p95/p99).

## Конфигурация замера

| Параметр | Значение |
|---|---|
| Версия кода (commit) | `9e40165` (до внедрения ADR-0005) |
| Образ | `privygate-gateway:bench`, `INSTALL_NER=false` |
| NER_ENABLED | `false` |
| PROCESS_MASK_WORKERS | `64` |
| Инструмент | k6 v2.3.0 (`scripts/k6/process_load.js`) + `scripts/k6/metrics_collector.py` |
| target-rps (peak) | 1000 |
| ramp-seconds | 60 (полный) / 30 (уменьшенный) |
| hold-seconds | 60 (полный) / 30 (уменьшенный) |
| connections (VU) | 200 |
| keepalive | k6 по умолчанию (HTTP/1.1 keep-alive) |
| timeout | 10 (hard timeout контракта) |
| max-retries | 2 |
| Payload | синтетический (`syntheticPayload` в `process_load.js`) |
| Hardware | macOS (darwin), Docker Desktop |

## Результаты

### Baseline (до ADR-0005) — Python load test

- Дата: 2026-09-23
- Commit: `9e40165`
- NER: off, workers=64
- Инструмент: `scripts/load_test_process.py` (120s, ramp 60s → peak 1000 RPS)

| Метрика | Значение |
|---|---|
| Total requests | 42888 |
| Successful | 42888 |
| Failed | 0 |
| Rate-limited (429) | 0 |
| Total duration | 121.5 s |
| Requests/sec | 353.01 |
| Ramp-up RPS | 457.8 |
| Peak RPS | 250.8 |
| Masking mean / p50 / p95 / p99 | 0.4646 / 0.4810 / 0.9251 / 1.2004 s |
| Demasking mean / p50 / p95 / p99 | 0.4553 / 0.4672 / 0.9107 / 1.1690 s |
| All mean / p50 / p95 / p99 | 0.4599 / 0.4740 / 0.9165 / 1.1860 s |
| Memory (container) | ~110 MiB |

### Baseline (до ADR-0005) — k6 + metrics collector

- Дата: 2026-09-23
- Commit: `9e40165`
- NER: off, workers=64
- Инструмент: `scripts/k6/run_benchmark.sh` (ramp 30s + hold 30s, peak 1000 RPS, 200 VU)

| Метрика | Значение |
|---|---|
| Total requests | 32050 |
| Successful | 32050 |
| Failed | 0 |
| Rate-limited (429) | 0 |
| Retries | 0 |
| Requests/sec | 528.72 |
| Masking mean / p50 / p95 / p99 | 336.87 / 350.66 / 750.58 / 862.35 ms |
| Demasking mean / p50 / p95 / p99 | 195.40 / 205.00 / 438.02 / 521.05 ms |
| All mean / p50 / p95 / p99 | 266.14 / 252.65 / 685.73 / 831.77 ms |
| Round-trip checks | 100% (0 failed) |

**Серверные метрики (real-time, пик нагрузки):**

| Метрика | Значение |
|---|---|
| CPU процесса | достигает 100% (~13s после старта) |
| Event loop delay | растёт 0ms → 83ms → 218ms → 334ms |
| RSS | 52 MiB → 78 MiB |
| Store sessions | 0 → 15894 (TTL 900s для ACTIVE) |
| Store bytes | 0 → 12.2 MB |
| Retries / 429 | 0 / 0 |

### Baseline (до ADR-0005) — 5-минутный прогон + CPU-профиль

- Дата: 2026-09-23
- Commit: `9e40165`
- NER: off, workers=64
- Инструмент: `scripts/k6/cpu_profile.sh` (ramp 60s + hold 240s, peak 1000 HTTP RPS, 200 VU)
- Профиль: `py-spy record` внутри контейнера, анализ `scripts/k6/analyze_profile.py`

| Метрика | Значение |
|---|---|
| Total requests | 66608 (33304 mask + 33304 demask) |
| Successful | 66608 |
| Failed | 0 |
| Rate-limited (429) | 0 |
| Retries | 0 |
| Requests/sec | 221.18 |
| **Dropped iterations** | **101695** (k6 не смог сгенерировать target 500 итераций/с) |
| Masking mean / p50 / p95 / p99 | 1.01s / 1.10s / 1.80s / 2.35s |
| Demasking mean / p50 / p95 / p99 | 550.84 / 567.66 / 1.07s / 1.49s |
| All mean / p50 / p95 / p99 | 781.18 / 717.15 / 1.67s / 2.18s |
| Round-trip checks | 100% (0 failed) |

**CPU-профиль (py-spy, активные сэмплы без idle worker threads):**

| Категория | Доля активного CPU |
|---|---|
| **ProcessStore** (`_evict_expired_locked`) | **78.77%** |
| HTTP (uvicorn/asyncio/FastAPI) | 14.52% |
| Detectors (pii.py, address_detector) | 6.69% |
| Other | 0.02% |

**Ключевое открытие:** bottleneck — **НЕ маскирование (детекторы), а
`ProcessStore._evict_expired_locked`** (78% активного CPU). При 1000 RPS store
накапливает тысячи сессий (TTL 900s для ACTIVE), и `_evict_expired_locked`
вызывается при каждом `get_or_create_pending`/`get`, сканируя **все** сессии
(O(n) на запрос → O(n²) суммарно), блокируя event loop. Worker threads
маскирования в основном idle (70% сэмплов — idle), т.к. детекторы быстрые.

### После ADR-0006 (min-heap eviction)

- Дата: 2026-09-23
- Commit: после внедрения ADR-0006 (min-heap индекс истечения, фоновая eviction)
- NER: off, workers=64
- Инструмент: `scripts/k6/run_benchmark.sh` (ramp 30s + hold 30s, peak 1000 HTTP RPS, 200 VU)

| Метрика | Baseline (до) | После ADR-0006 | Улучшение |
|---|---|---|---|
| Requests/sec | 528.72 | **748.81** | +42% |
| Masking p50 / p95 / p99 | 350.66 / 750.58 / 862.35 ms | **1.74 / 12.91 / 129.87 ms** | ~200x / ~58x / ~6.6x |
| Demasking p50 / p95 / p99 | 205.00 / 438.02 / 521.05 ms | **0.885 / 5.13 / 41.15 ms** | ~230x / ~85x / ~13x |
| All p50 / p95 / p99 | 252.65 / 685.73 / 831.77 ms | **1.44 / 8.24 / 77.25 ms** | ~175x / ~83x / ~11x |
| Dropped iterations | 101695 (5-мин) | **33** | ~3000x |
| Event loop delay | до 334ms | **0ms** | — |
| CPU процесса | 100% | **~68-76%** | — |
| Retries / 429 / errors | 0 / 0 / 0 | 0 / 0 / 0 | — |

**CPU-профиль после ADR-0006 (py-spy):**

| Категория | До | После |
|---|---|---|
| **ProcessStore** | **78.77%** | **0.70%** |
| HTTP (uvicorn/asyncio/FastAPI) | 14.52% | 13.48% |
| Detectors (pii.py, address_detector) | 6.69% | 4.53% |
| Idle (worker threads) | 70% | 81.28% |

**Результат:** bottleneck устранён. `ProcessStore._evict_expired_locked` упал с
78.77% до 0.70% активного CPU. Event loop delay 0ms, latency упала на порядки,
dropped_iterations с 101695 до 33. RPS вырос с 528 до 748. Остаточное
ограничение RPS (~748, не 1000) — не ProcessStore, а worker threads маскирования
(64) / HTTP-обработка; процесс не упирается в CPU (68-76%).

### Полный 5-минутный прогон (после ADR-0006 + tombstone heap)

- Дата: 2026-09-23
- Commit: после ADR-0006 + исправление O(n)-очистки tombstones (min-heap)
- NER: off, workers=64
- Инструмент: `scripts/k6/run_benchmark.sh` (ramp 60s + hold 240s, peak 1000 HTTP RPS, 200 VU)

**Общий прогон (300s):**

| Метрика | Значение |
|---|---|
| Total requests | 269870 (134935 mask + 134935 demask) |
| Successful | 269870 |
| Failed / Retries / 429 | 0 / 0 / 0 |
| Requests/sec | 899.56 |
| Dropped iterations | 63 |
| Masking p50 / p95 / p99 | 1.57 / 7.68 / 101.11 ms |
| Demasking p50 / p95 / p99 | 0.775 / 3.50 / 24.47 ms |
| All p50 / p95 / p99 | 1.29 / 5.44 / 57.91 ms |
| Round-trip checks | 100% (0 failed) |

**Hold-фаза (240s) — achieved метрики:**

| Метрика | Значение |
|---|---|
| Mask count (hold) | 119938 за 240s = **499.7 mask/s** |
| **Achieved HTTP RPS (hold)** | **~999.5** (119938×2 / 240) |
| Masking p50 / p95 / p99 (hold) | 1.56 / 9.00 / 112.01 ms |
| Demasking p50 / p95 / p99 (hold) | 0.772 / 3.95 / 26.40 ms |
| Dropped iterations (hold) | ~0 (63 всего, в основном ramp) |

**Серверные метрики (real-time):**

| Метрика | Значение |
|---|---|
| Event loop delay | **0ms** (макс 12ms в одном тике, без всплесков) |
| Tombstones | **50000** (bounded, стабильно на лимите) |
| Sessions | ~59937 (стабильно, bounded) |
| CPU | ~65-76% |
| RSS | ~182 MiB (стабильно, bounded) |
| Retries / 429 / errors | 0 / 0 / 0 |

**Результат:** hold-фаза достигает **~1000 HTTP RPS** (999.5), latency p95 < 10ms,
dropped_iterations ~0 на hold. O(n)-очистка tombstones исправлена (min-heap):
tombstones bounded на 50000, event loop delay 0ms без всплесков. Heap ограничен
(≤2 события на сессию, включая stale entries).

### Выводы baseline

- При пике 1000 RPS система не справлялась: RPS ограничен ~528 (k6, 30s) / ~221
  (k6, 5-мин) / ~353 (Python load test) при 200 VU.
- **CPU-профиль показал: bottleneck — `ProcessStore._evict_expired_locked`
  (78% активного CPU), а не маскирование.** Store eviction — O(n) на запрос
  при большом store, блокирует event loop.
- **ADR-0006 (min-heap eviction) устранил bottleneck:** ProcessStore упал с
  78.77% до 0.70% активного CPU, event loop delay 0ms, latency упала на порядки.
- **Полный 5-минутный прогон подтверждает ~1000 HTTP RPS на hold** (999.5),
  mask p95 9ms, dropped_iterations ~0 на hold. O(n)-очистка tombstones исправлена
  (min-heap): tombstones bounded на 50000, event loop delay 0ms без всплесков.
- Вывод про worker threads маскирования и ADR-0005 (кэш детекторов) **пока не
  подтверждён** — требуется отдельный CPU-профиль на hold-фазе при 1000 RPS.

## Правило сравнения

- Улучшение: рост RPS и/или снижение p50/p95/p99 при той же конфигурации.
- Регрессия: падение RPS и/или рост latency при той же конфигурации.
- Любое изменение производительности фиксируется в этом файле с commit и датой.
- Оптимизация принимается только если не регрессирует golden dataset
  (`tests/test_golden_dataset.py`, ratchet).