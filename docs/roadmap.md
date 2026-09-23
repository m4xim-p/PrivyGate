# Roadmap

Roadmap отражает порядок работ, но не заменяет требования и ADR.

## Принцип приоритизации

Порядок задач определён по двум осям:

1. **Влияние на баллы жюри** (критерии из `docs/source/evaluation-criteria.md`):
   задачи с наибольшим потенциальным приростом баллов идут раньше.
2. **Изолированность** — задачи сгруппированы в треки по затрагиваемым файлам,
   чтобы разные люди могли работать параллельно без конфликтов.

Оценка баллов — экспертная готовность по критериям, не официальная метрика
организаторов (формула span-based score не раскрыта).

## Текущее состояние

- [x] OpenAI-like streaming proxy.
- [x] Request-scoped masking и boundary-safe demasking.
- [x] Detector protocol, overlap resolution и confidence threshold.
- [x] Regex/checksum detectors для базовых идентификаторов.
- [x] Optional локальный PERSON NER с fake-backend tests.
- [x] Первый набор rule-based detectors для всех обязательных категорий.
- [x] Safe diagnostics и explicit dev-only full-body logging.
- [x] `POST /process` (контракт P0 закрыт).
- [ ] Измеренный quality baseline по 17 категориям (есть строгий harness, но
  целевые 95% не достигнуты).

## Трек A — политики потребителей и надёжность (критерий 3.4, до +2.5 балла)

**Файлы:** новый `app/policy.py`, `app/main.py`, `app/process_service.py`.
**Изолирован:** не трогает `app/pii.py` — можно делать параллельно с Треком B.
**Почему раньше:** самый большой прирост баллов (+2.5) при относительно небольшой
работе; сейчас блок почти пустой (оценка 1.5/4).

- [x] Consumer policy registry (новый `app/policy.py`).
- [x] Автоматический drift test между `process_api.yaml`, FastAPI schema и contract
  models (контракт `/process`).
- [x] Allowlist и optional API keys для продуктового API; не блокировать evaluation
  `/process`, который приходит без auth headers.
- [x] Per-consumer PII types и thresholds (enabled/excluded_pii_types, min_confidence).
- [x] Per-consumer masking mode: synthetic (фиксированная синтетика) и
  format_preserving (сохранение длины/разделителей) реализованы.
- [x] Per-consumer demasking permission (allow_demasking).
- [x] Fail-closed для `/process` при недоступности детектора (5xx, не raw текст).
- [x] Degradation policy `rule_only`: при недоступности ML/NER-детектора
  продолжается rule-based маскирование.
- [x] Offline/pinned NER deployment: модель скачивается при сборке образа в
  `/models/ner`, в runtime грузится с `local_files_only=True` (без сети);
  HF_HUB_OFFLINE/TRANSFORMERS_OFFLINE задаются в коде (NER_OFFLINE, по умолчанию true).
- [x] Failure injection tests (fail-closed на недоступном детекторе).

Критерий готовности: per-consumer настройка работает без правки ядра; evaluation
`/process` не блокируется auth; fail-closed при недоступности детектора.

## Трек B — качество детекторов (критерии 3.1 и 3.3, до +2.5 балла)

**Файлы:** `app/pii.py`, `tests/test_*_detectors.py`, `docs/quality-baseline.md`.
**Изолирован:** один человек, т.к. все детекторы в общем `app/pii.py`.
**Почему раньше:** главный риск для допуска и баллов — низкий api recall (FN=112)
может дать утечку ПДН в LLM (стоп-сигнал финалистов) и снижение по 3.1.

- [ ] Поднять api recall (rule-based 0.536; с NER 0.578, F1 0.655 — улучшение, но
  всё ещё ниже целевого): FN в сложных предложениях (PASSPORT_ISSUE_DATE, PERSON,
  ADDRESS, DATE_OF_BIRTH, INN).
- [ ] Снизить FP: PHONE (горячая линия/служба поддержки), ADDRESS (организации),
  EMAIL, DRIVING_LICENSE.
