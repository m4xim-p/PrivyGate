# ADR-0005: Кэширование детекторов и оптимизация hot path `/process`

- Статус: Superseded by [ADR-0006](0006-process-store-eviction.md)
- Дата: 2026-09-23

## Контекст

Трек C (производительность) и критерий 3.5 требуют latency ≤0.5s при 1000 RPS и
обработку до 100k токенов. Baseline-замер (120s, ramp 60s → peak 1000 RPS,
200 соединений, keepalive 10) показал:

- RPS: 353.01 (ramp 457.8, **peak 250.8** — падение при пике);
- Masking latency: mean 0.46s, p50 0.48s, **p95 0.93s, p99 1.20s** (p99 превышает
  target ≤1s);
- Demasking latency: mean 0.46s, p50 0.47s, p95 0.91s, p99 1.17s;
- Memory: ~110 MiB.

Baseline сохранён как воспроизводимый артефакт:
[`docs/benchmarks/load-test-baseline.md`](../benchmarks/load-test-baseline.md)
(команда запуска, конфигурация, версия кода, hardware, результаты). Этот файл —
точка сравнения для подтверждения/опровержения улучшений или регрессий после
внедрения оптимизации.

При пиковой нагрузке система не справляется: peak RPS падает до ~250, latency
растёт. Bottleneck — CPU-bound маскирование в worker threads
(`PIIMaskingEngine.mask` → `PIIMasker.mask`), а не store/service (200 прямых
вызовов `ProcessService.process` за ~0.03s).

`PIIMaskingEngine.mask` (app/pii_engine.py:42-57) создаёт новый `PIIMasker` на
каждый запрос. `PIIMasker.__init__` вызывает `_with_pin_rule` (app/pii.py:1825),
который пересоздаёт `PinCodeDetector` и пересобирает кортеж детекторов на каждый
запрос. Это лишние аллокации и повторная инициализация на hot path.

## Источники решения

- Приложения A/B: [`../source/track-specification.txt`](../source/track-specification.txt).
- Критерии оценивания: [`../source/evaluation-criteria.md`](../source/evaluation-criteria.md).
- Сохранённые ответы организаторов:
  [`../source/organizer-clarifications.txt`](../source/organizer-clarifications.txt).
- `SECURITY.md` (fail-closed, безопасные логи), `docs/architecture.md`
  (single-process baseline, общее PII-ядро), `docs/evaluation-contract.md`
  (default profile `alfasonar`).
- ADR-0001 (гибридная детекция, NER = bottleneck), ADR-0002 (in-memory store,
  single-process), ADR-0003 (typed placeholders), ADR-0004 (policy registry).

## Решение

### 1. Кэшировать предсобранные детекторы, а не `PIIMasker`

`PIIMasker.mask()` **не сбрасывает** `mapping`, `_counters`, `_pii_types`,
`decisions` между вызовами (app/pii.py:1838-1893). Поэтому **нельзя** кэшировать
сам `PIIMasker` и переиспользовать его `mask()` между запросами — mapping будет
накапливаться и утекать между запросами (нарушение изоляции и потенциальная
утечка PII).

Вместо этого кэшируем **предсобранные детекторы**, которые stateless в
`detect()` (проверено: повторные вызовы `detect()` на одних детекторах не
накапливают состояние). `PIIMasker` создаётся дёшево на каждый запрос, но без
повторной пересборки детекторов через `_with_pin_rule`.

### 2. Изменение `PIIMaskingEngine`

`require_card_for_pin` — **per-consumer** параметр (ADR-0004, `ConsumerPolicy`):
по умолчанию `True`, но может быть `False` для конкретного consumer. Поэтому кэш
детекторов строится **на каждое значение** `require_card_for_pin` (True/False),
чтобы сохранить per-consumer гибкость ADR-0004. Для `/process` default profile
`alfasonar` используется `require_card_for_pin=True`.

`PIIMaskingEngine.__init__` предварительно собирает детекторы с применённым
`_with_pin_rule` для каждого значения `require_card_for_pin` и кэширует их. В
`mask()` создаёт `PIIMasker` с кэшированными детекторами, не пересобирая их.

Контракт `mask(text, policy) -> (masked, count, types)` **не меняется** — это
внутренний интерфейс, `/process` контракт не затрагивается.

### 3. Оптимизация context-паттернов в `app/pii.py` (отложено)

Предкомпиляция context-паттернов и кэш `casefold()` в детекторах дают
дополнительный выигрыш, но `app/pii.py` — hotspot Трека B. Оптимизация
выполняется **только после согласования с владельцем Трека B** и с прогоном
ratchet (`tests/test_golden_dataset.py`). В рамках данного ADR не выполняется.

### 4. Затрагиваемые компоненты

- `app/pii_engine.py` — кэшировать предсобранные детекторы; не пересобирать их
  на каждый запрос.
- `app/pii.py` — **не меняется** в рамках этого ADR (отложено, см. п.3).
- `app/main.py` — без изменений (передаёт детекторы в engine как раньше).
- `scripts/load_test_process.py` — без изменений (используется для замера).

## Последствия

Плюсы:

- устраняется пересоздание `PinCodeDetector` и пересборка кортежа детекторов на
  каждый запрос (основной выигрыш на hot path);
- сохраняется изоляция mapping между запросами (нет утечки PII);
- контракт `/process` и `/v1/chat/completions` не меняются;
- единое PII-ядро сохраняется — нет второго pipeline;
- согласуется с ADR-0002 (single-process, store не bottleneck).

Минусы:

- `PIIMaskingEngine` хранит кэш детекторов — требуется тест, что детекторы
  остаются stateless в `detect()` (см. «Условия принятия»);
- выигрыш ограничен: основная CPU-стоимость остаётся в самих детекторах
  (оптимизация `pii.py` отложена).

## Условия принятия

1. **Тест stateless-детекторов (обязательный).** Тест, что повторные вызовы
   `detect()` на кэшированных детекторах не накапливают состояние
   (positive/negative/overlap/round-trip). Без него кэширование детекторов —
   скрытый риск межзапросной утечки состояния (и потенциально PII).
2. **Воспроизводимый benchmark.** Baseline сохранён в
   [`docs/benchmarks/load-test-baseline.md`](../benchmarks/load-test-baseline.md).
   После внедрения оптимизации — повторный замер той же командой и конфигурацией,
   результат фиксируется в том же файле. Оптимизация принимается только если нет
   регрессии RPS/latency и не регрессирует golden dataset
   (`tests/test_golden_dataset.py`, ratchet).

## Не делать

- Не кэшировать сам `PIIMasker` и не переиспользовать его `mask()` между
  запросами (утечка mapping/PII).
- Не менять контракт `/process` (schema, state machine).
- Не создавать второй PII pipeline.
- Не добавлять `--workers`/Redis без shared store (сломает пары mask/demask,
  ADR-0002).
- Не оптимизировать `app/pii.py` без согласования с владельцем Трека B.