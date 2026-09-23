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

- [ ] Consumer policy registry (новый `app/policy.py`).
- [ ] Автоматический drift test между `process_api.yaml`, FastAPI schema и contract
  models (контракт `/process`).
- [ ] Allowlist и optional API keys для продуктового API; не блокировать evaluation
  `/process`, который приходит без auth headers.
- [ ] Per-consumer PII types, thresholds и masking mode.
- [ ] Per-consumer demasking permission.
- [ ] Fail-closed и явно разрешённая degradation policy.
- [ ] Offline/pinned NER deployment.
- [ ] Failure injection tests.

Критерий готовности: per-consumer настройка работает без правки ядра; evaluation
`/process` не блокируется auth; fail-closed при недоступности детектора.

## Трек B — качество детекторов (критерии 3.1 и 3.3, до +2.5 балла)

**Файлы:** `app/pii.py`, `tests/test_*_detectors.py`, `docs/quality-baseline.md`.
**Изолирован:** один человек, т.к. все детекторы в общем `app/pii.py`.
**Почему раньше:** главный риск для допуска и баллов — низкий api recall (FN=112)
может дать утечку ПДН в LLM (стоп-сигнал финалистов) и снижение по 3.1.

- [ ] Поднять api recall (сейчас 0.536, выше порога 0.50, но низкий): FN в
  сложных предложениях (PASSPORT_ISSUE_DATE, PERSON, ADDRESS, DATE_OF_BIRTH, INN).
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

**Файлы:** `scripts/load_test_process.py`, конфигурация, `app/process_service.py`.
**Изолирован:** не трогает `app/pii.py` — можно делать параллельно с Треком B.
**Почему раньше:** 1000 RPS не подтверждён (~628); latency ≤ 0.5 с — целевой
уровень критерия 3.5.

- [x] Rate-controlled load test для `/process` (`scripts/load_test_process.py`).
- [ ] Подтверждение пика 1000 RPS на целевой конфигурации (сейчас ~628 RPS при
  keepalive=10; 1000 RPS НЕ доказан).
- [ ] Профиль нагрузки: ступенчатый разгон до 1000 RPS с удержанием, до 200
  connections; дополнительно сверять средний RPS (~330 по уточнению организаторов).
- [ ] Равное количество masking и demasking запросов с последовательной парой.
- [ ] Baseline single-process in-memory store.
- [ ] Bounded NER concurrency и backpressure.
- [ ] Chunked/bounded обработка до 100 000 токенов.
- [ ] Проверка memory usage и timeout behavior.
- [ ] ONNX/quantization или shared store только при подтверждённом bottleneck.

Критерий готовности: сохранён benchmark с версией кода, конфигурацией, hardware и
размером входа; целевой путь не превышает hard timeout.

## Трек D — metrics endpoint и безопасность (критерий 3.6, до +1 балла)

**Файлы:** `app/main.py`, новый `app/metrics.py`.
**Изолирован:** не трогает `app/pii.py` — можно делать параллельно с Треком B.
**Почему здесь:** небольшой прирост баллов (+0.5-1), но нужен для демо и критерия 3.6.

- [ ] Metrics endpoint (RPS/TPS/latency) + Mean/p50/p95/p99, error/429 и cache-hit
  metrics. Сейчас latency логируется, но отдельного metrics endpoint нет.
- [ ] Логирование выявленных типов ПДН по каждому запросу (подтвердить).

Критерий готовности: `/metrics` отдаёт RPS/TPS/latency; логи содержат типы ПДН
без raw PII.

## Трек E — дополнительные возможности (критерий 3.7, до +3 баллов)

**Файлы:** `app/pii.py` (комбинации, документы), `app/policy.py` (настройка вида).
**Зависимость:** частично от Трека A (настройка вида маскирования под систему).
**Почему здесь:** свободный критерий, по 1 баллу за возможность, максимум 3.
Самый «дешёвый» способ добрать баллы после базового качества.

- [ ] Токенизация/детокенизация или замена синтетическими данными.
- [ ] Настройка вида маскирования/токенизации под систему (зависит от Трека A).
- [ ] RPS 2000 при Latency ≤ 0.5 с (зависит от Трека C).
- [ ] Идентификация документов, удостоверяющих личность, кроме паспорта РФ
  (частично: DRIVING_LICENSE, SNILS уже есть).
- [ ] Контекстное маскирование по комбинации типов ПДН (PIN + номер карты),
  с настраиваемым правилом.

Критерий готовности: каждая заявленная возможность подтверждена демо и тестом,
без «для галочки» (см. критерии финалистов «Сила решения»).

## Трек F — сдача и демонстрация (критерий 3.8 и артефакты, до +1.5 балла)

**Файлы:** `README.md`, `docs/`, `DEPLOY.md`.
**Изолирован:** не трогает код — можно делать параллельно с любым треком.
**Почему здесь:** обязательные артефакты на сдачу; без них решение не учитывается.

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
| C — производительность | `scripts/`, конфиг | 1 человек |
| D — metrics | `main.py`, `metrics.py` | 1 человек |
| E — доп. возможности | `pii.py`, `policy.py` | после A/B |
| F — сдача | README, docs | 1 человек |

Треки A, C, D, F не пересекаются по файлам и могут идти параллельно. Трек B —
единственный, кто трогает `app/pii.py`; E зависит от A и B.