- [ ] Исправление границ context detectors и overlap conflicts.
- [ ] Systematic suppression для historical persons и organization addresses.
- [ ] Проверка точности границ spans и исключение служебных слов из маски.
- [ ] Recall-first tuning: пропуски дороже лишних масок, но без blanket masking.
- [ ] Exact round-trip report.
- [ ] Precision/recall/F1 и false-positive report по типам.

Критерий готовности: api recall поднят без регрессии golden/extended (ratchet
`tests/test_golden_dataset.py` зелёный); каждая регрессия закреплена тестом.

## Трек C — производительность и большие тексты (критерий 3.5, до +2 балла)

**Файлы:** `scripts/load_test_process.py`, конфигурация, `app/process_service.py`,
`app/pii_engine.py` (ADR-0005).
**Изолирован:** не трогает `app/pii.py` — можно делать параллельно с Треком B.
**Почему раньше:** 1000 RPS не подтверждён (~628); latency ≤ 0.5 с — целевой
уровень критерия 3.5.

- [x] Rate-controlled load test для `/process` (`scripts/load_test_process.py`).
- [x] Baseline `/process` load test сохранён как воспроизводимый артефакт
  (`docs/benchmarks/load-test-baseline.md`, ADR-0005).
- [x] k6 load test (`scripts/k6/process_load.js`) + real-time metrics collector
  (`scripts/k6/metrics_collector.py`, `run_benchmark.sh`) — профиль 1000 RPS,
  ramp-up, 200 VU, mask/demask отдельно, retries, 429, store size, event loop
  delay, CPU/RSS.
- [x] CPU-профилирование (`scripts/k6/cpu_profile.sh` + `analyze_profile.py`,
  py-spy) — разбивка по ProcessStore/Detectors/HTTP.
- [x] `/metrics` endpoint (Prometheus text): счётчики mask/demask/retry/429,
  store size, event loop delay (Трек D, частично).
- [x] **Оптимизация `ProcessStore` eviction (ADR-0006)** — min-heap индекс
  истечения, ленивая проверка TTL, фоновая eviction порциями, min-heap для
  tombstones (O(n) → O(k log n)). Устранил bottleneck: ProcessStore 78.77% →
  0.70% активного CPU, event loop delay 0ms.
- [x] **Tombstone retention (ADR-0006, раздел 7)** — tombstone TTL 60s, лимит
  100k, действующие tombstones не вытесняются рано; при переполнении новые ID
  отклоняются с 429 + Retry-After (защита 410 сохраняется).
- [x] Полный 5-минутный прогон: **~1000 HTTP RPS на hold** (999.5), mask p95
  9ms, dropped_iterations ~0 на hold, tombstones bounded.
- [x] Профиль нагрузки: ступенчатый разгон до 1000 RPS с удержанием, до 200
  connections (k6 `ramping-arrival-rate`, 200 VU); средний RPS ~330 сверяется.
- [x] Равное количество masking и demasking запросов с последовательной парой
  (k6 `processPair`: mask → demask на один `payload_id`).
- [x] Baseline single-process in-memory store (benchmark-артефакт,
  `docs/benchmarks/load-test-baseline.md`).
- [x] Проверка memory usage (RSS bounded в benchmark) и timeout behavior.
- [ ] Кэширование предсобранных детекторов в `PIIMaskingEngine` (ADR-0005,
  superseded) — **вывод про worker threads пока не подтверждён**, требуется
  CPU-профиль на hold-фазе при 1000 RPS.
- [ ] Подтверждение пика 1000 RPS на целевой конфигурации (после ADR-0006:
  hold-фаза ~999.5 HTTP RPS, mask p95 9ms, dropped_iterations ~0 — близко к
  цели, требуется финальное подтверждение).
- [ ] Bounded NER concurrency и backpressure.
- [ ] Chunked/bounded обработка до 100 000 токенов.
- [ ] ONNX/quantization или shared store только при подтверждённом bottleneck.

Критерий готовности: сохранён benchmark с версией кода, конфигурацией, hardware и
размером входа; целевой путь не превышает hard timeout.

## Трек D — metrics endpoint и безопасность (критерий 3.6, до +1 балла)

**Файлы:** `app/main.py`, новый `app/metrics.py`.
**Изолирован:** не трогает `app/pii.py` — можно делать параллельно с Треком B.
**Почему здесь:** небольшой прирост баллов (+0.5-1), но нужен для демо и критерия 3.6.

