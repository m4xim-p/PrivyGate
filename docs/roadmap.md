# Roadmap

Roadmap отражает порядок работ, но не заменяет требования и ADR.

## Текущее состояние

- [x] OpenAI-like streaming proxy.
- [x] Request-scoped masking и boundary-safe demasking.
- [x] Detector protocol, overlap resolution и confidence threshold.
- [x] Regex/checksum detectors для базовых идентификаторов.
- [x] Optional локальный PERSON NER с fake-backend tests.
- [x] Первый набор rule-based detectors для всех обязательных категорий.
- [x] Safe diagnostics и explicit dev-only full-body logging.
- [ ] Измеренный quality baseline по 17 категориям.
- [x] `POST /process`.

## P0 — автоматический контракт

- [x] `ProcessRequest`/`ProcessResponse`.
- [x] Атомарный in-memory `ProcessStore` с ACTIVE/COMPLETED TTL, entry и byte bounds.
- [x] Idempotent `ProcessService`.
- [x] `POST /process` без вызова LLM backend.
- [x] Retry masking/demasking, completed retention, expiry, conflict, no-PII и
  concurrency tests.
- [x] Admission control, `429` и `Retry-After`.
- [x] Safe logs без payload/mapping.
- [ ] Раздельная валидация payload bytes и estimated tokens без допущения «100k = 400 КБ».
- [x] OpenAPI-спецификация `process_api.yaml` из Приложения A.
- [ ] Автоматический drift test между `process_api.yaml`, FastAPI schema и contract models.
- [x] README curl examples.

Критерий готовности: контрактные tests проходят; первый/повторный masking и
demasking детерминированы; конфликт не изменяет session.

## P1 — измеримое качество

- [ ] Синтетический golden dataset по 17 категориям.
- [ ] Positive, negative, mixed, case и format variants.
- [ ] Precision/recall/F1 и false-positive report по типам.
- [ ] Exact round-trip report.
- [ ] Recall-first tuning: пропуски дороже лишних масок, но без blanket masking.
- [ ] Проверка точности границ spans и исключение служебных слов из маски.
- [ ] Typed placeholders оставить default `alfasonar`; общий `MaskingPolicy` перенести
  в per-consumer расширения.
- [ ] Исправление границ context detectors и overlap conflicts.
- [ ] Systematic suppression для historical persons и organization addresses.
- [ ] Combination policy: PIN/CVV вместе с картой.

Критерий готовности: воспроизводимый отчёт приближается к целевым 95%, а каждая
регрессия закреплена тестом.

## P2 — производительность и большие тексты

- [x] Rate-controlled load test для `/process` (`scripts/load_test_process.py`).
- [ ] Подтверждение пика 1000 RPS на целевой конфигурации (сейчас ~628 RPS при keepalive=10; 1000 RPS НЕ доказан).
- [ ] Профиль нагрузки: ступенчатый разгон до 1000 RPS с удержанием, до 200 connections;
  дополнительно сверять средний RPS (~330 по уточнению организаторов).
- [ ] Mean/p50/p95/p99, RPS, TPS, error/429 и cache-hit metrics.
- [ ] Равное количество masking и demasking запросов с последовательной парой.
- [ ] Baseline single-process in-memory store.
- [ ] Bounded NER concurrency и backpressure.
- [ ] Chunked/bounded обработка до 100 000 токенов.
- [ ] Проверка memory usage и timeout behavior.
- [ ] ONNX/quantization или shared store только при подтверждённом bottleneck.

Критерий готовности: сохранён benchmark с версией кода, конфигурацией, hardware и
размером входа; целевой путь не превышает hard timeout.

## P3 — политики потребителей и надёжность

- [ ] Consumer policy registry.
- [ ] Allowlist и optional API keys для продуктового API; не блокировать evaluation
  `/process`, который приходит без auth headers.
- [ ] Per-consumer PII types, thresholds и masking mode.
- [ ] Per-consumer demasking permission.
- [ ] Fail-closed и явно разрешённая degradation policy.
- [ ] Offline/pinned NER deployment.
- [ ] Failure injection tests.

## P4 — сдача и демонстрация

- [ ] Лёгкий ZIP без `.git`, environments, caches, models и build outputs.
- [ ] Доступный evaluation URL.
- [ ] Инструкция настройки не более пяти предложений.
- [ ] Архитектурная схема и результаты quality/load tests.
- [ ] Демо нормального, trap, policy и failure сценариев.
- [ ] Список известных ограничений и план развития.
- [ ] Проверка complexity/DRY/KISS/deprecated APIs перед ZIP.