- [x] Metrics endpoint (RPS/TPS/latency) + Mean/p50/p95/p99, error/429 и cache-hit
  metrics. Реализован `/metrics` (Prometheus text); RPS/latency собираются k6.
- [x] `/metrics` endpoint (Prometheus text): счётчики mask/demask/retry/429,
  store size (sessions/pending/tombstones/bytes), event loop delay, uptime.
- [ ] Логирование выявленных типов ПДН по каждому запросу (подтвердить).

Критерий готовности: `/metrics` отдаёт RPS/TPS/latency; логи содержат типы ПДН
без raw PII.

## Трек E — дополнительные возможности (критерий 3.7, до +3 баллов)

**Файлы:** `app/pii.py` (комбинации, документы), `app/policy.py` (настройка вида).
**Зависимость:** частично от Трека A (настройка вида маскирования под систему).
**Почему здесь:** свободный критерий, по 1 баллу за возможность, максимум 3.
Самый «дешёвый» способ добрать баллы после базового качества.

- [x] Токенизация/детокенизация или замена синтетическими данными
  (`masking_mode: synthetic`, `_synthetic_value` в `app/pii.py`).
- [x] Настройка вида маскирования/токенизации под систему (зависит от Трека A)
  — per-consumer `masking_mode` (typed_placeholder / synthetic / format_preserving).
- [ ] RPS 2000 при Latency ≤ 0.5 с (зависит от Трека C).
- [ ] Идентификация документов, удостоверяющих личность, кроме паспорта РФ
  (частично: DRIVING_LICENSE, SNILS уже есть).
- [x] Контекстное маскирование по комбинации типов ПДН (PIN + номер карты),
  с настраиваемым правилом (`require_card_for_pin`).
- [x] Custom terms маскирование по потребителю (ADR-0007): per-consumer список
  терминов, маскируются только для заданной системы (`ConsumerPolicy.custom_terms`).

Критерий готовности: каждая заявленная возможность подтверждена демо и тестом,
без «для галочки» (см. критерии финалистов «Сила решения»).

## Трек F — сдача и демонстрация (критерий 3.8 и артефакты, до +1.5 балла)

**Файлы:** `README.md`, `docs/`, `DEPLOY.md`.
**Изолирован:** не трогает код — можно делать параллельно с любым треком.
**Почему здесь:** обязательные артефакты на сдачу; без них решение не учитывается.

- [x] Реальная LLM upstream (ADR-0008): `UpstreamClient` (OpenAI-compatible
  SSE/text), mock fallback, гибрид mock + реальная модель для демо.
- [ ] Лёгкий ZIP без `.git`, environments, caches, models и build outputs.
- [ ] Доступный evaluation URL.
- [ ] Инструкция настройки не более пяти предложений.
- [ ] Инструкция для жюри по проверке (тестовый текст, маскирование/демаскирование,
  логи и метрики).
- [ ] Архитектурная схема и результаты quality/load tests.
- [ ] Демо нормального, trap, policy и failure сценариев.
- [ ] Список известных ограничений и план развития.
- [x] Merge-blocking Ruff/mypy/Bandit/pip-audit gate для complexity,
  conventions, deprecated APIs, типизации, SAST и уязвимостей зависимостей.

Критерий готовности: решение запускается по README, evaluation URL доступен,
демо-сценарии показаны, артефакты собраны.

## Параллельная работа (без конфликтов)

| Трек | Файлы | Кто |
|---|---|---|
| A — политики | `app/policy.py`, `main.py`, `process_service.py` | 1 человек |
| B — качество | `app/pii.py`, тесты детекторов | 1 человек (файл общий) |
| C — производительность | `scripts/`, конфиг, `pii_engine.py` | 1 человек |
| D — metrics | `main.py`, `metrics.py` | 1 человек |
| E — доп. возможности | `pii.py`, `policy.py` | после A/B |
| F — сдача | README, docs | 1 человек |

Треки A, C, D, F не пересекаются по файлам и могут идти параллельно. Трек B —
единственный, кто трогает `app/pii.py`; E зависит от A и B.